import concurrent.futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, init_db_indexes, round_half_up_2, round_half_up_4

test_client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_indexes():
    init_db_indexes()

# --------------------------------------------------------------------------- #
# 1. Rounding half-up precision tests
# --------------------------------------------------------------------------- #
def test_rounding_half_up_exact():
    # In Banker's rounding, round(0.005, 2) is 0.00, and round(0.025, 2) is 0.02.
    # In half-up rounding, 0.005 -> 0.01 and 0.025 -> 0.03!
    assert round_half_up_2(0.005) == 0.01
    assert round_half_up_2(0.015) == 0.02
    assert round_half_up_2(0.025) == 0.03
    assert round_half_up_2(0.035) == 0.04
    assert round_half_up_2(0.045) == 0.05

    # 4 decimals half-up
    assert round_half_up_4(0.00005) == 0.0001
    assert round_half_up_4(0.00015) == 0.0002
    assert round_half_up_4(0.00025) == 0.0003

# --------------------------------------------------------------------------- #
# 2. Concurrency Safety Tests
# --------------------------------------------------------------------------- #
def test_concurrent_employee_creation():
    emp_code = "EMP8888"
    db.employees.delete_many({"emp_code": emp_code})

    barrier = threading.Barrier(5)

    def create():
        c = TestClient(app)
        barrier.wait()
        return c.post(
            "/employees",
            json={
                "emp_code": emp_code,
                "name": "Race User",
                "email": "race@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(create) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(201) == 1
    assert codes.count(409) == 4

def test_concurrent_punch_in():
    emp_code = "EMP8889"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Punch Race User",
            "email": "punchrace@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    fixed_time = int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    barrier = threading.Barrier(5)

    def punch():
        c = TestClient(app)
        barrier.wait()
        return c.post(
            "/attendance/punch-in",
            json={"emp_code": emp_code, "punched_at": fixed_time, "status": "PRESENT"},
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(punch) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(201) == 1
    assert codes.count(409) == 4

def test_concurrent_punch_out():
    emp_code = "EMP8890"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Punchout Race User",
            "email": "punchoutrace@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    pi_time = int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": pi_time, "status": "PRESENT"},
    )

    po_time = int(datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    barrier = threading.Barrier(5)

    def punch_out():
        c = TestClient(app)
        barrier.wait()
        return c.post(
            "/attendance/punch-out",
            json={"emp_code": emp_code, "punched_at": po_time},
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(punch_out) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(200) == 1
    assert codes.count(409) == 4

def test_concurrent_regularize():
    emp_code = "EMP8891"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Reg Race User",
            "email": "regrace@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    pi_time = int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": pi_time, "status": "PRESENT"},
    )

    WORKERS = 8
    barrier = threading.Barrier(WORKERS)

    def regularize(i):
        c = TestClient(app)
        po = int(datetime(2026, 7, 20, 13, i, 0, tzinfo=timezone.utc).timestamp() * 1000)
        barrier.wait()
        resp = c.patch(
            f"/attendance/{emp_code}/2026-07-20",
            json={
                "punch_out": po,
                "reason": f"Concurrent correction {i}",
                "regularized_by": f"admin_{i}",
            },
        )
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(regularize, i) for i in range(1, WORKERS + 1)]
        codes = [f.result() for f in futures]

    successes = codes.count(200)
    failures = codes.count(409)

    # All responses must be exactly 200 (applied) or 409 (conflict); no 5xx or unhandled errors.
    assert successes + failures == WORKERS, (
        f"Unexpected status codes in concurrent regularize: {codes}"
    )
    assert successes >= 1
    assert failures >= 1

    # The history in the database must have exactly as many entries as there
    # were successful (200) responses – no entry lost, no phantom entries.
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": "2026-07-20"})
    assert rec is not None
    actual_history = rec.get("history") or []
    assert len(actual_history) == successes, (
        f"History length {len(actual_history)} != {successes} successful responses; codes={codes}"
    )


