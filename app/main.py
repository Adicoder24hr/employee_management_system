"""Employee Attendance & Analytics API starter endpoints."""

import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
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


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def check_null_punches(cls, data):
        if isinstance(data, dict):
            for field in ("punch_in", "punch_out"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            if value < 100_000_000_000 or value > MAX_EPOCH_MS:
                raise ValueError("Timestamp is outside the supported epoch-millisecond range")
        return value


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
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="ix_employees_dept_joined")
    db.employees.create_index([("joined_on", 1)], name="ix_employees_joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_emp_code")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="ix_attendance_status_date_emp_code")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="ix_attendance_date_late")


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


def compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str = "18:30") -> str:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
    end_minutes = end_hour * 60 + end_minute
    attendance_day = punch_in_ist.date()
    if overnight and punch_minutes < end_minutes:
        attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()


def to_ist_datetime(epoch_ms: int, field_name: str = "punched_at") -> datetime:
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
        raise HTTPException(422, f"{field_name} must be epoch milliseconds")
    if epoch_ms < 100_000_000_000 or epoch_ms > MAX_EPOCH_MS:
        raise HTTPException(422, f"{field_name} is outside the supported epoch-millisecond range")
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).astimezone(IST).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, f"Invalid {field_name} timestamp")


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


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: str,
    body: RegularizeIn,
):
    try:
        if date.fromisoformat(attendance_date).isoformat() != attendance_date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="attendance_date must be a valid YYYY-MM-DD")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    shift_start = employee.get("shift_start", "09:30")
    shift_end = employee.get("shift_end", "18:30")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot provide punch times when setting status to ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            punch_in_ist = to_ist_datetime(body.punch_in, "punch_in")
            target_punch_in = punch_in_ist.astimezone(UTC).replace(tzinfo=None)
        else:
            target_punch_in = normalize_datetime(record["punch_in"]).replace(tzinfo=None) if record.get("punch_in") else None

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        punch_in_ist = target_punch_in.replace(tzinfo=UTC).astimezone(IST)
        r1_date = compute_attendance_date(punch_in_ist, shift_start, shift_end)
        if r1_date != attendance_date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to date {r1_date}, not {attendance_date} under R1",
            )

        if body.punch_out is not None:
            punch_out_ist = to_ist_datetime(body.punch_out, "punch_out")
            target_punch_out = punch_out_ist.astimezone(UTC).replace(tzinfo=None)
        else:
            target_punch_out = normalize_datetime(record["punch_out"]).replace(tzinfo=None) if record.get("punch_out") else None

        if target_punch_out is not None:
            elapsed = target_punch_out - target_punch_in
            if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_punch_out_ist = target_punch_out.replace(tzinfo=UTC).astimezone(IST)
            new_overtime_minutes = compute_overtime(target_punch_out_ist, shift_end, attendance_date, shift_start)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(target_punch_in, shift_start, shift_end)

    old_status = record.get("status")
    old_punch_in = normalize_datetime(record["punch_in"]).replace(tzinfo=None) if record.get("punch_in") else None
    old_punch_out = normalize_datetime(record["punch_out"]).replace(tzinfo=None) if record.get("punch_out") else None
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if target_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if target_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected")

    now_utc = datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    existing_history = record.get("history")
    update_filter = {"_id": record["_id"], "punch_out": record.get("punch_out")}
    if existing_history is None:
        update_filter["history"] = {"$exists": False}
    else:
        update_filter["history"] = {"$size": len(existing_history)}

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter, update_doc, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification detected")

    return attendance_for_api(updated)


def round_half_up(value: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("1" if decimals == 0 else f"0.{'0' * (decimals - 1)}1")
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$project": {
                "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "half_day": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$project": {
                "present_contrib": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_days": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "late_count": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contrib"},
                "leave_days": {"$sum": "$leave_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    joined_on = employee.get("joined_on", "2000-01-01")
    if joined_on > month_end:
        working_days = 0
    else:
        start_date = max(date(year, m, 1), date.fromisoformat(joined_on))
        end_date = date(year, m, num_days)
        working_days = sum(1 for d in range((end_date - start_date).days + 1) if (start_date + timedelta(days=d)).weekday() < 5)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        agg = results[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

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


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list:
    match_filter = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    return [
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
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_records": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                            ]
                        },
                    }
                },
                "work_hours_records": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_records": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                    }
                },
                "leave_records": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.status", "LEAVE"]},
                    }
                },
                "on_duty_records": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$present_records",
                            "as": "r",
                            "in": {"$cond": [{"$ifNull": ["$$r.half_day", False]}, 0.5, 1.0]},
                        }
                    }
                },
                "avg_work_hours": {"$avg": "$work_hours_records.work_hours"},
                "late_count": {"$size": "$late_records"},
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$late_records",
                            "as": "r",
                            "in": {"$ifNull": ["$$r.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {"$size": "$leave_records"},
                "on_duty_count": {"$size": "$on_duty_records"},
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
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
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    if department is not None:
        pipeline.append({"$match": {"department": department}})
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    days_count: int,
    from_date_obj: date,
) -> list:
    from_dt = datetime(from_date_obj.year, from_date_obj.month, from_date_obj.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$push": "$emp_code"},
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "emp_codes": 1,
                "employees": 1,
                "day_indices": {"$range": [0, days_count]},
            }
        },
        {"$unwind": "$day_indices"},
        {
            "$project": {
                "department": 1,
                "emp_codes": 1,
                "employees": 1,
                "curr_date": {
                    "$dateAdd": {
                        "startDate": from_dt,
                        "unit": "day",
                        "amount": "$day_indices",
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_codes": 1,
                "employees": 1,
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$curr_date"}},
                "day_of_week": {"$dayOfWeek": "$curr_date"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date_str", "codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$codes"]},
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
                "date": "$date_str",
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date_str"]},
                        }
                    }
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$ifNull": ["$$pl.half_day", False]}, 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
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
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_obj = date.fromisoformat(from_date)
        to_obj = date.fromisoformat(to_date)
        if from_obj.isoformat() != from_date or to_obj.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="from and to must be valid YYYY-MM-DD dates")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")

    days_count = (to_obj - from_obj).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, days_count, from_obj)
    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r["attendance_rate"], 4),
            "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4),
        })
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
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
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, f"{month}-01", f"{month}-{num_days:02d}")
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_department_summary_pipeline(f"{month}-01", f"{month}-{num_days:02d}", department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_late_leaderboard_pipeline(f"{month}-01", f"{month}-{num_days:02d}", limit, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        from_obj = date.fromisoformat(from_date)
        to_obj = date.fromisoformat(to_date)
        days_count = (to_obj - from_obj).days + 1
        pipeline = build_department_trend_pipeline(department, days_count, from_obj)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    explain_clean = json.loads(json_util.dumps(explain_res))
    return {"endpoint": endpoint, "collection": collection, "explain": explain_clean}
