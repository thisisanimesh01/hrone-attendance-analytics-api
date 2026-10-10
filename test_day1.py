"""
Comprehensive unit and integration test suite for Day 1 endpoints and business rules (R1 - R5).
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main
from app.main import (
    app,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    get_attendance_date,
    from_epoch_ms,
    to_epoch_ms,
    IST,
    UTC,
)


class TestDay1AttendanceLogic(unittest.TestCase):
    def test_instant_truncation_r1(self):
        # 09:40:00.900 ms should truncate to 09:40:00
        # 2026-07-06 09:40:00.900 IST
        # 09:40:00 IST is 04:10:00 UTC
        dt_with_ms = from_epoch_ms(1783311000900)
        self.assertEqual(dt_with_ms.microsecond, 0)
        self.assertEqual(to_epoch_ms(dt_with_ms), 1783311000000)

    def test_r1_overnight_shift_date(self):
        # Shift 22:00 to 06:00
        # Punch-in at 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
        dt1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST).astimezone(UTC)
        d1 = get_attendance_date(dt1, "22:00", "06:00")
        self.assertEqual(d1, "2026-07-06")

        # Punch-in at 05:45 IST on 2026-07-07 (< 06:00 shift end) -> belongs to 2026-07-06!
        dt2 = datetime(2026, 7, 7, 5, 45, 0, tzinfo=IST).astimezone(UTC)
        d2 = get_attendance_date(dt2, "22:00", "06:00")
        self.assertEqual(d2, "2026-07-06")

        # Regular daytime shift: 09:30 to 18:30 on 2026-07-06
        dt3 = datetime(2026, 7, 6, 9, 25, 0, tzinfo=IST).astimezone(UTC)
        d3 = get_attendance_date(dt3, "09:30", "18:30")
        self.assertEqual(d3, "2026-07-06")

    def test_r2_late_grace_boundary(self):
        # Shift 09:30
        # 09:40:00 IST (exactly 10 min) -> 0 late minutes
        dt_exact = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_late_minutes(dt_exact, "09:30", "2026-07-06"), 0)

        # 09:40:01 IST (strictly > 10 min) -> 10 late minutes
        dt_late_1s = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_late_minutes(dt_late_1s, "09:30", "2026-07-06"), 10)

        # 10:15:59 IST -> 45 late minutes
        dt_late_45 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_late_minutes(dt_late_45, "09:30", "2026-07-06"), 45)

        # 09:20:00 IST (early) -> 0 late minutes
        dt_early = datetime(2026, 7, 6, 9, 20, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_late_minutes(dt_early, "09:30", "2026-07-06"), 0)

    def test_r3_overtime_threshold(self):
        # Shift end 18:30
        # Punch out 18:59 (29 min OT) -> 0
        dt_29 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(dt_29, "09:30", "18:30", "2026-07-06"), 0)

        # Punch out 19:00 (30 min OT) -> 30
        dt_30 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(dt_30, "09:30", "18:30", "2026-07-06"), 30)

        # Punch out 19:10 (40 min OT) -> 40
        dt_40 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(dt_40, "09:30", "18:30", "2026-07-06"), 40)

        # Overnight shift 22:00 to 06:00
        # Punch out next day 06:40 IST -> 40 min OT
        dt_ot_night = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(compute_overtime(dt_ot_night, "22:00", "06:00", "2026-07-06"), 40)

    def test_r4_half_up_work_hours(self):
        # 1 hour 30 seconds = 3630 seconds / 3600 = 1.008333... -> 1.01
        dt_in = datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)
        dt_out = datetime(2026, 7, 6, 10, 0, 30, tzinfo=UTC)
        self.assertEqual(compute_work_hours(dt_in, dt_out), 1.01)

        # Half-up check: 18 seconds = 0.005 hours -> rounds up to 0.01
        dt_out_18s = datetime(2026, 7, 6, 9, 0, 18, tzinfo=UTC)
        self.assertEqual(compute_work_hours(dt_in, dt_out_18s), 0.01)

    def test_r5_half_day_boundary(self):
        # rounded work_hours < 4.50 -> half_day = True
        self.assertTrue(compute_half_day(4.49))
        self.assertFalse(compute_half_day(4.50))
        self.assertFalse(compute_half_day(8.00))
        self.assertFalse(compute_half_day(None))


class TestDay1ApiEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        # Point to test database
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.test_db = cls.mongo["test_attendance_day1_db"]
        # Monkeypatch db in app.main
        import app.main as app_module
        cls.orig_db = app_module.db
        app_module.db = cls.test_db
        app_module.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        import app.main as app_module
        cls.mongo.drop_database("test_attendance_day1_db")
        app_module.db = cls.orig_db

    def setUp(self):
        self.test_db.employees.delete_many({})
        self.test_db.attendance_logs.delete_many({})

    def test_health_check(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_employee_creation_and_duplicates(self):
        payload = {
            "emp_code": "EMP1001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res1 = self.client.post("/employees", json=payload)
        self.assertEqual(res1.status_code, 201)
        data = res1.json()
        self.assertEqual(data["emp_code"], "EMP1001")
        self.assertIn("created_at", data)
        self.assertIsInstance(data["created_at"], int)
        self.assertNotIn("_id", data)

        # Duplicate emp_code must return 409
        res2 = self.client.post("/employees", json=payload)
        self.assertEqual(res2.status_code, 409)

    def test_employee_validation_rules(self):
        # Invalid emp_code format (no EMP prefix)
        bad_code = {
            "emp_code": "1001",
            "name": "Bad Code",
            "email": "bad@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        }
        self.assertEqual(self.client.post("/employees", json=bad_code).status_code, 422)

        # Same shift_start and shift_end -> 422
        bad_shift = {
            "emp_code": "EMP1002",
            "name": "Bad Shift",
            "email": "badshift@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        }
        self.assertEqual(self.client.post("/employees", json=bad_shift).status_code, 422)

    def test_employee_pagination_and_total(self):
        for i in range(1, 26):
            self.client.post(
                "/employees",
                json={
                    "emp_code": f"EMP{i:04d}",
                    "name": f"User {i}",
                    "email": f"user{i}@example.com",
                    "department": "Engineering" if i % 2 == 0 else "Sales",
                    "joined_on": "2026-01-01",
                },
            )

        # Page 1, size 10
        r1 = self.client.get("/employees?page=1&page_size=10")
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertEqual(len(d1["items"]), 10)
        self.assertEqual(d1["total"], 25)
        self.assertEqual(d1["items"][0]["emp_code"], "EMP0001")

        # Page 3, size 10 (remaining 5)
        r3 = self.client.get("/employees?page=3&page_size=10")
        self.assertEqual(r3.status_code, 200)
        d3 = r3.json()
        self.assertEqual(len(d3["items"]), 5)
        self.assertEqual(d3["items"][0]["emp_code"], "EMP0021")

        # Filter by department
        r_sales = self.client.get("/employees?department=Sales&page=1&page_size=10")
        self.assertEqual(r_sales.status_code, 200)
        self.assertEqual(r_sales.json()["total"], 13)

        # Pagination validation: page < 1 -> 422, page_size > 100 -> 422
        self.assertEqual(self.client.get("/employees?page=0").status_code, 422)
        self.assertEqual(self.client.get("/employees?page_size=101").status_code, 422)

    def test_attendance_punch_in_and_duplicate(self):
        # Create employee
        self.client.post(
            "/employees",
            json={
                "emp_code": "EMP2001",
                "name": "Punch In Test",
                "email": "punch@example.com",
                "department": "Ops",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

        # Punch in at 09:25 IST on 2026-07-06
        # 09:25 IST is 03:55 UTC -> 1783309500000 ms
        ms_punch = int(datetime(2026, 7, 6, 9, 25, 0, tzinfo=IST).timestamp() * 1000)
        r1 = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP2001", "punched_at": ms_punch, "status": "PRESENT"},
        )
        self.assertEqual(r1.status_code, 201)
        doc = r1.json()
        self.assertEqual(doc["emp_code"], "EMP2001")
        self.assertEqual(doc["date"], "2026-07-06")
        self.assertEqual(doc["punch_in"], ms_punch)
        self.assertIsNone(doc["punch_out"])
        self.assertEqual(doc["late_minutes"], 0)
        self.assertNotIn("id", doc)
        self.assertNotIn("_id", doc)

        # Duplicate punch in for same day -> 409
        r2 = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP2001", "punched_at": ms_punch, "status": "PRESENT"},
        )
        self.assertEqual(r2.status_code, 409)

    def test_attendance_punch_out_and_concurrency(self):
        # Create employee
        self.client.post(
            "/employees",
            json={
                "emp_code": "EMP3001",
                "name": "Punch Out Test",
                "email": "punchout@example.com",
                "department": "Ops",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

        # Punch in at 09:30 IST on 2026-07-06
        in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP3001", "punched_at": in_ms},
        )

        # Punch out at 19:15 IST (9 hours 45 min = 9.75 hrs; 45 min OT)
        out_ms = int(datetime(2026, 7, 6, 19, 15, 0, tzinfo=IST).timestamp() * 1000)
        res_out = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP3001", "punched_at": out_ms},
        )
        self.assertEqual(res_out.status_code, 200)
        data = res_out.json()
        self.assertEqual(data["work_hours"], 9.75)
        self.assertEqual(data["overtime_minutes"], 45)
        self.assertFalse(data["half_day"])
        self.assertEqual(data["punch_out"], out_ms)

        # Second punch-out on same record -> 409 already punched out
        res_out2 = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP3001", "punched_at": out_ms + 1000},
        )
        self.assertEqual(res_out2.status_code, 409)

    def test_punch_out_validation(self):
        self.client.post(
            "/employees",
            json={
                "emp_code": "EMP4001",
                "name": "Punch Val Test",
                "email": "val@example.com",
                "department": "Ops",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )
        in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP4001", "punched_at": in_ms},
        )

        # Punch out before punch in -> 422
        res_early = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP4001", "punched_at": in_ms - 1000},
        )
        self.assertEqual(res_early.status_code, 422)

        # Punch out > 24 hours later -> 422
        res_late = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP4001", "punched_at": in_ms + 25 * 3600 * 1000},
        )
        self.assertEqual(res_late.status_code, 422)


if __name__ == "__main__":
    unittest.main()
