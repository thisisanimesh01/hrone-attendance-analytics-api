"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

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
    except Exception as e:
        # Log or preserve failure state
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers & Business Calculations (R1 - R5)
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
    # If two simultaneous punch-outs arrive, exactly one succeeds and the other gets 409
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
        return_document=True  # Return the updated doc
    )

    if not updated_doc:
        # The record was already punched out
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
    # Validate date_from and date_to
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


# --------------------------------------------------------------------------- #
# Endpoints to be implemented in Day 2 & Day 3:
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
