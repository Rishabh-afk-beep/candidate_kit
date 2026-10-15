import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import pathlib
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from bson import json_util
import yaml

from app.main import app, db, client, init_db_indexes

here = pathlib.Path(__file__).parent.parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)

def reset_db():
    init_db_indexes()
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)

@pytest.fixture(autouse=True)
def setup_teardown():
    reset_db()
    yield

test_client = TestClient(app)

# --------------------------------------------------------------------------- #
# 1. /health
# --------------------------------------------------------------------------- #
def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

# --------------------------------------------------------------------------- #
# 2. POST & GET /employees
# --------------------------------------------------------------------------- #
def test_create_and_list_employees():
    # Valid creation
    new_emp = {
        "emp_code": "EMP9999",
        "name": "Audit Test User",
        "email": "audit@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01",
    }
    res = test_client.post("/employees", json=new_emp)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000

    # Duplicate emp_code -> 409
    res_dup = test_client.post("/employees", json=new_emp)
    assert res_dup.status_code == 409

    # Equal shift_start and shift_end -> 422
    bad_emp = dict(new_emp, emp_code="EMP9998", shift_end="09:30")
    res_bad = test_client.post("/employees", json=bad_emp)
    assert res_bad.status_code == 422

    # Bad pattern emp_code -> 422
    res_bad_code = test_client.post("/employees", json=dict(new_emp, emp_code="12345"))
    assert res_bad_code.status_code == 422

    # List employees with department filter
    res_list = test_client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert list_data["page_size"] == 10
    # Engineering should have EMP0001, EMP0002, and now EMP9999 -> total 3
    assert list_data["total"] == 3
    codes = [item["emp_code"] for item in list_data["items"]]
    assert codes == sorted(codes)  # sorted ascending

# --------------------------------------------------------------------------- #
# 3. /attendance/punch-in & /attendance/punch-out
# --------------------------------------------------------------------------- #
def test_punch_in_and_punch_out():
    IST = timezone(timedelta(hours=5, minutes=30))
    # Test R2: 09:40:00 -> 0 late min; 09:40:01 -> 10 late min; 10:15:59 -> 45 late min
    # Shift 09:30 IST on 2026-07-20 for EMP0001
    # 2026-07-20 09:40:00 IST is 2026-07-20 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc)
    ms_094000 = int(dt_094000.timestamp() * 1000)

    res_pi_0 = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": ms_094000, "status": "PRESENT"},
    )
    assert res_pi_0.status_code == 201
    data_0 = res_pi_0.json()
    assert data_0["date"] == "2026-07-20"
    assert data_0["late_minutes"] == 0
    assert data_0["work_hours"] is None
    assert data_0["punch_out"] is None
    assert "_id" not in data_0
    assert "id" not in data_0

    # Duplicate punch in for same day -> 409
    res_dup = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": ms_094000, "status": "PRESENT"},
    )
    assert res_dup.status_code == 409

    # Punch in for unknown employee -> 404
    res_unk = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9991", "punched_at": ms_094000},
    )
    assert res_unk.status_code == 404

    # Test Punch Out on 2026-07-20
    # Punch out at 19:10:00 IST (13:40:00 UTC). Shift end is 18:30 IST -> overtime is 40 min!
    # Work hours: 09:40:00 to 19:10:00 = 9.50 hours
    dt_po = datetime(2026, 7, 20, 13, 40, 0, tzinfo=timezone.utc)
    ms_po = int(dt_po.timestamp() * 1000)

    res_po = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": ms_po},
    )
    assert res_po.status_code == 200
    po_data = res_po.json()
    assert po_data["punch_out"] == ms_po
    assert po_data["work_hours"] == 9.5
    assert po_data["overtime_minutes"] == 40
    assert po_data["half_day"] is False

    # Second punch out on already closed record -> 409
    res_po_again = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": ms_po + 10000},
    )
    assert res_po_again.status_code == 409

# --------------------------------------------------------------------------- #
# 4. Overnight Shift (R1, R3)
# --------------------------------------------------------------------------- #
def test_overnight_shift():
    # EMP0005 has shift 22:00 to 06:00
    # Punch in on 2026-07-21 at 05:30:00 IST (00:00:00 UTC).
    # Since 05:30 is earlier than 06:00 (shift_end), it belongs to 2026-07-20 shift!
    dt_pi = datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc)
    ms_pi = int(dt_pi.timestamp() * 1000)

    res_pi = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": ms_pi},
    )
    assert res_pi.status_code == 201
    pi_data = res_pi.json()
    assert pi_data["date"] == "2026-07-20"
    # Shift started 2026-07-20 22:00 IST. Punch-in was 2026-07-21 05:30 IST.
    # Late minutes: 7.5 hours = 450 minutes
    assert pi_data["late_minutes"] == 450

    # Punch out at 2026-07-21 06:45:00 IST (01:15:00 UTC).
    # Shift end was 2026-07-21 06:00 IST -> overtime = 45 minutes!
    dt_po = datetime(2026, 7, 21, 1, 15, 0, tzinfo=timezone.utc)
    ms_po = int(dt_po.timestamp() * 1000)

    res_po = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": ms_po},
    )
    assert res_po.status_code == 200
    po_data = res_po.json()
    assert po_data["overtime_minutes"] == 45
    assert po_data["work_hours"] == 1.25
    assert po_data["half_day"] is True  # work_hours < 4.50

