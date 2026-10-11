"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml.  Stored data: DATA_MODEL.md.  Business rules R1-R10 are referenced inline.
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority

log = logging.getLogger("attendance")

# tz_aware=True: every datetime read back from MongoDB is an aware UTC datetime, never a naive one.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

GRACE_SECONDS = 10 * 60          # R2
OVERTIME_MIN_MINUTES = 30        # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE = ["ABSENT", "LEAVE"]

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

D_HALF = Decimal128("0.5")
D_ROUND4 = Decimal128("10000")


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    emp, logs = db.employees, db.attendance_logs
    # Uniqueness of the business key; also every lookup by emp_code and list sorted by emp_code.
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # GET /employees?department=  (filter + sort), department trend / 404 check.
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    # Department summary: headcount = joined_on <= end of month (R9).
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")

    # One record per (emp_code, date): makes punch-in race-safe; serves PATCH, monthly, $lookup, trend.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_code_date")
    # GET /attendance default sort (date desc, emp_code asc) and date-range analytics (leaderboard).
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # GET /attendance?status=  with the same sort.
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code")
    # Punch-out: latest record whose punch_in <= punched_at.
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # do not crash the app; /health reports MongoDB state
        log.error("index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def validation_error(field: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's default body shape ({"detail": [{loc, msg, type}]})."""
    return RequestValidationError([{"loc": field, "msg": msg, "type": "value_error"}])


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_date(value: str, field: tuple) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise validation_error(field, "must be a date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise validation_error(field, "must be a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def round_half_up(value, places: int) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri count in [start, end] (R7)."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Helpers: business rules R1-R5
# --------------------------------------------------------------------------- #
def _hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return _hhmm(emp["shift_end"]) <= _hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of punch_in; an overnight-shift punch-in before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < _hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants for the attendance day; an overnight shift ends on the next day."""
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, _hhmm(emp["shift_start"]), IST)
    end = datetime.combine(d, _hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (exact Decimal arithmetic, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derived_fields(status: str, punch_in, punch_out, date_str: str, emp: dict) -> dict:
    """All four derived fields for a record (DATA_MODEL.md section 3 table)."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(date_str, emp)
    out = {"work_hours": None, "late_minutes": compute_late_minutes(punch_in, start),
           "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=wh, overtime_minutes=compute_overtime(punch_out, end),
                   half_day=Decimal(str(wh)) < HALF_DAY_HOURS)  # R5 uses the rounded value
    return out


# --------------------------------------------------------------------------- #
# Serialization (MongoDB -> API)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def record_out(doc: dict) -> dict:
    """Applies the documented defaults for legacy records (missing history / half_day / minutes)."""
    history = [
        {
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                        for k, c in (h.get("changes") or {}).items()},
        }
        for h in (doc.get("history") or [])
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be a date YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """Derived fields sent by a client are ignored (extra keys are dropped)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} may be omitted but not null")
        return self


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=50)]


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
_weekday = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
_is_presence = {"$in": ["$status", PRESENCE]}
_is_half = {"$eq": ["$half_day", True]}
_present_weight = {"$cond": [_is_presence, {"$cond": [_is_half, 0.5, 1]}, 0]}
_is_late = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def _count_if(cond) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def _round4(expr) -> dict:
    """Half-up to 4 decimals in Decimal128 (MongoDB's $round is half-to-even)."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, D_ROUND4]}, D_HALF]}}, D_ROUND4]}


def build_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        df = parse_date(date_from, ("query", "date_from")) if date_from else None
        dt = parse_date(date_to, ("query", "date_to")) if date_to else None
        if df and dt and df > dt:
            raise validation_error(("query", "date_from"), "date_from must be <= date_to")
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status.value
    return {"filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}


def build_employee_monthly(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            # R5-R7: present days count only Mon-Fri records, half day = 0.5
            "present_days": {"$sum": {"$cond": [{"$lte": [_weekday, 5]}, _present_weight, 0]}},
            "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": _count_if(_is_late),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end.isoformat()}}  # R9
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [_is_presence, {"$isNumber": "$work_hours"}]}
    return [
        # Start from employees so that people with zero logs are still counted.
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [{"$lte": [_weekday, 5]}, _present_weight, 0]}},
                    "work_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "work_n": _count_if(has_hours),
                    "late_count": _count_if(_is_late),
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "work_sum": {"$sum": "$s.work_sum"},
            "work_n": {"$sum": "$s.work_n"},  # average over records, not of per-employee averages
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Inner join: logs whose employee does not exist are dropped.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "e"}},
        {"$unwind": "$e"},
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4); limit applies to the rank, not to the row count.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def parse_trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    f = parse_date(date_from, ("query", "from"))
    t = parse_date(date_to, ("query", "to"))
    if t < f:
        raise validation_error(("query", "to"), "to must be >= from")
    if (t - f).days + 1 > 92:
        raise validation_error(("query", "to"), "range must not exceed 92 days")
    return f, t


