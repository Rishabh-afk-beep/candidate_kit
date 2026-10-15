# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce uniqueness and race safety. Supporting indexes include `attendance_logs(date: -1, emp_code: 1)` for cursor pagination, `attendance_logs(emp_code, punch_in: -1)` for fast punch-out lookups across midnight, `attendance_logs(date, late_minutes)` for leaderboard filtering, and `employees(department, joined_on)` for department analytics. I considered indexing the `history` array but rejected it because history is an append-only audit log that clients never query directly.

2. **Punch-in race.**
Both requests check the employee and compute the IST attendance date. When both attempt `insert_one()`, MongoDB's unique index on `(emp_code, date)` guarantees only one insert commits. The second request immediately throws a `DuplicateKeyError`, which my code catches and converts into an HTTP 409 conflict, completely eliminating check-then-insert race conditions.

3. **Ties.**
I use standard competition ranking (`$rank` in `$setWindowFields`): tied employees share a rank, and the next rank is skipped (1, 2, 2, 4). The `limit` check applies after ranking (`rank <= limit`), with tied rows ordered by `emp_code` ascending. If two employees tie at the cutoff rank, both are returned, so the response can intentionally contain more rows than `limit`.

4. **Headcount.**
Department summary starts its aggregation directly from the `employees` collection (matching `joined_on <= month_end`), grouping by department to count headcount (`$sum: 1`) before looking up attendance logs. This ensures employees who joined on time but have zero attendance logs are still counted in headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million logs), I would pre-aggregate daily department metrics and monthly totals via background workers or change streams into a summary collection, avoiding costly real-time multi-stage aggregations across un-sharded collections.
