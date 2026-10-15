# REVIEW.md

### Defects Found and Fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `punch_out` | Subtracting an aware datetime from a naive datetime from PyMongo raised a TypeError and returned a 500. | Calling `/attendance/punch-out` after a normal punch-in. | Normalized all timestamps to UTC naive datetimes before subtracting. |
| 2 | `regularize_attendance` | The PATCH update filter only checked `history` length, so a concurrent punch-out between read and update got overwritten. | Sending a punch-out while regularizing an open record. | Added `punch_out: record.get("punch_out")` to the atomic update filter so concurrent writes trigger 409 instead of losing data. |
| 3 | `create_indexes` | Missing index on `joined_on` caused employee-driven aggregation queries to perform slow full collection scans. | Running `/admin/explain/department_summary` without an index on joined dates. | Added composite index `(department, joined_on)` and `(joined_on)` on `employees`. |

### Not Defects (Examined and Kept)

1. **Unique-index race handling in punch-in**: Using database unique index constraint violations (`DuplicateKeyError`) instead of check-then-insert is intentional and safe against concurrent requests.
2. **Atomic compare-and-set in punch-out**: Filtering on `{"_id": id, "punch_out": None}` inside `find_one_and_update` is the standard atomic way to prevent double punch-outs.
3. **`load_dotenv()` behavior**: `load_dotenv()` defaults to not overriding existing environment variables, which is correct so real production environment variables take priority.
4. **`on_event("startup")` deprecation warning**: FastAPI deprecation warning for `startup` is harmless and works reliably without adding complexity.
5. **Fallback shift time "09:30" in punch-out**: Harmless fallback in case an employee document lacks a shift value.
