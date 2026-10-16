# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup.

## Implemented
All endpoints in `openapi.yaml`: health, employees (create/list), punch-in, punch-out, attendance list, regularization (PATCH with audit history), the four analytics endpoints (monthly, department summary, late leaderboard, department trend) and `/admin/explain/{endpoint}`.

## Notes / known limits
- Analytics run as MongoDB aggregation pipelines (`$lookup`, `$setWindowFields`, `$densify`); they need MongoDB 6.0+.
- Rounding is half-up (computed in Python with `Decimal`, or with a floor-based expression inside the trend pipeline).
- Written reviews are in `REVIEW.md` and `DECISIONS.md`.