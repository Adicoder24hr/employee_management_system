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

## Testing & Verification

- All endpoints (CRUD, PATCH regularization, analytics, and explain) and business rules (R1–R10) are implemented and verified.
- Tested against MongoDB Atlas on focused edge-case fixtures:
  - Concurrency: simultaneous duplicate punch-ins, simultaneous punch-outs, and concurrent PATCH updates.
  - Business logic: overnight shifts spanning midnight, mid-month joiners, weekend logs, tied ranks at cutoff boundaries, employees with zero attendance records, and legacy documents missing fields.
- Query plans verified via `/admin/explain` to ensure full index coverage (`IXSCAN`) and zero collection scans (`COLLSCAN`).

## Out of Scope / Not Done

- Production reverse-proxy deployment (e.g. Nginx, Docker), as specified by the brief to run directly with Uvicorn.
- User authentication and role-based access control (e.g. JWT / OAuth), as the specification accepts free-text names (`regularized_by`) without an authentication layer.
- Distributed load and stress testing under high synthetic traffic.