def test_regularize_absent_history_field():
    """Regression: PATCH must succeed when the stored document has no 'history' field at all.
    The optimistic-concurrency filter must handle both missing-field and empty-array."""
    emp_code = "EMP8895"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Absent History User",
            "email": "absenthistory@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    pi_time = int(datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": pi_time, "status": "PRESENT"},
    )

    # Manually remove the 'history' field from the document to simulate a legacy/seeded record.
    db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": "2026-07-22"},
        {"$unset": {"history": ""}},
    )

    po_time = int(datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = test_client.patch(
        f"/attendance/{emp_code}/2026-07-22",
        json={
            "punch_out": po_time,
            "reason": "Absent history field regression test",
            "regularized_by": "qa.engineer",
        },
    )
    # Must succeed, not return 409 due to the absent history field.
    assert res.status_code == 200, f"Expected 200 but got {res.status_code}: {res.text}"
    data = res.json()
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "qa.engineer"

# --------------------------------------------------------------------------- #
# 3. Boundaries for Overtime and Half Day
# --------------------------------------------------------------------------- #
def test_overtime_and_half_day_boundaries():
    emp_code = "EMP8892"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Boundary User",
            "email": "boundary@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # 1. Test exactly 29 minutes past shift end: shift end is 18:30 IST. 18:59 IST -> overtime = 0
    # Punch in 09:30 IST (04:00 UTC), punch out 18:59 IST (13:29 UTC)
    pi_ms = int(datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    po_ms_29 = int(datetime(2026, 7, 22, 13, 29, 0, tzinfo=timezone.utc).timestamp() * 1000)

    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_ms})
    res_29 = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_ms_29})
    assert res_29.status_code == 200
    assert res_29.json()["overtime_minutes"] == 0

    # 2. Test exactly 30 minutes past shift end: 19:00 IST (13:30 UTC) -> overtime = 30
    db.attendance_logs.delete_many({"emp_code": emp_code})
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_ms})
    po_ms_30 = int(datetime(2026, 7, 22, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_30 = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_ms_30})
    assert res_30.status_code == 200
    assert res_30.json()["overtime_minutes"] == 30

    # 3. Test half_day boundary:
    # 4.49 hours worked -> half_day = True
    # 4.49 hours = 16164 seconds
    db.attendance_logs.delete_many({"emp_code": emp_code})
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_ms})
    po_ms_half = pi_ms + (16164 * 1000)
    res_half = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_ms_half})
    assert res_half.status_code == 200
    assert res_half.json()["work_hours"] == 4.49
    assert res_half.json()["half_day"] is True

    # 4.50 hours worked -> half_day = False
    db.attendance_logs.delete_many({"emp_code": emp_code})
    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_ms})
    po_ms_full = pi_ms + int(4.5 * 3600 * 1000)
    res_full = test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_ms_full})
    assert res_full.status_code == 200
    assert res_full.json()["work_hours"] == 4.5
    assert res_full.json()["half_day"] is False

# --------------------------------------------------------------------------- #
# 4. Weekend attendance records (R7)
# --------------------------------------------------------------------------- #
def test_weekend_attendance_records():
    emp_code = "EMP8893"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Weekend User",
            "email": "weekend@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )

    # 2026-07-11 is Saturday.
    # Punch in 10:00 IST (30 min late), punch out 19:30 IST (60 min overtime)
    pi_sat = int(datetime(2026, 7, 11, 4, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    po_sat = int(datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_sat})
    test_client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_sat})

    res = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    # R7: Weekend records DO NOT count toward present_days:
    assert data["present_days"] == 0.0
    # But R7: Weekend records DO add to late and overtime totals:
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 30
    assert data["total_overtime_minutes"] == 60

# --------------------------------------------------------------------------- #
# 5. Department with 0 logs in Trend and Summary (R9)
# --------------------------------------------------------------------------- #
def test_department_with_zero_logs():
    emp_code = "EMP8894"
    dept = "Legal"
    db.employees.delete_many({"department": dept})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    test_client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Legal User",
            "email": "legal@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
        },
    )

    # Summary: Legal must appear with headcount=1, present_days=0.0, avg_work_hours=null
    res_sum = test_client.get(f"/analytics/departments/summary?month=2026-07&department={dept}")
    assert res_sum.status_code == 200
    items = res_sum.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == dept
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 0.0
    assert items[0]["avg_work_hours"] is None
    assert items[0]["late_count"] == 0

    # Trend: Legal from 2026-07-06 to 2026-07-08 (3 days)
    # Headcount=1, present_count=0.0, working days have rate=0.0, moving_avg=0.0
    res_trend = test_client.get(f"/analytics/departments/{dept}/trend?from=2026-07-06&to=2026-07-08")
    assert res_trend.status_code == 200
    t_items = res_trend.json()["items"]
    assert len(t_items) == 3
    for row in t_items:
        assert row["headcount"] == 1
        assert row["present_count"] == 0.0
        assert row["attendance_rate"] == 0.0
        assert row["moving_avg_7d"] == 0.0
