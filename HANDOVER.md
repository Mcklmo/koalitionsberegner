# Handover — plan 3 section A (A3, A4, A6, A7 left)

Scope: A1, A3, A4, A6, A7 only. B/C/D belong to another agent.

## Done (this session)
- A1 frontend finished and wired end to end: `provenance` footnote in
  `js/app.js`/`index.html`/`js/strings.csv` (both languages), GitHub
  new-issue link (label `data-problem`); `config.issues_url()` +
  `PublicConfig.issues_url` in `main.py`; `js/api.js` maps it (https +
  github.com only) and maps `provenance` on summaries; `js/main.js` now
  actually assigns `issuesUrl`/`summaries` (were dead placeholders) and
  passes `electionHash` through both `selectElection` and the shared-link
  path so `provenanceOf` resolves. Tests added: `test_config.py`,
  `test_api.py` (backend), `test/api.test.mjs` (frontend, +2 cases).
- Leftover wiring: `"wikidata"` added to `observability.NOTABLE_SYSTEMS`;
  `Wikipedia._article` renamed to public `Wikipedia.article` (docstring
  added), `calendar.py`'s `WikipediaArticles.read` now calls it directly,
  its old "left for when this is wired in" comment removed.
- IFES: already off by construction in the merged `calendar.py` (item 3's
  docstring: "IFES ElectionGuide is not used" until the owner has it in
  writing) — nothing further to switch. Still need: a line in
  `doc/contribute.md` saying so (A7 step below covers rollout docs).

## Next concrete step
1. A3: wrangler crons, worker `scheduled` dispatch (`SCHEDULE_SECRET_HEADER`),
   `/api/internal/refresh`, `/api/internal/calendar-scan`, `/api/admin/tracked`
   routes (GET/POST/PUT).
2. A4: `backend/app/refresh.py` (`RefreshService`) + `LlmElectionParser
   .parse_resolved(resolved, request, *, want)`.
3. A6: verify caps/skip-unchanged land naturally out of A4's design; add
   `REFRESH_MODEL`, `REFRESH_MAX_PER_TICK` if not already in config.py.
4. A7: rollout steps + IFES note + Firestore index deploy note in
   `doc/contribute.md`.

## Decisions — do not re-litigate
- Tracked timestamps are epoch floats in storage, tz-aware UTC datetimes in the
  dataclass; `check_fields` refuses a naive datetime.
- `due_tracked` filters dueness in Python, not in the Firestore query.
- `HANDOVER.md` is listed in `.assetsignore`; delete both in the final commit.

## Test lines
`cd backend && uv run pytest -q` → 1004 passed.
`node --test test/*.test.mjs` → 234 pass.
