import pytest
from datetime import datetime, timezone, timedelta, date as date_type
from fastapi.testclient import TestClient
from dotenv import load_dotenv

load_dotenv()

from app.main import app, db, _half_up, _to_ms, _attendance_date, _late_minutes, _overtime_minutes, _work_hours, _half_day, IST, UTC

client = TestClient(app)

def test_rounding_half_up():
    # 9.125 rounds to 9.13 half-up, but banker's rounds to 9.12
    assert _half_up(9.125, 2) == 9.13
    assert _half_up(9.124, 2) == 9.12
    assert _half_up(9.126, 2) == 9.13
    assert _half_up(0.12345, 4) == 0.1235

def test_late_minutes_boundary():
    # Shift 09:30
    att_date = "2026-07-06"
    # 09:40:00 IST -> exact 600s -> 0 late
    dt_600 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert _late_minutes(dt_600, "09:30", att_date) == 0

    # 09:40:01 IST -> 601s -> strictly > 600s -> int(601/60) = 10
    dt_601 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert _late_minutes(dt_601, "09:30", att_date) == 10

    # 10:15:59 IST -> 45 minutes late
    dt_1015 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert _late_minutes(dt_1015, "09:30", att_date) == 45

    # Early punch-in (09:20:00) -> 0
    dt_early = datetime(2026, 7, 6, 9, 20, 0, tzinfo=IST)
    assert _late_minutes(dt_early, "09:30", att_date) == 0

def test_overtime_minutes():
    # Shift 09:30 - 18:30
    att_date = "2026-07-06"
    # Punch out at 18:59 (29 min OT) -> 0 (< 30)
    po_29 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST)
    assert _overtime_minutes(po_29, "18:30", att_date, "09:30") == 0

    # Punch out at 19:00 (30 min OT) -> 30 (>= 30)
    po_30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert _overtime_minutes(po_30, "18:30", att_date, "09:30") == 30

    # Punch out at 19:45 (75 min OT) -> 75
    po_75 = datetime(2026, 7, 6, 19, 45, 0, tzinfo=IST)
    assert _overtime_minutes(po_75, "18:30", att_date, "09:30") == 75

def test_overnight_shift_calculations():
    # Overnight shift: 22:00 - 06:00
    # Punch in 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
    pi_night = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    assert _attendance_date(pi_night, "22:00", "06:00") == "2026-07-06"

    # Punch in 01:30 IST on 2026-07-07 -> time < 06:00 -> belongs to previous calendar day 2026-07-06
    pi_morning = datetime(2026, 7, 7, 1, 30, 0, tzinfo=IST)
    assert _attendance_date(pi_morning, "22:00", "06:00") == "2026-07-06"

    # Punch out 06:40 IST on 2026-07-07 -> shift ended 06:00 on 2026-07-07 -> 40 min overtime
    po_night = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert _overtime_minutes(po_night, "06:00", "2026-07-06", "22:00") == 40

def test_work_hours_and_half_day():
    pi = datetime(2026, 7, 6, 9, 30, 0, tzinfo=UTC)
    # Exactly 4.5 hours
    po_4_5 = pi + timedelta(hours=4, minutes=30)
    wh = _work_hours(pi, po_4_5)
    assert wh == 4.50
    assert not _half_day(wh) # >= 4.50 is NOT half day

    # 4 hours 29 minutes (< 4.50)
    po_4_29 = pi + timedelta(hours=4, minutes=29)
    wh_half = _work_hours(pi, po_4_29)
    assert wh_half < 4.50
    assert _half_day(wh_half)

