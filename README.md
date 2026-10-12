# Employee Attendance & Analytics API

A robust REST API for employee management, attendance lifecycle tracking, and workforce analytics, built with **Python 3.11+**, **FastAPI**, and **MongoDB 6.0+**.

---

## Tech Stack
- **Framework:** FastAPI
- **Validation:** Pydantic v2
- **Database:** MongoDB (via PyMongo)
- **ASGI Server:** Uvicorn
- **Testing:** Pytest

---

## Features & Implemented Endpoints

Every endpoint specified in `openapi.yaml` is implemented and verified:

### 1. System
- `GET /health` — Readiness probe checking live MongoDB ping connectivity; returns 200 `{"status": "ok"}` or 503 Service Unavailable.

### 2. Employees
- `POST /employees` — Creates an employee with business code format (`EMP\d{4,6}`), email regex, shift inequality validation, server-generated UTC creation time, and database-enforced unique index on `emp_code` (HTTP 409 on conflict).
- `GET /employees` — Paginated listing filtered by case-sensitive department, sorted by `emp_code` ascending, with accurate filtered totals.

### 3. Attendance
- `POST /attendance/punch-in` — Creates daily attendance records enforcing Rule R1 (IST dates & overnight shift mapping), whole-second instant truncation, Rule R2 (10-minute grace cutoff), and race-safe unique index on `(emp_code, date)` (HTTP 409 on duplicate punch-in).
- `POST /attendance/punch-out` — Closes open punch records atomically via `find_one_and_update` on `{"punch_out": None}`; computes Rule R3 overtime (>= 30 min minimum), Rule R4 work hours (`ROUND_HALF_UP` to 2 decimal places), and Rule R5 half-day status (`work_hours < 4.50`). Validates the 24-hour window and rejects simultaneous punch-outs with HTTP 409.
- `GET /attendance` — Fast paginated search across attendance logs using MongoDB compound indexes (`date` desc, `emp_code` asc) with `date_from`/`date_to` validation (HTTP 422 if inverted).
- `PATCH /attendance/{emp_code}/{date}` — Regularization (manual correction) with atomic audit trails appended to `history`; recomputes all affected derived fields; rejects empty changes or punches with `ABSENT`/`LEAVE`.

### 4. Analytics
- `GET /analytics/employees/{emp_code}/monthly` — Monthly summary for an employee: Rule R7 working days (Mon–Fri) from `joined_on` for mid-month joiners, present days (0.5 for half day, Mon–Fri only), and weekend inclusion in late/overtime totals.
- `GET /analytics/departments/summary` — Cross-collection aggregation computing department headcount (Rule R9 includes employees with zero logs), mean work hours across present records, overtime, and leave stats.
- `GET /analytics/leaderboard/late` — Late-arrival ranking with ties evaluated using standard competition ranking (`1, 1, 3`) and cutoff limits applying to `rank <= limit`.
- `GET /analytics/departments/{department}/trend` — Department daily trend featuring MongoDB `$densify` gap filling, daily attendance rates, and 7-day moving averages within the requested period (up to 92 days).

### 5. Query Explainability
- `GET /admin/explain/{endpoint}` — Runs MongoDB `db.command("explain", ..., verbosity="executionStats")` on representative query shapes for all supported endpoints, proving `IXSCAN` index usage.

---

## Local Setup

### 1. Prerequisites
- Python 3.11+
- MongoDB 6.0+ (running locally on port 27017 or accessible via MongoDB Atlas)

### 2. Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Configuration
The application reads configuration from environment variables (a local `.env` is supported, but real environment variables take precedence):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 4. Start the Application
From the repository root, run:
```bash
uvicorn app.main:app --port 8000
```
Interactive API documentation (Swagger UI) is available at:
`http://127.0.0.1:8000/docs`

---

## Testing & Verification

Run the automated test suite:

```bash
PYTHONPATH=. ./.venv/bin/python -m pytest test_day1.py test_day2.py -v
```

**Latest test result:** 24 tests passed, 0 failures, with 6 deprecation warnings.

The suite covers attendance calculations, API validation, employee management, regularization, monthly analytics, department summaries, trend calculations, leaderboard ranking, and query explainability.