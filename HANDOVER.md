# Handover: plan 3 sections B, C, D + 2 carried-over items

## Done
- Merged plan 3 section A (691a9d0) into this branch.
- D2: removed dead `Job.owner` / `store.claim(..., owner=)` (always `None` in
  prod). Left the `owner` DB columns in place (sqlite/firestore), matching the
  `selected` precedent — just stopped writing meaningful values. Both suites
  green (1023 pytest, 238 node).

## Next concrete step
Implement carried-over item 2: IFES ElectionGuide switch in
`backend/app/calendar.py` (`IFES_ELECTIONGUIDE=on|off`, default off; new
`IfesElectionGuide` class + `parse_ifes_electionguide`, wired in
`config.get_calendar_scanner`). Then carried-over item 1 (daily report
"Tracked elections" section). Then section C (LICENSE, README, data
attribution, funding/support, grants pitch, README rewrite,
CONTRIBUTING.md/issue template). Then section D9 + section B (front door
search/grouping) — biggest remaining piece.

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