def test_punch_in_and_punch_out_flow():
    # Create test employee
    emp_code = "EMP8888"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    r = client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Integration Tester",
        "email": "tester@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert r.status_code == 201

    # Punch in at 09:35 IST on 2026-08-03 (Monday)
    pi_dt = datetime(2026, 8, 3, 9, 35, 0, tzinfo=IST)
    pi_ms = int(pi_dt.timestamp() * 1000)

    r = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": pi_ms,
        "status": "PRESENT"
    })
    assert r.status_code == 201
    doc = r.json()
    assert doc["emp_code"] == emp_code
    assert doc["date"] == "2026-08-03"
    assert doc["punch_in"] == pi_ms
    assert doc["late_minutes"] == 0
    assert doc["punch_out"] is None
    assert doc["work_hours"] is None

    # Duplicate punch in for same day must return 409
    r_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": pi_ms,
        "status": "PRESENT"
    })
    assert r_dup.status_code == 409

    # Punch out at 19:15 IST (45 min overtime)
    po_dt = datetime(2026, 8, 3, 19, 15, 0, tzinfo=IST)
    po_ms = int(po_dt.timestamp() * 1000)

    # Invalid punch out: before punch in -> 422
    r_early = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": pi_ms - 1000
    })
    assert r_early.status_code == 422

    # Invalid punch out: > 24 hours after -> 422
    r_late = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": pi_ms + 86401 * 1000
    })
    assert r_late.status_code == 422

    # Valid punch out
    r_out = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": po_ms
    })
    assert r_out.status_code == 200
    out_doc = r_out.json()
    assert out_doc["punch_out"] == po_ms
    assert out_doc["work_hours"] == 9.67 # (19:15 - 09:35) = 9h 40m = 9.6666... -> 9.67
    assert out_doc["overtime_minutes"] == 45
    assert out_doc["half_day"] is False

    # Second punch out for already closed record must return 409
    r_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": po_ms + 10000
    })
    assert r_out_dup.status_code == 409

def test_regularize_flow():
    emp_code = "EMP8888"
    date_str = "2026-08-03"

    # Regularize with no changes -> 422
    r = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "reason": "testing no change error",
        "regularized_by": "admin"
    })
    assert r.status_code == 422

    # Regularize to ABSENT with punch_in sent -> 422
    r = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "status": "ABSENT",
        "punch_in": 1783312500000,
        "reason": "bad absent attempt",
        "regularized_by": "admin"
    })
    assert r.status_code == 422

    # Regularize to ABSENT cleanly
    r = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "status": "ABSENT",
        "reason": "was actually absent",
        "regularized_by": "admin"
    })
    assert r.status_code == 200
    reg_doc = r.json()
    assert reg_doc["status"] == "ABSENT"
    assert reg_doc["punch_in"] is None
    assert reg_doc["punch_out"] is None
    assert reg_doc["work_hours"] is None
    assert len(reg_doc["history"]) == 1
    assert reg_doc["history"][0]["by"] == "admin"
    assert reg_doc["history"][0]["reason"] == "was actually absent"
    assert "status" in reg_doc["history"][0]["changes"]

    # Regularize back to PRESENT with punch_in and punch_out
    new_pi_dt = datetime(2026, 8, 3, 9, 30, 0, tzinfo=IST)
    new_po_dt = datetime(2026, 8, 3, 18, 30, 0, tzinfo=IST)
    new_pi_ms = int(new_pi_dt.timestamp() * 1000)
    new_po_ms = int(new_po_dt.timestamp() * 1000)

    r = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "status": "PRESENT",
        "punch_in": new_pi_ms,
        "punch_out": new_po_ms,
        "reason": "corrected biometric record",
        "regularized_by": "hr.manager"
    })
    assert r.status_code == 200
    reg_doc2 = r.json()
    assert reg_doc2["status"] == "PRESENT"
    assert reg_doc2["punch_in"] == new_pi_ms
    assert reg_doc2["punch_out"] == new_po_ms
    assert reg_doc2["work_hours"] == 9.00
    assert len(reg_doc2["history"]) == 2

    # Cleanup test data
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

def test_explain_all_endpoints():
    endpoints = [
        "/admin/explain/attendance_list",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10",
    ]
    for ep in endpoints:
        r = client.get(ep)
        assert r.status_code == 200, f"Explain failed for {ep}: {r.status_code} {r.text}"
        data = r.json()
        assert "endpoint" in data
        assert "collection" in data
        assert "explain" in data

    # Bad endpoint -> 422
    r = client.get("/admin/explain/unknown_endpoint")
    assert r.status_code == 422
