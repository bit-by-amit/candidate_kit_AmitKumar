"""
Employee Attendance & Analytics API
FastAPI + MongoDB implementation adhering strictly to openapi.yaml and DATA_MODEL.md.

How to run:
    uvicorn app.main:app --port 8000
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# =========================================================================== #
# 1. CONFIGURATION & DATABASE CONNECTION
# =========================================================================== #

# Load local .env file if it exists (real environment variables will take priority)
load_dotenv()

# Read database connection parameters from environment variables
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Initialize the MongoDB client (tz_aware=True ensures UTC awareness for datetimes)
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# Indian Standard Time (IST) is UTC+05:30 as defined by business rule R1
IST = timezone(timedelta(hours=5, minutes=30))

# Allowed range for valid epoch milliseconds (rejects numbers that look like seconds or floats)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# =========================================================================== #
# 2. HELPER FUNCTIONS FOR BUSINESS RULES (R1 - R8)
# =========================================================================== #

def half_up_round(value: Union[float, Decimal, None], decimals: int = 2) -> Optional[float]:
    """
    R4 & R8: Half-up rounding (arithmetic rounding).
    Standard Python round() uses 'banker's rounding' (round to even).
    To follow the specification strictly, we use Decimal with ROUND_HALF_UP.
    """
    if value is None:
        return None
    decimal_val = Decimal(str(value))
    format_pattern = "0." + "0" * decimals if decimals > 0 else "0"
    return float(decimal_val.quantize(Decimal(format_pattern), rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    """
    R1: Converts an epoch milliseconds integer into a UTC datetime.
    Also applies Rule R1: instants are truncated to whole seconds (e.g. 09:40:00.900 -> 09:40:00).
    """
    # Truncate any millisecond remainder down to whole seconds
    truncated_ms = (epoch_ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a Python datetime object into an integer count of epoch milliseconds
    for serialization in HTTP API responses.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def current_utc_truncated() -> datetime:
    """
    Returns the current server clock time in UTC, truncated to whole seconds (R1).
    """
    now_seconds = int(datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp(now_seconds, tz=timezone.utc)


def parse_hh_mm(time_str: str) -> Tuple[int, int]:
    """
    Splits an 'HH:MM' string into integer hours and minutes.
    """
    hours, minutes = map(int, time_str.split(":"))
    return hours, minutes


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Rule R1 (Time zone and attendance date):
    - Shift times and attendance dates are in Indian Standard Time (IST, UTC+05:30).
    - The record's date is normally the IST calendar date of punch_in.
    - Overnight shift exception (shift_end <= shift_start, e.g., 22:00 to 06:00):
      A punch-in whose IST time is earlier than shift_end belongs to the shift that
      started the previous day.
    """
    # 1. Convert the UTC punch instant to IST
    punch_in_ist = punch_in_utc.astimezone(IST)

    # 2. Check if the shift is an overnight shift
    start_hour, start_min = parse_hh_mm(shift_start)
    end_hour, end_min = parse_hh_mm(shift_end)
    is_overnight = (end_hour, end_min) <= (start_hour, start_min)

    # 3. If overnight and punched in after midnight before shift_end, it counts as yesterday
    shift_end_time = time(end_hour, end_min)
    if is_overnight and punch_in_ist.time() < shift_end_time:
        attendance_date = punch_in_ist.date() - timedelta(days=1)
    else:
        attendance_date = punch_in_ist.date()

    return attendance_date.isoformat()


