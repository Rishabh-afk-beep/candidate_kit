"""
Exhaustive Contract Audit Tests
Verifies exact request & response schemas, field types, nullability,
HTTP status codes (200, 201, 404, 409, 422, 503), parameter constraints,
and business rule assertions across all 12 endpoints against openapi.yaml.
"""
from datetime import datetime, timezone
import json
import pathlib
import pytest
from fastapi.testclient import TestClient
from bson import json_util

from app.main import app, db, client as mongo_client, init_db_indexes

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


client = TestClient(app)


# --------------------------------------------------------------------------- #
# 1. GET /health
# --------------------------------------------------------------------------- #
def test_contract_health(monkeypatch):
    # 200 OK
    res = client.get("/health")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    data = res.json()
    assert set(data.keys()) == {"status"}
    assert data["status"] == "ok"

    # 503 Service Unavailable when MongoDB fails
    def mock_ping(*args, **kwargs):
        raise ConnectionError("Simulated DB connection failure")

    monkeypatch.setattr("pymongo.database.Database.command", mock_ping)
    res_down = client.get("/health")
    assert res_down.status_code == 503
    assert "detail" in res_down.json()


# --------------------------------------------------------------------------- #
# 2. POST /employees
# --------------------------------------------------------------------------- #
def test_contract_create_employee():
    # Success 201
    payload = {
        "emp_code": "EMP7001",
        "name": "Jane Contract",
        "email": "jane@contract.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()

    # Exact required fields in Employee schema:
    required_fields = {
        "emp_code", "name", "email", "department",
        "shift_start", "shift_end", "joined_on", "created_at"
    }
    assert set(data.keys()) == required_fields
    assert "_id" not in data and "id" not in data
    assert isinstance(data["emp_code"], str)
    assert isinstance(data["name"], str)
    assert isinstance(data["email"], str)
    assert isinstance(data["department"], str)
    assert isinstance(data["shift_start"], str)
    assert isinstance(data["shift_end"], str)
    assert isinstance(data["joined_on"], str)
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000

    # 409 Conflict: Duplicate emp_code
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409
    assert "detail" in res_dup.json()

    # 422: shift_start == shift_end
    bad_shift = dict(payload, emp_code="EMP7002", shift_end="09:30")
    res_shift = client.post("/employees", json=bad_shift)
    assert res_shift.status_code == 422

    # 422: Invalid emp_code regex pattern (must be EMP followed by 4-6 digits)
    res_bad_code = client.post("/employees", json=dict(payload, emp_code="USER001"))
    assert res_bad_code.status_code == 422

    # 422: Invalid email pattern
    res_bad_email = client.post("/employees", json=dict(payload, emp_code="EMP7003", email="not-an-email"))
    assert res_bad_email.status_code == 422

    # 422: Invalid joined_on date
    res_bad_date = client.post("/employees", json=dict(payload, emp_code="EMP7004", joined_on="2026-02-30"))
    assert res_bad_date.status_code == 422

    # 422: Missing required fields
    for field in ("emp_code", "name", "email", "department", "joined_on"):
        incomplete = dict(payload, emp_code="EMP7005")
        del incomplete[field]
        assert client.post("/employees", json=incomplete).status_code == 422

    # 422: Empty string name (min_length=1)
    assert client.post("/employees", json=dict(payload, emp_code="EMP7006", name="")).status_code == 422


# --------------------------------------------------------------------------- #
# 3. GET /employees
# --------------------------------------------------------------------------- #
def test_contract_list_employees():
    res = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()

    # Schema EmployeePage: items, total, page, page_size
    assert set(data.keys()) == {"items", "total", "page", "page_size"}
    assert isinstance(data["items"], list)
    assert isinstance(data["total"], int)
    assert data["page"] == 1
    assert data["page_size"] == 10

    for item in data["items"]:
        assert "_id" not in item and "id" not in item
        assert isinstance(item["emp_code"], str)
        assert isinstance(item["created_at"], int)
        assert item["department"] == "Engineering"

    # Sorted by emp_code ascending
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)

    # Filtering by nonexistent department returns empty list, total 0
    res_empty = client.get("/employees?department=NonexistentDept")
    assert res_empty.status_code == 200
    assert res_empty.json()["items"] == []
    assert res_empty.json()["total"] == 0

    # Pagination validation constraints:
    # page < 1 -> 422
    assert client.get("/employees?page=0").status_code == 422
    # page_size > 100 -> 422
    assert client.get("/employees?page_size=101").status_code == 422
    # page_size < 1 -> 422
    assert client.get("/employees?page_size=0").status_code == 422


