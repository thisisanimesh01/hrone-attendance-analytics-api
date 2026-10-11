"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Real environment variables take precedence
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB_NAME]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Startup & Index Management
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at application startup."""
    # 1. employees: unique emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    # For filtering by department and sorting by emp_code:
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_department_emp_code")
    # For headcount queries checking joined_on
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")
    # Compound index for department and joined_on
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_dept_joined_on")

    # 2. attendance_logs: unique (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_emp_code_date_unique"
    )
    # For listing queries with sorting date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_emp_code_asc"
    )
    # For date range lookups and analytics
    db.attendance_logs.create_index([("date", ASCENDING)], name="idx_attendance_date_asc")
    # Compound index for date and late_minutes
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_date_late_min")
    # Compound index for emp_code, date, and status
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="idx_emp_date_status")
    # For punch-out search (finding open record by emp_code and punch_in <= punched_at)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_emp_punch_in"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes at startup
    try:
        ensure_indexes()
    except Exception:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers & Business Calculations (R1 - R8)
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or timezone-aware datetime into epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    # Truncate to whole seconds per R1: 09:40:00.900 counts as 09:40:00
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def parse_hh_mm(hh_mm: str) -> Tuple[int, int]:
    h, m = map(int, hh_mm.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift
    that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()
    ist_time = punch_in_ist.time()

    start_h, start_m = parse_hh_mm(shift_start)
    end_h, end_m = parse_hh_mm(shift_end)

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and ist_time < time(end_h, end_m):
        # Belongs to the shift that started the previous day
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period is 10 minutes.
    A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = dt_date.fromisoformat(attendance_date_str)
    start_h, start_m = parse_hh_mm(shift_start)
    scheduled_start_ist = datetime.combine(d, time(start_h, start_m), tzinfo=IST)

    diff_seconds = int((punch_in_ist - scheduled_start_ist).total_seconds())
    if diff_seconds > 600:  # strictly more than 10 minutes (600 seconds)
        return diff_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = diff_sec / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    d = dt_date.fromisoformat(attendance_date_str)
    start_h, start_m = parse_hh_mm(shift_start)
    end_h, end_m = parse_hh_mm(shift_end)

    if (end_h, end_m) <= (start_h, start_m):
        # Overnight shift ends the next calendar day
        end_date = d + timedelta(days=1)
    else:
        end_date = d

    scheduled_end_ist = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)
    diff_sec = int((punch_out_ist - scheduled_end_ist).total_seconds())
    diff_minutes = diff_sec // 60
    if diff_minutes >= 30:
        return diff_minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_2_half_up(val: Optional[float]) -> Optional[float]:
    """R8: 2 decimals half-up."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_4_half_up(val: Optional[float]) -> Optional[float]:
    """R8: Rates 4 decimals half-up."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def count_working_days(start_date: dt_date, end_date: dt_date) -> int:
    """R7: Working days Monday-Friday."""
    if start_date > end_date:
        return 0
    cur = start_date
    w_days = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # 0 to 4 is Mon-Fri
            w_days += 1
        cur += timedelta(days=1)
    return w_days


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize employee doc for API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_serialized = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_ms(to_val)
        changes_serialized[k] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_serialized,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize attendance doc for API response per openapi.yaml."""
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request Schemas (Pydantic v2)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shifts_differ(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start")
        if shift_start is not None and shift_start == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {str(e)}"
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(UTC)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")
    except PyMongoError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = from_epoch_ms(body.punched_at)
    else:
        # Default to now, truncated to whole seconds (R1)
        now_ms = int(datetime.now(UTC).timestamp()) * 1000
        punch_in_utc = from_epoch_ms(now_ms)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date"
        )
    except PyMongoError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = from_epoch_ms(body.punched_at)
    else:
        now_ms = int(datetime.now(UTC).timestamp()) * 1000
        punch_out_utc = from_epoch_ms(now_ms)

    # Find the employee's most recent record that has a punch_in
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No matching punch-in found for employee"
        )

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in -> 422
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly greater than punch_in"
        )
    if diff_sec > 86400:  # 24 hours
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )

    # Compute R3, R4, R5
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    half_day_val = compute_half_day(work_hrs)

    # Atomic update: only if punch_out is currently null!
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_day_val,
            }
        },
        return_document=True
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is already punched out"
        )

    return serialize_attendance(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attendance_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if attendance_status:
        q["status"] = attendance_status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", status_code=status.HTTP_200_OK)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Supplying punch_in or punch_out with ABSENT or LEAVE status is invalid"
            )
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires punch_in"
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=UTC)

        in_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if in_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in must stay on attendance date {date}, got {in_date}"
            )

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly greater than punch_in"
                )
            if diff_sec > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )

    # Derived field calculation
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    changes = {}

    def compare_field(key, old_v, new_v):
        if key in ("punch_in", "punch_out"):
            old_dt = old_v.replace(tzinfo=UTC) if (old_v and old_v.tzinfo is None) else old_v
            new_dt = new_v.replace(tzinfo=UTC) if (new_v and new_v.tzinfo is None) else new_v
            if old_dt != new_dt:
                changes[key] = {"from": old_dt, "to": new_dt}
        else:
            if old_v != new_v:
                changes[key] = {"from": old_v, "to": new_v}

    compare_field("status", record["status"], target_status)
    compare_field("punch_in", record.get("punch_in"), new_punch_in)
    compare_field("punch_out", record.get("punch_out"), new_punch_out)
    compare_field("work_hours", record.get("work_hours"), new_work_hours)
    compare_field("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    compare_field("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    compare_field("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changes nothing"
        )

    now_utc = datetime.now(UTC)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency update
    current_history_len = len(record.get("history", []))
    update_res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=True
    )

    if not update_res:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict during regularization"
        )

    return serialize_attendance(update_res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year_str, month_str = month.split("-")
    year = int(year_str)
    m = int(month_str)
    num_days = calendar.monthrange(year, m)[1]
    month_start = dt_date(year, m, 1)
    month_end = dt_date(year, m, num_days)

    joined_date = dt_date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start, joined_date)
    working_days = count_working_days(effective_start, month_end)

    start_date_str = f"{month}-01"
    end_date_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_str, "$lte": end_date_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": {"$dateFromString": {"dateString": "$date"}},
                        "timezone": "+05:30",
                    }
                },
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    if agg_result:
        row = agg_result[0]
        raw_present = row.get("present_days", 0.0)
        present_days = round_2_half_up(raw_present) or 0.0
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct_val = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(pct_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year_str, month_str = month.split("-")
    year = int(year_str)
    m = int(month_str)
    num_days = calendar.monthrange(year, m)[1]
    month_end_str = f"{month}-{num_days:02d}"
    start_date_str = f"{month}-01"
    end_date_str = month_end_str

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
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
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "date": {"$dateFromString": {"dateString": "$date"}},
                                    "timezone": "+05:30",
                                }
                            },
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
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "sum_work_hours": {
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
                            "count_work_hours": {
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
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "logs_summary",
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        headcount = r["headcount"]
        if headcount == 0:
            continue
        dept_name = r["_id"]
        logs_summary = r.get("logs_summary", [])
        if logs_summary:
            ls = logs_summary[0]
            raw_pres = ls.get("present_days", 0.0)
            present_days = round_2_half_up(raw_pres) or 0.0
            sum_wh = ls.get("sum_work_hours", 0.0)
            count_wh = ls.get("count_work_hours", 0)
            if count_wh > 0:
                avg_wh = round_2_half_up(sum_wh / count_wh)
            else:
                avg_wh = None
            late_count = int(ls.get("late_count", 0))
            total_late_minutes = int(ls.get("total_late_minutes", 0))
            leave_count = int(ls.get("leave_count", 0))
            on_duty_count = int(ls.get("on_duty_count", 0))
        else:
            present_days = 0.0
            avg_wh = None
            late_count = 0
            total_late_minutes = 0
            leave_count = 0
            on_duty_count = 0

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year_str, month_str = month.split("-")
    year = int(year_str)
    m = int(month_str)
    num_days = calendar.monthrange(year, m)[1]
    start_date_str = f"{month}-01"
    end_date_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date_str, "$lte": end_date_str},
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

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.append({
        "$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }
    })
    pipeline.append({
        "$sort": {
            "total_late_minutes": DESCENDING,
            "emp_code": ASCENDING,
        }
    })

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Compute standard competition ranking (1, 1, 3)
    ranked_items = []
    current_rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            if row["total_late_minutes"] == rows[i - 1]["total_late_minutes"]:
                rank = current_rank
            else:
                rank = i + 1
                current_rank = rank
        else:
            rank = 1
            current_rank = 1

        if rank <= limit:
            ranked_items.append({
                "rank": rank,
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            })

    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_d = dt_date.fromisoformat(from_date)
        end_d = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from and to must be valid YYYY-MM-DD dates"
        )

    if end_d < start_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to date cannot be earlier than from date"
        )

    total_days = (end_d - start_d).days + 1
    if total_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days"
        )

    # Check department existence
    dept_employees = list(db.employees.find({"department": department}))
    if not dept_employees:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found"
        )

    dept_emp_codes = [e["emp_code"] for e in dept_employees]

    # Use $densify aggregation
    bounds_start = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
    bounds_end = datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [bounds_start, bounds_end],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    daily_rows = list(db.attendance_logs.aggregate(pipeline))
    row_by_date = {r["date"]: r for r in daily_rows}

    # Headcount lookup and rate calculation
    rates = []
    items = []
    cur_d = start_d
    while cur_d <= end_d:
        d_str = cur_d.isoformat()
        is_working_day = cur_d.weekday() < 5
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= d_str)

        row_data = row_by_date.get(d_str, {"present_count": 0.0, "late_count": 0})
        raw_pres = row_data["present_count"]
        present_count = round_2_half_up(raw_pres) or 0.0
        late_count = int(row_data["late_count"])

        if is_working_day and headcount > 0:
            rate_val = Decimal(str(present_count)) / Decimal(str(headcount))
            attendance_rate = float(rate_val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            attendance_rate = None

        rates.append(attendance_rate)

        # 7-day moving average
        idx = len(rates) - 1
        window = [r for r in rates[max(0, idx - 6) : idx + 1] if r is not None]
        if not window:
            moving_avg_7d = None
        else:
            m_val = Decimal(str(sum(window))) / Decimal(str(len(window)))
            moving_avg_7d = float(m_val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })
        cur_d += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attendance_status: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be greater than date_to"
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if attendance_status:
            q["status"] = attendance_status

        skip = (page - 1) * page_size
        command_spec = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required for employee_monthly"
            )
        year_str, month_str = month.split("-")
        num_days = calendar.monthrange(int(year_str), int(month_str))[1]
        start_date_str = f"{month}-01"
        end_date_str = f"{month}-{num_days:02d}"

        pipe = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date_str, "$lte": end_date_str},
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                }
            },
        ]
        command_spec = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary"
            )
        year_str, month_str = month.split("-")
        num_days = calendar.monthrange(int(year_str), int(month_str))[1]
        month_end_str = f"{month}-{num_days:02d}"

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
        if department is not None:
            emp_match["department"] = department

        pipe = [
            {"$match": emp_match},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
        ]
        command_spec = {
            "aggregate": "employees",
            "pipeline": pipe,
            "cursor": {},
        }
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard"
            )
        year_str, month_str = month.split("-")
        num_days = calendar.monthrange(int(year_str), int(month_str))[1]
        start_date_str = f"{month}-01"
        end_date_str = f"{month}-{num_days:02d}"

        pipe = [
            {
                "$match": {
                    "date": {"$gte": start_date_str, "$lte": end_date_str},
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
        ]
        command_spec = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend"
            )
        try:
            start_d = dt_date.fromisoformat(from_date)
            end_d = dt_date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="from and to must be valid dates"
            )
        if end_d < start_d:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="to date cannot be earlier than from date"
            )
        if (end_d - start_d).days + 1 > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Date range cannot exceed 92 days"
            )

        dept_employees = list(db.employees.find({"department": department}))
        dept_emp_codes = [e["emp_code"] for e in dept_employees]

        pipe = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {"$sum": 1},
                }
            },
        ]
        command_spec = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    try:
        raw_explain = db.command("explain", command_spec, verbosity="executionStats")
        clean_explain = json.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": collection_name,
            "explain": clean_explain,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Explain failed: {str(e)}"
        )
