# DECISIONS.md

1. **Indexes.**
   Created `employees(emp_code)` (unique) to enforce employee identity, and `attendance_logs(emp_code, date)` (unique) to guarantee at most one record per day. Added compound index `attendance_logs(date: -1, emp_code: 1)` to eliminate in-memory sorting on list queries, `attendance_logs(emp_code: 1, punch_in: -1)` for fast open punch-out resolution, and `employees(department: 1)` for filtered analytics. Considered and rejected indexing `late_minutes` standalone because late calculations are always constrained by a date or department filter.

2. **Punch-in race.**
   Both concurrent requests compute the target attendance date and attempt `insert_one`. MongoDB's compound unique index on `(emp_code, date)` atomically accepts the first document (HTTP 201). The second concurrent insert violates the unique constraint and raises `DuplicateKeyError`, which our handler catches to return HTTP 409. No race-prone check-then-insert is used.

3. **Ties.**
   The aggregation pipeline uses `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). The `rank <= limit` filter is applied after ranking; therefore, all tied employees at the boundary are returned, even if total rows exceed `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.**
   The department aggregation starts from the `employees` collection (filtering `joined_on <= period_end`), then `$lookup`s logs using `$unwind(preserveNullAndEmptyArrays: True)`. Collecting `emp_code` via `$addToSet` ensures employees with zero attendance records are fully counted in department headcount.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily and monthly metrics into roll-up summary collections updated incrementally via MongoDB Change Streams, avoiding expensive multi-stage `$lookup` and `$densify` pipeline scans across millions of raw logs on every request.
