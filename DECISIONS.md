1. **Indexes.** Unique indexes on `employees.emp_code` and `attendance_logs (emp_code, date)` enforce key uniqueness. Employee department/join-date indexes support roster filtering. Attendance date, status/date, employee/date, late-minute, and employee/punch indexes support list, monthly, and punch queries. Actual index use still needs live `explain("executionStats")` evidence.

2. **Identical punch-ins.** Both may pass the existence check, but the unique index allows only one insert. The winner receives 201; duplicate-key errors become HTTP 409. This intended guarantee has not been tested with concurrent requests against live MongoDB.

3. **Leaderboard ties.** Competition ranking gives equal totals the same rank and skips the next rank. Filtering `rank <= limit` after ranking returns every employee tied at the cutoff, ordered by late minutes descending and `emp_code` ascending.

4. **Employees without logs.** Department summary starts with employees joined by month end and preserves empty lookup results. Eligible employees without logs remain in headcount and contribute zero attendance.

5. **100x data.** Benchmark representative workloads and execution-stat plans, then tune compound indexes to measured query shapes. Consider precomputed summaries for repeated expensive reports; evaluate sharding only if measurements justify it. Preserve aggregation semantics and test against the larger dataset.