# --------------------------------------------------------------------------- #
# 4. POST /attendance/punch-in
# --------------------------------------------------------------------------- #
def test_contract_punch_in():
    ms_pi = int(datetime(2026, 7, 20, 4, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    payload = {"emp_code": "EMP0001", "punched_at": ms_pi, "status": "PRESENT"}
    res = client.post("/attendance/punch-in", json=payload)
    assert res.status_code == 201
    data = res.json()

    # Exact AttendanceRecord schema
    req_fields = {
        "emp_code", "date", "status", "punch_in", "punch_out",
        "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"
    }
    assert set(data.keys()) == req_fields
    assert "_id" not in data and "id" not in data
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-20"
    assert data["status"] == "PRESENT"
    assert data["punch_in"] == ms_pi
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert isinstance(data["late_minutes"], int)
    assert data["late_minutes"] == 15  # Shift 09:30, punch 09:45 -> 15 min late
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []

    # Omitted punched_at and status default correctly
    res_defaults = client.post("/attendance/punch-in", json={"emp_code": "EMP0002"})
    assert res_defaults.status_code == 201
    d_def = res_defaults.json()
    assert d_def["status"] == "PRESENT"
    assert isinstance(d_def["punch_in"], int)

    # 404: Unknown employee
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms_pi})
    assert res_404.status_code == 404

    # 409: Duplicate punch-in for same attendance date
    res_409 = client.post("/attendance/punch-in", json=payload)
    assert res_409.status_code == 409

    # 422: Invalid epoch millis (< 100000000000 or string or float)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1720000000}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": "1783312500000"}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1783312500000.5}).status_code == 422

    # 422: Invalid status (only PRESENT, WFH, ON_DUTY allowed for punch-in)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "status": "ABSENT"}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "status": "LEAVE"}).status_code == 422


# --------------------------------------------------------------------------- #
# 5. POST /attendance/punch-out
# --------------------------------------------------------------------------- #
def test_contract_punch_out():
    # Setup a punch-in first on 2026-07-20
    ms_pi = int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_pi})

    # Punch-out at 13:30 UTC (19:00 IST)
    ms_po = int(datetime(2026, 7, 20, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_po})
    assert res.status_code == 200
    data = res.json()

    req_fields = {
        "emp_code", "date", "status", "punch_in", "punch_out",
        "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"
    }
    assert set(data.keys()) == req_fields
    assert data["punch_out"] == ms_po
    assert isinstance(data["work_hours"], float)
    assert data["work_hours"] == 9.5
    assert data["overtime_minutes"] == 30
    assert data["half_day"] is False

    # 404: Unknown employee
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": ms_po}).status_code == 404

    # 409: Already punched out
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_po + 1000}).status_code == 409

    # 422: punched_at <= punch_in
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms_pi})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms_pi - 1000}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms_pi}).status_code == 422

    # 422: punched_at > 24 hours after punch_in
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms_pi + (86401 * 1000)}).status_code == 422


