# Handover — plan 3 section A (A1, A3, A4, A6, A7)

Scope: A1, A3, A4, A6, A7 only. B/C/D belong to another agent. A2
(`refresh_config.py`, `refresh.yaml`) and A5 (`calendar.py`) were already merged —
wire them in, never rebuild them.

## Done
- A1 backend: `TrackedElection`/`TrackedStatus`/`TrackedStore` + `InMemoryTrackedStore`
  in `store.py`; `SqliteTrackedStore`; `FirestoreTrackedStore`; composite index
  `tracked_elections (status, next_refresh_at)` in `firestore.indexes.json`;
  `provenance` on `StoredElection`/`ElectionSummary`; `put_election` on all four
  stores (incl. `cached_store`); `config.get_tracked_store/get_refresh_config/
  refresh_max_per_tick/refresh_model`; `validate_configuration` now loads the
  schedule; `pyyaml` in pyproject + `uv lock`. Tests: `tests/test_tracked_store.py`.

## Next concrete step
1. A1 frontend: `provenance` footnote in `js/app.js` (+ `index.html`, both columns
   of `js/strings.csv`), GitHub new-issue link with label `data-problem`.
2. A3: wrangler crons `0 6 * * *` + `*/30 * * * *`, worker `scheduled` dispatch
   (rename constant to `SCHEDULE_SECRET_HEADER`, keep header value `x-report-secret`),
   `POST /api/internal/refresh`, `POST /api/internal/calendar-scan?years=`,
   `GET|POST /api/admin/tracked`, `PUT /api/admin/tracked/{key}`.
3. A4: `backend/app/refresh.py` + `LlmElectionParser.parse_resolved(resolved,
   request, *, want)`.
4. Wiring left: `"wikidata"` into `observability.NOTABLE_SYSTEMS`; make
   `Wikipedia._article` public; IFES switch OFF by default (owner decision
   2026-09-19) — put it in `calendar.py`, documented in `doc/contribute.md`.
5. A7 rollout steps into `doc/contribute.md` (+ the Firestore index deploy note).

## Decisions — do not re-litigate
- Tracked timestamps are epoch floats in storage, tz-aware UTC datetimes in the
  dataclass; `check_fields` refuses a naive datetime (two backends would disagree).
- `due_tracked` filters dueness in Python, not in the Firestore query: an
  inequality on `next_refresh_at` would drop exactly the never-scheduled rows.
- `HANDOVER.md` is listed in `.assetsignore` so `test_cloudflare.py` passes;
  delete both the file and that line in the final commit.

## Test lines
`cd backend && uv run pytest -q` → 1003 passed. `node --test test/*.test.mjs` → 232 pass.
