# Employee Attendance & Analytics API

Backend service built with FastAPI and MongoDB for attendance logging, regularizations, and departmental analytics.

## How to Run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Start the server:
   ```bash
   uvicorn app.main:app --port 8000
   ```

The app is ready once `GET http://localhost:8000/health` returns `{"status": "ok"}`.

## Configuration

The app reads MongoDB connection details directly from environment variables (or `.env`):
- `MONGO_URI` (defaults to `mongodb://localhost:27017`)
- `MONGO_DB` (defaults to `attendance_db`)

All necessary unique and query indexes are created automatically and idempotently on startup.

## Testing

Run the automated test suite:
```bash
pytest tests/ -v
```

## Documentation
- `REVIEW.md`: Line-by-line review of bugs found in the starter kit.
- `DECISIONS.md`: My answers explaining indexing, concurrency, leaderboard ties, and scaling trade-offs.
