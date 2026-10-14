<!--
# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
-->

# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation for employee attendance tracking and business intelligence analytics, fully compliant with OpenAPI 3.0.3 contract and business rules R1 – R10.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested on MongoDB 8.0 / 7.0)

## Quickstart

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment** (Optional):
   Set `MONGO_URI` and `MONGO_DB` as environment variables if using a custom MongoDB instance. By default, it connects to:
   - `MONGO_URI`: `mongodb://localhost:27017`
   - `MONGO_DB`: `attendance_db`

3. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

4. **Run Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

5. **Verify Readiness**:
   ```bash
   curl http://localhost:8000/health
   ```
   Should return: `{"status": "ok"}`

## Summary of Work Done

- **Part A: Defect Review & Core Bug Fixes (`app/main.py`)**:
  - Fixed IST/UTC datetime conversions, instant whole-second truncation, and overnight shift calendar date calculation (R1).
  - Fixed late grace boundary condition (`> 10:00`) (R2) and 30-minute overtime minimum threshold (R3).
  - Replaced banker's rounding with standard arithmetic half-up rounding (`Decimal.quantize`) for hours and rates (R4, R8).
  - Replaced check-then-insert race conditions with database unique index constraints (HTTP 409).
  - Fixed off-by-one pagination and eliminated memory-intensive in-memory list sorting in `/attendance`.
  - Documented all findings, symptoms, and solutions in [`REVIEW.md`](REVIEW.md).

- **Part B: Feature Implementation & Aggregation Pipelines**:
  - Implemented `POST /attendance/punch-out` with atomic `find_one_and_update`, overtime, and half-day checks (R5).
  - Implemented `PATCH /attendance/{emp_code}/{date}` (regularization) with atomic `history` change tracking and validation.
  - Built 4 MongoDB aggregation pipelines:
    - `GET /analytics/employees/{emp_code}/monthly`: Mon–Fri working days, mid-month joiner handling (R7), and presence rates.
    - `GET /analytics/departments/summary`: Cross-collection `$lookup` ensuring employees with zero logs are counted in headcount (R9).
    - `GET /analytics/leaderboard/late`: Standard Competition Ranking (`$rank` window function) with tie handling at cutoff.
    - `GET /analytics/departments/{department}/trend`: Calendar gap-filling via `$densify` and 7-day moving averages via `$setWindowFields`.

- **Part C: Indexing, Diagnostics & Architecture**:
  - Added idempotent startup indexing on unique natural keys and sort paths.
  - Implemented `GET /admin/explain/{endpoint}` returning raw `executionStats` confirming index scans (`IXSCAN`) without `COLLSCAN`.
  - Completed [`DECISIONS.md`](DECISIONS.md) detailing choices on indexes, race resolution, ties, headcount, and 100x data scaling.

## Deliverables
- `app/main.py`: Complete production implementation (CRUD, concurrency-safe operations, analytics aggregation pipelines, and `/admin/explain`).
- `requirements.txt`: Project dependencies.
- `REVIEW.md`: Detailed defect analysis and resolution log of starter code flaws.
- `DECISIONS.md`: Architecture decisions covering indexing, race condition handling, tie-breaking, headcount tracking, and 100x scaling strategy.

