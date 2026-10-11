# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All application code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup.

## Notes
- All endpoints in `openapi.yaml` are implemented.
- Interpretations:
  - "range exceeds 92 days" means more than 92 calendar days inclusive.
  - In PATCH, `status`/`punch_in`/`punch_out` cannot be sent as `null`.
