# Handover — plan 3 section A (A3, A7 left)

Scope: A1, A3, A4, A6, A7 only. B/C/D belong to another agent.

## Done (this session)
- A1 frontend finished and wired end to end (provenance footnote, issue link).
- Leftover wiring: `"wikidata"` in `NOTABLE_SYSTEMS`; `Wikipedia._article` is
  now public `Wikipedia.article`; IFES already off by construction in the
  merged `calendar.py` (its own docstring explains why).
- A4: `backend/app/refresh.py` — `RefreshService` (lease, resolve-once, polls
  vs results by election day, the four gates, digest-based stability,
  backoff/park via `refresh_config.next_refresh_at`). `LlmElectionParser`
  gained `parse_resolved(resolved, request, *, want)` and
  `peek_source(resolved, *, want)` (app/parser.py). Tests: `test_refresh.py`
  (11 cases, offline, `FakeRefreshParser`).
- A6: `config.get_refresh_parser()` (REFRESH_MODEL, reuses `_build_parser`);
  A6.1's unchanged-page skip lives in `RefreshService._refresh_polls`/
  `_refresh_results` via `peek_source`; A6.2 is `_resolved_for`'s "once,
  ever"; A6.4's caps (`REFRESH_MAX_PER_TICK`, `keep_newest`) are consumed by
  `RefreshService`/the route — the route (A3) still needs to slice
  `due_tracked(now, limit)` and pass `keep_newest` through the config it
  already threads in.
- Deliberate simplification, flag for review: `RefreshService`'s store calls
  are synchronous, not `run_in_threadpool`-wrapped like `ImportService`'s —
  the plan calls the refresh tick "synchronous on purpose" and it is capped
  at `REFRESH_MAX_PER_TICK` rows, but this differs from the codebase's usual
  pattern for blocking Firestore calls inside async code. Worth a second look.

## Next concrete step
1. A3: wrangler crons (`0 6 * * *`, `*/30 * * * *`), `worker/index.js`
   `scheduled(controller, env, ctx)` dispatch, rename the Worker's header
   constant to `SCHEDULE_SECRET_HEADER` (keep the header name
   `x-report-secret`). Backend: `POST /api/internal/refresh` (behind
   `require_schedule`, picks `due_tracked` oldest-first up to
   `refresh_max_per_tick()`, runs `RefreshService(get_tracked_store(),
   get_store(), get_refresh_parser(), get_refresh_config()).run()`
   sequentially, returns the counts dict the plan names), `POST
   /api/internal/calendar-scan?years=`, and the `/api/admin/tracked` routes
   (GET list, POST add, PUT update/untrack/refresh_now). `test/worker.test.mjs`
   needs the cron-dispatch tests too.
2. A7: rollout steps + the IFES note + the Firestore composite index deploy
   note, in `doc/contribute.md`.
3. Re-check A6 is fully satisfied once the route exists (nothing else should
   be needed — it was designed to fall out of A4's shape).

## Decisions — do not re-litigate
- Tracked timestamps are epoch floats in storage, tz-aware UTC datetimes in
  the dataclass.
- `due_tracked` filters dueness in Python, not in the Firestore query.
- `HANDOVER.md` is listed in `.assetsignore`; delete both in the final commit.

## Test lines
`cd backend && uv run pytest -q` → 1016 passed.
`node --test test/*.test.mjs` → 234 pass.
