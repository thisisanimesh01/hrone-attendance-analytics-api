# Design Decisions

1. **Indexes.** Unique `employees.emp_code` ensures distinct employee IDs. Unique `attendance_logs.(emp_code, date)` guarantees at most one log per day. Compound `(date, emp_code)` serves sorted pagination without in-memory sorting. Compound `(department, emp_code)` and `(department, joined_on)` speed department filtering and headcount checks. We rejected indexing `history.at` because audit entries are append-only and never queried standalone.

2. **Punch-in race.** Simultaneous requests both calculate the attendance date and attempt `insert_one`. The database unique index on `(emp_code, date)` guarantees atomicity: exactly one insert commits (201), while the other triggers `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Ties.** We use standard competition ranking (`1, 1, 3`). If employees tie at the cutoff, all are returned because the contract filters `rank <= limit`. Rows with equal totals sort deterministically by `emp_code` ascending.

4. **Headcount.** The pipeline begins on `employees` matching `joined_on <= period_end`, groups by department, and performs a `$lookup` to `attendance_logs`. Employees without logs remain in the stream and count toward headcount.

5. **One thing you would change for 100x data.** Pre-aggregate attendance stats into daily and monthly rollups on punch events instead of executing on-the-fly cross-collection `$lookup` and `$densify` aggregations over millions of rows.
