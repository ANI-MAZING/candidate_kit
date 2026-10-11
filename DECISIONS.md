# DECISIONS.md

## 1. Indexes

I chose the indexes based on the queries each collection needs to support.

**`employees`**
- Unique index on `emp_code` to enforce employee identity and prevent duplicate records.
- Compound index on `{ department, emp_code }` for department-wise employee lists and trends.
- Index on `joined_on` for headcount calculations over a given period.

**`attendance_logs`**
- Unique compound index on `{ emp_code, date }` to enforce one attendance record per employee per day. It also handles concurrent punch-in requests and supports PATCH operations, monthly queries, summary lookups, and trend calculations.
- Compound index on `{ date: -1, emp_code: 1 }` to support the attendance list's sort order and date-range queries used by the leaderboard.
- Compound index on `{ status, date: -1, emp_code }` for attendance lists filtered by status.
- Compound index on `{ emp_code, punch_in: -1 }` to find the employee's most recent punch-in at or before a given time.

I did not add a separate `{ date: 1 }` index because the existing compound index starts with `date` and can support date-range queries without another index.

## 2. Handling Concurrent Punch-ins

Two punch-in requests for the same employee can arrive at the same time. Both may read the employee record and calculate the same attendance date before attempting to insert a log.

The unique `{ emp_code, date }` index prevents both inserts from succeeding. One request creates the record and returns `201`; the other hits a `DuplicateKeyError`, which is handled as `409 Conflict`.

This avoids relying on a read-before-write check, which would be vulnerable to race conditions under concurrent requests.

## 3. Leaderboard Ties

The leaderboard uses MongoDB's `$setWindowFields` with `$rank` to assign competition rankings. For example, tied employees receive ranks `1, 2, 2, 4`.

The query filters on `rank <= limit` rather than limiting the number of returned documents. This ensures that everyone tied at the cutoff is included, even if the result contains more employees than the requested limit.

Results are then ordered by `emp_code` to keep the output deterministic when ranks are equal.

## 4. Headcount Calculation

The summary pipeline starts from `employees` and filters out anyone whose `joined_on` date falls after the end of the reporting month.

It then uses `$lookup` to fetch the corresponding attendance logs. `$unwind` with `preserveNullAndEmptyArrays: true` retains employees who have no matching logs.

This is important because headcount must be calculated from employee records, not attendance records. Employees without attendance logs still contribute to the total headcount.

## 5. Scaling to 100× Data

At significantly higher data volumes, repeatedly aggregating raw attendance logs for every summary or trend request would become expensive.

I would introduce daily rollups grouped by department, updated whenever an attendance record is created or modified through punch-in or PATCH operations.

The summary and trend endpoints could then query these pre-aggregated records instead of scanning millions of attendance logs on each request. This reduces the amount of data processed per query and makes response times more predictable as the dataset grows.
