"""Employee Attendance & Analytics API starter endpoints."""

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    """Return whole minutes late, applying the 10-minute grace period."""
    punch_local = normalize_datetime(punch_in).astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    shift_start_local = punch_local.replace(
        hour=start_hour, minute=start_minute, second=0, microsecond=0
    )
    shift_end_minutes = end_hour * 60 + end_minute
    shift_start_minutes = start_hour * 60 + start_minute
    if shift_end_minutes <= shift_start_minutes and (
        punch_local.hour * 60 + punch_local.minute < shift_end_minutes
    ):
        shift_start_local -= timedelta(days=1)

    elapsed_seconds = int((punch_local - shift_start_local).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def normalize_datetime(value: datetime) -> datetime:
    """Treat naive MongoDB datetimes as UTC and truncate to whole seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.replace(microsecond=0)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed = normalize_datetime(punch_out) - normalize_datetime(punch_in)
    elapsed_seconds = elapsed.days * 86_400 + elapsed.seconds
    hours = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_date: str,
    shift_start: str,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    attendance_day = date.fromisoformat(attendance_date)

    shift_start_local = datetime(
        attendance_day.year, attendance_day.month, attendance_day.day,
        start_hour, start_minute, tzinfo=IST,
    )
    shift_end_local = datetime(
        attendance_day.year, attendance_day.month, attendance_day.day,
        end_hour, end_minute, tzinfo=IST,
    )
    if shift_end_local <= shift_start_local:
        shift_end_local += timedelta(days=1)

    punch_out_local = normalize_datetime(punch_out).astimezone(IST)
    elapsed_minutes = int((punch_out_local - shift_end_local).total_seconds() // 60)
    return elapsed_minutes if elapsed_minutes >= 30 else 0


def epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    normalized = normalize_datetime(value).astimezone(UTC)
    return int(normalized.timestamp() * 1000)


def attendance_for_api(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["punch_in"] = epoch_millis(result.get("punch_in"))
    result["punch_out"] = epoch_millis(result.get("punch_out"))
    result["half_day"] = result.get("half_day", False)
    result["history"] = result.get("history", [])
    for entry in result["history"]:
        entry["at"] = epoch_millis(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_millis(change[side])
    return result


def employee_for_api(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["created_at"] = epoch_millis(result["created_at"])
    return result


class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database connection unavailable")


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employees_department_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_emp_code")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="ix_attendance_status_date_emp_code")


@app.on_event("startup")
def startup():
    create_indexes()


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_for_api(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query).sort("emp_code", 1)
        .skip((page - 1) * page_size).limit(page_size)
    )
    return {"items": [employee_for_api(item) for item in items], "total": total,
            "page": page, "page_size": page_size}


def to_ist_datetime(epoch_ms: int) -> datetime:
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
        raise HTTPException(422, "punched_at must be epoch milliseconds")
    if epoch_ms < 100_000_000_000 or epoch_ms > MAX_EPOCH_MS:
        raise HTTPException(422, "punched_at is outside the supported epoch-millisecond range")
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).astimezone(IST).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "Invalid punched_at timestamp")


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    timestamp = datetime.now(IST).replace(microsecond=0) if body.punched_at is None else to_ist_datetime(body.punched_at)
    shift_start = employee.get("shift_start", "09:30")
    shift_end = employee.get("shift_end", "18:30")
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    punch_minutes = timestamp.hour * 60 + timestamp.minute
    end_minutes = end_hour * 60 + end_minute
    attendance_day = timestamp.date()
    if overnight and punch_minutes < end_minutes:
        attendance_day -= timedelta(days=1)
    attendance_date = attendance_day.isoformat()
    utc_timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": utc_timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(timestamp, shift_start, shift_end),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance already exists for this employee and date")
    return attendance_for_api(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    timestamp = datetime.now(IST).replace(microsecond=0) if body.punched_at is None else to_ist_datetime(body.punched_at)
    punch_out_utc = timestamp.astimezone(UTC).replace(tzinfo=None)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        if db.attendance_logs.count_documents({"emp_code": body.emp_code}) > 0:
            raise HTTPException(status_code=422, detail="No punch-in exists at or before punched_at")
        raise HTTPException(status_code=404, detail="No attendance record found")
    if record.get("status") not in PRESENCE_STATUSES:
        raise HTTPException(status_code=404, detail="No punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_utc = normalize_datetime(record["punch_in"]).replace(tzinfo=None)
    elapsed = punch_out_utc - punch_in_utc
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in and within 24 hours")

    shift_start = employee.get("shift_start", "09:30")
    shift_end = employee.get("shift_end", "18:30")
    hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime = compute_overtime(timestamp, shift_end, record["date"], shift_start)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_out_utc, "work_hours": hours,
                  "overtime_minutes": overtime, "half_day": hours < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")
    return attendance_for_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail="Dates must be valid YYYY-MM-DD values")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    documents = list(
        db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size).limit(page_size)
    )
    return {"items": [attendance_for_api(item) for item in documents], "total": total,
            "page": page, "page_size": page_size}