def calc_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    Rule R2 (Late Minutes):
    - Grace period is 10 minutes.
    - An employee is late only if punch_in is STRICTLY more than 10 minutes after shift_start.
    - When late, late_minutes = whole minutes (floor) elapsed since shift_start (not grace end).
    - Example (shift 09:30):
        09:40:00 -> 0 minutes late (inside grace)
        09:40:01 -> 10 minutes late (exceeded grace, counted from 09:30)
    """
    attendance_date = date.fromisoformat(att_date_str)
    start_hour, start_min = parse_hh_mm(shift_start)

    # Shift start instant in IST on the attendance day
    shift_start_ist = datetime.combine(attendance_date, time(start_hour, start_min), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    # Calculate the end of the 10-minute grace period
    grace_cutoff = shift_start_ist + timedelta(minutes=10)

    # If the punch is strictly after the grace cutoff, calculate whole minutes late
    if punch_in_ist > grace_cutoff:
        seconds_late = (punch_in_ist - shift_start_ist).total_seconds()
        return max(0, int(seconds_late // 60))

    return 0


def calc_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    Rule R3 (Overtime Minutes):
    - Whole minutes (floor) between shift_end and punch_out.
    - Counted ONLY when that duration is >= 30 minutes; otherwise 0.
    - For overnight shifts, shift_end falls on the next calendar day.
    """
    attendance_date = date.fromisoformat(att_date_str)
    start_hour, start_min = parse_hh_mm(shift_start)
    end_hour, end_min = parse_hh_mm(shift_end)

    # Overnight shift check: if shift_end <= shift_start, the shift ends on the next calendar day
    if (end_hour, end_min) <= (start_hour, start_min):
        shift_end_date = attendance_date + timedelta(days=1)
    else:
        shift_end_date = attendance_date

    shift_end_ist = datetime.combine(shift_end_date, time(end_hour, end_min), tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    overtime_seconds = (punch_out_ist - shift_end_ist).total_seconds()
    overtime_minutes = int(overtime_seconds // 60)

    # Only credit overtime if it reaches or exceeds the 30-minute threshold
    return overtime_minutes if overtime_minutes >= 30 else 0


def calc_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Rule R4 (Work Hours):
    - (punch_out - punch_in) in seconds / 3600.
    - Rounded to 2 decimals half-up.
    """
    duration_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = duration_seconds / 3600.0
    return half_up_round(hours, decimals=2)


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats an attendance document from MongoDB into the schema required by the HTTP API:
    - Removes internal MongoDB '_id'.
    - Converts BSON datetimes into epoch milliseconds integers.
    - Formats the 'history' array and all audit trail changes cleanly.
    """
    formatted_history = []
    for entry in doc.get("history", []) or []:
        entry_time = entry.get("at")
        if isinstance(entry_time, datetime):
            entry_time = utc_dt_to_epoch_ms(entry_time)

        formatted_changes = {}
        for field_name, change in (entry.get("changes") or {}).items():
            from_val = change.get("from")
            to_val = change.get("to")

            # Punch times inside history.changes must also be epoch milliseconds
            if isinstance(from_val, datetime):
                from_val = utc_dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = utc_dt_to_epoch_ms(to_val)

            formatted_changes[field_name] = {"from": from_val, "to": to_val}

        formatted_history.append({
            "at": entry_time,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": formatted_changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": formatted_history,
    }


def sanitize_mongo_doc(obj: Any) -> Any:
    """
    Recursively converts BSON datetimes and custom BSON objects into standard JSON-serializable types.
    Used by the /admin/explain endpoint to safely serialize MongoDB explain output.
    """
    if isinstance(obj, dict):
        return {k: sanitize_mongo_doc(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_mongo_doc(item) for item in obj]
    elif isinstance(obj, datetime):
        return utc_dt_to_epoch_ms(obj)
    elif hasattr(obj, "__str__") and type(obj).__name__ in ("ObjectId", "Decimal128", "Int64"):
        return str(obj)
    return obj


# =========================================================================== #
# 3. DATABASE LIFESPAN & STARTUP INDEXES
# =========================================================================== #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI Lifespan handler: runs automatically when the application starts up.
    Creates all necessary MongoDB indexes idempotently so queries use index scans (IXSCAN)
    and race conditions are prevented at the database level.
    """
    try:
        # Index on employees:
        # 1. Unique index on emp_code (enforces identity uniqueness)
        db.employees.create_index([("emp_code", pymongo.ASCENDING)], unique=True)
        # 2. Indexes for department filtering and headcount lookup
        db.employees.create_index([("department", pymongo.ASCENDING)])
        db.employees.create_index([("joined_on", pymongo.ASCENDING)])

        # Indexes on attendance_logs:
        # 1. Unique compound index on (emp_code, date) -> prevents duplicate punch-ins atomically
        db.attendance_logs.create_index(
            [("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)],
            unique=True,
        )
        # 2. Compound index for sorted pagination (date descending, emp_code ascending)
        db.attendance_logs.create_index(
            [("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]
        )
        # 3. Index for finding latest open punch-in across midnight
        db.attendance_logs.create_index(
            [("emp_code", pymongo.ASCENDING), ("punch_in", pymongo.DESCENDING)]
        )
        # 4. Indexes for date-range and monthly analytics aggregations
        db.attendance_logs.create_index([("date", pymongo.ASCENDING), ("status", pymongo.ASCENDING)])
        db.attendance_logs.create_index([("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING), ("late_minutes", pymongo.ASCENDING)])
    except Exception as error:
        print(f"Notice: Index setup message: {error}")

    yield  # Application runs here while active


# Initialize FastAPI application with title, version, and the startup lifespan handler
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# =========================================================================== #
# 4. REQUEST VALIDATION SCHEMAS (PYDANTIC)
# =========================================================================== #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, end_val: str, info):
        # Shift start must be different from shift end (equal shift is invalid)
        start_val = info.data.get("shift_start")
        if start_val and end_val == start_val:
            raise ValueError("shift_start must differ from shift_end")
        return end_val


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, value: Optional[int]):
        if value is not None and (value < MIN_EPOCH_MS or value > MAX_EPOCH_MS):
            raise ValueError("Epoch milliseconds out of range")
        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, value: Optional[int]):
        if value is not None and (value < MIN_EPOCH_MS or value > MAX_EPOCH_MS):
            raise ValueError("Epoch milliseconds out of range")
        return value


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_epoch_timestamps(cls, value: Optional[int]):
        if value is not None and (value < MIN_EPOCH_MS or value > MAX_EPOCH_MS):
            raise ValueError("Epoch milliseconds out of range")
        return value


# =========================================================================== #
# 5. CORE ENDPOINTS (HEALTH, EMPLOYEES, ATTENDANCE)
# =========================================================================== #

@app.get("/health")
def health():
    """
    Readiness probe:
    Returns HTTP 200 {"status": "ok"} only if MongoDB answers a ping.
    Returns HTTP 503 if the database is unreachable.
    """
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Creates a new employee record.
    - Validates that shift_start != shift_end.
    - created_at is generated on the server as a UTC datetime.
    - Enforced by unique database index on emp_code: duplicate requests get HTTP 409.
    """
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    employee_doc = body.model_dump()
    creation_time = current_utc_truncated()
    employee_doc["created_at"] = creation_time

    # Attempt insertion into MongoDB
    try:
        db.employees.insert_one(employee_doc)
    except DuplicateKeyError:
        # Database unique index caught a duplicate emp_code
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return {
        "emp_code": employee_doc["emp_code"],
        "name": employee_doc["name"],
        "email": employee_doc["email"],
        "department": employee_doc["department"],
        "shift_start": employee_doc["shift_start"],
        "shift_end": employee_doc["shift_end"],
        "joined_on": employee_doc["joined_on"],
        "created_at": utc_dt_to_epoch_ms(creation_time),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists employees with pagination and optional department filter.
    - Sorted by emp_code ascending.
    - 'total' reflects the filtered count, not the entire database count.
    - page is 1-indexed (skip = (page - 1) * page_size).
    """
    query: Dict[str, Any] = {}
    if department:
        query["department"] = department

    # Total count of documents matching the filter
    total_count = db.employees.count_documents(query)

    # Calculate pagination skip
    skip_count = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", pymongo.ASCENDING).skip(skip_count).limit(page_size)

    items = []
    for emp in cursor:
        items.append({
            "emp_code": emp["emp_code"],
            "name": emp["name"],
            "email": emp["email"],
            "department": emp["department"],
            "shift_start": emp["shift_start"],
            "shift_end": emp["shift_end"],
            "joined_on": emp["joined_on"],
            "created_at": utc_dt_to_epoch_ms(emp.get("created_at")),
        })

    return {"items": items, "total": total_count, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Records an employee punch-in event:
    - 404 if employee does not exist.
    - Determines attendance date using Rule R1 (handles overnight shifts).
    - Calculates late minutes using Rule R2 (10-minute grace period).
    - Prevents race conditions via MongoDB unique index on (emp_code, date) -> 409 on duplicate.
    """
    # 1. Verify that employee exists
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Unknown employee")

    # 2. Get punch instant (truncated to whole seconds per Rule R1)
    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_in_utc = current_utc_truncated()

    # 3. Determine the correct attendance date (R1) and late minutes (R2)
    attendance_date = determine_attendance_date(punch_in_utc, employee["shift_start"], employee["shift_end"])
    late_minutes = calc_late_minutes(punch_in_utc, employee["shift_start"], employee["shift_end"], attendance_date)

    # 4. Prepare initial attendance document
    record_doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 5. Insert document into database atomically
    try:
        db.attendance_logs.insert_one(record_doc)
    except DuplicateKeyError:
        # Atomic guarantee: if record already exists for this (emp_code, date), return 409
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_doc(record_doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Records an employee punch-out event:
    - Closes employee's most recent record whose punch_in <= punched_at.
    - Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    - Validates: punched_at > punch_in and duration <= 24 hours (422).
    - Atomic update: prevents duplicate punch-out races (loser gets 409).
    """
    # 1. Verify that employee exists
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Unknown employee")

    # 2. Get punch-out instant in UTC
    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_out_utc = current_utc_truncated()

    # 3. Find the most recent record with punch_in <= punched_at
    candidate_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", pymongo.DESCENDING)],
    )

    if not candidate_record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    # 4. If record is already closed, reject with 409
    if candidate_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = candidate_record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # 5. Validate duration
    duration_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if duration_seconds <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if duration_seconds > 86400:  # More than 24 hours
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    # 6. Calculate derived fields
    work_hours = calc_work_hours(punch_in_utc, punch_out_utc)
    overtime_min = calc_overtime_minutes(punch_out_utc, employee["shift_start"], employee["shift_end"], candidate_record["date"])
    is_half_day = work_hours < 4.50

    # 7. Atomic update (matches punch_out: None to ensure concurrency safety)
    updated_record = db.attendance_logs.find_one_and_update(
        {"_id": candidate_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        # A concurrent request already punched out this record
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return format_attendance_doc(updated_record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists attendance records with filters and database-level sorting & pagination:
    - Validates date_from <= date_to (422 if invalid).
    - Sorted by date descending, then emp_code ascending.
    - Paged directly in MongoDB without loading 100k records into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    # Paged cursor in MongoDB
    total_count = db.attendance_logs.count_documents(query)
    skip_count = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort([
        ("date", pymongo.DESCENDING),
        ("emp_code", pymongo.ASCENDING),
    ]).skip(skip_count).limit(page_size)

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total_count, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date_str"),
    body: RegularizeRequest = ...,
):
    """
    Regularizes (manually corrects) an attendance record:
    - Validates natural key (emp_code, date).
    - If status is ABSENT or LEAVE: clears punch times (422 if supplied).
    - If status is presence: requires punch_in, validates date match (R1), duration <= 24h.
    - If no fields changed: 422.
    - Appends audit entry to 'history' atomically.
    """
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$", date_str):
        raise HTTPException(status_code=422, detail="Invalid date format")

    # 1. Verify employee and attendance record exist
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not existing_record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # 2. Calculate target status and final fields
    target_status = body.status if body.status is not None else existing_record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        # Rule: ABSENT and LEAVE cannot have punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Punch times cannot be supplied for ABSENT or LEAVE status")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status: requires a punch_in
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_utc_dt(body.punch_in)
        else:
            final_punch_in = existing_record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_in is required for presence status")

        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        # Rule R1: punch_in must stay on the same attendance date
        calculated_date = determine_attendance_date(final_punch_in, employee["shift_start"], employee["shift_end"])
        if calculated_date != date_str:
            raise HTTPException(status_code=422, detail="punch_in does not match attendance date")

        # Handle punch_out if present
        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_utc_dt(body.punch_out)
        else:
            final_punch_out = existing_record.get("punch_out")

        if final_punch_out is not None and final_punch_out.tzinfo is None:
            final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        final_late_minutes = calc_late_minutes(final_punch_in, employee["shift_start"], employee["shift_end"], date_str)

        if final_punch_out is not None:
            diff_seconds = (final_punch_out - final_punch_in).total_seconds()
            if diff_seconds <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_seconds > 86400:
                raise HTTPException(status_code=422, detail="Duration cannot exceed 24 hours")

            final_work_hours = calc_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = calc_overtime_minutes(final_punch_out, employee["shift_start"], employee["shift_end"], date_str)
            final_half_day = final_work_hours < 4.50
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # 3. Detect which fields actually changed
    field_changes: Dict[str, Dict[str, Any]] = {}
    bson_changes: Dict[str, Dict[str, Any]] = {}

    def check_diff(field: str, old_val: Any, new_val: Any, is_date: bool = False):
        if is_date:
            old_ms = utc_dt_to_epoch_ms(old_val)
            new_ms = utc_dt_to_epoch_ms(new_val)
            if old_ms != new_ms:
                field_changes[field] = {"from": old_ms, "to": new_ms}
                bson_changes[field] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                field_changes[field] = {"from": old_val, "to": new_val}
                bson_changes[field] = {"from": old_val, "to": new_val}

    check_diff("status", existing_record.get("status"), target_status)
    check_diff("punch_in", existing_record.get("punch_in"), final_punch_in, is_date=True)
    check_diff("punch_out", existing_record.get("punch_out"), final_punch_out, is_date=True)
    check_diff("work_hours", existing_record.get("work_hours"), final_work_hours)
    check_diff("late_minutes", existing_record.get("late_minutes", 0) or 0, final_late_minutes)
    check_diff("overtime_minutes", existing_record.get("overtime_minutes", 0) or 0, final_overtime_minutes)
    check_diff("half_day", bool(existing_record.get("half_day", False)), final_half_day)

    # Reject if nothing changed
    if not field_changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # 4. Prepare history audit entry
    history_entry = {
        "at": current_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": bson_changes,
    }

    # 5. Optimistic concurrency check: match current history length so concurrent edits don't overwrite each other
    current_history_length = len(existing_record.get("history", []) or [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": existing_record["_id"],
            "history": {"$size": current_history_length},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated_doc)


# =========================================================================== #
# 6. ANALYTICS AGGREGATION ENDPOINTS
# =========================================================================== #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee:
    - working_days: Mon-Fri days in month, starting from joined_on for mid-month joiners (R7).
    - present_days: Sum of presence days on Mon-Fri (half day = 0.5).
    - attendance_pct: (present_days / working_days) * 100, R8 half-up rounding.
    """
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 1. Calculate calendar working days in the month (Mon-Fri)
    year, month_num = map(int, month.split("-"))
    first_day_of_month = date(year, month_num, 1)
    next_month = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
    last_day_of_month = next_month - timedelta(days=1)

    employee_joined = date.fromisoformat(employee["joined_on"])
    if employee_joined > last_day_of_month:
        working_days = 0
    else:
        start_date = max(first_day_of_month, employee_joined)
        days_in_range = (last_day_of_month - start_date).days + 1
        working_days = sum(1 for offset in range(days_in_range) if (start_date + timedelta(days=offset)).weekday() < 5)

    # 2. MongoDB aggregation pipeline on attendance_logs
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-\\d\\d$"}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                # MongoDB $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
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
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},  # Mon-Fri only
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    if agg_result:
        metrics = agg_result[0]
        present_days = float(metrics.get("present_days", 0.0))
        leave_days = int(metrics.get("leave_days", 0))
        late_count = int(metrics.get("late_count", 0))
        total_late = int(metrics.get("total_late_minutes", 0))
        total_ot = int(metrics.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    # 3. Calculate percentage
    if working_days > 0:
        attendance_percentage = half_up_round((present_days / working_days) * 100.0, decimals=2)
    else:
        attendance_percentage = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_percentage,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    Department monthly summary via MongoDB aggregation across both collections:
    - Headcount (R9): Employees with joined_on <= end of month (even if they have zero logs).
    - avg_work_hours: Mean work_hours over records that are presence-status and have work_hours.
    - Omit departments whose headcount is 0.
    """
    year, month_num = map(int, month.split("-"))
    next_month = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
    last_day_of_month = (next_month - timedelta(days=1)).isoformat()

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": last_day_of_month}}
    if department:
        match_filter["department"] = department

    # Pipeline begins from 'employees' collection to guarantee zero-log employees are counted
    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}-\\d\\d$"}},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ne": ["$logs", None]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_set": {"$addToSet": "$emp_code"},  # Accurately counts distinct employees
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "present_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$emp_set"},
                "present_days": 1,
                "avg_work_hours": {"$avg": "$present_work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": pymongo.ASCENDING}},
    ]

    results = []
    for doc in db.employees.aggregate(pipeline):
        if doc.get("headcount", 0) > 0:
            avg_hours = half_up_round(doc.get("avg_work_hours"), decimals=2) if doc.get("avg_work_hours") is not None else None
            results.append({
                "department": doc["department"],
                "headcount": doc["headcount"],
                "present_days": float(doc.get("present_days", 0.0)),
                "avg_work_hours": avg_hours,
                "late_count": int(doc.get("late_count", 0)),
                "total_late_minutes": int(doc.get("total_late_minutes", 0)),
                "leave_count": int(doc.get("leave_count", 0)),
                "on_duty_count": int(doc.get("on_duty_count", 0)),
            })

    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    Late-comers leaderboard for a month:
    - Standard Competition Ranking via $rank (ties share rank, subsequent ranks skipped: 1, 2, 2, 4).
    - Limit is applied AFTER ranking: all employees with rank <= limit are returned.
    - Sorted by total_late_minutes descending, then emp_code ascending.
    """
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$regex": f"^{month}-\\d\\d$"}, "late_minutes": {"$gt": 0}}},
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
        {"$unwind": "$emp"},  # Ignore logs whose employee doesn't exist
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},  # Standard competition ranking
            }
        },
        {"$match": {"rank": {"$lte": limit}}},  # Applied after ranking
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
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department:
    - Exactly one row per calendar day from 'from' to 'to' inclusive (uses MongoDB $densify).
    - Weekends and days with no records are included.
    - Moving average over 7 days calculated directly in MongoDB via $setWindowFields.
    - Returns 404 if department has no employees; 422 if to < from or range > 92 days.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    start_cal_date = date.fromisoformat(from_date)
    end_cal_date = date.fromisoformat(to_date)
    if (end_cal_date - start_cal_date).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if department does not exist or has no employees
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(start_cal_date.year, start_cal_date.month, start_cal_date.day, tzinfo=timezone.utc)
    to_dt_exclusive = datetime(end_cal_date.year, end_cal_date.month, end_cal_date.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": from_date}}}},
        # $densify generates every calendar day in the date range
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_exclusive]}}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "dayOfWeek": {"$dayOfWeek": "$d"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}],
                "as": "employees",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "emp_codes": "$employees.emp_code",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "emp_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        # 7-day moving window directly in MongoDB
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
        {"$sort": {"date": 1}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    formatted_items = []
    for item in raw_items:
        rate = item.get("attendance_rate")
        moving_avg = item.get("moving_avg_7d")
        formatted_items.append({
            "date": item["date"],
            "is_working_day": bool(item["is_working_day"]),
            "headcount": int(item["headcount"]),
            "present_count": float(item["present_count"]),
            "late_count": int(item["late_count"]),
            "attendance_rate": half_up_round(rate, decimals=4) if rate is not None else None,
            "moving_avg_7d": half_up_round(moving_avg, decimals=4) if moving_avg is not None else None,
        })

    return {"department": department, "items": formatted_items}


# =========================================================================== #
# 7. ADMIN EXPLAIN ENDPOINT (DIAGNOSTICS)
# =========================================================================== #

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """
    Runs MongoDB's explain with executionStats verbosity on the main query or pipeline
    for the named endpoint. Proves that indexes are used (IXSCAN) and no full collection
    scans (COLLSCAN) occur on large datasets.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        query: Dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        skip_count = ((page or 1) - 1) * (page_size or 20)
        command_spec = {
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip_count,
                "limit": page_size or 20,
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(command_spec)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(raw_explain)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-\\d\\d$"}}},
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
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
                                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        command_spec = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(command_spec)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(raw_explain)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, month_num = map(int, month.split("-"))
        next_month = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
        last_day_str = (next_month - timedelta(days=1)).isoformat()
        match_filter: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
        if department:
            match_filter["department"] = department
        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$regexMatch": {"input": "$date", "regex": f"^{month}-\\d\\d$"}},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "emp_set": {"$addToSet": "$emp_code"},
                }
            },
        ]
        command_spec = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(command_spec)
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_mongo_doc(raw_explain)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-\\d\\d$"}, "late_minutes": {"$gt": 0}}},
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
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit or 10}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        command_spec = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(command_spec)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(raw_explain)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        start_d = date.fromisoformat(from_date)
        end_d = date.fromisoformat(to_date)
        from_dt = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
        to_dt_exclusive = datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc) + timedelta(days=1)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": from_date}}}},
            {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_exclusive]}}},
            {
                "$lookup": {
                    "from": "employees",
                    "pipeline": [{"$match": {"department": department}}],
                    "as": "employees",
                }
            },
        ]
        command_spec = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(command_spec)
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_mongo_doc(raw_explain)}


# =========================================================================== #
# ORIGINAL STARTER CODE (PRESERVED AS COMMENTS)
# =========================================================================== #
# def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
#     h, m = map(int, shift_start.split(":"))
#     start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
#     minutes = int((punch_in - start).total_seconds() / 60)
#     return minutes if minutes > 10 else 0
#
# def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
#     return round((punch_out - punch_in).total_seconds() / 3600, 2)
#
# def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
#     h, m = map(int, shift_end.split(":"))
#     end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
#     return max(0, int((punch_out - end).total_seconds() / 60))
#
# class EmployeeIn(BaseModel):
#     emp_code: str
#     name: str
#     email: str
#     department: str
#     shift_start: str = "09:30"
#     shift_end: str = "18:30"
#     joined_on: str
#
# class PunchInIn(BaseModel):
#     emp_code: str
#     punched_at: Optional[int] = None
#     status: str = "PRESENT"
#
# @app.get("/health")
# def health():
#     return {"status": "ok"}
#
# @app.post("/employees", status_code=201)
# def create_employee(body: EmployeeIn):
#     if db.employees.find_one({"emp_code": body.emp_code}):
#         raise HTTPException(409, "emp_code already exists")
#     doc = body.model_dump()
#     doc["created_at"] = datetime.now()
#     db.employees.insert_one(doc)
#     doc.pop("_id", None)
#     return doc
#
# @app.get("/employees")
# def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
#     q = {}
#     if department:
#         q["department"] = department
#     skip = page * page_size
#     total = db.employees.count_documents({})
#     items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
#     return {"items": items, "total": total, "page": page, "page_size": page_size}
#
# @app.post("/attendance/punch-in", status_code=201)
# def punch_in(body: PunchInIn):
#     emp = db.employees.find_one({"emp_code": body.emp_code})
#     ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
#     d = ts.date().isoformat()
#     if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
#         raise HTTPException(409, "already punched in for this date")
#     doc = {
#         "emp_code": body.emp_code,
#         "date": d,
#         "status": body.status,
#         "punch_in": ts,
#         "punch_out": None,
#         "work_hours": None,
#         "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
#         "overtime_minutes": 0,
#         "half_day": False,
#         "history": [],
#     }
#     res = db.attendance_logs.insert_one(doc)
#     doc["id"] = str(res.inserted_id)
#     doc.pop("_id", None)
#     return doc
#
# @app.get("/attendance")
# def list_attendance(
#     emp_code: Optional[str] = None,
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 20,
# ):
#     q = {}
#     if emp_code:
#         q["emp_code"] = emp_code
#     if date_from or date_to:
#         q["date"] = {}
#         if date_from:
#             q["date"]["$gte"] = date_from
#         if date_to:
#             q["date"]["$lte"] = date_to
#     if status:
#         q["status"] = status
#     docs = list(db.attendance_logs.find(q))
#     docs.sort(key=lambda d: d["date"], reverse=True)
#     total = len(docs)
#     page_docs = docs[(page - 1) * page_size : page * page_size]
#     for d in page_docs:
#         d["id"] = str(d.pop("_id"))
#     return {"items": page_docs, "total": total, "page": page, "page_size": page_size}
