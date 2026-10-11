# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+, tested on 7.0). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a local .env
python sample_seed.py            # optional: the nine sample documents
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup. Nothing else is needed.

## Notes
- All endpoints in `openapi.yaml` are implemented. The analytics are MongoDB aggregation pipelines (`$lookup`,
  `$setWindowFields`/`$rank`, `$densify`, windowed `$avg`).
- `/admin/explain/{endpoint}` reuses the same builder functions as the real endpoints, so the explained query is exactly the one that runs.
- Half-up rounding: work hours and API-side rounding use Python `Decimal`. Inside pipelines, rates are rounded half-up in
  `Decimal128` (`$round` is half-to-even).
- Interpretations:
  - "range exceeds 92 days" means more than 92 calendar days inclusive.
  - In PATCH, `status`/`punch_in`/`punch_out` may be omitted but not sent as `null` (422).
  - The R1 "same attendance date" check is applied to a `punch_in` supplied in the PATCH body.
- Tested against MongoDB 7 with a local end-to-end suite: validation, concurrency (parallel create, punch-in, punch-out,
  PATCH), R1-R5 edge cases, ties, weekend records, mid-month joiners, no-log employees, and an empty trend range. Also
  tested on a 100k-record dataset, where every endpoint answers in < 60 ms and all explain plans use IXSCAN with no COLLSCAN.
