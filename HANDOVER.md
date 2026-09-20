# Handover — plan 3 section A (A7 left)

Scope: A1, A3, A4, A6, A7 only. B/C/D belong to another agent.

## Done (this session)
- A1 frontend, A4's `RefreshService`, A6's cheaper-model wiring: see prior
  commits (`07484de`, `059b32b`).
- A3, fully wired:
  - `wrangler.jsonc`: three crons (`0 6 * * *`, `*/30 * * * *`,
    `0 5 1 * *`), documented inline.
  - `worker/index.js`: `scheduled` dispatches on `controller.cron`
    (`DAILY_CRON`/`REFRESH_CRON`/`CALENDAR_SCAN_CRON` exported constants);
    `REPORT_SECRET_HEADER` renamed to `SCHEDULE_SECRET_HEADER` (header value
    unchanged: `x-report-secret`); `calendarScanYears()` computes the
    `years=` query from the clock. `test/worker.test.mjs`: 12 new/updated
    cases.
  - `backend/app/main.py`: `POST /api/internal/refresh` (drains
    `due_tracked` up to `refresh_max_per_tick()` through `RefreshService`,
    answers the six counts), `POST /api/internal/calendar-scan?years=`
    (runs `CalendarScanner.scan`, tracks every surviving entry,
    `added_by="calendar"`), `GET/POST /api/admin/tracked`, `PUT
    /api/admin/tracked/{request_key}` (untrack / correct the date /
    `refresh_now`, which reactivates a parked row). `backend/app/config.py`
    gained `get_calendar_scanner()`. Tests: `backend/tests
    /test_tracked_routes.py` (17 cases).
- A6 is now fully exercised end to end through the route (nothing left).

## Next concrete step
1. A7: rollout steps into `doc/contribute.md` — deploy the cron with no
   tracked rows first (watch the report answer all zeros), add three
   elections by hand through `POST /api/admin/tracked` (one months away, one
   within two weeks, one already held this year), watch a week, then run
   `POST /api/internal/calendar-scan` once by hand and read its proposal
   before letting the monthly cron own it. Also fold in: the new
   `firestore.indexes.json` composite index's deploy note (`gcloud firestore
   indexes composite create`, mirroring the outreach ones already
   documented there); `GITHUB_ISSUES_REPO` is reused for the A1 issue link
   (no new env var); the IFES-not-wired-in note (already in `calendar.py`'s
   own docstring — link to it rather than re-explain).
2. Nothing else from A1/A3/A4/A6 is outstanding. B/C/D remain another
   agent's.

## Decisions — do not re-litigate
- `RefreshService`'s store calls are synchronous, not `run_in_threadpool`-
  wrapped like `ImportService`'s — the plan calls the refresh tick
  "synchronous on purpose" and it is capped at `REFRESH_MAX_PER_TICK` rows.
  Flagged for review, not changed.
- Tracked timestamps are epoch floats in storage, tz-aware UTC datetimes in
  the dataclass.
- `HANDOVER.md` is listed in `.assetsignore`; delete both in the final commit.

## Test lines
`cd backend && uv run pytest -q` → 1033 passed.
`node --test test/*.test.mjs` → 238 pass.
