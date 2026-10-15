# DECISIONS.md

### 1. Indexes
I created these indexes at startup:
- `employees.emp_code` (unique): stops duplicate employee codes at the database level.
- `employees.(department, emp_code)`: covers listing employees filtered by department.
- `employees.(department, joined_on)` and `employees.joined_on`: speeds up department headcount filters by joined date so queries do not scan the whole collection.
- `attendance_logs.(emp_code, date)` (unique): guarantees only one record exists per employee per day and resolves punch-in race conditions.
- `attendance_logs.(date, emp_code)`: supports default date-sorted attendance lists.
- `attendance_logs.(status, date, emp_code)`: covers status-filtered attendance queries.
- `attendance_logs.(date, late_minutes)`: speeds up leaderboard month and late minute lookups.

I considered an index on `attendance_logs.punch_out` alone, but rejected it because punch-out lookups already filter by `emp_code` and sort by `punch_in`, so a standalone `punch_out` index was redundant.

### 2. Punch-in race
When two identical punch-in requests hit the server at the exact same moment:
1. Both read the employee record and calculate the attendance date.
2. Both run `insert_one` with `{"emp_code": ..., "date": ...}`.
3. The database unique index on `(emp_code, date)` lets the first insert succeed with HTTP 201.
4. The second insert hits a `DuplicateKeyError`.
5. My code catches `DuplicateKeyError` and turns it into HTTP 409. No bad data is saved.

### 3. Ties
When two employees tie on late minutes at the cutoff:
I use MongoDB's `$rank` inside `$setWindowFields`. Tied employees receive the same rank (like 1, 2, 2, 4). The query filters by `rank <= limit` after ranking. Because of this, both tied employees are returned, which can produce more rows than `limit`. The tie is broken by sorting by `emp_code` ascending.

### 4. Headcount
The department summary query starts by querying the `employees` collection, not the logs. It filters employees by `joined_on <= last_day_of_month`. It groups by department to get the true headcount first, then looks up logs for that month. Employees who have no attendance records still contribute 1 to the headcount.

### 5. One thing I would change for 100x the data
If the data grew by 100x, running aggregation pipelines over millions of raw attendance documents on every request would get slow. I would pre-aggregate daily and monthly department stats into a summary collection whenever punch-out or PATCH regularizations happen.
