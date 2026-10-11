# REVIEW.md

## Day 1 — Starter Code Defects

| # | Where | Defect | Fix |
|---|---|---|---|
| 1 | Startup | Missing indexes and uniqueness constraints. | Added lifespan index initialization and unique indexes. |
| 2 | `/health` | Did not check MongoDB connectivity. | Added database ping; returns 503 on failure. |
| 3 | `EmployeeIn` | Missing employee code, email, and shift validation. | Added Pydantic validation. |
| 4 | `create_employee` | Race conditions and incorrect timestamp serialization. | Added duplicate-key handling, UTC timestamps, and epoch-ms responses. |
| 5 | `list_employees` | Incorrect pagination, filtering, and sorting. | Fixed skip calculation, filtered counts, and ordering. |
| 6 | `compute_late_minutes` | Incorrect timezone, truncation, and grace-period handling. | Applied IST, whole-second truncation, and strict 10-minute grace logic. |
| 7 | `compute_work_hours` | Used ties-to-even rounding. | Applied `Decimal` with `ROUND_HALF_UP`. |
| 8 | `compute_overtime` | Missed the 30-minute threshold and overnight shifts. | Added threshold and overnight calculations. |
| 9 | `punch_in` | Incorrect overnight dates and duplicate handling. | Fixed shift-date logic, conflicts, and response serialization. |
| 10 | `list_attendance` | In-memory pagination, invalid date ranges, and exposed IDs. | Moved pagination to MongoDB and added validation. |

## Day 2 — Missing Features and Fixes

| # | Endpoint / Area | Issue | Fix |
|---|---|---|---|
| 11 | Attendance regularization | Missing updates, recalculation, and audit history. | Added validation, derived-field recalculation, and change history. |
| 12 | Monthly analytics | Missing working-day and half-day calculations. | Added monthly employee analytics with join-date and weekend rules. |
| 13 | Department summary | Incorrect or incomplete headcount and attendance statistics. | Added cross-collection aggregation, including employees without logs. |
| 14 | Late leaderboard | Missing tie-aware ranking. | Added standard competition ranking and deterministic ordering. |
| 15 | Department trend | Missing dates and moving averages. | Added `$densify`, seven-day moving averages, and range validation. |
| 16 | Admin explain | Missing query execution diagnostics. | Added MongoDB `executionStats` explain support. |
| 17 | Analytics rules | Inconsistent status handling and rounding. | Applied specified attendance classifications and `ROUND_HALF_UP`. |
| 18 | Database indexes | Missing analytics query indexes. | Added indexes for join dates, departments, dates, and attendance filters. |

## Not Defects

- **Client-supplied `emp_code`:** Required by the API contract.
- **Null punches for `ABSENT`/`LEAVE`:** Required by the data model.

## Verification

- **24 tests passed** across `test_day1.py` and `test_day2.py`.
- Python compilation and `git diff --check` passed.
- Local smoke tests for `/health`, `/docs`, and `/openapi.json` returned HTTP 200.
- Department trends use BSON dates for `$densify` and return formatted date strings.

**Note:** Passed tests and local smoke tests do not guarantee correctness; they only confirm that the code runs without errors.