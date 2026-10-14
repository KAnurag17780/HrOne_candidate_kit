# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I created three indexes at startup: a unique index on `employees.emp_code` (enforces one-employee-per-code atomically, serves all emp_code lookups), a unique compound index on `attendance_logs(emp_code, date)` (enforces one-record-per-day, serves lookups by natural key and analytics matches), and a compound index on `attendance_logs(date, emp_code)` (serves the list endpoint's sort by date desc + emp_code asc, date-range analytics, and the explain endpoint). I considered indexing `status` but rejected it — low cardinality (5 values) means MongoDB would rarely choose it over the compound date index.

2. **Punch-in race.** Both requests hit `insert_one`. The unique `(emp_code, date)` index means MongoDB serialises the inserts internally. One succeeds (201), the other raises `DuplicateKeyError`, which I catch and return as 409. No check-then-insert.

3. **Ties.** I use `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). After ranking, I `$match` where `rank <= limit`, so both tied employees appear. The response may exceed `limit` rows.

4. **Headcount.** The department summary starts from the `employees` collection, filters by `joined_on <= end_of_month`, then `$lookup`s logs. Employees with no logs produce an empty lookup array but still count in `headcount` via `$addToSet`.

5. **One thing I would change** if this had to serve 100x the data: I would introduce a pre-aggregated daily snapshot collection (`daily_department_stats`) maintained at write time (punch-out and regularization), storing per-department per-day sums. Analytics would query this small collection instead of scanning millions of attendance records.