# --------------------------------------------------------------------------- #
# 6. GET /attendance
# --------------------------------------------------------------------------- #
def test_contract_list_attendance():
    res = client.get("/attendance?emp_code=EMP0001&page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()

    # Schema AttendancePage: items, total, page, page_size
    assert set(data.keys()) == {"items", "total", "page", "page_size"}
    assert isinstance(data["items"], list)
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert data["total"] >= 2

    for rec in data["items"]:
        assert "_id" not in rec and "id" not in rec
        assert rec["emp_code"] == "EMP0001"
        assert isinstance(rec["history"], list)
        assert rec["punch_in"] is None or isinstance(rec["punch_in"], int)
        assert rec["punch_out"] is None or isinstance(rec["punch_out"], int)
        assert rec["work_hours"] is None or isinstance(rec["work_hours"], float)

    # Check sorting: date descending, then emp_code ascending
    res_all = client.get("/attendance?page=1&page_size=50")
    all_items = res_all.json()["items"]
    for i in range(len(all_items) - 1):
        d1, c1 = all_items[i]["date"], all_items[i]["emp_code"]
        d2, c2 = all_items[i+1]["date"], all_items[i+1]["emp_code"]
        if d1 == d2:
            assert c1 <= c2
        else:
            assert d1 >= d2

    # 422: date_from > date_to
    assert client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05").status_code == 422
    # 422: Malformed date_from / date_to
    assert client.get("/attendance?date_from=invalid-date").status_code == 422
    assert client.get("/attendance?date_to=invalid-date").status_code == 422
    # 422: Invalid status filter
    assert client.get("/attendance?status=INVALID").status_code == 422


# --------------------------------------------------------------------------- #
# 7. PATCH /attendance/{emp_code}/{date}
# --------------------------------------------------------------------------- #
def test_contract_regularize():
    # Regularize EMP0006 on 2026-07-07
    ms_po = int(datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    body = {
        "punch_out": ms_po,
        "reason": "Contract audit test correction",
        "regularized_by": "lead.auditor",
    }
    res = client.patch("/attendance/EMP0006/2026-07-07", json=body)
    assert res.status_code == 200
    data = res.json()

    req_fields = {
        "emp_code", "date", "status", "punch_in", "punch_out",
        "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"
    }
    assert set(data.keys()) == req_fields
    assert data["punch_out"] == ms_po
    assert len(data["history"]) == 1

    h_entry = data["history"][0]
    # HistoryEntry schema: at, by, reason, changes
    assert set(h_entry.keys()) == {"at", "by", "reason", "changes"}
    assert isinstance(h_entry["at"], int)
    assert h_entry["by"] == "lead.auditor"
    assert h_entry["reason"] == "Contract audit test correction"
    assert isinstance(h_entry["changes"], dict)

    # Setting status to LEAVE clears both punches, resets derived fields
    res_leave = client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={"status": "LEAVE", "reason": "Approved retroactive leave", "regularized_by": "hr.manager"},
    )
    assert res_leave.status_code == 200
    d_leave = res_leave.json()
    assert d_leave["status"] == "LEAVE"
    assert d_leave["punch_in"] is None
    assert d_leave["punch_out"] is None
    assert d_leave["work_hours"] is None
    assert d_leave["late_minutes"] == 0
    assert d_leave["overtime_minutes"] == 0
    assert d_leave["half_day"] is False
    assert len(d_leave["history"]) == 2

    # 404: Unknown employee or date
    assert client.patch("/attendance/EMP9999/2026-07-07", json=body).status_code == 404
    assert client.patch("/attendance/EMP0006/2026-01-01", json=body).status_code == 404

    # 422: Changing nothing
    assert client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={"status": "LEAVE", "reason": "No change attempted", "regularized_by": "hr.manager"},
    ).status_code == 422

    # 422: reason too short (< 5 chars)
    assert client.patch("/attendance/EMP0006/2026-07-07", json={"reason": "foo", "regularized_by": "admin"}).status_code == 422

    # 422: Supplying punch_in or punch_out with status ABSENT or LEAVE
    assert client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={"status": "ABSENT", "punch_out": ms_po, "reason": "Testing invalid combo", "regularized_by": "admin"}
    ).status_code == 422

    # 422: Setting presence status on LEAVE record without providing punch_in
    assert client.patch(
        "/attendance/EMP0006/2026-07-07",
        json={"status": "PRESENT", "reason": "Switch back without punch in", "regularized_by": "admin"}
    ).status_code == 422


# --------------------------------------------------------------------------- #
# 8. GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def test_contract_employee_monthly():
    # EMP0001: July 2026 (Mon-Fri = 23 working days)
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    # Schema EmployeeMonthly
    req_fields = {
        "emp_code", "month", "working_days", "present_days", "leave_days",
        "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct"
    }
    assert set(data.keys()) == req_fields
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["attendance_pct"] == 8.7  # (2.0 / 23) * 100 = 8.6956... -> 8.70%
    assert isinstance(data["leave_days"], int)
    assert isinstance(data["late_count"], int)
    assert isinstance(data["total_late_minutes"], int)
    assert isinstance(data["total_overtime_minutes"], int)

    # 404: Unknown employee
    assert client.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404

    # 422: Malformed month
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly?month=July-2026").status_code == 422


# --------------------------------------------------------------------------- #
# 9. GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def test_contract_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    # Schema DepartmentSummary: month, items
    assert set(data.keys()) == {"month", "items"}
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1

    item_req = {
        "department", "headcount", "present_days", "avg_work_hours",
        "late_count", "total_late_minutes", "leave_count", "on_duty_count"
    }
    for item in data["items"]:
        assert set(item.keys()) == item_req
        assert isinstance(item["department"], str)
        assert isinstance(item["headcount"], int)
        assert isinstance(item["present_days"], float)
        assert item["avg_work_hours"] is None or isinstance(item["avg_work_hours"], float)
        assert isinstance(item["late_count"], int)
        assert isinstance(item["total_late_minutes"], int)
        assert isinstance(item["leave_count"], int)
        assert isinstance(item["on_duty_count"], int)

    # Sorted by department ascending
    depts = [item["department"] for item in data["items"]]
    assert depts == sorted(depts)

    # Department filter returns single department
    res_eng = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res_eng.status_code == 200
    items_eng = res_eng.json()["items"]
    assert len(items_eng) == 1
    assert items_eng[0]["department"] == "Engineering"

    # 422: Malformed month
    assert client.get("/analytics/departments/summary?month=invalid").status_code == 422


