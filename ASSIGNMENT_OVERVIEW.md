# HROne Assignment: Requirements vs Implementation Summary

This document outlines the expected requirements from `PROBLEM_STATEMENT.docx`, `openapi.yaml`, and `DATA_MODEL.md`, along with the technical solutions implemented across the codebase.

---

## 1. Executive Summary

| Assignment Part | Primary Objective | Required Output | Implementation Status |
|---|---|---|---|
| **Part A: Review** | Find and fix all starter bugs in `app/main.py` | [REVIEW.md](file:///e:/candidate_kit/REVIEW.md) table with exact failure symptoms and fixes | **100% Complete** (18 defects resolved) |
| **Part B: Build** | Implement all 12 API endpoints & business rules R1–R10 | [app/main.py](file:///e:/candidate_kit/app/main.py) single-file backend service | **100% Complete** (all 12 endpoints functional) |
| **Part C: Prove & Explain** | Query index verification and architecture write-up | [DECISIONS.md](file:///e:/candidate_kit/DECISIONS.md) & `/admin/explain/{endpoint}` | **100% Complete** (all queries use `IXSCAN`, 0 `COLLSCAN`) |

---

## 2. What Was Expected vs What Was Done

### Part A: Review of the Starter Kit
- **What was expected**:
  - The starter file `app/main.py` was written quickly and had 10+ hidden bugs across helper functions and starter endpoints.
  - You had to find them, fix them, and record each one in `REVIEW.md` with: location, defect, failing test/input symptom, and how it was resolved.
  - Document anything checked that was *not* a bug.
- **What was done**:
  - Identified and fixed **18 distinct bugs**, including:
    - Float/Banker's rounding in work hours (replaced with Decimal half-up rounding).
    - Integer truncation bug in late minutes where `09:40:01` returned 0 instead of 10.
    - Missing 30-minute threshold in overtime calculation.
    - Check-then-insert race conditions in employee creation and punch-in.
    - Off-by-one pagination bug in `list_employees` (`skip = page * page_size` skipped the first 20 records on page 1).
    - In-memory collection loading in `list_attendance` (replaced with indexed cursor pagination).
    - Unhandled `None` crashes and missing regex validations.
  - Documented all 18 bugs in [REVIEW.md](file:///e:/candidate_kit/REVIEW.md) in clear, plain developer language.

---

### Part B: The 12 Endpoints & Business Rules (R1 – R10)

| Endpoint | What was Expected | What was Implemented & Verified |
|---|---|---|
| **1. GET `/health`** | Return 200 only if MongoDB answers a ping; else 503. | Pings MongoDB via `client.admin.command("ping")`; returns 503 on database drop. |
| **2. POST `/employees`** | Create employee with unique `emp_code` (`^EMP\d{4,6}$`), server-set `created_at` (epoch ms), validate `shift_start != shift_end`. | Backed by unique index on `emp_code`. Catches `DuplicateKeyError` -> 409. Validates shift times and returns clean `Employee` schema. |
| **3. GET `/employees`** | Filter by department, paginated (1-indexed), sorted by `emp_code` ascending, total count reflecting filter. | Indexed cursor query with `.sort([("emp_code", 1)])`. `total` counts only the filtered department. |
| **4. POST `/attendance/punch-in`** | Create day's record. Concurrency-safe: duplicate punch-in returns 409. Apply R1 (IST dates, overnight shifts) and R2 (10-min grace). | Unique compound index on `(emp_code, date)`. Converts to IST. Handles overnight shifts where early AM punch-in maps to previous day. Strict epoch ms validation. |
| **5. POST `/attendance/punch-out`** | Close most recent punch-in (across midnight for overnight). Compute work hours (R4), overtime (R3), half-day (R5). Simultaneous punch-outs yield one 200 and one 409. | Atomic update `find_one_and_update({"_id": rec["_id"], "punch_out": None}, ...)`. Quantized half-up rounding for hours, >= 30 min check for OT, half-day boolean. |
| **6. GET `/attendance`** | Filter by employee, date range, status. Sorted by `date` desc, `emp_code` asc. Fast at 100k docs (no in-memory arrays). Validate `date_from <= date_to`. | Database-level compound sort and skip/limit. Returns 422 if `date_from > date_to`. Populates legacy missing fields (`half_day: False`, `history: []`). |
| **7. PATCH `/attendance/{emp_code}/{date}`** | Regularize attendance: recalculate derived fields, append audit trail to `history`. Concurrency-safe: concurrent edits must not lose history. Reject no-op changes. | Optimistic locking on history array length `{"history": {"$size": len(history)}}` (loser gets 409). Generates audit entry recording only changed fields. Validates presence and shift boundaries. |
| **8. GET `/analytics/employees/{emp_code}/monthly`** | Monthly aggregation for 1 employee: Mon-Fri working days from `joined_on`, present days (half-day = 0.5), leave count, late/OT totals. | In-database aggregation on `attendance_logs`. Working days count mid-month joiners accurately. Weekend logs add to late/OT without inflating present days. |
| **9. GET `/analytics/departments/summary`** | Cross-collection monthly aggregation: headcount includes zero-log employees (R9). Average work hours over records (not average of averages). | Aggregation starts from `employees` matching `joined_on <= month_end`, groups headcount, lookups `attendance_logs`, computes mean work hours. Omit 0-headcount departments. |
| **10. GET `/analytics/leaderboard/late`** | Month late-comers ranking: standard competition ranking with ties (1, 2, 2, 4). `limit` applied after ranking. Tied rows sorted by `emp_code` asc. | Uses MongoDB `$setWindowFields` with `$rank`. Filters `rank <= limit` after ranking. Joins `employees` for department/name. |
| **11. GET `/analytics/departments/{department}/trend`** | Daily series with zero gaps from `from` to `to` (including weekends). 7-day moving average over non-null rates. Performed in DB, not Python loop. | MongoDB `$densify` generates every single calendar day. Evaluates daily headcount, rate, and `$setWindowFields` 7-day moving average ignoring nulls. |
| **12. GET `/admin/explain/{endpoint}`** | MongoDB explain with `executionStats` on target endpoint's query/aggregation. Grader verifies `IXSCAN` and NO `COLLSCAN`. | Runs `explain` on the exact production pipelines for all 5 analytics/list query paths. Verified: every query uses `IXSCAN`. |

---

### Part C: Prove & Explain
- **What was expected**:
  - Create all required indexes idempotently on startup.
  - Implement `/admin/explain/{endpoint}` to prove query performance.
  - Write concise answers (~150–200 words) to 5 questions in `DECISIONS.md`.
- **What was done**:
  - Implemented `init_db_indexes()` triggered during FastAPI lifespan startup.
  - Configured indexes:
    - `employees(emp_code)` (unique)
    - `employees(department, joined_on)`
    - `attendance_logs(emp_code, date)` (unique)
    - `attendance_logs(date: -1, emp_code: 1)`
    - `attendance_logs(emp_code: 1, date: -1)`
    - `attendance_logs(status, date, emp_code)`
    - `attendance_logs(emp_code, punch_in: -1)`
    - `attendance_logs(date, late_minutes)`
  - Written [DECISIONS.md](file:///e:/candidate_kit/DECISIONS.md) in ~180 words addressing:
    1. Indexes created and why `history` indexing was rejected.
    2. Exact execution trace of simultaneous duplicate punch-in race conditions.
    3. Tie-breaking behavior with competition ranking (`$rank`) at cutoff.
    4. Headcount calculation logic preserving employees with zero attendance records.
    5. Architecture scaling strategy for 100x dataset (pre-aggregated rollups / change streams).

---

## 3. Business Rules Checklist (R1 – R10)

| Rule | Requirement | Implemented Logic |
|---|---|---|
| **R1** | IST Timezone & Overnight Shifts | Instants truncated to whole seconds. Overnight shift punch-ins earlier than `shift_end` roll back to previous calendar day. |
| **R2** | 10-Minute Grace Period | Late minutes counted only if punch-in strictly exceeds `shift_start + 10 mins`. Floor elapsed minutes from `shift_start`. |
| **R3** | Overtime Minimum | Overtime counted as floor minutes past shift end only if >= 30 minutes; otherwise 0. Handles overnight shift ends. |
| **R4** | Work Hours Rounding | `(punch_out - punch_in) / 3600` rounded half-up to 2 decimal places using Python `Decimal.quantize`. |
| **R5** | Half Day | Evaluated after punch-out: `work_hours < 4.50`. Counts as 0.5 of a present day. |
| **R6** | Presence Statuses | `PRESENT`, `WFH`, `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not. |
| **R7** | Working Days & Weekends | Mon-Fri working days counted from `joined_on`. Present days only count Mon-Fri records. Weekend records still add to late/OT totals. |
| **R8** | Reported Number Rounding | Quantized half-up: 2 decimals for counts/hours, 4 decimals for rates. |
| **R9** | Headcount Definition | Employees with `joined_on <= period_end`, including those with zero attendance logs. |
| **R10** | Pagination | 1-indexed `page`, default `page_size: 20` (max 100). Total reflects filtered set. |

---

## 4. Verification & Test Suite

The implementation was validated using 20 automated tests in `tests/`:
1. `test_health`: MongoDB ping probe response.
2. `test_create_and_list_employees`: Creation validation, regex enforcement, duplicate rejection, pagination.
3. `test_punch_in_and_punch_out`: Grace periods, double punch-out rejection, status updates.
4. `test_overnight_shift`: Shift crossover (22:00 - 06:00), midnight calculations, half-day flags.
5. `test_list_attendance`: Compound sorting (`date` desc, `emp_code` asc), date validation (`date_from > date_to` -> 422).
6. `test_regularize_attendance`: Audit trail logging, change diffing, rejection of no-op changes.
7. `test_employee_monthly_analytics`: Mid-month joiner working days, present day totals, attendance percentages.
8. `test_department_summary`: Cross-collection headcount, mean work hours, department isolation.
9. `test_late_leaderboard_and_ties`: Competition ranking (1, 1, 3), tie-breaker ordering, limit cutoff.
10. `test_department_trend`: Gap filling via `$densify`, weekend null rates, 7-day moving average.
11. `test_admin_explain_endpoints`: Verified `IXSCAN` present and `COLLSCAN` absent on all 5 query paths.
12. `test_openapi_schema_compatibility`: Verifies all paths and methods match `openapi.yaml`.
13. `test_rounding_half_up_exact`: Commercial half-up rounding boundary tests.
14. `test_concurrent_employee_creation`: Parallel duplicate creates (1 success, 4 conflicts).
15. `test_concurrent_punch_in`: Simultaneous duplicate punch-ins (1 success, 4 conflicts).
16. `test_concurrent_punch_out`: Simultaneous punch-outs on same record (1 success, 4 conflicts).
17. `test_concurrent_regularize`: Simultaneous corrections with optimistic locking.
18. `test_overtime_and_half_day_boundaries`: 29 vs 30 min overtime; 4.49 vs 4.50 hour cutoffs.
19. `test_weekend_attendance_records`: Weekend records adding to late/OT without inflating present days.
20. `test_department_with_zero_logs`: Zero-log department presence in trend and summary.

**Result**: **20 passed in 0.80 seconds** on MongoDB 8.0.
