# Handover: plan 3 section D, the rest

## Done (this pass)
- Section C (committed separately): licence, data attribution, donate/
  sponsor, README/CONTRIBUTING, embed API, `election_key` pulled forward.
- Section B, in full, with D9 folded in: `js/picker.js` (new) — search box,
  grouping by `election_key` (result leads, newest poll next, older polls
  one click deeper — D9), keyboard nav (arrows + enter, mouse for older
  polls), the "ask for it"/"import it" fallback (`parseQueryAsRequest` +
  `js/import-ui.js`'s new `requestOrImport`), and the default-election
  landing (`pickDefaultElection`: soonest upcoming election with a stored
  poll, else the most recent result; shared links bypass it).
  `js/import-ui.js` no longer renders a picker — it exposes
  `requestOrImport`/`isImportAllowed`/`isRequestAllowed` and a `refreshList`
  callback instead. `index.html`'s `<select id="picker">` became a
  search input + `<ul>` combobox (`#picker-search`/`#picker-results`).
  `test/picker.test.mjs` (30 tests) covers the pure functions and the DOM;
  six picker-only tests moved out of `test/import-ui.test.mjs`.
  1046 pytest / 266 node green.

## Next concrete step
Section D's remaining items (D1, D3, D4, D5, D6, D7, D8) are explicitly
deferred by the plan's own wording — D1/D3 wait on real-world timing (Plan 1
live a while / next secret rotation), D4/D5/D7/D8 are "watch and revisit"
notes with no code to write today, D6 waits on a non-Latin-script election
actually being tracked. **Nothing to implement here.** Confirm this reading
against `doc/plans/03-remaining-work.md`'s Section D before closing the
plan out, then this handover file (and its `.assetsignore` line, already
gone — LICENSE/CONTRIBUTING.md are the real entries) can be deleted for
good.

## Decisions not to re-litigate
- Licence: AGPL-3.0. Data attribution note is one fixed string, not
  per-source branching (see prior handover entry, still true).
- D9: implemented inside Section B's picker, not separately.
- The picker's fallback reuses `js/import-ui.js`'s form/network logic via
  `requestOrImport` rather than re-implementing request/import in
  `js/picker.js` — one code path for "type into the form" and "type into
  search and click ask for it".
- `js/picker.js` and `js/import-ui.js` reference each other's public methods
  through a forward-declared `let importUi` in `js/main.js`, the same
  pattern `adminUi` already used — neither closure runs until both exist.

## Dead ends / gotchas
- `~/.claude/todos/` writes are blocked on purpose — track progress here.
- Watch for stray literal Unicode combining characters when writing a regex
  with `̀`-style escapes through a tool call — one such write silently
  produced raw combining marks instead of the escape text in `js/picker.js`
  and had to be replaced with `/\p{Mn}/gu` (Unicode property escape,
  `u`-flagged). If a regex looks right in the tool call but wrong on disk,
  `cat -A` the line before trusting it.
- `test/i18n.test.mjs`'s fake DOM in "the calculator speaks English" only
  registers a handful of element ids; any new element `app.js` looks up by
  id must be guarded with `if (!el.xyz) return;` the way `autoNote` already
  is.
