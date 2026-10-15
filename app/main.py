"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Contract for the HROne engineering assignment.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[Any]) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, int):
        return dt
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    # Truncate to whole seconds per R1
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def current_truncated_utc_dt() -> datetime:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    sec = now_ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def round_half_up_2(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def parse_time_hm(hm_str: str) -> tuple[int, int]:
    h, m = map(int, hm_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    h_s, m_s = parse_time_hm(shift_start)
    h_e, m_e = parse_time_hm(shift_end)
    return (h_e, m_e) <= (h_s, m_s)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """Computes the IST attendance date for a punch-in instant per R1."""
    punch_in_ist = punch_in_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        h_end, m_end = parse_time_hm(shift_end)
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_time < (h_end, m_end, 0):
            # Punch belongs to previous day's shift
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, att_date: str) -> int:
    """Computes late minutes per R2 (10-minute grace period; floor minutes since shift_start)."""
    h_s, m_s = parse_time_hm(shift_start)
    punch_in_ist = punch_in_dt.astimezone(IST)
    shift_start_dt = datetime.fromisoformat(att_date).replace(
        hour=h_s, minute=m_s, second=0, microsecond=0, tzinfo=IST
    )
    grace_dt = shift_start_dt + timedelta(minutes=10)
    if punch_in_ist > grace_dt:
        elapsed = int((punch_in_ist - shift_start_dt).total_seconds() // 60)
        return max(0, elapsed)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """Computes work hours per R4: seconds / 3600 rounded to 2 decimals half-up."""
    diff_sec = Decimal(int((punch_out_dt - punch_in_dt).total_seconds()))
    hours_dec = diff_sec / Decimal(3600)
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_dt: datetime, shift_end: str, att_date: str, shift_start: str
) -> int:
    """Computes overtime per R3 (whole minutes floor >= 30, otherwise 0)."""
    h_e, m_e = parse_time_hm(shift_end)
    punch_out_ist = punch_out_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        # Shift ends on the next calendar day
        shift_end_dt = (datetime.fromisoformat(att_date) + timedelta(days=1)).replace(
            hour=h_e, minute=m_e, second=0, microsecond=0, tzinfo=IST
        )
    else:
        shift_end_dt = datetime.fromisoformat(att_date).replace(
            hour=h_e, minute=m_e, second=0, microsecond=0, tzinfo=IST
        )
    if punch_out_ist > shift_end_dt:
        diff_min = int((punch_out_ist - shift_end_dt).total_seconds() // 60)
        return diff_min if diff_min >= 30 else 0
    return 0


def compute_month_boundaries(month_str: str) -> tuple[str, str]:
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{num_days:02d}"


def count_working_days(eff_start: str, month_end: str) -> int:
    d_start = date.fromisoformat(eff_start)
    d_end = date.fromisoformat(month_end)
    if d_start > d_end:
        return 0
    cnt = 0
    curr = d_start
    while curr <= d_end:
        if curr.weekday() < 5:  # Mon-Fri
            cnt += 1
        curr += timedelta(days=1)
    return cnt


def format_history_for_api(history: list) -> list:
    out = []
    for h in history:
        item = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        changes = h.get("changes", {})
        for k, v in changes.items():
            if k in ("punch_in", "punch_out"):
                f_val = v.get("from")
                t_val = v.get("to")
                item["changes"][k] = {
                    "from": dt_to_epoch_ms(f_val) if isinstance(f_val, datetime) else f_val,
                    "to": dt_to_epoch_ms(t_val) if isinstance(t_val, datetime) else t_val,
                }
            else:
                item["changes"][k] = {
                    "from": v.get("from"),
                    "to": v.get("to"),
                }
        out.append(item)
    return out


def format_attendance_for_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": format_history_for_api(doc.get("history", [])),
    }


def format_employee_for_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request / Response Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid date format for joined_on, expected YYYY-MM-DD")
        return v


def validate_epoch_ms_val(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Must be integer epoch milliseconds")
    if v < 100000000000 or v > 4102444800000:
        raise ValueError("Epoch milliseconds out of range")
    return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms_val(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms_val(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", mode="before")
    @classmethod
    def validate_punch_in(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms_val(v)

    @field_validator("punch_out", mode="before")
    @classmethod
    def validate_punch_out(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms_val(v)


class HealthOut(BaseModel):
    status: str


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePageOut(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Optional[Any]]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntryOut]


class AttendancePageOut(BaseModel):
    items: List[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: List[DepartmentSummaryItemOut]


class LeaderboardItemOut(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: List[LeaderboardItemOut]


class TrendItemOut(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class TrendOut(BaseModel):
    department: str
    items: List[TrendItemOut]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. Health
@app.get(
    "/health",
    response_model=HealthOut,
    tags=["system"],
    summary="Readiness probe",
    operation_id="health",
)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# 2. Create Employee
@app.post(
    "/employees",
    response_model=EmployeeOut,
    status_code=201,
    tags=["employees"],
    summary="Create an employee",
    operation_id="createEmployee",
)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", "shift_end"], "msg": "shift_start must differ from shift_end", "type": "value_error"}],
        )

    now_dt = current_truncated_utc_dt()
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_for_api(doc)


# 3. List Employees
@app.get(
    "/employees",
    response_model=EmployeePageOut,
    tags=["employees"],
    summary="List employees",
    operation_id="listEmployees",
)
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee_for_api(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 4. Punch In
@app.post(
    "/attendance/punch-in",
    response_model=AttendanceRecordOut,
    status_code=201,
    tags=["attendance"],
    summary="Record a punch-in",
    operation_id="punchIn",
)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = current_truncated_utc_dt()

    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_for_api(doc)


# 5. Punch Out
@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecordOut,
    status_code=200,
    tags=["attendance"],
    summary="Record a punch-out",
    operation_id="punchOut",
)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = current_truncated_utc_dt()

    # Find the most recent record with a punch-in
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not rec or rec.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_end"], rec["date"], emp["shift_start"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return format_attendance_for_api(updated)


# 6. List Attendance
@app.get(
    "/attendance",
    response_model=AttendancePageOut,
    tags=["attendance"],
    summary="List attendance records",
    operation_id="listAttendance",
)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["query", "date_from"], "msg": "Invalid date_from format", "type": "value_error"}],
            )
    if date_to:
        try:
            date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["query", "date_to"], "msg": "Invalid date_to format", "type": "value_error"}],
            )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["query", "date_from"], "msg": "date_from cannot be after date_to", "type": "value_error"}],
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_for_api(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 7. Regularize Attendance
@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecordOut,
    tags=["attendance"],
    summary="Regularize attendance",
    operation_id="regularizeAttendance",
)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    cur_status = rec["status"]
    cur_punch_in = rec.get("punch_in")
    if cur_punch_in and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=timezone.utc)
    cur_punch_out = rec.get("punch_out")
    if cur_punch_out and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=timezone.utc)
    cur_work_hours = rec.get("work_hours")
    cur_late_minutes = rec.get("late_minutes", 0)
    cur_overtime_minutes = rec.get("overtime_minutes", 0)
    cur_half_day = rec.get("half_day", False)

    new_status = body.status if body.status is not None else cur_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Supplying punch_in or punch_out when setting status ABSENT or LEAVE is not allowed",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = cur_punch_in

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on record's attendance date
        computed_att_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_att_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record's attendance date")

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = cur_punch_out

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check for actual changes
    changes = {}
    if cur_status != new_status:
        changes["status"] = {"from": cur_status, "to": new_status}
    if cur_punch_in != new_punch_in:
        changes["punch_in"] = {"from": cur_punch_in, "to": new_punch_in}
    if cur_punch_out != new_punch_out:
        changes["punch_out"] = {"from": cur_punch_out, "to": new_punch_out}
    if cur_work_hours != new_work_hours:
        changes["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}
    if cur_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": cur_late_minutes, "to": new_late_minutes}
    if cur_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": cur_overtime_minutes, "to": new_overtime_minutes}
    if cur_half_day != new_half_day:
        changes["half_day"] = {"from": cur_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="A request that changes nothing is rejected")

    history_entry = {
        "at": current_truncated_utc_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    cur_history = rec.get("history") or []
    # Use $or so the filter works when the field is absent (treated as []) and
    # when it is present. This keeps the optimistic-concurrency guarantee:
    # if another request appended an entry between our read and this write,
    # the size will no longer match and we return 409.
    if len(cur_history) == 0:
        history_filter = {
            "$or": [
                {"history": {"$exists": False}},
                {"history": {"$size": 0}},
            ]
        }
    else:
        history_filter = {"history": {"$size": len(cur_history)}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], **history_filter},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return format_attendance_for_api(updated)


# 8. Employee Monthly Analytics
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyOut,
    tags=["analytics"],
    summary="Monthly attendance summary for one employee",
    operation_id="employeeMonthly",
)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end = compute_month_boundaries(month)
    eff_start = max(month_start, emp["joined_on"])
    working_days = count_working_days(eff_start, month_end)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = stats["present_days"]
        leave_days = stats["leave_days"]
        late_count = stats["late_count"]
        total_late = stats["total_late_minutes"]
        total_ot = stats["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up_2((present_days / working_days) * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# 9. Department Summary Analytics
@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryOut,
    tags=["analytics"],
    summary="Department summary for a month",
    operation_id="departmentSummary",
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    month_start, month_end = compute_month_boundaries(month)
    match_filter = {"joined_on": {"$lte": month_end}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "work_hours": 1,
                            "late_minutes": 1,
                            "status": 1,
                            "half_day": 1,
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "wh_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "wh_cnt": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"_id": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        stats = doc.get("stats") or {}
        wh_cnt = stats.get("wh_cnt", 0)
        wh_sum = stats.get("wh_sum", 0.0)
        avg_wh = round_half_up_2(wh_sum / wh_cnt) if wh_cnt > 0 else None

        items.append(
            {
                "department": doc["_id"],
                "headcount": doc["headcount"],
                "present_days": stats.get("present_days", 0.0),
                "avg_work_hours": avg_wh,
                "late_count": stats.get("late_count", 0),
                "total_late_minutes": stats.get("total_late_minutes", 0),
                "leave_count": stats.get("leave_count", 0),
                "on_duty_count": stats.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": items}


# 10. Late Leaderboard Analytics
@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardOut,
    tags=["analytics"],
    summary="Late-comers leaderboard for a month",
    operation_id="lateLeaderboard",
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    month_start, month_end = compute_month_boundaries(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# 11. Department Trend Analytics
@app.get(
    "/analytics/departments/{department}/trend",
    response_model=TrendOut,
    tags=["analytics"],
    summary="Daily attendance trend for a department",
    operation_id="departmentTrend",
)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Verify department exists
    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_next_dt = datetime.fromisoformat((d_to + timedelta(days=1)).isoformat()).replace(tzinfo=timezone.utc)

    emp_codes = [e["emp_code"] for e in dept_emps]

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": {"$literal": from_dt}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_next_dt]},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "dow": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", emp_codes]},
                                    {"$eq": ["$date", "$$d"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "log_stats",
            }
        },
        {"$unwind": {"path": "$log_stats", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_emps,
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": {"$ifNull": ["$log_stats.present_count", 0.0]},
                "late_count": {"$ifNull": ["$log_stats.late_count", 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for row in raw_items:
        items.append(
            {
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": row["headcount"],
                "present_count": row["present_count"],
                "late_count": row["late_count"],
                "attendance_rate": round_half_up_4(row["attendance_rate"]),
                "moving_avg_7d": round_half_up_4(row["moving_avg_7d"]),
            }
        )

    return {"department": department, "items": items}


# 12. Admin Explain
@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainOut,
    tags=["admin"],
    summary="MongoDB explain for an endpoint's main query",
    operation_id="explainEndpoint",
)
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")

    if endpoint == "attendance_list":
        # Run the same date-format validation as the real list_attendance endpoint.
        if date_from:
            try:
                date.fromisoformat(date_from)
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail=[{"loc": ["query", "date_from"], "msg": "Invalid date_from format", "type": "value_error"}],
                )
        if date_to:
            try:
                date.fromisoformat(date_to)
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail=[{"loc": ["query", "date_to"], "msg": "Invalid date_to format", "type": "value_error"}],
                )
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = ((page or 1) - 1) * (page_size or 20)
        explain_res = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size or 20)
            .explain()
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        month_start, month_end = compute_month_boundaries(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {
                "$project": {
                    "status": 1,
                    "half_day": 1,
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                    "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = compute_month_boundaries(month)
        match_filter = {"joined_on": {"$lte": month_end}}
        if department:
            match_filter["department"] = department
        pipeline = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "work_hours": 1,
                                "late_minutes": 1,
                                "status": 1,
                                "half_day": 1,
                                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                ]
                                            },
                                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    }
                                },
                                "wh_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            "$work_hours",
                                            0.0,
                                        ]
                                    }
                                },
                                "wh_cnt": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                                },
                                "total_late_minutes": {
                                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                                },
                                "leave_count": {
                                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                                },
                                "on_duty_count": {
                                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                                },
                            }
                        },
                    ],
                    "as": "stats",
                }
            },
            {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
            {"$sort": {"_id": 1}},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = compute_month_boundaries(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit or 10}}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$emp.name",
                        "department": "$emp.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
                {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            ]
        )
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        dept = department
        if not dept or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from or (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        dept_emps = list(db.employees.find({"department": dept}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        to_next_dt = datetime.fromisoformat((d_to + timedelta(days=1)).isoformat()).replace(tzinfo=timezone.utc)
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {"$match": {"department": dept}},
            {"$limit": 1},
            {"$project": {"date_dt": {"$literal": from_dt}}},
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_next_dt]},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "dow": {"$dayOfWeek": "$date_dt"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"d": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", emp_codes]},
                                        {"$eq": ["$date", "$$d"]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                                },
                            }
                        },
                    ],
                    "as": "log_stats",
                }
            },
            {"$unwind": {"path": "$log_stats", "preserveNullAndEmptyArrays": True}},
            {
                "$addFields": {
                    "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": dept_emps,
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        }
                    },
                    "present_count": {"$ifNull": ["$log_stats.present_count", 0.0]},
                    "late_count": {"$ifNull": ["$log_stats.late_count", 0]},
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
