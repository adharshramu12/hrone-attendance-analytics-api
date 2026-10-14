import sys
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from dotenv import load_dotenv

load_dotenv()

from app.main import app, db, _create_indexes

client = TestClient(app)

def run_tests():
    print("=== 1. Health check ===")
    r = client.get("/health")
    assert r.status_code == 200, f"Health check failed: {r.status_code} {r.text}"
    assert r.json() == {"status": "ok"}
    print("Health check OK!")

    print("\n=== 2. Seed data verification ===")
    emp_count = db.employees.count_documents({})
    log_count = db.attendance_logs.count_documents({})
    print(f"Employees: {emp_count}, Attendance logs: {log_count}")
    assert emp_count == 6
    assert log_count == 9

    print("\n=== 3. Employees endpoints ===")
    # List employees
    r = client.get("/employees")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    # Ascending sort check
    codes = [e["emp_code"] for e in data["items"]]
    assert codes == sorted(codes)
    # Check created_at is epoch ms int
    for e in data["items"]:
        assert isinstance(e["created_at"], int)
        assert e["created_at"] > 100_000_000_000

    # Filter by department
    r = client.get("/employees?department=Engineering")
    assert r.status_code == 200
    eng_data = r.json()
    assert eng_data["total"] == 2
    assert len(eng_data["items"]) == 2

    # Create employee duplicate
    r = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Duplicate Asha",
        "email": "asha2@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })
    assert r.status_code == 409, f"Expected 409 for duplicate emp_code, got {r.status_code}"

    # Create employee invalid shift
    r = client.post("/employees", json={
        "emp_code": "EMP9999",
        "name": "Test Emp",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-05"
    })
    assert r.status_code == 422, f"Expected 422 for equal shift times, got {r.status_code}"
    print("Employees endpoints OK!")

    print("\n=== 4. Attendance list endpoint ===")
    r = client.get("/attendance")
    assert r.status_code == 200
    att_data = r.json()
    assert att_data["total"] == 9
    assert len(att_data["items"]) == 9
    # Check date descending, emp_code ascending sort
    prev = None
    for item in att_data["items"]:
        curr = (-int(item["date"].replace("-", "")), item["emp_code"])
        if prev is not None:
            assert prev <= curr, f"Sorting violation: {prev} vs {curr}"
        prev = curr
    
    # date_from > date_to validation
    r = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert r.status_code == 422

    print("Attendance list endpoint OK!")

    print("\n=== 5. Analytics: Employee Monthly ===")
    # Test for EMP0001 for 2026-07
    r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200, f"Failed: {r.text}"
    emp_m = r.json()
    print("EMP0001 Monthly:", emp_m)
    assert emp_m["emp_code"] == "EMP0001"
    assert emp_m["month"] == "2026-07"
    assert emp_m["working_days"] == 23 # July 2026 has 23 working days
    assert emp_m["present_days"] >= 2.0
    
    # 404 on unknown employee
    r = client.get("/analytics/employees/UNKNOWN999/monthly?month=2026-07")
    assert r.status_code == 404
    print("Employee monthly OK!")

    print("\n=== 6. Analytics: Department Summary ===")
    r = client.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200, f"Failed: {r.text}"
    dept_s = r.json()
    print("Department summary items:", len(dept_s["items"]))
    for item in dept_s["items"]:
        print(f"Dept {item['department']}: headcount={item['headcount']}, present={item['present_days']}, avg_wh={item['avg_work_hours']}")
        assert item["headcount"] > 0
    print("Department summary OK!")

    print("\n=== 7. Analytics: Late Leaderboard ===")
    r = client.get("/analytics/leaderboard/late?month=2026-07")
    assert r.status_code == 200, f"Failed: {r.text}"
    lb = r.json()
    print("Late leaderboard items:", len(lb["items"]))
    ranks = [item["rank"] for item in lb["items"]]
    print("Ranks:", ranks)
    # Check standard competition ranking
    for i, item in enumerate(lb["items"]):
        assert item["rank"] <= i + 1
        assert item["total_late_minutes"] > 0
    print("Late leaderboard OK!")

    print("\n=== 8. Analytics: Department Trend ===")
    r = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    assert r.status_code == 200, f"Failed: {r.text}"
    trend = r.json()
    print("Engineering trend items count:", len(trend["items"]))
    assert len(trend["items"]) == 10, f"Expected 10 days, got {len(trend['items'])}"
    # Check all dates present in sequence
    for i, item in enumerate(trend["items"]):
        expected_date = f"2026-07-{i+1:02d}"
        assert item["date"] == expected_date, f"Expected {expected_date}, got {item['date']}"
        assert "is_working_day" in item
        assert "headcount" in item
        assert "present_count" in item
        assert "late_count" in item
        assert "attendance_rate" in item
        assert "moving_avg_7d" in item
    print("Department trend OK!")

    print("\n=== 9. Admin Explain ===")
    # 1. attendance_list
    r = client.get("/admin/explain/attendance_list")
    assert r.status_code == 200, f"Failed explain attendance_list: {r.text}"
    assert r.json()["endpoint"] == "attendance_list"
    assert "explain" in r.json()

    # 2. employee_monthly
    r = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert r.status_code == 200, f"Failed explain employee_monthly: {r.text}"

    # 3. department_summary
    r = client.get("/admin/explain/department_summary?month=2026-07")
    assert r.status_code == 200, f"Failed explain department_summary: {r.text}"

    # 4. late_leaderboard
    r = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert r.status_code == 200, f"Failed explain late_leaderboard: {r.text}"

    # 5. department_trend
    r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert r.status_code == 200, f"Failed explain department_trend: {r.text}"
    print("Admin Explain OK!")

    print("\n=== ALL INITIAL SMOKE TESTS PASSED ===")

if __name__ == "__main__":
    run_tests()
