"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import calendar
import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup — idempotent index creation
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _round_half_up(value, decimals=2):
    """Round a number using ROUND_HALF_UP (not banker's rounding)."""
    d = Decimal(str(value))
    if decimals == 2:
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    elif decimals == 4:
        return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    else:
        q = Decimal(10) ** -decimals
        return float(d.quantize(q, rounding=ROUND_HALF_UP))


def _truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def _epoch_ms_to_dt(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


def _dt_to_epoch_ms(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Assume UTC for naive datetimes from MongoDB
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _validate_epoch_ms(value, field_name="punched_at"):
    """Validate that value is a valid epoch millisecond integer."""
    if value is None:
        return
    if not isinstance(value, int):
        raise HTTPException(422, f"{field_name} must be an integer (epoch milliseconds)")
    if value < 100000000000 or value > 4102444800000:
        raise HTTPException(422, f"{field_name} out of range")


def _parse_shift_time(time_str: str):
    """Parse HH:MM into (hour, minute)."""
    h, m = map(int, time_str.split(":"))
    return h, m


def _is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if this is an overnight shift (shift_end <= shift_start)."""
    sh, sm = _parse_shift_time(shift_start)
    eh, em = _parse_shift_time(shift_end)
    return (eh * 60 + em) <= (sh * 60 + sm)


def _compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """Compute the attendance date (IST calendar date) per R1.
    For overnight shifts, if punch_in IST time is before shift_end,
    the date is the previous day (the shift started the day before).
    """
    ist_time = punch_in_ist.astimezone(IST)
    if _is_overnight_shift(shift_start, shift_end):
        eh, em = _parse_shift_time(shift_end)
        punch_time_minutes = ist_time.hour * 60 + ist_time.minute
        end_minutes = eh * 60 + em
        if punch_time_minutes < end_minutes:
            # Punch-in is before shift_end → belongs to previous day's shift
            return (ist_time.date() - timedelta(days=1)).isoformat()
    return ist_time.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """Compute late_minutes per R2.
    Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    late_minutes = floor of minutes since shift_start (not since grace end).
    """
    sh, sm = _parse_shift_time(shift_start)
    att_date = date_type.fromisoformat(attendance_date)

    # Build shift start datetime in IST
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day,
                              sh, sm, 0, tzinfo=IST)

    punch_in_ist = punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_ist.astimezone(IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds < 0:
        # Edge case: for overnight shifts, the shift_start may be interpreted differently
        # but for normal shifts, being early is not late
        return 0

    # R2: strictly more than 10:00 (600 seconds)
    if diff_seconds > 600:
        return int(diff_seconds // 60)  # floor
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Compute work_hours per R4. Half-up rounding to 2 decimals."""
    if punch_out is None:
        return None
    # Make both timezone-aware for correct subtraction
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    seconds = (punch_out - punch_in).total_seconds()
    return _round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """Compute overtime_minutes per R3.
    Floor minutes from shift_end to punch_out, only if >= 30, otherwise 0.
    For overnight shifts, shift_end is on the next calendar day.
    """
    if punch_out is None:
        return 0

    eh, em = _parse_shift_time(shift_end)
    att_date = date_type.fromisoformat(attendance_date)

    # For overnight shifts, shift_end is on the next calendar day
    if _is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day,
                            eh, em, 0, tzinfo=IST)

    punch_out_tz = punch_out if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc)
    punch_out_tz = punch_out_tz.astimezone(IST)

    diff_seconds = (punch_out_tz - shift_end_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = int(diff_seconds // 60)  # floor
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """Compute half_day per R5. True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def _format_attendance_record(doc: dict) -> dict:
    """Format an attendance record for API response.
    Converts BSON datetimes to epoch ms, removes _id, ensures all fields present.
    """
    doc.pop("_id", None)
    # Convert punch_in/punch_out to epoch ms
    if doc.get("punch_in") is not None:
        doc["punch_in"] = _dt_to_epoch_ms(doc["punch_in"])
    else:
        doc["punch_in"] = None
    if doc.get("punch_out") is not None:
        doc["punch_out"] = _dt_to_epoch_ms(doc["punch_out"])
    else:
        doc["punch_out"] = None

    # Ensure defaults for missing fields
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    doc.setdefault("work_hours", None)

    # Convert history entries
    for entry in doc.get("history", []):
        if "at" in entry and entry["at"] is not None:
            if isinstance(entry["at"], datetime):
                entry["at"] = _dt_to_epoch_ms(entry["at"])
        changes = entry.get("changes", {})
        for key in ["punch_in", "punch_out"]:
            if key in changes:
                change = changes[key]
                if "from" in change and isinstance(change["from"], datetime):
                    change["from"] = _dt_to_epoch_ms(change["from"])
                elif "from" in change and change["from"] is None:
                    pass  # keep None
                if "to" in change and isinstance(change["to"], datetime):
                    change["to"] = _dt_to_epoch_ms(change["to"])
                elif "to" in change and change["to"] is None:
                    pass  # keep None

    return doc


def _format_employee(doc: dict) -> dict:
    """Format an employee document for API response."""
    doc.pop("_id", None)
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = _dt_to_epoch_ms(doc["created_at"])
    return doc


def _validate_page_params(page: int, page_size: int):
    """Validate pagination parameters per R10."""
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def _validate_month(month: str) -> tuple:
    """Validate YYYY-MM format, return (year, month_num)."""
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
        raise HTTPException(422, "month must be YYYY-MM format")
    parts = month.split("-")
    return int(parts[0]), int(parts[1])


def _month_date_range(year: int, month: int):
    """Return (first_day, last_day) as ISO strings for a given year/month."""
    first = f"{year:04d}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    last = f"{year:04d}-{month:02d}-{last_day:02d}"
    return first, last


def _count_working_days(start_date: str, end_date: str) -> int:
    """Count Mon-Fri days from start_date to end_date (inclusive)."""
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date)
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# ----- Health ----- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# ----- Employees ----- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # Validate emp_code format
    if not re.match(r'^EMP\d{4,6}$', body.emp_code):
        raise HTTPException(422, "emp_code must match ^EMP\\d{4,6}$")

    # Validate name
    if not body.name or len(body.name) > 100:
        raise HTTPException(422, "name must be 1-100 characters")

    # Validate email
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', body.email):
        raise HTTPException(422, "invalid email format")
    if len(body.email) > 120:
        raise HTTPException(422, "email must be <= 120 characters")

    # Validate department
    if not body.department or len(body.department) > 50:
        raise HTTPException(422, "department must be 1-50 characters")

    # Validate shift times format
    time_pattern = r'^([01]\d|2[0-3]):[0-5]\d$'
    if not re.match(time_pattern, body.shift_start):
        raise HTTPException(422, "shift_start must be HH:MM (24-hour)")
    if not re.match(time_pattern, body.shift_end):
        raise HTTPException(422, "shift_end must be HH:MM (24-hour)")

    # Validate shift_start != shift_end
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")

    # Validate joined_on format
    try:
        date_type.fromisoformat(body.joined_on)
    except ValueError:
        raise HTTPException(422, "joined_on must be YYYY-MM-DD")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return _format_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    _validate_page_params(page, page_size)
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        if "created_at" in item and isinstance(item["created_at"], datetime):
            item["created_at"] = _dt_to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ----- Punch-in ----- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Validate status
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(PRESENCE_STATUSES)}")

    # Validate punched_at
    if body.punched_at is not None:
        _validate_epoch_ms(body.punched_at)
        ts = _truncate_to_seconds(_epoch_ms_to_dt(body.punched_at))
    else:
        ts = _truncate_to_seconds(datetime.now(timezone.utc))

    # Check employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Compute attendance date per R1
    d = _compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    # Compute late_minutes
    late = compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return _format_attendance_record(doc)


# ----- Punch-out ----- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # Validate punched_at
    if body.punched_at is not None:
        _validate_epoch_ms(body.punched_at)
        ts = _truncate_to_seconds(_epoch_ms_to_dt(body.punched_at))
    else:
        ts = _truncate_to_seconds(datetime.now(timezone.utc))

    # Check employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Find the most recent record with punch_in <= punched_at and punch_out is null
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no open punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punched_at > punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")

    # Validate: within 24 hours
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    wh = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], emp["shift_start"], record["date"])
    hd = compute_half_day(wh)

    # Atomic update: only update if punch_out is still None (concurrency safe)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "already punched out")

    return _format_attendance_record(result)


# ----- List Attendance ----- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    _validate_page_params(page, page_size)

    # Validate date_from > date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    if status and status not in ALL_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(ALL_STATUSES)}")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [_format_attendance_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ----- Regularize (PATCH) ----- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # Validate reason and regularized_by
    if len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, "reason must be 5-200 characters")
    if len(body.regularized_by) < 1 or len(body.regularized_by) > 50:
        raise HTTPException(422, "regularized_by must be 1-50 characters")

    # Check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Find the attendance record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for this date")

    # Validate status if provided
    if body.status is not None and body.status not in ALL_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(ALL_STATUSES)}")

    # Validate epoch ms values
    if body.punch_in is not None:
        _validate_epoch_ms(body.punch_in, "punch_in")
    if body.punch_out is not None:
        _validate_epoch_ms(body.punch_out, "punch_out")

    new_status = body.status if body.status is not None else record["status"]

    # If changing to ABSENT or LEAVE, punch times must not be supplied
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times with ABSENT/LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status: need a punch_in (supplied or existing)
        if body.punch_in is not None:
            new_punch_in = _truncate_to_seconds(_epoch_ms_to_dt(body.punch_in))
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in")

        if body.punch_out is not None:
            new_punch_out = _truncate_to_seconds(_epoch_ms_to_dt(body.punch_out))
        else:
            new_punch_out = record.get("punch_out")

        # Validate punch_in is on the record's attendance date (R1)
        if new_punch_in is not None:
            pin_tz = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
            computed_date = _compute_attendance_date(pin_tz, emp["shift_start"], emp["shift_end"])
            if computed_date != date:
                raise HTTPException(422, "punch_in must be on the record's attendance date")

        # Validate punch_out > punch_in and within 24 hours
        if new_punch_out is not None and new_punch_in is not None:
            pin_tz = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
            pout_tz = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
            if pout_tz <= pin_tz:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (pout_tz - pin_tz).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Detect "no-op" changes: the request must change at least something
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_status = record.get("status")

    changes_dict = {}

    # Compare status
    if body.status is not None and new_status != old_status:
        changes_dict["status"] = {"from": old_status, "to": new_status}

    # Compare punch_in
    if new_punch_in != old_punch_in:
        # Handle both being None
        if not (new_punch_in is None and old_punch_in is None):
            changes_dict["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

    # Compare punch_out
    if new_punch_out != old_punch_out:
        if not (new_punch_out is None and old_punch_out is None):
            changes_dict["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

    # Compute new derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)
        if new_punch_out is not None:
            new_wh = compute_work_hours(new_punch_in, new_punch_out)
            new_ot = compute_overtime(new_punch_out, emp["shift_end"], emp["shift_start"], date)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Compare derived fields
    old_late = record.get("late_minutes", 0)
    old_wh = record.get("work_hours")
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    if new_late != old_late:
        changes_dict["late_minutes"] = {"from": old_late, "to": new_late}
    if new_wh != old_wh:
        changes_dict["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_ot != old_ot:
        changes_dict["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes_dict["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes_dict:
        raise HTTPException(422, "no changes to apply")

    # Build history entry
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_dict,
    }

    # Build update
    update_set = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    # Atomic update with history length check for concurrency safety
    old_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": old_history_len},
        },
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "concurrent modification detected")

    return _format_attendance_record(result)


# ----- Analytics: Employee Monthly ----- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    year, mon = _validate_month(month)

    # Check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first_day, last_day = _month_date_range(year, mon)

    # Compute working_days per R7: Mon-Fri from max(joined_on, month_start) to month_end
    joined = emp["joined_on"]
    effective_start = max(first_day, joined)

    if effective_start > last_day:
        working_days = 0
    else:
        working_days = _count_working_days(effective_start, last_day)

    # MongoDB aggregation for the attendance metrics
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": first_day, "$lte": last_day},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            # Only Mon-Fri for present_days
                            {"$in": [
                                {"$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d"
                                    }
                                }},
                                [2, 3, 4, 5, 6]  # Mon=2..Fri=6 in $dayOfWeek
                            ]},
                        ]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            },
        }},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        r = result[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    # R8: attendance_pct = present_days / working_days * 100, 2 decimals half-up, null if working_days=0
    if working_days == 0:
        att_pct = None
    else:
        att_pct = _round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


# ----- Analytics: Department Summary ----- #
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    year, mon = _validate_month(month)
    first_day, last_day = _month_date_range(year, mon)

    # Build employee match: joined_on <= last_day (R9)
    emp_match = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        # Lookup attendance logs for this month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$ec"]},
                    "date": {"$gte": first_day, "$lte": last_day},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": [{"$type": "$logs"}, "missing"]},
                            {"$in": [
                                "$logs.status",
                                ["PRESENT", "WFH", "ON_DUTY"]
                            ]},
                            # Only Mon-Fri for present_days
                            {"$in": [
                                {"$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$logs.date",
                                        "format": "%Y-%m-%d"
                                    }
                                }},
                                [2, 3, 4, 5, 6]
                            ]},
                        ]},
                        {"$cond": [
                            {"$ifNull": ["$logs.half_day", False]},
                            0.5,
                            1,
                        ]},
                        0,
                    ]
                }
            },
            "work_hours_values": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$ne": [{"$type": "$logs"}, "missing"]},
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]},
                            {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                        ]},
                        "$logs.work_hours",
                        "$$REMOVE",
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": [{"$type": "$logs"}, "missing"]},
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                        ]},
                        1,
                        0,
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$cond": [
                        {"$ne": [{"$type": "$logs"}, "missing"]},
                        {"$ifNull": ["$logs.late_minutes", 0]},
                        0,
                    ]
                }
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": [{"$type": "$logs"}, "missing"]},
                            {"$eq": ["$logs.status", "LEAVE"]},
                        ]},
                        1,
                        0,
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": [{"$type": "$logs"}, "missing"]},
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                        ]},
                        1,
                        0,
                    ]
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "work_hours_values": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        if r["headcount"] == 0:
            continue
        wh_values = [v for v in r.get("work_hours_values", []) if v is not None and v != "$$REMOVE"]
        if wh_values:
            avg_wh = _round_half_up(sum(wh_values) / len(wh_values), 2)
        else:
            avg_wh = None

        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


# ----- Analytics: Late Leaderboard ----- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = _validate_month(month)
    first_day, last_day = _month_date_range(year, mon)

    # Build match for logs
    log_match = {
        "date": {"$gte": first_day, "$lte": last_day},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": log_match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    # If department filter, apply after lookup
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        # Standard competition ranking using $setWindowFields
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {},
                }
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })

    return {"month": month, "items": items}


def _build_department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    from_dt = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
    to_bound = to_dt + timedelta(days=1)

    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "_id": 0}))
    emp_codes = [e["emp_code"] for e in dept_employees]

    return [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": date_from, "$lte": date_to},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        1,
                        0
                    ]
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date_str": "$_id",
            "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_bound]
            }
        }},
        {"$project": {
            "date": {
                "$ifNull": [
                    "$date_str",
                    {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d", "timezone": "UTC"}}
                ]
            },
            "date_dt": 1,
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {
                "$in": [
                    {"$dayOfWeek": {"date": "$date_dt", "timezone": "UTC"}},
                    [2, 3, 4, 5, 6]
                ]
            },
        }},
        {"$sort": {"date": 1}},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$d"]}
                        ]
                    }
                }},
                {"$count": "total"}
            ],
            "as": "hc_docs"
        }},
        {"$project": {
            "date": 1,
            "date_dt": 1,
            "is_working_day": 1,
            "headcount": {
                "$ifNull": [
                    {"$arrayElemAt": ["$hc_docs.total", 0]},
                    0
                ]
            },
            "present_count": 1,
            "late_count": 1,
        }},
        {"$project": {
            "date": 1,
            "date_dt": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date_dt": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {
                "$cond": [
                    {"$ne": ["$moving_avg_7d", None]},
                    {"$round": ["$moving_avg_7d", 4]},
                    None
                ]
            }
        }}
    ]


# ----- Analytics: Department Trend ----- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    # Validate department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    # Validate dates
    try:
        from_date = date_type.fromisoformat(date_from)
        to_date = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, "invalid date format")

    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (to_date - from_date).days > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    pipeline = _build_department_trend_pipeline(department, date_from, date_to)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# ----- Admin: Explain ----- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    valid_endpoints = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        explain_result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        year, mon = _validate_month(month)
        first_day, last_day = _month_date_range(year, mon)

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }},
            {"$group": {
                "_id": None,
                "count": {"$sum": 1},
            }},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, mon = _validate_month(month)
        first_day, last_day = _month_date_range(year, mon)

        emp_match = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$ec"]},
                        "date": {"$gte": first_day, "$lte": last_day},
                    }},
                ],
                "as": "logs",
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
            }},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        year, mon = _validate_month(month)
        first_day, last_day = _month_date_range(year, mon)

        log_match = {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }

        pipeline = [
            {"$match": log_match},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
        ]
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")

        dept_exists = db.employees.find_one({"department": department})
        if not dept_exists:
            raise HTTPException(404, "department not found")

        try:
            f_d = date_type.fromisoformat(from_date)
            t_d = date_type.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "invalid date format")

        if t_d < f_d or (t_d - f_d).days > 92:
            raise HTTPException(422, "invalid date range")

        pipeline = _build_department_trend_pipeline(department, from_date, to_date)
        explain_result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}
