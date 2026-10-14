# Employee Attendance & Analytics API

FastAPI + MongoDB REST API for employee attendance tracking, manual corrections, and analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier M0)

## Local Setup

```bash
python -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate

pip install -r requirements.txt

# Copy and edit environment variables
cp .env.example .env       # set MONGO_URI and MONGO_DB

# Optional: start MongoDB locally with Docker
docker run -d -p 27017:27017 mongo:7

# Seed sample data (9 documents)
python sample_seed.py

# Run the API
uvicorn app.main:app --port 8000 --reload
```

API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

## Implementation Notes

All 12 endpoints from `openapi.yaml` are implemented in `app/main.py`:

| Tag | Endpoint | Notes |
|---|---|---|
| system | `GET /health` | MongoDB ping; 503 on failure |
| employees | `POST /employees` | Unique index → concurrent-safe |
| employees | `GET /employees` | Sorted, filtered, paginated |
| attendance | `POST /attendance/punch-in` | IST date (R1), grace (R2), unique index concurrency guard |
| attendance | `POST /attendance/punch-out` | Overnight-safe lookup, R3–R5, atomic update |
| attendance | `GET /attendance` | Server-side sort + paginate, never loads full collection |
| attendance | `PATCH /attendance/{emp_code}/{date}` | Recomputes R2–R5, appends history, optimistic concurrency |
| analytics | `GET /analytics/employees/{emp_code}/monthly` | R7–R9 working days, half-up rounding |
| analytics | `GET /analytics/departments/summary` | Headcount includes zero-log employees (R9) |
| analytics | `GET /analytics/leaderboard/late` | Standard competition ranking; ties handled correctly |
| analytics | `GET /analytics/departments/{department}/trend` | Gap-filled daily rows, 7-day moving average |
| admin | `GET /admin/explain/{endpoint}` | executionStats explain for all 5 named queries |

### Key design decisions

- **All instants** are stored as UTC BSON datetimes and returned as epoch milliseconds.
- **IST handling**: attendance dates and shift comparisons always convert to IST (UTC+05:30) before operating on them.
- **Rounding**: all reported numbers use half-up rounding via Python's `Decimal` module — not `round()`, which uses banker's rounding.
- **Concurrency**: punch-in and punch-out are safe under concurrent requests using unique indexes and `find_one_and_update`.
- **Indexes created at startup** (idempotently) to ensure IXSCAN on all queries.

See `REVIEW.md` for all 17 bugs found in the starter and `DECISIONS.md` for design rationale.

## What is complete

Everything in `openapi.yaml` is implemented. All business rules R1–R10 are handled.
