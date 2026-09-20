# Handover: plan 3 sections B, C, D

## Done (this pass)
- Section C: LICENSE (AGPL-3.0) + `.assetsignore` (already on disk at
  handover); README rewrite (pitch intro, Licence, Data and its licence,
  Funding, Embedding and the API, Contributing sections); `CONTRIBUTING.md`;
  `.github/ISSUE_TEMPLATE/election-request.md`. Donate/sponsor footer link
  and "Supported by …" wired end to end (`config.py` → `main.py` →
  `js/api.js` → `js/main.js` → `index.html`, strings `footer.support`/
  `footer.sponsoredBy`). Data attribution note wired the same way
  (`data.attribution` string, `js/app.js`'s `renderAttributionNote`,
  `#attribution-note`). C6: `GET /api/elections` and `GET
  /api/elections/{hash}` now send `Cache-Control: public, max-age=60` and
  are documented as stable/embeddable in the README.
- B1 (pulled forward because it touches the same `ElectionSummary`):
  `election_key` — `identity.election_hash` without the forecast tuple — on
  every summary; `list_elections` sorted by `election_date` descending
  server-side; `js/api.js`'s `toSummary` maps `electionKey` (falls back to
  `electionHash` for an older backend). Tests added in `test_api.py` and
  `test/api.test.mjs`. 1046 pytest / 240 node green.

## Next concrete step
Section B, the rest: a search box above the calculator (client-side filter
on nation/state/title/year/publisher, accent- and case-insensitive),
grouping by `election_key` (newest poll or result preselected, older ones
one click deeper — this is D9, folded in per the owner's decision), keyboard
nav (arrows + enter), the "Not here yet? Ask for it" / "Import it" fallback
reusing `import-form.js`, and the default-election logic for a plain `/`
(nearest upcoming tracked election with a stored poll, else the most recent
result; bundled election is the pre-arrival render and offline fallback;
shared links bypass it). Split `js/import-ui.js`'s picker into `js/picker.js`
with `test/picker.test.mjs`, per the plan.

Then Section D: D2 and D9 are done (D9 via the B rework above). D1, D3-D8 are
explicitly deferred by the plan's own wording — no code needed for this
pass; leave them as-is in `doc/plans/03-remaining-work.md`.

## Decisions not to re-litigate
- Licence: AGPL-3.0, `LICENSE` and `CONTRIBUTING.md`/`.github` in
  `.assetsignore` (not the Cloudflare-published set — same pattern as
  README.md).
- Data attribution note shows on every election regardless of provenance,
  with one fixed string naming the source and noting CC BY-SA "where the
  source is Wikipedia" — no per-source hostname branching; simpler and still
  honest.
- Older polls stay in the picker after a result is final, grouped under the
  election (D9) — implement inside section B's picker rework.
- `election_key` fallback to `electionHash` client-side keeps a mixed old/new
  backend from crashing; don't remove it without checking the whole fleet is
  on the new backend.

## Dead ends / gotchas
- `~/.claude/todos/` writes are blocked on purpose — track progress here.
- `test/i18n.test.mjs`'s fake DOM in "the calculator speaks English" only
  registers a handful of element ids; any new element app.js looks up by id
  must be guarded with `if (!el.xyz) return;` the way `autoNote` already is,
  or that test's fake `getElementById` returns `undefined` and breaks it.
