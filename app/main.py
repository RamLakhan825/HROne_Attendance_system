"""Employee Attendance & Analytics API."""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# load_dotenv() never overwrites variables that already exist in the environment,
# so real environment variables win over .env (as the contract requires).
load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                    # datetimes come back timezone-aware (UTC)
    serverSelectionTimeoutMS=5000,    # fail fast if MongoDB is unreachable
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60        # R2: grace period
MIN_OVERTIME_MINUTES = 30      # R3: overtime only counts from 30 minutes
HALF_DAY_HOURS = 4.5           # R5
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / regularize: at most 24h after punch-in
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]   # R6


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds. A naive datetime is assumed to be UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current time, UTC, truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def hhmm_to_minutes(hhmm: str) -> int:
    """'09:30' -> 570 (minutes since midnight)."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends the next day."""
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def at_ist(day: date, hhmm: str) -> datetime:
    """The IST datetime for a calendar day plus an 'HH:MM' time."""
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance day (IST) for a punch-in, as 'YYYY-MM-DD'."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        minutes_into_day = local.hour * 60 + local.minute
        if minutes_into_day < hhmm_to_minutes(shift_end):
            day -= timedelta(days=1)   # belongs to the shift that started yesterday
    return day.isoformat()


def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    return at_ist(date.fromisoformat(att_date), shift_start)


def shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(att_date)
    if is_overnight(shift_start, shift_end):
        day += timedelta(days=1)
    return at_ist(day, shift_end)


# --------------------------------------------------------------------------- #
# Business rules R2 - R5, R8
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, att_date: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start."""
    seconds_after = int((punch_in - shift_start_dt(att_date, shift_start)).total_seconds())
    if seconds_after > GRACE_SECONDS:
        return seconds_after // 60     # floor, counted from shift start
    return 0


def compute_overtime(punch_out: datetime, att_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if at least 30."""
    seconds_after = int((punch_out - shift_end_dt(att_date, shift_start, shift_end)).total_seconds())
    minutes = seconds_after // 60      # floor (negative stays negative)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours rounded to 2 decimals, half-up, using exact Decimal arithmetic."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: based on the already-rounded work_hours."""
    return work_hours < HALF_DAY_HOURS