# --------------------------------------------------------------------------- #
# 10. GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def test_contract_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()

    # Schema Leaderboard: month, items
    assert set(data.keys()) == {"month", "items"}
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)

    item_req = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
    for item in data["items"]:
        assert set(item.keys()) == item_req
        assert isinstance(item["rank"], int)
        assert item["rank"] >= 1
        assert isinstance(item["emp_code"], str)
        assert isinstance(item["name"], str)
        assert isinstance(item["department"], str)
        assert isinstance(item["total_late_minutes"], int)
        assert item["total_late_minutes"] > 0  # Only > 0 late minutes included
        assert isinstance(item["late_count"], int)

    # 422: limit out of bounds (1 <= limit <= 50)
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    # 422: Malformed month
    assert client.get("/analytics/leaderboard/late?month=2026-00").status_code == 422


# --------------------------------------------------------------------------- #
# 11. GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def test_contract_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()

    # Schema Trend: department, items
    assert set(data.keys()) == {"department", "items"}
    assert data["department"] == "Engineering"
    assert isinstance(data["items"], list)
    assert len(data["items"]) == 5

    item_req = {
        "date", "is_working_day", "headcount", "present_count",
        "late_count", "attendance_rate", "moving_avg_7d"
    }
    for item in data["items"]:
        assert set(item.keys()) == item_req
        assert isinstance(item["date"], str)
        assert isinstance(item["is_working_day"], bool)
        assert isinstance(item["headcount"], int)
        assert isinstance(item["present_count"], float)
        assert isinstance(item["late_count"], int)
        assert item["attendance_rate"] is None or isinstance(item["attendance_rate"], float)
        assert item["moving_avg_7d"] is None or isinstance(item["moving_avg_7d"], float)

    # 404: Unknown department
    assert client.get("/analytics/departments/Nonexistent/trend?from=2026-07-06&to=2026-07-10").status_code == 404

    # 422: to < from
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-06").status_code == 422

    # 422: range > 92 days
    assert client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01").status_code == 422

    # 422: Malformed from/to date
    assert client.get("/analytics/departments/Engineering/trend?from=bad&to=2026-07-10").status_code == 422


# --------------------------------------------------------------------------- #
# 12. GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
def test_contract_admin_explain():
    endpoints = [
        ("attendance_list", {"page": 1, "page_size": 10}, "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "attendance_logs"),
        ("department_summary", {"month": "2026-07"}, "employees"),
        ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}, "employees"),
    ]

    for ep, params, exp_coll in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed on {ep}: {res.text}"
        data = res.json()

        # Schema: endpoint, collection, explain
        assert set(data.keys()) == {"endpoint", "collection", "explain"}
        assert data["endpoint"] == ep
        assert data["collection"] == exp_coll
        assert isinstance(data["explain"], dict)

        explain_str = json.dumps(data["explain"])
        assert "IXSCAN" in explain_str, f"Missing IXSCAN index scan on {ep}"
        assert "COLLSCAN" not in explain_str, f"Forbidden COLLSCAN found on {ep}"

    # 422: Unknown endpoint name
    assert client.get("/admin/explain/nonexistent_ep").status_code == 422

    # 422: Missing required parameters for all endpoints
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    assert client.get("/admin/explain/department_trend").status_code == 422


def test_explain_attendance_list_date_validation():
    """Bug 3 regression: /admin/explain/attendance_list must apply the same
    date-format validation as the real /attendance endpoint, not just order
    checks.  Malformed dates and reversed ranges must both return 422."""
    # Valid date range: must succeed
    res_ok = client.get(
        "/admin/explain/attendance_list",
        params={"date_from": "2026-07-01", "date_to": "2026-07-31"},
    )
    assert res_ok.status_code == 200, f"Unexpected error on valid dates: {res_ok.text}"

    # 422: date_from is not a valid ISO date
    res_bad_from = client.get(
        "/admin/explain/attendance_list",
        params={"date_from": "not-a-date"},
    )
    assert res_bad_from.status_code == 422, (
        f"Expected 422 for malformed date_from but got {res_bad_from.status_code}"
    )

    # 422: date_to is not a valid ISO date
    res_bad_to = client.get(
        "/admin/explain/attendance_list",
        params={"date_to": "32-13-2026"},
    )
    assert res_bad_to.status_code == 422, (
        f"Expected 422 for malformed date_to but got {res_bad_to.status_code}"
    )

    # 422: date_from > date_to (same check as list_attendance)
    res_reversed = client.get(
        "/admin/explain/attendance_list",
        params={"date_from": "2026-07-31", "date_to": "2026-07-01"},
    )
    assert res_reversed.status_code == 422, (
        f"Expected 422 for reversed date range but got {res_reversed.status_code}"
    )
