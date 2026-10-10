# Employee Attendance & Analytics API

A REST API for employee management and attendance tracking, built with **Python, FastAPI, and MongoDB**.

### Features
- Employee creation and listing with pagination
- Attendance punch-in and punch-out
- Late-arrival, overtime, and work-hour calculations
- Attendance filtering and validation

### Tech Stack
Python · FastAPI · MongoDB · PyMongo · Pytest

### Run Locally

```bash
git clone https://github.com/thisisanimesh01/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Requires a running MongoDB instance configured for the application.

API documentation: `http://127.0.0.1:8000/docs`

### Tests

```bash
PYTHONPATH=. python -m pytest test_day1.py -v
```

**Current status:** Core employee and attendance APIs implemented. Analytics and remaining endpoints are under development.