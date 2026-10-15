# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The API contract and business rules are in
[`openapi.yaml`](./openapi.yaml); MongoDB document shapes are in [`DATA_MODEL.md`](./DATA_MODEL.md). All application
code is in `app/main.py`.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (the assignment grader uses MongoDB 7.0); Atlas or a local MongoDB instance is suitable
- Dependencies listed in `requirements.txt`: FastAPI, Uvicorn, PyMongo with SRV support, Pydantic, and python-dotenv
- Tests additionally require `pytest` and `httpx`, which are not included in `requirements.txt`; install them separately
  with `python -m pip install pytest httpx` before running the test command below.

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate` instead.

Configure `MONGO_URI` and `MONGO_DB` in the process environment or in a local `.env` file in the project root. The
application loads `.env` and uses `mongodb://localhost:27017` and `attendance_db` when those variables are absent.
For local MongoDB, use a running MongoDB 6.0+ server. For Atlas, set `MONGO_URI` to the cluster connection URI and
`MONGO_DB` to a database reserved for this application. Do not put credentials in this README or commit `.env`.

Start the service with the assignment's command:

```powershell
uvicorn app.main:app --port 8000
```

The employee and attendance dashboard is served by FastAPI at **http://127.0.0.1:8000/**. Start MongoDB and configure
the existing application database as above, then run the command from the project directory and open that URL in a
browser. The page uses the existing API routes; no frontend dependencies or separate CORS configuration are required.

Indexes are requested during FastAPI startup. Successful creation, idempotency on a live server, and resulting query
plans have not been verified against a live MongoDB instance in the latest test run.

## Tests

Run the local suite with:

```powershell
python -m pytest -q
```

Latest recorded result: **13 passed, 1 skipped, 2 warnings**. The skipped integration test requires `MONGO_URI` and is
not evidence of real MongoDB behavior. It drops the `employees` and `attendance_logs` collections in its selected
database before running; only configure it with a disposable, dedicated test database. Do not point it at Atlas or any
database containing data you need to preserve.

## Limitations and verification status

The local suite uses mocked collections for most endpoint and pipeline checks. It does not prove that MongoDB accepts
and executes every aggregation pipeline, that concurrent requests produce the specified outcomes on a live server, or
that explain plans use `IXSCAN` without `COLLSCAN`. Those requirements still need verification against an isolated
MongoDB 6.0+ test database with representative data (including the assignment's approximately 100,000 attendance
records).

## Submission notes

The assignment requests a public Git repository containing the application, dependency manifest, and these documents.
Do not submit `.env`, credentials, a virtual environment, or a Dockerfile.
