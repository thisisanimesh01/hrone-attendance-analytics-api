"""
Comprehensive automated test suite for Day 2:
- Attendance regularization (PATCH /attendance/{emp_code}/{date})
- Monthly employee analytics (GET /analytics/employees/{emp_code}/monthly)
- Department summary (GET /analytics/departments/summary)
- Late-arrival leaderboard (GET /analytics/leaderboard/late)
- Department attendance trend (GET /analytics/departments/{department}/trend)
- Admin query explainability (GET /admin/explain/{endpoint})
"""
from datetime import datetime, timezone
import os
import unittest

from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main
from app.main import app, IST, UTC


class TestDay2Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.test_db = cls.mongo["test_attendance_day2_db"]
        import app.main as app_module
        cls.orig_db = app_module.db
        app_module.db = cls.test_db
        app_module.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        import app.main as app_module
        cls.mongo.drop_database("test_attendance_day2_db")
        app_module.db = cls.orig_db

    def setUp(self):
        self.test_db.employees.delete_many({})
        self.test_db.attendance_logs.delete_many({})

    # ----------------------------------------------------------------------- #
    # A. Regularization (PATCH /attendance/{emp_code}/{date})
    # ----------------------------------------------------------------------- #
    def test_regularization_success_and_history(self):
        # 1. Create employee
        self.client.post("/employees", json={
            "emp_code": "EMP5001",
            "name": "Audit Test",
            "email": "audit@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        # 2. Punch in at 10:05 IST (35 min late)
        in_ms = int(datetime(2026, 7, 7, 10, 5, 0, tzinfo=IST).timestamp() * 1000)
        p_res = self.client.post("/attendance/punch-in", json={"emp_code": "EMP5001", "punched_at": in_ms})
        self.assertEqual(p_res.status_code, 201)
        self.assertEqual(p_res.json()["late_minutes"], 35)

        # 3. Regularize punch_in to 09:28 IST (inside grace period -> 0 late minutes)
        new_in_ms = int(datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST).timestamp() * 1000)
        patch_res = self.client.patch("/attendance/EMP5001/2026-07-07", json={
            "punch_in": new_in_ms,
            "reason": "Biometric terminal glitch",
            "regularized_by": "hr.manager",
        })
        self.assertEqual(patch_res.status_code, 200)
        data = patch_res.json()
        self.assertEqual(data["late_minutes"], 0)
        self.assertEqual(data["punch_in"], new_in_ms)
        self.assertEqual(len(data["history"]), 1)
        hist = data["history"][0]
        self.assertEqual(hist["by"], "hr.manager")
        self.assertEqual(hist["reason"], "Biometric terminal glitch")
        self.assertIn("punch_in", hist["changes"])
        self.assertEqual(hist["changes"]["punch_in"]["from"], in_ms)
        self.assertEqual(hist["changes"]["punch_in"]["to"], new_in_ms)
        self.assertIn("late_minutes", hist["changes"])
        self.assertEqual(hist["changes"]["late_minutes"]["from"], 35)
        self.assertEqual(hist["changes"]["late_minutes"]["to"], 0)

    def test_regularization_no_change_422(self):
        self.client.post("/employees", json={
            "emp_code": "EMP5002",
            "name": "No Change",
            "email": "nochange@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
        in_ms = int(datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP5002", "punched_at": in_ms})

        # Send same punch_in -> no changes -> 422
        res = self.client.patch("/attendance/EMP5002/2026-07-07", json={
            "punch_in": in_ms,
            "reason": "Duplicate update",
            "regularized_by": "admin",
        })
        self.assertEqual(res.status_code, 422)

    def test_regularization_absent_cannot_have_punches(self):
        self.client.post("/employees", json={
            "emp_code": "EMP5003",
            "name": "Absent Test",
            "email": "absent@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
        in_ms = int(datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP5003", "punched_at": in_ms})

        res = self.client.patch("/attendance/EMP5003/2026-07-07", json={
            "status": "ABSENT",
            "punch_in": in_ms,
            "reason": "Invalid absent punch",
            "regularized_by": "admin",
        })
        self.assertEqual(res.status_code, 422)

    def test_regularization_missing_records(self):
        res = self.client.patch("/attendance/EMP9999/2026-07-07", json={
            "status": "PRESENT",
            "reason": "Unknown employee",
            "regularized_by": "admin",
        })
        self.assertEqual(res.status_code, 404)

    # ----------------------------------------------------------------------- #
    # B. Monthly Employee Analytics (/analytics/employees/{emp_code}/monthly)
    # ----------------------------------------------------------------------- #
    def test_employee_monthly_analytics_with_midmonth_joiner(self):
        # Joined on Wednesday, July 15, 2026
        # July 2026 has 31 days. Mon-Fri in entire month = 23 days.
        # From July 15 to July 31:
        # Wed Jul 15 (1), Thu 16 (2), Fri 17 (3)
        # Mon Jul 20 to Fri 24 (5)
        # Mon Jul 27 to Fri 31 (5)
        # Total working days = 3 + 5 + 5 = 13 days
        self.client.post("/employees", json={
            "emp_code": "EMP6001",
            "name": "Mid Month",
            "email": "mid@example.com",
            "department": "Sales",
            "joined_on": "2026-07-15",
        })

        # Add 1 full day (Jul 15) and 1 half day (Jul 16)
        in_15 = int(datetime(2026, 7, 15, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        out_15 = int(datetime(2026, 7, 15, 18, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": in_15})
        self.client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": out_15})

        in_16 = int(datetime(2026, 7, 16, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        out_16 = int(datetime(2026, 7, 16, 13, 0, 0, tzinfo=IST).timestamp() * 1000)  # 3.5 hrs -> half_day True
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": in_16})
        self.client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": out_16})

        # Add 1 weekend record (Saturday, Jul 18) - 40 min late, 40 min overtime
        # Weekend records should NOT add to present_days, but SHOULD add to late/overtime totals
        in_18 = int(datetime(2026, 7, 18, 10, 10, 0, tzinfo=IST).timestamp() * 1000)
        out_18 = int(datetime(2026, 7, 18, 19, 10, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": in_18})
        self.client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": out_18})

        res = self.client.get("/analytics/employees/EMP6001/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["working_days"], 13)
        self.assertEqual(data["present_days"], 1.5)  # 1.0 (Jul 15) + 0.5 (Jul 16)
        self.assertEqual(data["late_count"], 1)  # from weekend
        self.assertEqual(data["total_late_minutes"], 40)
        self.assertEqual(data["total_overtime_minutes"], 40)
        # attendance_pct = 1.5 / 13 * 100 = 11.53846... -> 11.54
        self.assertEqual(data["attendance_pct"], 11.54)

    def test_employee_monthly_zero_logs(self):
        self.client.post("/employees", json={
            "emp_code": "EMP6002",
            "name": "Zero Logs",
            "email": "zero@example.com",
            "department": "Sales",
            "joined_on": "2026-07-01",
        })
        res = self.client.get("/analytics/employees/EMP6002/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["working_days"], 23)
        self.assertEqual(data["present_days"], 0.0)
        self.assertEqual(data["attendance_pct"], 0.0)

    # ----------------------------------------------------------------------- #
    # C. Department Summary (/analytics/departments/summary)
    # ----------------------------------------------------------------------- #
    def test_department_summary_includes_zero_log_employees(self):
        # 2 employees in Engineering: one with logs, one with zero logs
        self.client.post("/employees", json={
            "emp_code": "EMP7001",
            "name": "Eng Active",
            "email": "active@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-01",
        })
        self.client.post("/employees", json={
            "emp_code": "EMP7002",
            "name": "Eng Idle",
            "email": "idle@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-01",
        })

        in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        out_ms = int(datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP7001", "punched_at": in_ms})
        self.client.post("/attendance/punch-out", json={"emp_code": "EMP7001", "punched_at": out_ms})

        res = self.client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["department"], "Engineering")
        self.assertEqual(item["headcount"], 2)  # Both counted!
        self.assertEqual(item["present_days"], 1.0)
        self.assertEqual(item["avg_work_hours"], 9.0)

    # ----------------------------------------------------------------------- #
    # D. Late Leaderboard (/analytics/leaderboard/late)
    # ----------------------------------------------------------------------- #
    def test_late_leaderboard_standard_competition_ranking(self):
        # 3 employees: EMP8001 (60 min), EMP8002 (60 min tied), EMP8003 (30 min)
        # Ranks must be: EMP8001 -> 1, EMP8002 -> 1, EMP8003 -> 3 (skips 2!)
        for i, code in enumerate(["EMP8001", "EMP8002", "EMP8003"]):
            self.client.post("/employees", json={
                "emp_code": code,
                "name": f"User {code}",
                "email": f"{code}@example.com",
                "department": "Support",
                "joined_on": "2026-01-01",
            })

        # EMP8001: 60 min late (punch at 10:30)
        in_1 = int(datetime(2026, 7, 6, 10, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP8001", "punched_at": in_1})

        # EMP8002: 60 min late
        in_2 = int(datetime(2026, 7, 6, 10, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP8002", "punched_at": in_2})

        # EMP8003: 30 min late (punch at 10:00)
        in_3 = int(datetime(2026, 7, 6, 10, 0, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP8003", "punched_at": in_3})

        res = self.client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[0]["total_late_minutes"], 60)
        self.assertEqual(items[1]["rank"], 1)
        self.assertEqual(items[1]["total_late_minutes"], 60)
        self.assertEqual(items[2]["rank"], 3)  # Standard competition ranking!
        self.assertEqual(items[2]["total_late_minutes"], 30)

        # Cutoff check: limit=1 returns both rank 1s (because rank <= limit)!
        res_limit_1 = self.client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
        self.assertEqual(res_limit_1.status_code, 200)
        self.assertEqual(len(res_limit_1.json()["items"]), 2)

    # ----------------------------------------------------------------------- #
    # E. Department Attendance Trend (/analytics/departments/{dept}/trend)
    # ----------------------------------------------------------------------- #
    def test_department_trend_with_densify_and_moving_average(self):
        self.client.post("/employees", json={
            "emp_code": "EMP9001",
            "name": "Trend User",
            "email": "trend@example.com",
            "department": "Finance",
            "joined_on": "2026-07-01",
        })

        # Present on 2026-07-06 (Monday)
        in_ms = int(datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).timestamp() * 1000)
        self.client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": in_ms})

        res = self.client.get("/analytics/departments/Finance/trend?from=2026-07-05&to=2026-07-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["department"], "Finance")
        items = data["items"]
        # Exactly 3 days: 05 (Sunday), 06 (Monday), 07 (Tuesday)
        self.assertEqual(len(items), 3)

        # Sunday: weekend -> attendance_rate is null
        self.assertFalse(items[0]["is_working_day"])
        self.assertIsNone(items[0]["attendance_rate"])
        self.assertIsNone(items[0]["moving_avg_7d"])

        # Monday: working day -> present_count = 1.0, headcount = 1 -> rate = 1.0
        self.assertTrue(items[1]["is_working_day"])
        self.assertEqual(items[1]["present_count"], 1.0)
        self.assertEqual(items[1]["attendance_rate"], 1.0)
        self.assertEqual(items[1]["moving_avg_7d"], 1.0)

        # Tuesday: working day -> present_count = 0.0, headcount = 1 -> rate = 0.0
        self.assertTrue(items[2]["is_working_day"])
        self.assertEqual(items[2]["present_count"], 0.0)
        self.assertEqual(items[2]["attendance_rate"], 0.0)
        # moving_avg_7d of [1.0, 0.0] = 0.5
        self.assertEqual(items[2]["moving_avg_7d"], 0.5)

    def test_department_trend_validation(self):
        # Range > 92 days -> 422
        res1 = self.client.get("/analytics/departments/Finance/trend?from=2026-01-01&to=2026-05-01")
        self.assertEqual(res1.status_code, 422)

        # to < from -> 422
        res2 = self.client.get("/analytics/departments/Finance/trend?from=2026-07-10&to=2026-07-01")
        self.assertEqual(res2.status_code, 422)

        # Unknown department -> 404
        res3 = self.client.get("/analytics/departments/GhostDept/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(res3.status_code, 404)

    # ----------------------------------------------------------------------- #
    # F. Admin Explain Endpoint (/admin/explain/{endpoint})
    # ----------------------------------------------------------------------- #
    def test_admin_explain_endpoints(self):
        # 1. attendance_list
        r1 = self.client.get("/admin/explain/attendance_list?emp_code=EMP9001")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["endpoint"], "attendance_list")
        self.assertIn("explain", r1.json())

        # 2. employee_monthly
        r2 = self.client.get("/admin/explain/employee_monthly?emp_code=EMP9001&month=2026-07")
        self.assertEqual(r2.status_code, 200)

        # 3. department_summary
        r3 = self.client.get("/admin/explain/department_summary?month=2026-07")
        self.assertEqual(r3.status_code, 200)

        # 4. late_leaderboard
        r4 = self.client.get("/admin/explain/late_leaderboard?month=2026-07")
        self.assertEqual(r4.status_code, 200)

        # 5. department_trend
        r5 = self.client.get("/admin/explain/department_trend?department=Finance&from=2026-07-01&to=2026-07-05")
        self.assertEqual(r5.status_code, 200)

        # 6. Invalid endpoint -> 422
        r6 = self.client.get("/admin/explain/unknown_endpoint")
        self.assertEqual(r6.status_code, 422)

        # 7. Missing required params -> 422
        r7 = self.client.get("/admin/explain/employee_monthly")
        self.assertEqual(r7.status_code, 422)


if __name__ == "__main__":
    unittest.main()
