# REVIEW.md

Defects found in the starter `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the punch-in's own (UTC) date and `replace(hour, minute)`, so no IST and no attendance date; overnight shifts are wrong. | Employee on 09:30 IST shift punching in 10:05 IST gets a nonsense value; overnight punch-in after midnight is off by a day. | Compare against the shift start of the IST attendance date (`shift_start_dt`). |
| 2 | `compute_late_minutes` | Truncates to whole minutes *before* the grace check (`> 10`), so 09:40:01 counts as inside grace; `int()` also truncates negatives toward zero. | Punch-in at 09:40:01 returns 0, the rule says 10. | Compare in seconds (`> 600`), then floor-divide to minutes (R2). |
| 3 | `compute_work_hours` | Python `round()` is banker's rounding on a float; seconds are not truncated. | 18 seconds (0.005 h) rounds to 0.0, not 0.01. | `Decimal` with `ROUND_HALF_UP` (R4). |
| 4 | `compute_overtime` | No 30-minute minimum, ignores overnight shifts (end is next day), builds a naive datetime from `date_str`. | Punch-out 5 minutes after shift end gives overtime 5; overnight shift gives huge negative/zero. | `shift_end_dt` handles overnight; only returns minutes if >= 30 (R3). |
| 5 | `EmployeeIn` | No validation at all: bad emp_code, shift format, `shift_start == shift_end`, impossible `joined_on` (2026-02-30), long strings. | POST with `shift_start: "25:99"` is accepted. | Pydantic constraints and validators -> 422. |
| 6 | `create_employee` | Check-then-insert race: two simultaneous requests both pass the check. Uses naive local `datetime.now()`; returns a datetime instead of epoch ms. | Two parallel POSTs both return 201 (or a 500 on a unique index). | Unique index on `emp_code` + `DuplicateKeyError` -> 409; `created_at` is UTC and serialised through `employee_out`. |
| 7 | `list_employees` | `skip = page * page_size` skips the whole first page; `total` counts all employees, not the filtered set; no sort; no bounds on `page`/`page_size`. | `?page=1` returns the second page; `total` ignores `department`. | `(page-1)*page_size`, filtered `count_documents`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 8 | `punch_in` | Unknown employee -> `None["shift_start"]` -> 500 instead of 404. | POST with a made-up emp_code. | Explicit 404. |
| 9 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the server's local time zone, keeps fractional seconds, `if body.punched_at` treats 0 as "not given", accepts any int (even seconds). | Different results on a server in another time zone; `punched_at=1783312500` accepted. | `ms_to_dt` (UTC, truncated), `EpochMs` strict bounded type, `is not None`. |
| 10 | `punch_in` | Attendance date taken from the local date, not the IST attendance day (R1); overnight shifts land on the wrong day. | Overnight punch-in at 05:00 IST creates a record for the wrong date. | `attendance_date()` implements R1. |
| 11 | `punch_in` | Check-then-insert race, so duplicate records for the same day. | Two simultaneous identical requests both return 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 12 | `punch_in` | `status` is a free string, so `ABSENT`/`LEAVE` (or garbage) can be punched in. | `{"status": "LEAVE"}` returns 201. | `PresenceStatus` Literal -> 422. |
| 13 | `punch_in` response | Leaks `id` (ObjectId) and returns raw datetimes instead of epoch ms. | Look at the response body. | `attendance_out()` serialiser. |
| 14 | `list_attendance` | Loads the whole collection into memory and sorts in Python; no tiebreak on `emp_code`; no bounds on `page`/`page_size`; no 422 for `date_from > date_to`; unvalidated dates/status. | Slow on large data; unstable order for equal dates. | Database sort/skip/limit, supporting indexes, validated params. |
| 15 | `list_attendance` | Returns `_id` as `id`, raw datetimes, and legacy records without `history`/`half_day` lack those keys. | Response body; legacy documents. | `attendance_out()` applies defaults and converts all times (including inside `history`). |
| 16 | MongoClient | Not `tz_aware`, so datetimes are naive; no server selection timeout. | Wrong epoch ms if the server is not in UTC; long hangs when Mongo is down. | `tz_aware=True`, `serverSelectionTimeoutMS=5000`. |
| 17 | `/health` | Always "ok", even when MongoDB is down. | Stop MongoDB and call it. | Pings MongoDB; 503 on failure. |
| 18 | Startup | No indexes at all, so every query is a collection scan and uniqueness is not guaranteed. | `explain` shows COLLSCAN. | `create_indexes()` in the lifespan hook. |

**Looked at and decided it was not a defect:** `punch_in` returning 201 (matches the contract); excluding `_id` in `list_employees` with a projection (correct, just replaced by the serialiser for consistency); `GET /attendance` computing `total` from the filtered query (correct, R10).