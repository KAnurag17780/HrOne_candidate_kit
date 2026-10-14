# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` L72-74 | Returns `{"status": "ok"}` without pinging MongoDB. Should return 503 when DB is down. | Stop MongoDB → `/health` still returns 200 instead of 503. | Added `db.command("ping")` wrapped in try/except; raise HTTPException(503) on failure. |
| 2 | `create_employee()` L79 | Check-then-insert race condition. Uses `find_one` then `insert_one` — two concurrent creates can both succeed. | Two parallel `POST /employees` with the same `emp_code` → both return 201. | Created unique index on `emp_code`; catch `DuplicateKeyError` → 409. |
| 3 | `create_employee()` L82 | `datetime.now()` uses local time, not UTC. | `created_at` stored in local timezone; epoch-ms conversion returns wrong value. | Changed to `datetime.now(timezone.utc)`. |
| 4 | `create_employee()` L85 | `created_at` returned as raw datetime, not epoch milliseconds. | Response contains a datetime string instead of integer. | Convert `created_at` to epoch ms via `_dt_to_epoch_ms()` before returning. |
| 5 | `list_employees()` L93 | Wrong skip calculation: `page * page_size` instead of `(page - 1) * page_size`. | `GET /employees?page=1` returns empty — skips the first page. | Changed to `skip = (page - 1) * page_size`. |
| 6 | `list_employees()` L94 | `count_documents({})` ignores filters — total is always the full collection count. | Filter by department → total still shows count of all employees. | Changed to `count_documents(q)`. |
| 7 | `list_employees()` L95 | Missing `.sort("emp_code", 1)` — results appear in insertion order. | Results not sorted by emp_code ascending as spec requires. | Added `.sort("emp_code", ASCENDING)`. |
| 8 | `list_employees()` L95 | `created_at` returned as datetime, not epoch ms. | Response has datetime string instead of integer. | Convert `created_at` to epoch ms for each item. |
| 9 | `punch_in()` L101 | No 404 check for unknown employee. `emp` could be `None` → `TypeError` on `emp["shift_start"]` → 500. | Punch-in for nonexistent employee → 500 instead of 404. | Added `if not emp: raise HTTPException(404)`. |
| 10 | `punch_in()` L102 | `datetime.fromtimestamp(...)` uses system local timezone, not UTC+05:30 (IST). Date computed in wrong timezone. | Server in UTC → punch-in at 2 AM IST classified under previous day. | Use `datetime.fromtimestamp(epoch_ms/1000, tz=timezone.utc)` then convert to IST for date calculation. |
| 11 | `punch_in()` L102 | No truncation to whole seconds (R1 requires truncation). | Stored punch times have sub-second precision. | Added `_truncate_to_seconds()` — `.replace(microsecond=0)`. |
| 12 | `punch_in()` L104-118 | Check-then-insert race on attendance logs. `find_one` then `insert_one` is not atomic. | Two simultaneous punch-ins → both succeed. | Unique compound index on `(emp_code, date)` + catch `DuplicateKeyError` → 409. |
| 13 | `punch_in()` L109 | No validation of `status` — any string accepted. Only PRESENT/WFH/ON_DUTY valid. | Punch-in with `status: "ABSENT"` → succeeds instead of 422. | Validate against `PRESENCE_STATUSES` set, raise 422 if invalid. |
| 14 | `punch_in()` L102 | No validation of `punched_at` bounds (R1: 100000000000 ≤ value ≤ 4102444800000). | Seconds-looking values, floats, strings accepted. | Added `_validate_epoch_ms()` check. |
| 15 | `punch_in()` L103 | Overnight shift date logic missing (R1). For overnight shifts with punch-in before shift_end, date should be previous day. | Overnight shift punch-in at 01:00 IST → date is today instead of yesterday. | Implemented `_compute_attendance_date()` with overnight detection. |
| 16 | `punch_in()` L119 | Returns `"id"` field (MongoDB ObjectId as string) — not in the AttendanceRecord schema. | Response has extraneous `id` field. | Removed; `_format_attendance_record()` pops `_id` and doesn't add `id`. |
| 17 | `punch_in()` L121 | `punch_in` returned as datetime, not epoch ms. | Response has datetime instead of integer. | `_format_attendance_record()` converts all datetimes to epoch ms. |
| 18 | `compute_late_minutes()` L33-37 | Uses `punch_in.replace(hour=..., minute=...)` which puts shift_start on punch-in's date — wrong for overnight shifts. | Overnight shift late calculation completely wrong. | Rewrote: build shift_start datetime from attendance date in IST. |
| 19 | `compute_late_minutes()` L37 | Uses `minutes > 10` with float division (imprecise). R2 says "strictly more than 10:00" (600 seconds). | Edge case at exactly 10 minutes may give wrong result. | Changed to integer seconds comparison: `diff_seconds > 600`. |
| 20 | `compute_work_hours()` L41 | Uses Python `round()` (banker's rounding). R4 requires half-up. | `round(8.125, 2)` → 8.12 instead of 8.13. | Used `Decimal` with `ROUND_HALF_UP`. |
| 21 | `compute_overtime()` L47 | No 30-minute minimum threshold. R3 says overtime counts only when ≥ 30. | 20 minutes past shift end → returns 20 instead of 0. | Added `minutes >= 30` check; return 0 otherwise. |
| 22 | `compute_overtime()` L46 | Overnight shift end date wrong — uses attendance date for shift_end, but overnight shifts end on the next calendar day. | Overnight shift overtime is wildly wrong (negative or huge). | If overnight shift, add 1 day to shift end date. |
| 23 | `list_attendance()` L144-147 | `list(db.attendance_logs.find(q))` loads entire collection into memory, sorts in Python. Must use MongoDB sort/skip/limit for 100k records. | Endpoint is very slow and uses excessive memory at scale. | Used MongoDB `.sort().skip().limit()` and `count_documents()`. |
| 24 | `list_attendance()` L145 | Only sorts by date descending. Missing secondary sort by emp_code ascending. | Results with same date appear in undefined order. | Added `.sort([("date", DESCENDING), ("emp_code", ASCENDING)])`. |
| 25 | `list_attendance()` L149 | Returns `"id"` field (ObjectId as string) — not in the spec. | Response has extraneous `id` field. | Removed; `_format_attendance_record()` only removes `_id`. |
| 26 | `list_attendance()` L148-150 | `punch_in`/`punch_out` returned as BSON datetimes, not epoch ms. Same for history entries. | Response has datetime strings instead of integers. | `_format_attendance_record()` converts all datetimes to epoch ms. |
| 27 | `create_employee()` L53-60 | No input validation: `emp_code` pattern, email format, name/department length, `shift_start != shift_end`. | Invalid data accepted. | Added regex and length validations per OpenAPI schema. |
| 28 | All paginated endpoints | No `page_size` max validation (R10: max 100). | `page_size=999999` returns everything. | Added `_validate_page_params()` — rejects page_size > 100. |

### Things examined and decided were **not** defects

- **`IST = timezone(timedelta(hours=5, minutes=30))`** — Correct definition of IST offset. Fine.
- **`load_dotenv()` at module level** — Loads `.env` before env vars are read. The spec says real env vars must take priority, and `load_dotenv()` does not override existing env vars by default. Fine.
- **`MongoClient` at module level** — Acceptable; PyMongo is connection-pooled and lazy. No issue.
- **Pydantic model field types** — The models are basic but adequate as input containers; validation is handled explicitly.
