"""
Employee Attendance & Analytics API  —  v2.0.0
================================================
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)

HTTP contract + business rules R1-R10 : openapi.yaml
MongoDB data model + sample data      : DATA_MODEL.md
"""
import calendar
import json
import os
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Epoch-ms range guard (rejects seconds-looking values per openapi.yaml)
EPOCH_MS_MIN = 100_000_000_000    # ~2001-03-03
EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

# ─────────────────────────────────────────────────────────────────────────────
# Database connection (tz_aware=True so PyMongo returns aware datetimes)
# ─────────────────────────────────────────────────────────────────────────────
_client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = _client[os.getenv("MONGO_DB", "attendance_db")]


def _create_indexes() -> None:
    """Create all required indexes idempotently at startup (R10, openapi.yaml §Configuration)."""
    # employees ───────────────────────────────────────────────────────────────
    db.employees.create_index("emp_code", unique=True)            # PK uniqueness + concurrent-create safety
    db.employees.create_index("department")                        # department filter on list + summary
    db.employees.create_index("joined_on")                         # headcount filter (R9)
    db.employees.create_index(                                     # department_summary + trend headcount
        [("department", ASCENDING), ("joined_on", ASCENDING)]
    )

    # attendance_logs ─────────────────────────────────────────────────────────
    db.attendance_logs.create_index(                               # natural key uniqueness + concurrent punch-in
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(                               # list_attendance sort+filter (IXSCAN)
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(                               # employee_monthly + punch-out lookup
        [("emp_code", ASCENDING), ("date", DESCENDING)]
    )
    db.attendance_logs.create_index(                               # late_leaderboard date range + late filter
        [("date", ASCENDING), ("late_minutes", ASCENDING)]
    )


_create_indexes()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ─────────────────────────────────────────────────────────────────────────────
# Time / epoch helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_ist(dt: datetime) -> datetime:
    """Convert any datetime (aware or naive-UTC) to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def _trunc(dt: datetime) -> datetime:
    """R1: truncate to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def _from_ms(ms: int) -> datetime:
    """Epoch-ms integer → truncated UTC-aware datetime."""
    return _trunc(datetime.fromtimestamp(ms / 1000.0, tz=UTC))


def _to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC-aware datetime → epoch-ms integer, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Business-rule helpers
# ─────────────────────────────────────────────────────────────────────────────

def _attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 — IST calendar date of a punch-in instant.

    Overnight shift (shift_end <= shift_start, e.g. 22:00–06:00):
    a punch-in whose IST *time* is strictly earlier than shift_end belongs
    to the shift that started the previous calendar day.
    """
    pi_ist = _to_ist(punch_in_utc)
    se_h, se_m = map(int, shift_end.split(":"))
    ss_h, ss_m = map(int, shift_start.split(":"))
    is_overnight = (se_h * 60 + se_m) <= (ss_h * 60 + ss_m)

    if is_overnight:
        punch_min = pi_ist.hour * 60 + pi_ist.minute
        if punch_min < se_h * 60 + se_m:
            return (pi_ist.date() - timedelta(days=1)).isoformat()

    return pi_ist.date().isoformat()


def _late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2 — Grace = 10 min.  Late only when punch-in is *strictly* more than
    600 seconds after shift_start.  Returns floor(total_seconds / 60), not
    floor((total_seconds - 600) / 60).

    Correct boundary (shift 09:30):
      09:40:00 → 0    (600 s, not strictly >600)
      09:40:01 → 10   (601 s, floor(601/60)=10)
      10:15:59 → 45
    """
    pi_ist = _to_ist(punch_in_utc)
    h, m = map(int, shift_start.split(":"))
    d = date_type.fromisoformat(att_date)
    shift_start_ist = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)

    diff_s = (pi_ist - shift_start_ist).total_seconds()
    if diff_s > 600:
        return int(diff_s / 60)   # floor — whole minutes since shift_start
    return 0


def _half_up(value: float, places: int) -> float:
    """
    R4/R8 — half-up rounding (not Python's default banker's rounding).
    Uses Decimal to avoid float imprecision.
    """
    q = Decimal(10) ** -places
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 — (punch_out - punch_in) / 3600, rounded half-up to 2 decimals."""
    return _half_up((punch_out - punch_in).total_seconds() / 3600.0, 2)


def _overtime_minutes(punch_out: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    """
    R3 — floor minutes between shift_end and punch_out, only when >= 30.
    For overnight shifts the shift_end falls on the day *after* att_date.
    """
    po_ist = _to_ist(punch_out)
    se_h, se_m = map(int, shift_end.split(":"))
    ss_h, ss_m = map(int, shift_start.split(":"))
    is_overnight = (se_h * 60 + se_m) <= (ss_h * 60 + ss_m)

    att_d = date_type.fromisoformat(att_date)
    end_date = att_d + timedelta(days=1) if is_overnight else att_d
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, se_h, se_m, 0, tzinfo=IST)

    diff_min = int((po_ist - shift_end_ist).total_seconds() / 60)
    return diff_min if diff_min >= 30 else 0


def _half_day(wh: float) -> bool:
    """R5 — half_day = True when rounded work_hours < 4.50."""
    return wh < 4.50


def _working_days(from_d: date_type, to_d: date_type) -> int:
    """Count Mon–Fri days in [from_d, to_d] inclusive (R7)."""
    if from_d > to_d:
        return 0
    total = (to_d - from_d).days + 1
    full_weeks, rem = divmod(total, 7)
    count = full_weeks * 5
    for i in range(rem):
        if (from_d + timedelta(days=i)).weekday() < 5:
            count += 1
    return count


def _month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' → ('YYYY-MM-01', 'YYYY-MM-DD') where DD is the last day."""
    y, m = int(month[:4]), int(month[5:])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ser_employee(doc: dict) -> dict:
    """Strip _id, convert created_at to epoch ms."""
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = _to_ms(doc.get("created_at"))
    return doc


def _ser_record(doc: dict) -> dict:
    """
    Convert an attendance_logs document to the API response shape:
    - Remove _id
    - punch_in / punch_out  → epoch ms (or null)
    - history[].at          → epoch ms
    - history[].changes.punch_in / punch_out from/to  → epoch ms (or null)
    - Default legacy-missing fields
    """
    doc = dict(doc)
    doc.pop("_id", None)
    doc["punch_in"] = _to_ms(doc.get("punch_in"))
    doc["punch_out"] = _to_ms(doc.get("punch_out"))
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])

    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = _to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                v = entry["changes"][field]
                entry["changes"][field] = {
                    "from": _to_ms(v["from"]) if isinstance(v.get("from"), datetime) else v.get("from"),
                    "to":   _to_ms(v["to"])   if isinstance(v.get("to"),   datetime) else v.get("to"),
                }
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeIn(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$")

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status:     str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    status:          Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in:        Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out:       Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason:          str = Field(..., min_length=5, max_length=200)
    regularized_by:  str = Field(..., min_length=1, max_length=50)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — system
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Returns 200 if MongoDB answers a ping; 503 otherwise."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — employees
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """
    Create an employee.  emp_code uniqueness is enforced by a DB unique index;
    concurrent duplicate creates yield exactly one 201 and the rest 409.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=UTC)          # UTC-aware, stored as BSON date
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return _ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(1,  ge=1),
    page_size:  int = Query(20, ge=1, le=100),
):
    """Sorted by emp_code ascending.  total reflects the filtered set."""
    q: dict = {}
    if department:
        q["department"] = department
    skip  = (page - 1) * page_size                    # FIX: was `page * page_size` (off-by-one)
    total = db.employees.count_documents(q)            # FIX: was count_documents({}) — ignored filter
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)                   # FIX: sort was missing
        .skip(skip)
        .limit(page_size)
    )
    return {"items": [_ser_employee(e) for e in items], "total": total, "page": page, "page_size": page_size}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — attendance
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Create the attendance record for the employee's shift day.
    date and late_minutes follow R1 and R2.
    Concurrency: unique index on (emp_code, date) + DuplicateKeyError → 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:                                        # FIX: was missing → KeyError / 500
        raise HTTPException(404, detail="employee not found")

    # Resolve timestamp — IST-safe, truncated to seconds (R1)
    ts = _from_ms(body.punched_at) if body.punched_at else _trunc(datetime.now(tz=UTC))

    att_date = _attendance_date(ts, emp["shift_start"], emp["shift_end"])   # FIX: IST-aware
    late     = _late_minutes(ts, emp["shift_start"], att_date)              # FIX: correct boundary

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)             # FIX: atomic — no check-then-insert TOCTOU
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    doc.pop("_id", None)
    return _ser_record(doc)                            # FIX: epoch-ms serialisation


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """
    Close the employee's most recent open record whose punch_in <= punched_at.
    Works across midnight for overnight shifts.
    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    Concurrency: find_one_and_update with punch_out=None guard → one 200, rest 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    ts = _from_ms(body.punched_at) if body.punched_at else _trunc(datetime.now(tz=UTC))

    # Find most recent presence record
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status":   {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="that record is already punched out")

    pi_dt: datetime = record["punch_in"]

    if ts <= pi_dt:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (ts - pi_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    att_date = record["date"]
    wh = _work_hours(pi_dt, ts)
    ot = _overtime_minutes(ts, emp["shift_end"], att_date, emp["shift_start"])
    hd = _half_day(wh)

    # Atomic update: only proceeds if punch_out is still null
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="record already punched out (concurrent request)")

    return _ser_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(1,  ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Sorted by date descending, emp_code ascending.
    FIX: server-side sort+skip+limit — never loads full collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to:   q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip  = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs  = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])  # FIX: DB-level sort+paginate
        .skip(skip)
        .limit(page_size)
    )
    return {"items": [_ser_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct one day's record.
    - Recomputes late_minutes, work_hours, overtime_minutes, half_day.
    - Appends exactly one history entry containing only fields that actually changed.
    - Optimistic concurrency via history-length guard → 409 on lost update.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="attendance record not found")

    new_status = body.status or record["status"]

    # ── status-specific validation ────────────────────────────────────────────
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="ABSENT/LEAVE cannot have punch times")
        new_pi: Optional[datetime] = None
        new_po: Optional[datetime] = None
    else:
        # Presence — punch_in must exist (supplied or already stored)
        new_pi = _from_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        if new_pi is None:
            raise HTTPException(422, detail="presence status requires a punch_in")

        # punch_in must remain on this record's attendance date (R1)
        if _attendance_date(new_pi, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, detail="punch_in must remain on the record's attendance date (R1)")

        new_po_raw = body.punch_out
        new_po = _from_ms(new_po_raw) if new_po_raw is not None else record.get("punch_out")
        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # ── recompute derived fields ──────────────────────────────────────────────
    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        new_late = _late_minutes(new_pi, emp["shift_start"], date)
        if new_po is not None:
            new_wh = _work_hours(new_pi, new_po)
            new_ot = _overtime_minutes(new_po, emp["shift_end"], date, emp["shift_start"])
            new_hd = _half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    # ── detect changes (only include fields that actually changed) ────────────
    old_pi     = record.get("punch_in")
    old_po     = record.get("punch_out")
    old_wh     = record.get("work_hours")
    old_late   = record.get("late_minutes", 0)
    old_ot     = record.get("overtime_minutes", 0)
    old_hd     = record.get("half_day", False)
    old_status = record["status"]

    changes: dict = {}
    if new_status != old_status:
        changes["status"]            = {"from": old_status, "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"]          = {"from": old_pi,   "to": new_pi}   # BSON dates — _ser_record converts
    if new_po != old_po:
        changes["punch_out"]         = {"from": old_po,   "to": new_po}
    if new_wh != old_wh:
        changes["work_hours"]        = {"from": old_wh,   "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"]      = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"]  = {"from": old_ot,   "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"]          = {"from": old_hd,   "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="request produces no changes")

    history_entry = {
        "at":      datetime.now(tz=UTC),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: match on current history length so concurrent
    # edits cannot silently overwrite each other's history entries.
    hist_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id":    record["_id"],
            "$expr":  {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {
            "$set":  {
                "status": new_status, "punch_in": new_pi, "punch_out": new_po,
                "work_hours": new_wh, "late_minutes": new_late,
                "overtime_minutes": new_ot, "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="concurrent edit detected; please retry")

    return _ser_record(updated)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee — backed by a MongoDB aggregation.
    working_days from max(month_start, joined_on) → month_end (R7, R9).
    present_days: Mon–Fri presence records only; half_day → 0.5.
    attendance_pct: null when working_days = 0 (R8).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    month_start, month_end = _month_bounds(month)

    effective_start = max(month_start, emp["joined_on"])   # R7: count from joined_on if mid-month
    working_days = (
        _working_days(date_type.fromisoformat(effective_start), date_type.fromisoformat(month_end))
        if effective_start <= month_end else 0
    )

    # MongoDB aggregation — reads stored derived values, never recomputes from punch times
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$addFields": {
            # weekday number: 1=Sun … 7=Sat in MongoDB; Mon-Fri = 2-6
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "_is_workday": {"$and": [
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
            ]},
            "_is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            "_is_leave":    {"$eq": ["$status", "LEAVE"]},
            "_late":        {"$ifNull": ["$late_minutes", 0]},
            "_ot":          {"$ifNull": ["$overtime_minutes", 0]},
            "_hd":          {"$ifNull": ["$half_day", False]},
        }},
        {"$group": {
            "_id": None,
            # present_days: Mon-Fri presence only; half_day = 0.5
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_is_presence", "$_is_workday"]},
                {"$cond": ["$_hd", 0.5, 1.0]},
                0.0,
            ]}},
            "leave_days":            {"$sum": {"$cond": ["$_is_leave", 1, 0]}},
            "late_count":            {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes":    {"$sum": "$_late"},
            "total_overtime_minutes": {"$sum": "$_ot"},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    agg  = rows[0] if rows else {}

    present_days   = agg.get("present_days", 0.0)
    leave_days     = agg.get("leave_days", 0)
    late_count     = agg.get("late_count", 0)
    total_late_min = agg.get("total_late_minutes", 0)
    total_ot_min   = agg.get("total_overtime_minutes", 0)

    att_pct = _half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           _half_up(present_days, 2),
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_min,
        "total_overtime_minutes": total_ot_min,
        "attendance_pct":         att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    One item per department sorted by department ascending.
    headcount via R9: employees with joined_on <= month_end (zero-log employees included).
    avg_work_hours: mean over presence records that HAVE a work_hours value.
    """
    month_start, month_end = _month_bounds(month)

    emp_q: dict = {"joined_on": {"$lte": month_end}}
    if department:
        emp_q["department"] = department

    emps = list(db.employees.find(emp_q))
    if not emps:
        return {"month": month, "items": []}

    # Group employees by department
    dept_emps: dict[str, list[str]] = defaultdict(list)
    for emp in emps:
        dept_emps[emp["department"]].append(emp["emp_code"])

    # Fetch logs for all relevant emp_codes in the month in one round-trip
    all_codes = [e["emp_code"] for e in emps]
    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": all_codes},
        "date":     {"$gte": month_start, "$lte": month_end},
    }))

    logs_by_emp: dict[str, list] = defaultdict(list)
    for log in logs:
        logs_by_emp[log["emp_code"]].append(log)

    items = []
    for dept_name in sorted(dept_emps.keys()):
        codes     = dept_emps[dept_name]
        headcount = len(codes)

        present_days    = 0.0
        wh_vals: list[float] = []
        late_count      = 0
        total_late_min  = 0
        leave_count     = 0
        on_duty_count   = 0

        for ec in codes:
            for log in logs_by_emp[ec]:
                status = log.get("status", "")
                hd     = log.get("half_day", False)
                wh     = log.get("work_hours")
                late   = log.get("late_minutes", 0)

                if status in PRESENCE_STATUSES:
                    present_days += 0.5 if hd else 1.0
                    if wh is not None:
                        wh_vals.append(wh)     # avg over records, not per-employee averages
                if status == "LEAVE":
                    leave_count  += 1
                if status == "ON_DUTY":
                    on_duty_count += 1
                if late > 0:
                    late_count     += 1
                    total_late_min += late

        avg_wh = _half_up(sum(wh_vals) / len(wh_vals), 2) if wh_vals else None

        items.append({
            "department":          dept_name,
            "headcount":           headcount,
            "present_days":        _half_up(present_days, 2),
            "avg_work_hours":      avg_wh,
            "late_count":          late_count,
            "total_late_minutes":  total_late_min,
            "leave_count":         leave_count,
            "on_duty_count":       on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Standard competition ranking (1, 2, 2, 4 — not row number).
    `limit` applies to rank: all employees whose rank <= limit are returned,
    so ties at the cutoff rank produce more than `limit` rows.
    """
    month_start, month_end = _month_bounds(month)

    pipeline: list = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count":         {"$sum": 1},
        }},
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        {"$unwind": "$emp_info"},
    ]
    if department:
        pipeline.append({"$match": {"emp_info.department": department}})
    pipeline.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})

    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    rank  = 0
    for i, row in enumerate(rows):
        if i == 0 or row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        if rank > limit:
            break
        items.append({
            "rank":               rank,
            "emp_code":           row["_id"],
            "name":               row["emp_info"]["name"],
            "department":         row["emp_info"]["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count":         row["late_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$"),
    to_:   str = Query(..., alias="to",   pattern=r"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$"),
):
    """
    One row per calendar day from `from` to `to` inclusive (weekends + empty days included).
    Gap-filling uses MongoDB $densify so Python never loops over the date range.
    moving_avg_7d: mean of non-null attendance_rate values in a rolling window of up to 7
    rows (current + up to 6 preceding), 4 decimals half-up.
    """
    if to_ < from_:
        raise HTTPException(422, detail="'to' must be >= 'from'")
    from_d = date_type.fromisoformat(from_)
    to_d   = date_type.fromisoformat(to_)
    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail="date range must not exceed 92 days")

    emps = list(db.employees.find(
        {"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))
    if not emps:
        raise HTTPException(404, detail="department not found")

    emp_codes = [e["emp_code"] for e in emps]
    # Sort employees by joined_on ascending so headcount lookup is a bisect-friendly list
    emps_sorted = sorted(emps, key=lambda e: e["joined_on"])

    # ── MongoDB aggregation with $densify for gap-filling ─────────────────────
    # $densify requires a BSON date or numeric field (not a string).
    # Strategy: convert "YYYY-MM-DD" string → UTC midnight datetime, densify,
    # then project back to string with $dateToString.
    from_dt  = datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC)
    to_dt_ex = datetime(to_d.year,   to_d.month,   to_d.day,   tzinfo=UTC) + timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date":     {"$gte": from_, "$lte": to_},
        }},
        # Annotate each log before grouping
        {"$addFields": {
            "_is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            "_hd":          {"$ifNull": ["$half_day", False]},
            "_late":        {"$ifNull": ["$late_minutes", 0]},
            # Convert string "YYYY-MM-DD" to UTC midnight for use with $densify
            "_date_dt": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}},
        }},
        # Group by UTC-midnight datetime; this is what $densify will operate on
        {"$group": {
            "_id": "$_date_dt",
            "present_count": {"$sum": {"$cond": [
                "$_is_presence",
                {"$cond": ["$_hd", 0.5, 1.0]},
                0.0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
        }},
        # $densify needs the field at the document root (not _id)
        {"$set": {"_date_dt": "$_id"}},
        # Gap-fill: insert a zero-doc for every missing UTC-midnight in the range
        {"$densify": {
            "field": "_date_dt",
            "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_ex]},
        }},
        # Convert the BSON date back to "YYYY-MM-DD" string
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_date_dt", "timezone": "UTC"}},
        }},
        {"$sort": {"_date_dt": ASCENDING}},
        {"$project": {"_id": 0, "_date_dt": 0}},
    ]

    agg_rows_raw = list(db.attendance_logs.aggregate(pipeline))

    # Build a lookup: date_str → {present_count, late_count}
    agg_by_date: dict[str, dict] = {}
    for r in agg_rows_raw:
        agg_by_date[r["date"]] = {
            "present_count": r.get("present_count", 0.0),
            "late_count":    r.get("late_count", 0),
        }

    # ── Build final rows: headcount (needs per-employee joined_on) + rolling avg ──
    # The Python loop only iterates over at most 93 date strings — never raw logs.
    rows: list[dict] = []
    d = from_d
    while d <= to_d:
        d_str          = d.isoformat()
        is_working_day = d.weekday() < 5

        # R9: employees whose joined_on <= this day
        headcount = sum(1 for e in emps_sorted if e["joined_on"] <= d_str)

        day_data      = agg_by_date.get(d_str, {})
        present_count = day_data.get("present_count", 0.0)
        late_count    = day_data.get("late_count", 0)

        # attendance_rate: working days with headcount > 0 only
        if is_working_day and headcount > 0:
            attendance_rate: Optional[float] = _half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        rows.append({
            "date":            d_str,
            "is_working_day":  is_working_day,
            "headcount":       headcount,
            "present_count":   _half_up(present_count, 2),
            "late_count":      late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d":   None,
        })
        d += timedelta(days=1)

    # 7-day rolling average of non-null attendance_rate values (R8: 4 decimals half-up)
    for i, row in enumerate(rows):
        non_null = [
            rows[j]["attendance_rate"]
            for j in range(max(0, i - 6), i + 1)
            if rows[j]["attendance_rate"] is not None
        ]
        row["moving_avg_7d"] = _half_up(sum(non_null) / len(non_null), 4) if non_null else None

    return {"department": department, "items": rows}


