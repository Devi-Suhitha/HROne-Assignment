# uvicorn app.main:app --port 8000

import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}
ATTENDANCE_STATUSES = PRESENCE_STATUSES | NON_PRESENT_STATUSES


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="ux_employees_emp_code")
    db.employees.create_index("department", name="ix_employees_department")
    db.employees.create_index("joined_on", name="ix_employees_joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="ix_employees_department_joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="ux_attendance_logs_emp_code_date")
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_logs_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="ix_attendance_logs_status_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)],
        name="ix_attendance_logs_emp_code_date_desc",
    )
    db.attendance_logs.create_index("date", name="ix_attendance_logs_date")
    db.attendance_logs.create_index("status", name="ix_attendance_logs_status")
    db.attendance_logs.create_index("late_minutes", name="ix_attendance_logs_late_minutes")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_out", 1)], name="ix_attendance_logs_emp_code_punch_out")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_logs_emp_code_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def epoch_ms_from_dt(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).replace(microsecond=0).timestamp() * 1000)


def datetime_from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)


def coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, dict) and "$date" in value:
        nested = value["$date"]
        if isinstance(nested, str):
            ts = nested
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
        if isinstance(nested, datetime):
            return nested
        if isinstance(nested, (int, float)):
            return datetime.fromtimestamp(float(nested) / 1000, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key, change in (entry.get("changes") or {}).items():
        changes[key] = {
            "from": serialize_value(change.get("from")),
            "to": serialize_value(change.get("to")),
        }
    return {
        "at": epoch_ms_from_dt(coerce_datetime(entry.get("at"))),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_value(value: Any) -> Any:
    if isinstance(value, dict) and "$date" in value:
        return epoch_ms_from_dt(coerce_datetime(value))
    if isinstance(value, datetime):
        return epoch_ms_from_dt(value)
    if isinstance(value, list):
        return [serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize_value(v) for k, v in value.items()}
    return value


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = epoch_ms_from_dt(coerce_datetime(out["created_at"]))
    return out


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        if field in out:
            out[field] = epoch_ms_from_dt(coerce_datetime(out[field]))
    if "history" in out:
        out["history"] = [serialize_history_entry(e) for e in out.get("history", [])]
    return out


def _validate_epoch(value: Optional[int], *, allow_none: bool = True) -> Optional[int]:
    if value is None:
        if allow_none:
            return None
        raise ValueError("value is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch milliseconds value")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("must be a valid epoch milliseconds timestamp")
    return value


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - used in validation paths
        raise ValueError("must be a valid YYYY-MM-DD date") from exc


def parse_hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - used in validation paths
        raise ValueError("must be a valid HH:MM time") from exc


def round_half_up(value: Decimal | float | int, digits: int = 2) -> float:
    q = Decimal("1").scaleb(-digits)
    if isinstance(value, Decimal):
        dec = value
    else:
        dec = Decimal(str(value))
    return float(dec.quantize(q, rounding=ROUND_HALF_UP))


def to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)


def employee_shift_start(emp: dict[str, Any], record_date: date) -> datetime:
    return datetime.combine(record_date, parse_hhmm(emp["shift_start"]), tzinfo=IST)


def employee_shift_end(emp: dict[str, Any], record_date: date) -> datetime:
    end_dt = datetime.combine(record_date, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end_dt += timedelta(days=1)
    return end_dt


def attendance_date_for_punch(emp: dict[str, Any], punch_in: datetime) -> str:
    punch_in_ist = to_ist(punch_in)
    if emp.get("shift_end", "18:30") <= emp.get("shift_start", "09:30"):
        end_time = parse_hhmm(emp["shift_end"])
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: Optional[date] = None) -> int:
    punch_in_ist = to_ist(punch_in)
    if record_date is None:
        record_date = punch_in_ist.date()
    shift_dt = datetime.combine(record_date, parse_hhmm(shift_start), tzinfo=IST)
    delta_seconds = (punch_in_ist - shift_dt).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(delta_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (to_ist(punch_out) - to_ist(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime_minutes(emp: dict[str, Any], punch_out: datetime, record_date: date) -> int:
    end_dt = employee_shift_end(emp, record_date)
    delta_seconds = (to_ist(punch_out) - end_dt).total_seconds()
    if delta_seconds <= 0:
        return 0
    minutes = int(delta_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_derived_fields(emp: dict[str, Any], status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], record_date: Optional[str] = None) -> tuple[int, Optional[float], int, bool]:
    status = status or "PRESENT"
    if status in NON_PRESENT_STATUSES or punch_in is None:
        return 0, None, 0, False
    record_day = parse_iso_date(record_date or attendance_date_for_punch(emp, punch_in))
    late = compute_late_minutes(punch_in, emp["shift_start"], record_day)
    if punch_out is None:
        return late, None, 0, False
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime_minutes(emp, punch_out, record_day)
    half_day = hours < 4.5
    return late, hours, overtime, half_day


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_shift_and_joined_on(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        parse_iso_date(self.joined_on)
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = Field(default="PRESENT")

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return _validate_epoch(value, allow_none=True)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return _validate_epoch(value, allow_none=True)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise ValueError("status is invalid")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        return _validate_epoch(value, allow_none=True)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover - requires Mongo
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        created = body.model_dump()
        created["created_at"] = datetime.now(timezone.utc)
        db.employees.insert_one(created)
        return serialize_employee(created)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    projection = {"_id": 0, "emp_code": 1, "name": 1, "email": 1, "department": 1, "shift_start": 1, "shift_end": 1, "joined_on": 1, "created_at": 1}
    items = list(db.employees.find(q, projection).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    attendance_date = attendance_date_for_punch(emp, punched_at)
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}):
        raise HTTPException(status_code=409, detail="already punched in for this date")

    late_minutes, work_hours, overtime_minutes, half_day = compute_derived_fields(emp, body.status, punched_at, None, attendance_date)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    eligible_records = db.attendance_logs.find({
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punched_at},
        "status": {"$in": list(PRESENCE_STATUSES)},
    }).sort("punch_in", -1).limit(1)
    record = next(iter(eligible_records), None)
    if record is None:
        later_record = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE_STATUSES)},
            "punch_in": {"$gt": punched_at},
        })
        if later_record is not None:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        raise HTTPException(status_code=404, detail="open attendance record not found")

    if record.get("punch_in") is None:
        raise HTTPException(status_code=409, detail="attendance record is not punch-in complete")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")
    if punched_at <= record["punch_in"]:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (punch_out_dt := to_ist(punched_at)) is not None and (punch_in_dt := to_ist(record["punch_in"])) is not None:
        if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    final_status = record["status"]
    final_record_date = record["date"]
    late_minutes, work_hours, overtime_minutes, half_day = compute_derived_fields(emp, final_status, record["punch_in"], punched_at, final_record_date)
    update = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q, ATTENDANCE_PROJECTION)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    original = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    updates: dict[str, Any] = {}
    if body.status is not None:
        updates["status"] = body.status
    if body.punch_in is not None:
        updates["punch_in"] = datetime_from_epoch_ms(body.punch_in)
    if body.punch_out is not None:
        updates["punch_out"] = datetime_from_epoch_ms(body.punch_out)

    final_status = updates.get("status", record.get("status"))
    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))

    if final_status in NON_PRESENT_STATUSES:
        if "punch_in" in updates and updates["punch_in"] is not None:
            raise HTTPException(status_code=422, detail="punch_in cannot be supplied for ABSENT or LEAVE")
        if "punch_out" in updates and updates["punch_out"] is not None:
            raise HTTPException(status_code=422, detail="punch_out cannot be supplied for ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    elif final_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")
    if final_punch_in is not None:
        punch_in_dt = to_ist(final_punch_in)
        if attendance_date_for_punch(employee, punch_in_dt) != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
    if final_punch_in is not None and final_punch_out is not None:
        if final_punch_out <= final_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (to_ist(final_punch_out) - to_ist(final_punch_in)).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    if final_status in NON_PRESENT_STATUSES:
        final_punch_in = None
        final_punch_out = None

    if record.get("status") == final_status and record.get("punch_in") == final_punch_in and record.get("punch_out") == final_punch_out:
        raise HTTPException(status_code=422, detail="request changes nothing")

    late_minutes, work_hours, overtime_minutes, half_day = compute_derived_fields(employee, final_status, final_punch_in, final_punch_out, date)
    new_status = final_status
    new_doc = {
        "status": new_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes: dict[str, Any] = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old_value = original[key]
        new_value = new_doc.get(key)
        if old_value != new_value:
            changes[key] = {"from": serialize_value(old_value), "to": serialize_value(new_value)}
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {"$set": {**new_doc, "status": final_status}, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #


def month_bounds(month: str) -> tuple[date, date]:
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    end = next_month - timedelta(days=1)
    return start, end


def working_days_in_month(emp: dict[str, Any], month: str) -> int:
    start, end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    count = 0
    cur = max(start, joined)
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def is_working_day(value: date) -> bool:
    return value.weekday() < 5


def rounded_pct(numerator: float, denominator: float) -> Optional[float]:
    if denominator == 0:
        return None
    return round_half_up((numerator / denominator) * 100, 2)


def mongo_weekday(date_expression: str) -> dict[str, Any]:
    return {
        "$dayOfWeek": {
            "$dateFromString": {
                "dateString": date_expression,
                "format": "%Y-%m-%d",
            }
        }
    }


def mongo_half_up(expression: Any, scale: int) -> dict[str, Any]:
    factor = 10 ** scale
    return {
        "$divide": [
            {"$trunc": {"$add": [{"$multiply": [expression, factor]}, 0.5]}},
            factor,
        ]
    }


ATTENDANCE_PROJECTION = {
    "_id": 0,
    "emp_code": 1,
    "date": 1,
    "status": 1,
    "punch_in": 1,
    "punch_out": 1,
    "work_hours": 1,
    "late_minutes": 1,
    "overtime_minutes": 1,
    "half_day": 1,
    "history": 1,
}
ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    parsed_date_from = parse_query_date(date_from, "date_from") if date_from is not None else None
    parsed_date_to = parse_query_date(date_to, "date_to") if date_to is not None else None
    if parsed_date_from is not None and parsed_date_to is not None and parsed_date_from > parsed_date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    if status is not None and status not in ATTENDANCE_STATUSES:
        raise HTTPException(status_code=422, detail="status is invalid")

    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def parse_query_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date")
    return parsed


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    weekday = mongo_weekday("$date")
    present_weekday = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$in": [weekday, [2, 3, 4, 5, 6]]},
        ]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                present_weekday,
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_filter["department"] = department
    weekday = mongo_weekday("$logs.date")
    present_weekday = {
        "$and": [
            {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
            {"$in": [weekday, [2, 3, 4, 5, 6]]},
        ]
    }
    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start.isoformat()]},
                    {"$lte": ["$date", end.isoformat()]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                present_weekday,
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "avg_work_hours_total": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": ["$logs.work_hours", None]},
                ]},
                "$logs.work_hours",
                0,
            ]}},
            "avg_work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": ["$logs.work_hours", None]},
                ]},
                1,
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "avg_work_hours_total": {"$sum": "$avg_work_hours_total"},
            "avg_work_hours_count": {"$sum": "$avg_work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_: str, to: str) -> list[dict[str, Any]]:
    start = parse_query_date(from_, "from")
    end = parse_query_date(to, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    day_count = (end - start).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="trend range cannot exceed 92 days")
    return [
        {"$match": {"department": department}},
        {"$set": {"_trend_offsets": {"$range": [0, day_count]}}},
        {"$unwind": "$_trend_offsets"},
        {"$set": {
            "_trend_date": {"$dateAdd": {
                "startDate": {"$dateFromString": {"dateString": from_, "format": "%Y-%m-%d"}},
                "unit": "day",
                "amount": "$_trend_offsets",
            }},
            "_trend_date_string": {"$dateToString": {
                "format": "%Y-%m-%d",
                "date": {"$dateAdd": {
                    "startDate": {"$dateFromString": {"dateString": from_, "format": "%Y-%m-%d"}},
                    "unit": "day",
                    "amount": "$_trend_offsets",
                }},
            }},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code", "trend_date": "$_trend_date_string"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$employee_code"]},
                {"$eq": ["$date", "$$trend_date"]},
            ]}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_trend_date_string",
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_trend_date_string"]}, 1, 0]}},
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$set": {
            "is_working_day": {"$in": [mongo_weekday("$_id"), [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, "current"]},
                },
            },
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": mongo_half_up("$present_count", 4),
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$ne": ["$attendance_rate", None]},
                mongo_half_up("$attendance_rate", 4),
                None,
            ]},
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$moving_avg_7d_raw", None]},
                mongo_half_up("$moving_avg_7d_raw", 4),
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    totals = result[0] if result else {}
    working_days = working_days_in_month(emp, month)
    present_days = float(totals.get("present_days") or 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(totals.get("leave_days") or 0),
        "late_count": int(totals.get("late_count") or 0),
        "total_late_minutes": int(totals.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes") or 0),
        "attendance_pct": rounded_pct(present_days, working_days),
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    results = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in results:
        hours_count = int(row.get("avg_work_hours_count") or 0)
        avg_work_hours = None
        if hours_count:
            avg_work_hours = round_half_up(
                float(Decimal(str(row["avg_work_hours_total"])) / hours_count),
                2,
            )
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg_work_hours,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    pipeline = department_trend_pipeline(department, from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
):
    supported = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in supported:
        raise HTTPException(status_code=422, detail="unsupported endpoint")

    if endpoint == "attendance_list":
        find_command = {
            "find": "attendance_logs",
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "projection": ATTENDANCE_PROJECTION,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain_doc = db.command({
            "explain": find_command,
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        explain_doc = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": employee_monthly_pipeline(emp_code, month),
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        explain_doc = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": department_summary_pipeline(month, department),
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        explain_doc = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": late_leaderboard_pipeline(month, limit, department),
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        explain_doc = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": department_trend_pipeline(department, from_, to),
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    raise HTTPException(status_code=422, detail="unsupported endpoint")