# --------------------------------------------------------------------------- #
# 5. GET /attendance list
# --------------------------------------------------------------------------- #
def test_list_attendance():
    res = test_client.get("/attendance?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) <= 5
    assert data["total"] >= 9
    # Check date descending, then emp_code ascending sort
    for i in range(len(data["items"]) - 1):
        d1 = data["items"][i]["date"]
        d2 = data["items"][i+1]["date"]
        if d1 == d2:
            assert data["items"][i]["emp_code"] <= data["items"][i+1]["emp_code"]
        else:
            assert d1 >= d2

    # Validation: date_from > date_to -> 422
    res_bad = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_bad.status_code == 422

# --------------------------------------------------------------------------- #
# 6. PATCH /attendance/{emp_code}/{date} regularize
# --------------------------------------------------------------------------- #
def test_regularize_attendance():
    # Record EMP0006 on 2026-07-07 has punch_in: 2026-07-07T04:05:00Z (09:35 IST), punch_out: null
    # Regularize punch_out: 2026-07-07T13:05:00Z (18:35 IST).
    # Work hours: 09:35 to 18:35 = 9.00 hours.
    # Shift for EMP0006: Support (09:30 - 18:30). Overtime: 5 min (< 30 min) -> 0.
    dt_po = datetime(2026, 7, 7, 13, 5, 0, tzinfo=timezone.utc)
    ms_po = int(dt_po.timestamp() * 1000)

    res = test_client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={
            "punch_out": ms_po,
            "reason": "Forgot to punch out due to system outage",
            "regularized_by": "supervisor.bob",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.0
    assert data["overtime_minutes"] == 0
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "supervisor.bob"
    assert "punch_out" in hist["changes"]
    assert hist["changes"]["punch_out"]["from"] is None
    assert hist["changes"]["punch_out"]["to"] == ms_po
    assert hist["changes"]["work_hours"]["from"] is None
    assert hist["changes"]["work_hours"]["to"] == 9.0

    # Changing nothing -> 422
    res_no_change = test_client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={
            "punch_out": ms_po,
            "reason": "Trying to apply same change again",
            "regularized_by": "supervisor.bob",
        },
    )
    assert res_no_change.status_code == 422

    # Setting ABSENT with punch_in -> 422
    res_bad_absent = test_client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={
            "status": "ABSENT",
            "punch_in": ms_po,
            "reason": "Mistaken absent",
            "regularized_by": "supervisor.bob",
        },
    )
    assert res_bad_absent.status_code == 422

    # Unknown employee -> 404
    res_unk = test_client.patch(
        "/attendance/UNKNOWN/2026-07-07",
        json={"reason": "test valid reason", "regularized_by": "admin"},
    )
    assert res_unk.status_code == 404

# --------------------------------------------------------------------------- #
# 7. Analytics: Employee Monthly
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics():
    # EMP0001 in July 2026:
    # joined_on: 2026-01-05 -> working days in July 2026 (Mon-Fri) = 23 days (July 2026 has 23 weekdays)
    # logs in sample:
    # 2026-07-06: PRESENT (Monday) -> 1.0 present day, 0 late, 0 overtime
    # 2026-07-07: PRESENT (Tuesday) -> 1.0 present day, 0 late, 0 overtime
    # Total present_days = 2.0. Attendance pct = 2.0 / 23 * 100 = 8.6956... -> 8.70%
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["attendance_pct"] == 8.7
    assert data["leave_days"] == 0
    assert data["late_count"] == 0

    # EMP0002 joined 2026-07-13 (Monday)
    # Working days from 2026-07-13 to 2026-07-31:
    # 2026-07-13 is Monday. Weekdays from 13 to 31 = 15 weekdays!
    res_mid = test_client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res_mid.status_code == 200
    data_mid = res_mid.json()
    assert data_mid["working_days"] == 15

# --------------------------------------------------------------------------- #
# 8. Analytics: Department Summary
# --------------------------------------------------------------------------- #
def test_department_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts

    # Engineering has EMP0001 and EMP0002 -> headcount 2
    assert depts["Engineering"]["headcount"] == 2
    # Present records: EMP0001 on 07-06 (1), 07-07 (1); EMP0002 on 07-14 (1) -> present_days = 3.0
    assert depts["Engineering"]["present_days"] == 3.0
    # Work hours: 9.12 + 9.03 + 8.98 = 27.13 / 3 = 9.0433... -> 9.04
    assert depts["Engineering"]["avg_work_hours"] == 9.04

    # Sales has EMP0003 and EMP0004 -> headcount 2
    assert depts["Sales"]["headcount"] == 2
    # EMP0003: 07-06 PRESENT (1.0), 07-08 LEAVE
    # EMP0004: 07-07 WFH half_day=True (0.5), 07-08 ABSENT
    # Total present_days = 1.5
    assert depts["Sales"]["present_days"] == 1.5
    assert depts["Sales"]["leave_count"] == 1
    assert depts["Sales"]["late_count"] == 1
    assert depts["Sales"]["total_late_minutes"] == 35

