# DECISIONS.md

1. **Indexes.**
   - `employees`:
     - unique `emp_code`: identity and duplicate-create safety.
     - `{department, emp_code}`: department list, trend.
     - `joined_on`: summary headcount.
   - `attendance_logs`:
     - unique `{emp_code, date}`: one record per day, punch-in race, PATCH, monthly, the summary's `$lookup`, trend `$in`.
     - `{date:-1, emp_code:1}`: the list's exact sort, plus the leaderboard's month range.
     - `{status, date:-1, emp_code}`: status-filtered lists.
     - `{emp_code, punch_in:-1}`: punch-out's "latest punch-in ≤ t".

   I rejected a separate `{date:1}` index: the descending compound already serves date ranges.

2. **Punch-in race.** Both requests read the employee and compute the same `date`, then both call `insert_one`. The unique `{emp_code, date}` index lets exactly one insert succeed (201). The other gets `DuplicateKeyError`, which becomes 409. There is no read-before-write, so there is no window.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I filter on `rank <= limit`, not on row count, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and `$lookup`s their logs. An `$unwind` with `preserveNullAndEmptyArrays` keeps people with no logs, so each employee adds 1 to the headcount.

5. **100× data.** Precompute daily per-department rollups (updated on punch/PATCH) so the trend and summary read thousands of rows instead of millions.