def round_half_up(value: float, places: int) -> float:
    """R8: half-up rounding for reported numbers."""
    q = Decimal(1).scaleb(-places)     # places=2 -> Decimal('0.01')
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def div_round(num: float, den: int, places: int, mult: int = 1) -> float:
    """num * mult / den, half-up to `places`. num is cleaned of float noise first."""
    q = Decimal(str(round(num, 6))) * mult / Decimal(den)
    return float(q.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Calendar helpers (months, days, working days)
# --------------------------------------------------------------------------- #
def check_day(value: str, name: str = "date") -> str:
    """Validate 'YYYY-MM-DD' that is a real date; 422 otherwise."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise HTTPException(status_code=422, detail=f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} is not a real date")
    return value


def parse_month(month: str | None) -> tuple[date, date]:
    """'YYYY-MM' -> (first day, last day). 422 if missing or malformed."""
    if month is None or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    if y < 1:
        raise HTTPException(status_code=422, detail="month out of range")
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def working_days(joined_on: str, first: date, last: date) -> int:
    """R7: Mon-Fri days in [max(first, joined_on), last]."""
    start = max(first, date.fromisoformat(joined_on))
    count, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Models (request validation)
# --------------------------------------------------------------------------- #
# Epoch milliseconds: strict=True rejects floats, strings and bools; the bounds
# reject "seconds-looking" values (< 100000000000) and absurdly large ones.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def _check_real_date(v: str) -> str:
    date.fromisoformat(v)              # ValueError for e.g. 2026-02-30 -> 422
    return v


# 'YYYY-MM-DD' that is also a real calendar date. Used (inside a union with None) for query params.
DateStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_real_date)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_real_date(cls, v: str) -> str:
        date.fromisoformat(v)          # raises ValueError for e.g. 2026-02-30 -> 422
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs | None = None
    status: PresenceStatus = "PRESENT"   # ABSENT / LEAVE are rejected with 422


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMs | None = None


class RegularizeRequest(BaseModel):
    # Derived fields sent by a client are simply ignored (pydantic drops unknown keys).
    status: Status | None = None
    punch_in: EpochMs | None = None
    punch_out: EpochMs | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Response serializers (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    """Pick the public fields only (never _id) and convert created_at to epoch ms."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def opt_ms(dt: datetime | None) -> int | None:
    return None if dt is None else dt_to_ms(dt)


def history_out(entries: list | None) -> list:
    """Convert stored history to API form. Dates INSIDE 'changes' are converted too."""
    out = []
    for e in entries or []:                       # legacy records may have no history
        changes = {}
        for field, ch in e.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": opt_ms(ch.get("from")), "to": opt_ms(ch.get("to"))}
            else:
                changes[field] = ch
        out.append({"at": dt_to_ms(e["at"]), "by": e["by"], "reason": e["reason"], "changes": changes})
    return out


def attendance_out(doc: dict) -> dict:
    """Public shape of an attendance record. Missing legacy fields get their defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": opt_ms(doc.get("punch_in")),
        "punch_out": opt_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# Indexes (created at startup; create_index is idempotent)
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    # Guarantees emp_code is unique, and serves "sort by emp_code" + the leaderboard's $lookup.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # Serves "filter by department, sorted by emp_code" and the trend's department lookup.
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # Serves the department summary's headcount match (joined_on <= month end), with / without department.
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    # At most one record per (employee, day): makes punch-in race-safe. Also serves
    # "one employee's records", the monthly summary, the trend ($in on emp_code + date range)
    # and the $lookup of the department summary.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    # Serves GET /attendance without emp_code (sort date desc, emp_code asc) and the
    # month range scans of the leaderboard.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )
    # Same, with a status filter in front (equality first, then the sort fields).
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    # Punch-out: "latest record of this employee with punch_in <= t".
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()                   # runs once when the server starts
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints: system and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)   # the unique index decides the race, not us
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)          # counts the FILTERED set
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)                    # page 1 skips nothing
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    # R1: truncate to whole seconds, then work out the IST attendance date.
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)   # the unique index decides the race
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # The most recent record whose punch_in is at or before the punch-out instant.
    # (Records with a null punch_in - ABSENT/LEAVE - never match a $lte on a date.)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    p_in = rec["punch_in"]
    elapsed = (ts - p_in).total_seconds()
    if elapsed <= 0 or elapsed > MAX_SHIFT_SECONDS:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(p_in, ts)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(work_hours),
    }
    # Only closes the record if it is still open: of two simultaneous punch-outs, one wins.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return attendance_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain, so both build it the same way."""
    if date_from and date_to and date_from > date_to:    # ISO strings compare correctly
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from            # inclusive
        if date_to:
            query["date"]["$lte"] = date_to              # inclusive
    if status:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateStr | None = None,
    date_to: DateStr | None = None,
    status: Status | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)    # reflects the filters (R10)
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    rec_date = check_day(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": rec_date})
    if rec is None:
        raise HTTPException(status_code=404, detail="no record for that date")

    ss, se = emp["shift_start"], emp["shift_end"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]

    # ---- final state of status / punch times ---------------------------------
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(status_code=422, detail="a presence status requires punch_in")
        if attendance_date(new_in, ss, se) != rec_date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")
        if new_out is not None:
            gap = (new_out - new_in).total_seconds()
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # ---- derived fields recomputed from the final state (R2-R5) --------------
    if new_status in PRESENCE:
        late = compute_late_minutes(new_in, rec_date, ss)
        if new_out is not None:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, rec_date, ss, se)
            half_day = compute_half_day(work_hours)
        else:
            work_hours, overtime, half_day = None, 0, False
    else:
        late, work_hours, overtime, half_day = 0, None, 0, False

    new_vals = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    old_vals = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(status_code=422, detail="request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record is exactly as we read it
    # (same status/punches and same number of history entries); otherwise 409.
    flt = {
        "_id": rec["_id"],
        "status": old_vals["status"],
        "punch_in": old_in,       # None matches both null and missing
        "punch_out": old_out,
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        flt,
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently, retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared pipeline fragments
# --------------------------------------------------------------------------- #
def _is_present():
    return {"$in": ["$status", PRESENCE]}


def _present_weight():
    """1 for a present record, 0.5 for a present half day (R5), else 0."""
    return {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}


def _is_weekday():
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}


def _late():
    return {"$ifNull": ["$late_minutes", 0]}


def half_up_expr(expr, places: int = 4):
    """Half-up rounding inside a pipeline ($round would round half to even)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.500000001]}}, scale]}


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = parse_month(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_is_present(), _is_weekday()]}, _present_weight(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _late()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    wd = working_days(emp["joined_on"], first, last)
    present = r.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": div_round(present, wd, 2, 100) if wd > 0 else None,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: str | None) -> list:
    first, last = parse_month(month)
    start, end = first.isoformat(), last.isoformat()
    emp_match: dict = {"joined_on": {"$lte": end}}          # R9: joined on/before month end
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    return [
        # Start from EMPLOYEES so people with zero logs still count in the headcount.
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {
                    "_id": 0,
                    "pd": {"$cond": [{"$and": [_is_present(), _is_weekday()]}, _present_weight(), 0]},
                    "wh": {"$cond": [has_hours, "$work_hours", None]},
                    "whc": {"$cond": [has_hours, 1, 0]},
                    "late": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]},
                    "lm": _late(),
                    "lv": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "od": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$sum": "$logs.pd"}},
            "wh_sum": {"$sum": {"$sum": "$logs.wh"}},
            "wh_cnt": {"$sum": {"$sum": "$logs.whc"}},
            "late_count": {"$sum": {"$sum": "$logs.late"}},
            "total_late_minutes": {"$sum": {"$sum": "$logs.lm"}},
            "leave_count": {"$sum": {"$sum": "$logs.lv"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.od"}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: str | None = None):
    parse_month(month)
    items = []
    for r in db.employees.aggregate(build_department_summary_pipeline(month, department)):
        cnt = int(r["wh_cnt"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": div_round(r["wh_sum"], cnt, 2) if cnt > 0 else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard
# --------------------------------------------------------------------------- #
def build_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list:
    first, last = parse_month(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                      # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # rank inside the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, next rank skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},    # limit AFTER ranking: ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    parse_month(month)
    items = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend
# --------------------------------------------------------------------------- #
def build_trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    check_day(d_from, "from")
    check_day(d_to, "to")
    f, t = date.fromisoformat(d_from), date.fromisoformat(d_to)
    if t < f:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (t - f).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(status_code=404, detail="unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps)

    from_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    to_dt = datetime(t.year, t.month, t.day, tzinfo=UTC)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from, "$lte": d_to}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [_is_present(), _present_weight(), 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late(), 0]}, 1, 0]}},
        }},
        {"$addFields": {"date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"date": 1}},
        # Gap filling: one row per day. Upper bound padded by a day, trimmed right after.
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
        {"$match": {"date": {"$gte": from_dt, "$lte": to_dt}}},
        {"$addFields": {
            "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": joined, "as": "j", "cond": {"$lte": ["$$j", "$ds"]}}}},
        }},
        {"$addFields": {
            "rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                half_up_expr({"$divide": ["$present_count", "$headcount"]}),
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},   # nulls are skipped
        }},
        {"$project": {
            "_id": 0,
            "date": "$ds",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": "$rate",
            "moving_avg_7d": half_up_expr("$ma"),     # null stays null
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    items = list(db.attendance_logs.aggregate(build_trend_pipeline(department, from_, to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(value, name: str):
    if value is None or value == "":
        raise HTTPException(status_code=422, detail=f"{name} is required for this endpoint")
    return value


def _explain(cmd: dict) -> dict:
    raw = db.command("explain", cmd, verbosity="executionStats")
    return json.loads(json_util.dumps(raw))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: DateStr | None = None,
    date_to: DateStr | None = None,
    status: Status | None = None,
    from_: DateStr | None = Query(None, alias="from"),
    to: DateStr | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = build_attendance_query(emp_code, date_from, date_to, status)
        cmd = {
            "find": "attendance_logs",
            "filter": query,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            pipeline = build_employee_monthly_pipeline(_require(emp_code, "emp_code"), _require(month, "month"))
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            pipeline = build_department_summary_pipeline(_require(month, "month"), department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            pipeline = build_leaderboard_pipeline(_require(month, "month"), limit, department)
            collection = "attendance_logs"
        else:  # department_trend
            pipeline = build_trend_pipeline(_require(department, "department"), _require(from_, "from"), _require(to, "to"))
            collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(cmd)}