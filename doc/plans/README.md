# Plans: read this first

Four handover plans for turning koalitionsberegner from a paywalled Danish
calculator into a free, account-free tool for elections anywhere, with shareable
links, automatic imports and one honest marketing channel.

**Execution order, decided by the owner: 1 first, then 2 and 4 together, then
3.** Plan 1 runs alone because it rewrites most of the shared files. Plans 2 and
4 then run concurrently; their overlap is five files and the protocol for them
is in plan 4's "Running this beside plan 2". Plan 3 comes last.

| # | Plan | What it does | Needs |
| --- | --- | --- | --- |
| 1 | [01-open-access.md](01-open-access.md) | Removes accounts, sign-in, Stripe and curation. Opens the list and the requests to everyone. Puts the owner's imports behind `ADMIN_SECRET`. | nothing |
| 2 | [02-share-links.md](02-share-links.md) | Puts the coalition in the URL. Renders a preview image of the seat distribution so a pasted link unfurls into it. | 1, Phase B |
| 4 | [04-reddit-outreach.md](04-reddit-outreach.md) | The approval queue, the emailed one-time link and the posting for the `outreach` plugin. | 1 Phase C. Runs beside 2, needing nothing from it |
| 3 | [03-remaining-work.md](03-remaining-work.md) | Tracked elections and the YAML-configured refresh, the search front door, funding, housekeeping. | 1; Section B after 2 |

## What can run beside the plan in progress

The limit is file collisions, not logic. Four plans touch
`backend/app/main.py`; three each touch `wrangler.jsonc`, `worker/index.js`,
`js/strings.csv`, `js/import-ui.js`, `index.html` and `backend/pyproject.toml`.
Plan 1 Phase D is the worst: it deletes three frontend modules, rewrites
`js/main.js` and removes dozens of rows from the strings sheet.

So only **new-file work** parallelizes safely. These are complete and testable
on their own, and a second session can build them while plan 1 runs. They are
also where each of plans 2 and 4 should start, so their concurrent wave spends
as long as possible out of each other's way:

- Plan 2 WP1's `backend/app/share.py` and `backend/app/og_image.py`, with the
  fonts and their tests, and no route wiring.
- Plan 3 A2's `backend/app/refresh_config.py` and `refresh.yaml`, with tests.
- Plan 3 A5's `backend/app/calendar.py`.
- Plan 4's `backend/app/reddit.py`.

Wiring those modules into `main.py`, the Worker or the page is where plans 2
and 4 meet. Keep those edits small and late, append routes as one contiguous
block per plan, and let whichever branch lands first factor out the Worker's
`servePage` helper so the second one calls it rather than copying it.

## Rules for every session

- **Run both suites before you push.** `cd backend && uv run pytest` and
  `node --test test/*.test.mjs`.
- **`backend/tests/test_cloudflare.py` guards two things.** A new top-level file
  becomes a public URL unless `.assetsignore` lists it, and `_headers` must
  equal `main.SECURITY_HEADERS`. Keep new files under `backend/`, `js/`, `doc/`
  or `test/`, and change the two header lists together.
- **One session owns `backend/pyproject.toml` and `uv.lock` per wave.** Plan 1
  removes Stripe, plan 2 adds Pillow, plan 3 adds PyYAML. Three sessions
  regenerating the lock produce a conflict nobody wants to resolve by hand.
- **Two assertions in `backend/tests/test_cloudflare.py` bite new pages.** It
  asserts `_headers` flattens to exactly `main.SECURITY_HEADERS`, so a
  path-specific header block fails it; set such a header on the Worker's
  response instead. It also asserts the repo root publishes nothing but
  `index.html`, `js` and `_headers`, so a new top-level page must be added to
  that set in the same commit.
- **`js/strings.csv` and `index.html` change together.** `test/i18n.test.mjs`
  fails on a key used in markup but missing from the sheet, and on Danish markup
  that differs from the sheet. Every new string needs both columns.
- **Texts live in the sheet, never in a module.** Commit `88ae210` moved them
  all there; do not add a hardcoded sentence back.
- **Untrusted input stays untrusted.** Pages, polls and Reddit text are fenced
  as data in every prompt, validated against the canonical schema, and reach the
  DOM as text. `doc/threat-model.md` is the contract; each plan says which
  section it extends.

## Already in the tree

- `plugins/outreach/` is a working Claude Code plugin, not a plan. It scans
  subreddits with a local model, verifies with Claude, drafts replies and queues
  them. Seventeen offline tests pass:
  `uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts`.
  It posts nothing. Plan 4 builds the half that does.
- The app itself lives on
  `Feature--Import-any-election-by-pasting-its-official-results-URL`, not on
  `main`, which still holds the three-commit static page.

## Open decisions for the owner

These are named in the plans and are not a session's to make: the licence
(recommendation AGPL-3.0), whether older polls stay in the picker after a result
is final (recommendation keep), and the Stripe and Firebase cleanup in plan 1
Phase E, which only the account holder can do.
