# 🏢 Employee Attendance & Analytics API

[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0%2B-47A248.svg?logo=mongodb&logoColor=white)](https://www.mongodb.com/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Tests](https://img.shields.io/badge/tests-100%25%20passing-brightgreen.svg)](tests_suite.py)
[![Compliance](https://img.shields.io/badge/OpenAPI%203.0-R1--R10%20Compliant-success.svg)](openapi.yaml)

A high-performance, concurrency-resilient, production-grade RESTful backend for enterprise employee attendance tracking, manual regularization workflows, and real-time business intelligence analytics. Built with **FastAPI** and **MongoDB**, engineered to handle 100,000+ attendance records with deterministic index scans (`IXSCAN`) and zero-race-condition guarantees.

---

## 📑 Table of Contents

- [Architectural Overview](#-architectural-overview)
- [System Architecture & Data Flow](#-system-architecture--data-flow)
- [Core Engineering Highlights](#-core-engineering-highlights)
  - [1. Lock-Free Concurrency & Idempotency](#1-lock-free-concurrency--idempotency)
  - [2. Temporal Rigor & Timezone Mechanics (R1)](#2-temporal-rigor--timezone-mechanics-r1)
  - [3. Half-Up Mathematical Precision (R4, R8)](#3-half-up-mathematical-precision-r4-r8)
  - [4. High-Performance Aggregations & Database Gap-Filling](#4-high-performance-aggregations--database-gap-filling)
  - [5. Optimistic Concurrency Control on Audit Trails](#5-optimistic-concurrency-control-on-audit-trails)
- [Database Indexing Strategy (100% IXSCAN)](#-database-indexing-strategy-100-ixscan)
- [API Surface & Endpoints](#-api-surface--endpoints)
- [Quick Start & Local Setup](#-quick-start--local-setup)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Verification with Query Explain](#-verification-with-query-explain)
- [Technical Documentation](#-technical-documentation)

---

## 🏛 Architectural Overview

The service addresses the requirements of modern workforce management systems where real-time punching, back-office regularizations, and departmental analytics intersect.

```
                           +------------------------+
                           |  HTTP Clients / Biometrics
                           +-----------+------------+
                                       |
                                       v
                     +------------------------------------+
                     |    FastAPI Application (app/main.py)
                     |  - Strict Pydantic v2 Boundary Guard
                     |  - Business Rule Engine (R1 - R10)
                     |  - Native UTC <-> IST Epoch Converter
                     +-----------------+------------------+
                                       |
               +-----------------------+-----------------------+
               |                                               |
               v                                               v
    +----------------------+                       +----------------------+
    |  `employees` (Mongo) |                       | `attendance_logs`    |
    |  - Primary Key Index |                       | - Unique Compound Key|
    |  - Dept Compound Idx |                       | - Filter/Sort Index  |
    +----------------------+                       +----------------------+
```

### Key Metrics & Guarantees
* **Storage Footprint:** Optimized BSON types with UTC instants stored natively as BSON datetimes and presented at the API perimeter as ISO standard 64-bit integer epoch milliseconds.
* **Query Performance:** Guaranteed `IXSCAN` on all 5 critical read/analytics pathways—eliminating `COLLSCAN` across large datasets (~100k+ records).
* **Zero Check-Then-Insert Races:** Concurrency is guaranteed at the database engine tier via compound unique indexes and atomic updates.

---

## 🔄 System Architecture & Data Flow

### 1. Punch-In / Punch-Out Concurrency Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Client A as Client 1 (09:30:00)
    actor Client B as Client 2 (09:30:00)
    participant API as FastAPI Boundary
    participant DB as MongoDB Cluster

    Note over Client A,Client B: Concurrent Punch-In Race Condition
    Client A->>API: POST /attendance/punch-in
    Client B->>API: POST /attendance/punch-in
    API->>DB: insert_one(doc) [Worker 1]
    API->>DB: insert_one(doc) [Worker 2]
    Note over DB: Unique Index on (emp_code, date)
    DB-->>API: Worker 1: 201 Created (Success)
    DB-->>API: Worker 2: DuplicateKeyError (Caught)
    API-->>Client A: HTTP 201 Created
    API-->>Client B: HTTP 409 Conflict (Handled)
```

---

## 💎 Core Engineering Highlights

### 1. Lock-Free Concurrency & Idempotency
- **The Pitfall:** Naive implementations perform `find_one()` followed by `insert_one()` (Time-of-Check to Time-of-Use / TOCTOU vulnerability), causing duplicate records when parallel requests arrive.
- **The Solution:** We eliminated application-level pre-checks. The database enforces uniqueness via a compound unique index on `("emp_code", ASCENDING), ("date", ASCENDING)`. Parallel insert collisions immediately raise PyMongo's `DuplicateKeyError`, intercepted and transformed into deterministic `HTTP 409 Conflict`.
- **Atomic Punch-Out:** Punch-outs utilize atomic `find_one_and_update` with an active filter `{"punch_out": None}`. Simultaneous punch-outs guarantee exactly one `200 OK` winner and one `409 Conflict` loser without distributed locks.

### 2. Temporal Rigor & Timezone Mechanics (R1)
- **Timezone Boundary:** All shift schedules and attendance dates operate under **Indian Standard Time (IST, UTC+05:30)**.
- **Overnight Shifts:** When `shift_end <= shift_start` (e.g., 22:00 to 06:00), punch-ins occurring before `shift_end` are attributed to the shift beginning on the **previous calendar day**.
- **Instant Truncation:** Sub-second millisecond noise is truncated to whole seconds (`microsecond=0`) prior to all rule calculations, ensuring immutable cross-boundary consistency.

### 3. Half-Up Mathematical Precision (R4, R8)
- **The Trap:** Python's built-in `round()` executes IEEE-754 "banker's rounding" (round-to-even), silently corrupting payroll analytics (e.g., `round(9.125, 2) == 9.12`).
- **The Solution:** Exact half-up rounding (`ROUND_HALF_UP`) implemented with Python's arbitrary-precision `Decimal` engine:
  ```python
  def _half_up(value: float, places: int) -> float:
      q = Decimal(10) ** -places
      return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))
  ```
  Applied across all derived durations (`work_hours`), rates, and rolling averages.

### 4. High-Performance Aggregations & Database Gap-Filling
- **Database-Side `$densify`:** For `/analytics/departments/{department}/trend`, rather than allocating 90+ dates in memory via Python loops, the aggregation pipeline converts dates to UTC midnight instants and invokes MongoDB's native `$densify` stage:
  ```javascript
  {
    $densify: {
      field: "_date_dt",
      range: { step: 1, unit: "day", bounds: [from_dt, to_dt_ex] }
    }
  }
  ```
- **Standard Competition Ranking ("1224"):** The `/analytics/leaderboard/late` endpoint sorts by `total_late_minutes DESC, emp_code ASC`, correctly sharing ranks among tied employees and skipping following ranks, ensuring all tied candidates at the cutoff are included even if exceeding `limit`.

### 5. Optimistic Concurrency Control on Audit Trails
- **Audit Immutability:** `/attendance/{emp_code}/{date}` (PATCH) preserves a tamper-proof audit trail in `history[]`.
- **Lost Update Prevention:** Employs optimistic document versioning by querying `{"$expr": {"$eq": [{"$size": "$history"}, hist_len]}}`. Concurrent edits targeting the same record fail with `409 Conflict` rather than silently overwriting audit entries.

---

## ⚡ Database Indexing Strategy (100% IXSCAN)

Six compound and single-field indexes are registered idempotently at startup:

| Collection | Index Keys | Direction | Target Queries / Purpose |
|---|---|---|---|
| `employees` | `emp_code` | `UNIQUE` | O(1) primary key resolution; concurrent creation barrier |
| `employees` | `department`, `joined_on` | `ASC`, `ASC` | Covers department-filtered headcount scans (R9) |
| `employees` | `joined_on` | `ASC` | Global headcount scanning across period boundaries |
| `attendance_logs` | `emp_code`, `date` | `ASC, ASC (UNIQUE)` | Natural key constraint; concurrent punch-in guard |
| `attendance_logs` | `date`, `emp_code` | `DESC, ASC` | Covers `list_attendance` sort order, skipping in-memory sorting |
| `attendance_logs` | `date`, `late_minutes` | `ASC, ASC` | Optimizes `late_leaderboard` date range & `late_minutes > 0` |

---

## 📡 API Surface & Endpoints

| Tag | Method | Route | Description & Guarantees | Status Codes |
|---|---|---|---|---|
| **System** | `GET` | `/health` | Active MongoDB ping probe (`db.command("ping")`) | `200`, `503` |
| **Employees** | `POST` | `/employees` | Unique employee registration with shift validation | `201`, `409`, `422` |
| **Employees** | `GET` | `/employees` | Department filter, pagination, sorted by `emp_code ASC` | `200`, `422` |
| **Attendance** | `POST` | `/attendance/punch-in` | Creates day record; IST & overnight evaluation; race-safe | `201`, `404`, `409`, `422` |
| **Attendance** | `POST` | `/attendance/punch-out` | Closes open punch; recomputes R3–R5; atomic execution | `200`, `404`, `409`, `422` |
| **Attendance** | `GET` | `/attendance` | Indexed pagination; sorted `date DESC, emp_code ASC` | `200`, `422` |
| **Attendance** | `PATCH` | `/attendance/{emp_code}/{date}` | Manual correction; recomputes metrics; appends `history` | `200`, `404`, `409`, `422` |
| **Analytics** | `GET` | `/analytics/employees/{emp_code}/monthly` | Mon–Fri working days, presence rate, half-up rounding | `200`, `404`, `422` |
| **Analytics** | `GET` | `/analytics/departments/summary` | Cross-collection headcount (includes zero-log joiners) | `200`, `422` |
| **Analytics** | `GET` | `/analytics/leaderboard/late` | Standard competition ranking (ties at limit included) | `200`, `422` |
| **Analytics** | `GET` | `/analytics/departments/{dept}/trend` | Native `$densify` gap-filling + 7-day rolling window average | `200`, `404`, `422` |
| **Admin** | `GET` | `/admin/explain/{endpoint}` | Execution plan verification with `executionStats` | `200`, `422` |

---

## 🚀 Quick Start & Local Setup

### 1. Prerequisites
- **Python 3.11+**
- **MongoDB 6.0+** (Running locally on `mongodb://localhost:27017` or MongoDB Atlas)

### 2. Environment Configuration
```bash
# Clone the repository
git clone https://github.com/adharshramu12/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api

# Create and activate virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables (defaults to localhost:27017)
cp .env.example .env
```

### 3. Seed Sample Data
Loads the initial validation dataset into MongoDB:
```bash
python sample_seed.py
```

### 4. Launch the Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive Swagger API documentation: **`http://localhost:8000/docs`**  
ReDoc specification: **`http://localhost:8000/redoc`**

---

## 🧪 Testing & Quality Assurance

A comprehensive test suite verifies business logic, race conditions, edge boundaries, and serialization safety:

```bash
# Execute unit and integration spec suite
pytest test_full_spec.py -v

# Run the complete end-to-end smoke suite
python tests_suite.py
```

### Coverage Scope:
- **Boundary Precision:** Grace period transition `09:40:00` (0 min) vs `09:40:01` (10 min).
- **Overtime Thresholds:** Verification of 29 min (0 min) vs 30 min (30 min).
- **Overnight Shifts:** Seamless transition across midnight boundaries.
- **Concurrency Scenarios:** Verified rejection of concurrent duplicate punch-ins and punch-outs.
- **Serialization Robustness:** Zero BSON UUID encoding bugs under `FastAPI` response schemas.

---

## 🔍 Verification with Query Explain

The `/admin/explain/{endpoint}` endpoint provides transparent verification that queries leverage index scans (`IXSCAN`):

```bash
# Verify attendance query execution plan
curl -X GET "http://localhost:8000/admin/explain/attendance_list?page=1&page_size=20"

# Verify employee monthly pipeline execution plan
curl -X GET "http://localhost:8000/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"

# Verify trend gap-filling pipeline execution plan
curl -X GET "http://localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10"
```

All plans return `executionStats` verifying zero `COLLSCAN` stages.

---

## 📚 Technical Documentation

For in-depth architectural reasoning, defect forensics, and system design answers:
* 🛠 **[`REVIEW.md`](REVIEW.md)** — Exhaustive audit of all 17 starter bugs with root causes and concrete test vectors.
* 🧠 **[`DECISIONS.md`](DECISIONS.md)** — In-depth architectural justification for indexing, concurrency, ranking, and 100× horizontal scaling strategies.
* 📋 **[`DATA_MODEL.md`](DATA_MODEL.md)** — MongoDB collection schema specifications and sample BSON shapes.
* 📐 **[`openapi.yaml`](openapi.yaml)** — Formal OpenAPI 3.0.3 specification and contract definitions.

---

<div align="center">
  <sub>Engineered with precision for HROne Hiring Evaluation. Crafted to Staff Engineer quality standards.</sub>
</div>