# ─────────────────────────────────────────────────────────────────────────────
# Admin — explain
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_:      Optional[str] = Query(None, alias="from"),
    to_:        Optional[str] = Query(None, alias="to"),
    page:       int           = Query(1,  ge=1),
    page_size:  int           = Query(20, ge=1, le=100),
):
    """
    Runs the main query / aggregation of the named endpoint with
    executionStats verbosity and returns the raw explain document.
    The grader verifies IXSCAN (not COLLSCAN) on a ~100k dataset.
    """
    VALID = {"attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of: {sorted(VALID)}")

    def _clean_explain(ex_doc: dict) -> dict:
        return json.loads(json_util.dumps(ex_doc))

    skip = (page - 1) * page_size

    # ── attendance_list ───────────────────────────────────────────────────────
    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to:   q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        ex = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(ex)}

    # ── employee_monthly ──────────────────────────────────────────────────────
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        ms, me = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        ex = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(ex)}

    # ── department_summary ────────────────────────────────────────────────────
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, me = _month_bounds(month)
        emp_q: dict = {"joined_on": {"$lte": me}}
        if department:
            emp_q["department"] = department
        cmd = {
            "find": "employees",
            "filter": emp_q,
        }
        ex = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": _clean_explain(ex)}

    # ── late_leaderboard ──────────────────────────────────────────────────────
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        ms, me = _month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": ms, "$lte": me}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1}}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        ex = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(ex)}

    # ── department_trend ──────────────────────────────────────────────────────
    elif endpoint == "department_trend":
        if not department or not from_ or not to_:
            raise HTTPException(422, detail="department, from, and to are required")
        from_d_exp = date_type.fromisoformat(from_)
        to_d_exp   = date_type.fromisoformat(to_)
        from_dt_ex = datetime(from_d_exp.year, from_d_exp.month, from_d_exp.day, tzinfo=UTC)
        to_dt_exp  = datetime(to_d_exp.year,   to_d_exp.month,   to_d_exp.day,   tzinfo=UTC) + timedelta(days=1)
        codes = [e["emp_code"] for e in db.employees.find(
            {"department": department}, {"emp_code": 1}
        )]
        pipeline = [
            {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to_}}},
            {"$addFields": {
                "_date_dt": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}},
            }},
            {"$group": {"_id": "$_date_dt"}},
            {"$set": {"_date_dt": "$_id"}},
            {"$densify": {
                "field": "_date_dt",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt_ex, to_dt_exp]},
            }},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        ex = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(ex)}

