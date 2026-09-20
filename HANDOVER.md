# Handover: plan 3 sections B, C, D + 2 carried-over items

## Done
- Merged plan 3 section A (691a9d0) into this branch.
- D2: removed dead `Job.owner` / `store.claim(..., owner=)` (always `None` in
  prod). Left the `owner` DB columns in place (sqlite/firestore), matching the
  `selected` precedent — just stopped writing meaningful values.
- Carried-over item 2: IFES ElectionGuide switch. `IFES_ELECTIONGUIDE=on|off`
  (default off, config.py), `IfesElectionGuide` + `parse_ifes_electionguide`
  in `app/calendar.py`, wired into `CalendarScanner` and
  `config.get_calendar_scanner`. Docs updated: `.env.example`,
  `doc/contribute.md` (env table + the two rollout paragraphs). The parser is
  explicitly unverified — no code here has ever fetched a real IFES page, on
  purpose. Tests offline throughout (MockTransport only). 1038 pytest / 238
  node green.

- Carried-over item 1: daily report "Tracked elections" section. Four new
  `UsageEvent`s (`tracked_added`, `tracked_refreshed`, `tracked_failed`,
  `tracked_parked`) recorded in `main.py`'s `run_refresh` (per due row) and
  `run_calendar_scan` (per row actually added), rendered as a new section in
  `usage.py`'s `SECTIONS`. `doc/contribute.md`'s stopgap sentence rewritten to
  say what the report now does; the admin table is still what names *which*
  row. 1042 pytest / 238 node green.

## Next concrete step
Section C (mostly writing): LICENSE (AGPL-3.0) + `.assetsignore` entry +
README licence line; data attribution (`data.attribution` string under the
calculator, CC BY-SA paragraph in README) — needs `js/strings.csv` +
`index.html` in both languages per the house rule; donate/sponsor footer
link; README rewrite as a pitch; `CONTRIBUTING.md` +
`.github/ISSUE_TEMPLATE/election-request.md` (label from
`app.wishlist.LABEL`). Then section D9 (older polls stay in the picker,
grouped under the election) folded into section B's picker rework — the
biggest remaining piece: `ElectionSummary.election_key`/`provenance`, a
search box, grouping, keyboard nav, "ask for it" fallback, default-election
logic. Consider splitting `js/import-ui.js`'s picker into `js/picker.js` as
the plan suggests, with `test/picker.test.mjs`.

## Decisions not to re-litigate
- Licence: AGPL-3.0. LICENSE goes in `.assetsignore` (like README.md), not
  the Cloudflare-published set.
- Older polls stay in the picker after a result is final, grouped under the
  election (D9) — implement inside section B's picker rework.
- IFES: nothing may fetch it; the parser/class is written without ever
  fetching a real page (that itself would be an IFES read). Documented as
  unverified against the live site; verify before flipping the switch.
- D1 (drop `selected`), D3 (rename `USAGE_REPORT_SECRET`), D4-D8: explicitly
  deferred by the plan's own wording (not this pass's job).

## Dead ends / gotchas
- `~/.claude/todos/` writes are blocked by the sandbox for this task on
  purpose — don't retry; track progress in this file instead.
- Section A already returns refresh/scan counts as HTTP responses
  (`RefreshRun`, `CalendarScanOut`) but never records `UsageEvent`s for them —
  the "Tracked elections" report section needs new usage events recorded at
  those two route handlers.