# --------------------------------------------------------------------------- #
# 9. Analytics: Late Leaderboard
# --------------------------------------------------------------------------- #
def test_late_leaderboard_and_ties():
    # Only EMP0003 has late_minutes > 0 (35 min) in sample
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["emp_code"] == "EMP0003"
    assert data["items"][0]["rank"] == 1
    assert data["items"][0]["total_late_minutes"] == 35

    # Test tie ranking and limit cutoff with simulated tied logs:
    # Add late logs for EMP0001 and EMP0002 with identical late minutes = 50
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-25", "status": "PRESENT", "late_minutes": 50, "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0002", "date": "2026-07-25", "status": "PRESENT", "late_minutes": 50, "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []},
    ])
    # Now EMP0001 and EMP0002 both have 50 min -> Rank 1!
    # EMP0003 has 35 min -> Rank 3! (Skip rank 2!)
    res_tied = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    data_tied = res_tied.json()
    # If limit is 2, rank 1 <= 2 (EMP0001 and EMP0002 return), rank 3 > 2 (EMP0003 omitted)
    ranks = [item["rank"] for item in data_tied["items"]]
    assert ranks == [1, 1]
    # Check emp_code tie-break order
    assert data_tied["items"][0]["emp_code"] == "EMP0001"
    assert data_tied["items"][1]["emp_code"] == "EMP0002"

    # If limit is 3, rank 3 <= 3, so EMP0003 also returned
    res_tied_3 = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
    data_tied_3 = res_tied_3.json()
    ranks_3 = [item["rank"] for item in data_tied_3["items"]]
    assert ranks_3 == [1, 1, 3]  # Proves competition ranking (1, 1, 3)!

# --------------------------------------------------------------------------- #
# 10. Analytics: Department Trend
# --------------------------------------------------------------------------- #
def test_department_trend():
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7  # Exactly 7 days (including weekend)

    # July 6 (Mon): 1 headcount, 1 present -> rate = 1.0, moving_avg = 1.0
    day0 = data["items"][0]
    assert day0["date"] == "2026-07-06"
    assert day0["is_working_day"] is True
    assert day0["headcount"] == 1
    assert day0["present_count"] == 1.0
    assert day0["attendance_rate"] == 1.0
    assert day0["moving_avg_7d"] == 1.0

    # July 8 (Wed): 0 present -> rate = 0.0, moving_avg = (1+1+0)/3 = 0.6667
    day2 = data["items"][2]
    assert day2["date"] == "2026-07-08"
    assert day2["is_working_day"] is True
    assert day2["present_count"] == 0.0
    assert day2["attendance_rate"] == 0.0
    assert day2["moving_avg_7d"] == 0.6667

    # July 11 (Sat): weekend -> rate is None, moving_avg carries forward 0.4000
    day5 = data["items"][5]
    assert day5["date"] == "2026-07-11"
    assert day5["is_working_day"] is False
    assert day5["attendance_rate"] is None
    assert day5["moving_avg_7d"] == 0.4

    # Unknown department -> 404
    res_unk = test_client.get("/analytics/departments/Nonexistent/trend?from=2026-07-06&to=2026-07-12")
    assert res_unk.status_code == 404

    # Range > 92 days -> 422
    res_range = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
    assert res_range.status_code == 422

# --------------------------------------------------------------------------- #
# 11. Admin Explain
# --------------------------------------------------------------------------- #
def test_admin_explain_endpoints():
    endpoints = [
        ("attendance_list", {}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}),
    ]
    for ep, params in endpoints:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Failed on {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        import json
        text = json.dumps(data["explain"])
        assert "IXSCAN" in text, f"Missing IXSCAN in {ep}"
        assert "COLLSCAN" not in text, f"Unexpected COLLSCAN in {ep}"

    # Missing required param -> 422
    res_missing = test_client.get("/admin/explain/employee_monthly")
    assert res_missing.status_code == 422

# --------------------------------------------------------------------------- #
# 12. Contract schema comparison against openapi.yaml
# --------------------------------------------------------------------------- #
def test_openapi_schema_compatibility():
    with open("openapi.yaml", "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    app_spec = app.openapi()

    # Verify all 12 paths exist in app
    for path, path_item in spec["paths"].items():
        assert path in app_spec["paths"], f"Path {path} missing from FastAPI app"
        for method in path_item.keys():
            assert method in app_spec["paths"][path], f"Method {method} for {path} missing in FastAPI app"