def build_department_trend(employees: list, f: date, t: date) -> list:
    codes = [e["emp_code"] for e in employees]
    joined = [e["joined_on"] for e in employees]
    f_dt = datetime.combine(f, time(), timezone.utc)
    t_next = datetime.combine(t + timedelta(days=1), time(), timezone.utc)
    working = {"$lte": ["$dow", 5]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": f.isoformat(), "$lte": t.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _present_weight}, "late_count": _count_if(_is_late)}},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Anchor row so the range is filled even when there are no records at all.
        {"$unionWith": {"pipeline": [{"$documents": [{"day": f_dt, "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [f_dt, t_next]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
            "dow": {"$isoDayOfWeek": "$_id"},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"headcount": {"$size": {"$filter": {"input": joined, "cond": {"$lte": ["$$this", "$date"]}}}}}},  # R9
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": [working, {"$gt": ["$headcount", 0]}]},
            _round4({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}),
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": working,
            "headcount": 1,
            "present_count": {"$toDouble": "$present_count"},
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": _round4("$ma")},
        }},
    ]


def department_employees(department: str) -> list:
    return list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))


# --------------------------------------------------------------------------- #
# Endpoints: system & employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           **derived_fields(body.status, ts, None, d, emp), "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                      sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > MAX_SHIFT:
        raise validation_error(("body", "punched_at"), "must be after punch_in and within 24 hours of it")
    fields = derived_fields(rec["status"], rec["punch_in"], ts, rec["date"], emp)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # conditional update: only one concurrent punch-out matches
        {"$set": {"punch_out": ts, **fields}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    cursor = (db.attendance_logs.find(spec["filter"], {"_id": 0}).sort(list(spec["sort"].items()))
              .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
TRACKED_DEFAULTS = {"status": None, "punch_in": None, "punch_out": None, "work_hours": None,
                    "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date_str: str = Path(alias="date")):
    parse_date(date_str, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status.value if body.status is not None else rec["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if status in NON_PRESENCE:
        if new_in is not None or new_out is not None:
            raise validation_error(("body",), f"{status} clears punch times; do not send punch_in/punch_out")
    else:
        if new_in is not None and attendance_date(new_in, emp) != date_str:
            raise validation_error(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        new_in = new_in or rec.get("punch_in")
        new_out = new_out or rec.get("punch_out")
        if new_in is None:
            raise validation_error(("body", "punch_in"), "a presence status requires punch_in")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT):
            raise validation_error(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(status, new_in, new_out, date_str, emp)}
    old = {k: TRACKED_DEFAULTS[k] if rec.get(k) is None else rec[k] for k in TRACKED}
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED if old[k] != new[k]}
    if not changes:
        raise validation_error(("body",), "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic concurrency: the update only applies if nobody changed the record since we read it.
    guard = {"emp_code": emp_code, "date": date_str, f"history.{n}": {"$exists": False},
             "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    if n:
        guard[f"history.{n - 1}"] = {"$exists": True}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}},
        projection={"_id": 0}, return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Endpoints: analytics (MongoDB aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(start, joined), end)  # R7
    rows = list(db.attendance_logs.aggregate(build_employee_monthly(emp_code, month)))
    s = rows[0] if rows else {}
    present = s.get("present_days", 0)
    pct = None
    if working_days:
        pct = round_half_up(Decimal(str(present)) * 100 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(s.get("leave_days", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    rows = db.employees.aggregate(build_department_summary(month, department))
    items = []
    for r in rows:
        work_sum = r["work_sum"].to_decimal() if isinstance(r["work_sum"], Decimal128) else Decimal(str(r["work_sum"]))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(work_sum / r["work_n"], 2) if r["work_n"] else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(build_late_leaderboard(month, limit, department))
    items = [{**r, "total_late_minutes": int(r["total_late_minutes"])} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    f, t = parse_trend_range(date_from, date_to)
    employees = department_employees(department)
    if not employees:
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(build_department_trend(employees, f, t)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
class Explainable(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _require(value, name: str):
    if value is None:
        raise validation_error(("query", name), "field required")
    return value


def _check_month(month: Optional[str]) -> str:
    _require(month, "month")
    if not re.match(MONTH_PATTERN, month):
        raise validation_error(("query", "month"), "must be YYYY-MM")
    return month


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Explainable,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint is Explainable.attendance_list:
        coll = "attendance_logs"
        spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        cmd = {"find": coll, "filter": spec["filter"], "sort": spec["sort"], "projection": {"_id": 0},
               "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint is Explainable.employee_monthly:
            coll, pipeline = "attendance_logs", build_employee_monthly(_require(emp_code, "emp_code"), _check_month(month))
        elif endpoint is Explainable.department_summary:
            coll, pipeline = "employees", build_department_summary(_check_month(month), department)
        elif endpoint is Explainable.late_leaderboard:
            coll, pipeline = "attendance_logs", build_late_leaderboard(_check_month(month), limit, department)
        else:
            f, t = parse_trend_range(_require(from_, "from"), _require(to, "to"))
            coll = "attendance_logs"
            pipeline = build_department_trend(department_employees(_require(department, "department")), f, t)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint.value, "collection": coll, "explain": _jsonable(result)}


def _jsonable(v: Any) -> Any:
    """Explain output contains BSON types (Timestamp, ObjectId, Decimal128...) that JSON cannot encode."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return dt_to_ms(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)
