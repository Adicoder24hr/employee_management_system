# Employee Attendance & Analytics API

## How to Run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environment variables in `.env` or system environment:
   ```bash
   MONGO_URI="your_mongodb_connection_string"
   MONGO_DB="your_database_name"
   ```
3. Run the application:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## How It Works

- **PATCH regularizations**: Recomputes `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` directly from punch times and shift boundaries (R2-R5). Each edit appends an entry to the `history` audit list with old and new values.
- **Concurrency checks**: `find_one_and_update` verifies the prior length of `history` and the exact `punch_out` value at the moment of update. If another punch-out or regularization lands concurrently, the filter fails to match and returns HTTP 409.
- **Legacy records**: Records missing `half_day`, `history`, or derived minutes default to `false`, `[]`, or `0`. When regularized, missing fields are populated in MongoDB without schema migration scripts.
- **Epoch milliseconds boundary**: Timestamps in requests and responses are integer epoch milliseconds. Inside MongoDB, they are stored as UTC BSON dates.

## Not Done / Not Tested

- All six endpoints (Part A and Part B) and all 10 business rules (R1–R10) are implemented and verified.
- Tested against MongoDB Atlas on small custom fixtures (ties, weekends, mid-month joiners, 0-log headcounts, overnight shifts, and legacy records).
- Not tested against a full synthetic 100,000-record dataset on Atlas due to time constraints before 11:00 PM IST; however, query plans were verified via `/admin/explain` to confirm index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).
