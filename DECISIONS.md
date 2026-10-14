# DECISIONS.md

Five design decisions explained in plain language.

---

## 1. Indexes

I created six indexes at startup (idempotently, so re-runs are safe):

| Index | Collection | Purpose |
|---|---|---|
| `emp_code` unique | `employees` | Enforces uniqueness at the database level, making concurrent duplicate creates safe — only one `insert_one` succeeds; the rest raise `DuplicateKeyError` which I convert to 409. |
| `(emp_code ASC, date ASC)` unique | `attendance_logs` | The natural key for a record. Uniqueness at DB level solves the concurrent punch-in race without any application-level lock. Also serves `employee_monthly` and punch-out lookup via an IXSCAN. |
| `(date DESC, emp_code ASC)` | `attendance_logs` | Exactly matches the sort order of `list_attendance`. MongoDB can satisfy the sort from the index and skip directly to the requested page without loading all documents. |
| `(date ASC, late_minutes ASC)` | `attendance_logs` | `late_leaderboard` filters on a date range and `late_minutes > 0`; this compound index lets MongoDB skip zero-late rows entirely. |
| `(department ASC, joined_on ASC)` | `employees` | `department_summary` and `department_trend` filter employees by department and joined_on ≤ date; this compound index serves both. |
| `joined_on ASC` | `employees` | Covers the joined_on ≤ month_end headcount scan when no department filter is provided. |

**Considered and rejected:** A standalone `status` index on `attendance_logs`. With only five possible values the selectivity is low — MongoDB's query planner would prefer the date-range indexes anyway, and the extra index wastes write overhead. The compound `(date, late_minutes)` index covers the most common status-adjacent filter (`late_minutes > 0`).

---

## 2. Punch-in race condition

Two identical `POST /attendance/punch-in` requests arrive at the exact same millisecond for the same employee on the same date.

**What happens in my code:**

Both requests reach the `insert_one` call simultaneously. MongoDB processes inserts serially at the shard/replica level. The unique compound index on `(emp_code, date)` means only one insert can succeed; the second raises `pymongo.errors.DuplicateKeyError`. My code catches that exception and raises `HTTPException(409)`. Result: exactly one `201` and one `409`, with no check-then-insert race window.

I deliberately removed the `find_one` pre-check that was in the starter. That check-then-insert pattern has an inherent race: two threads both see "not found", both proceed to insert, and both succeed — corrupting the data.

The same atomic-insert pattern is applied to `punch-out` via `find_one_and_update` with `{"punch_out": None}` in the filter: if two simultaneous punch-out requests race, only one finds a document where `punch_out` is still `None`; the other receives `None` from `find_one_and_update` and returns 409.

---

## 3. Tie-breaking in the late leaderboard

Two employees tied on total late minutes at exactly the `limit` cut-off.

**What my leaderboard returns:**

I use standard competition ranking (also called "1224" ranking). The MongoDB aggregation sorts by `total_late_minutes DESC, emp_code ASC`, so the order is deterministic. I iterate the result set and assign the rank only when the current row's total is less than the previous row's total; otherwise the rank is inherited.

When I reach a rank that exceeds `limit`, I stop. Because all tied employees share the same rank (and that rank was <= `limit`), they are all included in the response — the response may therefore contain more than `limit` rows, exactly as the spec requires.

Example with `limit=2`: employees A (100 min, rank 1), B (80 min, rank 2), C (80 min, rank 2), D (50 min, rank 4). Loop stops at D (rank 4 > 2). Response: [A, B, C].

---

## 4. Headcount for employees with zero logs

The department summary must count every employee whose `joined_on` is on or before the last day of the month, even if they have no attendance records at all.

**How my code handles it:**

I query the `employees` collection first — filtering on `joined_on <= month_end` and optionally on `department` — and group the results by department. This gives me headcount directly from the employees list, before touching attendance logs at all. I then fetch logs for all those employee codes in a second query and join them in Python. An employee with zero logs simply contributes no iterations in the inner loop, but they are already counted in `headcount` from the first query. There is no aggregation stage that could accidentally drop them.

---

## 5. One thing I would change if this had to serve 100× the data

I would **shard `attendance_logs` on `emp_code`** (hash shard key) and **replace the synchronous PyMongo driver with Motor (async)**.

At 10 million records a single-node MongoDB handles analytics aggregations fine, but with 10 million records and hundreds of concurrent punch-in/punch-out requests the Python process becomes the bottleneck: synchronous I/O means one thread per request, and FastAPI on Uvicorn runs on an event loop that is being blocked. Switching to `Motor` + `async def` route handlers lets a single worker handle thousands of concurrent requests without blocking, at zero cost to correctness.

For the analytics endpoints I would also add a **short-lived cache** (e.g., Redis with a 5-minute TTL) for `department_summary` and `leaderboard`, because those are read-heavy and expensive to recompute on every request. The analytical queries touch every attendance log for the month; at 100× scale that's millions of documents per request. Pre-computing into a materialized collection nightly, and incrementally updating it on punch-in/punch-out, would make all analytics O(1).
