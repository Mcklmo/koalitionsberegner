# Plan 3 — Remaining work: tracked elections, due-based refresh, the front door, funding, housekeeping

Handover plan for a Claude Code session, and the last of the four
([README.md](README.md) has the order). Depends on
[01-open-access.md](01-open-access.md) for the admin secret and the open list.
Section B's front door comes after [02-share-links.md](02-share-links.md) so
shared links open on the new picker. [04-reddit-outreach.md](04-reddit-outreach.md)
lands before this plan, so Section A's tracked elections are what finally stop
the outreach scan reporting elections the site does not hold.

Order of work: A (tracked elections and refresh), then B (front door), then C
(funding and open source, which is mostly writing) and D (housekeeping) as
time allows.

## A — Tracked elections and automated refresh

Today an election enters the store when a person asks for it and the owner
imports it, poll by poll, result by result. The goal: the app knows which
elections are coming, reads their polls on a schedule that tightens as the day
approaches, reads the result the night of the election and keeps re-reading
until it is stable, and then stops. Everything it stores is visible to
everyone at once.

### A1 — Data model: a tracked election

New table `tracked_elections` in `sqlite_store.py` (add it to the `CREATE
TABLE IF NOT EXISTS` block and the column-backfill loop) and a
`tracked_elections` collection in `firestore_store.py`, behind a new
`TrackedStore` protocol in `store.py` with an in-memory implementation for
tests. Wire it in `config.get_store` next to the election store.

| Field | Meaning |
| --- | --- |
| `request_key` | primary key, `identity.request_key(year, nation, subnation)`; ties it to the import machinery |
| `year`, `nation`, `subnation` | the request as a person would type it |
| `election_date` | ISO date from the calendar, corrected by the resolver on first run |
| `resolved` | JSON of the `ResolvedElection` from the first run, so refreshes skip the resolver |
| `status` | `upcoming`, `counting`, `final`, `parked` (given up), `untracked` (owner said stop) |
| `last_refresh_at`, `next_refresh_at` | UTC timestamps |
| `consecutive_failures`, `last_error` | for backoff and the report |
| `result_hash` | the stored election's hash once a result exists |
| `result_digest`, `unchanged_reads` | SHA-256 of the last result read and how many reads in a row matched it |
| `source_digest` | SHA-256 of the condensed source text last handed to the model (see A6) |
| `lease_until` | single-flight lease while a refresh runs |
| `added_by` | `calendar` or `owner` |

Add `provenance` to `elections` (`manual` default for existing rows, `auto`
for anything the refresh stores), expose it on `ElectionSummary`, and show a
one-line footnote for `auto` elections in `js/app.js`: "Imported automatically
from {source}. Report a problem", linking to a prefilled GitHub new-issue URL
with the label `data-problem`.

### A2 — The schedule, from a YAML file

The intervals are configuration, not code. File: `backend/app/refresh.yaml`
(inside the package: a top-level file would become a public URL under
Cloudflare's asset root and `test_cloudflare.py` would fail). Override its
path with `REFRESH_CONFIG`. Add `pyyaml` to `pyproject.toml`.

```yaml
# How often a tracked election is re-read. Durations are an integer and a
# unit, chained without spaces: 30d, 12h, 30m, 1d12h, 1h30m. Units: d h m.

before_election:        # chosen by the time left until 00:00 UTC on election day;
  - more_than: 180d     # the first row whose more_than is exceeded wins,
    every: 30d          # so keep them in descending order
  - more_than: 60d
    every: 14d
  - more_than: 14d
    every: 7d
  - more_than: 0d       # under 14 days
    every: 1d

after_election:         # chosen by the time since 00:00 UTC on election day;
  - within: 2d          # the first row whose within is not yet passed wins
    every: 30m          # election night: results firm up hour by hour
  - within: 45d
    every: 1d           # recounts, late mandates, corrections

results:
  stable_after: 3       # identical consecutive reads before the election is final
                        # (after that nothing is fetched again)
polls:
  keep_newest: 3        # forecasts stored per refresh, newest first; older
                        # polls already stored stay
failures:
  backoff_factor: 2     # the due interval is multiplied by this per consecutive failure
  max_backoff: 7d
  park_after: 8         # consecutive failures before the election is parked and reported
```

`backend/app/refresh_config.py`:

- `parse_duration("1d12h30m") -> timedelta`. Grammar: one or more of
  `\d+[dhm]`, each unit at most once, in any order, no spaces, total above
  zero. Reject anything else with a message that quotes the value.
- `RefreshConfig.load(path)` validates: rows sorted (descending `more_than`,
  ascending `within`), a `more_than: 0d` row present, every `every` at
  least the tick (`REFRESH_TICK`, default `30m`) or log a warning naming the
  row, positive integers where integers are expected, no unknown keys.
  `validate_configuration` loads it so a typo stops the boot.
- `interval_for(config, now, election_date) -> timedelta | None`:

  ```python
  day0 = datetime.combine(election_date, time.min, tzinfo=UTC)
  if now < day0:
      left = day0 - now
      for row in config.before_election:          # descending
          if left > row.more_than:
              return row.every
  since = now - day0
  for row in config.after_election:               # ascending
      if since <= row.within:
          return row.every
  return None                                     # past the last window
  ```

- `is_due(tracked, config, now)`: `next_refresh_at is None or now >= next_refresh_at`.
  After a run, `next_refresh_at = now + interval * backoff_factor ** consecutive_failures`,
  capped at `max_backoff`. When `interval_for` returns `None` and the
  election is not final, park it and say so in the report.

Tests in `test_refresh_config.py`: the duration grammar (valid, invalid,
duplicate units, zero), each window boundary (exactly 180 days, one second
either side of `day0`), the `None` past the last window, and a fixture YAML
that mirrors the table above.

### A3 — Cron plumbing

`wrangler.jsonc`:

```jsonc
"triggers": { "crons": ["0 6 * * *", "*/30 * * * *"] }
```

`worker/index.js` `scheduled(controller, env, ctx)`: dispatch on
`controller.cron`. `0 6 * * *` keeps calling `/api/internal/usage-reports`
(and, once a month, `/api/internal/calendar-scan`, see A5: trigger it on the
1st by checking the date in the handler, or add a third cron `0 5 1 * *`).
`*/30 * * * *` calls `POST /api/internal/refresh`. Both carry `x-report-secret`
as today; keep the secret's name to avoid rotating it, but rename the header
constant in the Worker to `SCHEDULE_SECRET_HEADER` for honesty. Cloudflare's
cron granularity and the YAML's smallest `every` should agree: 30 minutes
today. Tests in `test/worker.test.mjs` for the dispatch.

Backend, `main.py`:

- `POST /api/internal/refresh` behind `require_schedule`. Picks due tracked
  elections, oldest `next_refresh_at` first, at most `REFRESH_MAX_PER_TICK`
  (default 5), runs them sequentially and synchronously, and answers with
  counts: `{ "refreshed": n, "stored_polls": n, "stored_results": n,
  "finalised": n, "failed": n, "skipped_unchanged": n }`. Synchronous on
  purpose: Cloud Run throttles CPU after the response unless it is set to
  always-on, and the Worker's fetch waits happily for a minute. The cap keeps
  one tick inside Cloud Run's request timeout.
- `POST /api/internal/calendar-scan?years=2026,2027` behind `require_schedule`.
- Admin routes behind `require_admin`: `GET /api/admin/tracked` (the table),
  `POST /api/admin/tracked` (body `{year, nation, subnation?, election_date}`,
  adds one by hand; this is how the rollout starts), `PUT
  /api/admin/tracked/{request_key}` (`{status: "untracked"}` or a corrected
  `election_date`, or `{"refresh_now": true}` which sets `next_refresh_at`
  to now).

### A4 — What a refresh does

New `backend/app/refresh.py`, `RefreshService(tracked_store, election_store,
parser, config, clock)`. One `run(tracked) -> Outcome` per election:

1. **Lease.** `tracked_store.lease(request_key, until=now + 10 min)` in a
   transaction (Firestore) or `UPDATE ... WHERE lease_until < ?` (SQLite).
   No lease, no run: two instances or a double-fired cron must not read the
   same page twice.
2. **Resolve once.** If `resolved` is empty, run the resolver
   (`LlmElectionParser._resolve`) and store the JSON; on later runs skip it.
   This needs `LlmElectionParser` to expose `parse_resolved(resolved,
   request, *, want: Literal["polls", "results"])`, which is the body of
   `parse()` after `_resolve`. `want` overrides `_upcoming`: on election
   night the date is today, `_upcoming` says polls, and we want results.
3. **Before election day** (`now < day0`): `want="polls"`. Take the newest
   `polls.keep_newest` forecasts; each already has its own identity hash
   (`identity.election_hash` with the forecast tuple), so store the ones not
   yet stored via a new `ElectionStore.put_election(election_hash, election,
   provenance="auto")` that bypasses staging (the job table is for humans).
   Idempotent by construction.
4. **From election day** (`now >= day0`): `want="results"`. Then the gates,
   because nobody confirms this preview:
   - `parser.is_wanted(election, resolved, request)` (identity matches);
   - `sum(seats) == total_seats` (the schema checks this already; keep it
     explicit here);
   - `total_seats == resolved.assembly_seats` when the resolver knew it;
   - the source is Wikipedia or one of `resolved.sources`;
   - at least two parties.
   A failed gate is a failed run with `last_error` naming the gate; the
   result is not stored.
   Passing: compute `result_digest` over the canonical JSON of the election.
   Not stored yet: `put_election`, `status=counting`, `unchanged_reads=1`.
   Stored and different: `replace_election` (results firm up as counting
   completes; the hash does not change because the identity does not),
   `unchanged_reads=1`. Stored and identical: `unchanged_reads += 1`; at
   `results.stable_after`, `status=final` and `next_refresh_at=None`.
5. **Failures.** Any exception: `consecutive_failures += 1`, `last_error`
   from `service._user_message`, backoff as in A2, `parked` at
   `failures.park_after`. Success resets the counter.
6. **Release the lease** in a `finally`.

`RefreshService` reuses `io_span` for logging; sizes and hashes only, never
page text. Tests in `test_refresh.py` use `StubWikipedia`, the mock parser
and a fake clock: the poll path stores three new forecasts and nothing on the
second run; the results path stores, replaces on a changed read, finalises on
the third identical read, refuses a result whose total disagrees with the
resolver, backs off on failure and parks after eight.

### A5 — Calendar scan

`backend/app/calendar.py`, `CalendarScanner.upcoming(years) ->
list[CalendarEntry]` with `nation`, `state`, `election_date`, `title`,
`source_url`, `kind`.

Sources, in order of preference; verify each at implementation time, none is
guaranteed from memory:

1. **Wikidata**, via its SPARQL endpoint: items that are instances of a
   subclass of "legislative election" with a point in time between now and
   now plus two years, with their country and label. Machine-readable, CC0,
   no scraping. Coverage of future elections is uneven, hence source 2.
2. **Wikipedia's yearly electoral calendar articles** ("2026 national
   electoral calendar" and the following year), read through the existing
   `Wikipedia` client and its API (the `User-Agent` rule applies), parsed
   deterministically with BeautifulSoup as a first attempt, with the
   extraction agent and a strict `CalendarEntry` schema as the fallback when
   the table shape defeats the parser. The same fenced-data prompt discipline
   as `extractor.py` applies (see `doc/threat-model.md` T3, T4).
3. IFES ElectionGuide. The owner decided to use it (2026-09-19), because the
   project is becoming a non-profit funded by sponsors. Its data use policy
   allows personal and non-commercial use only. A non-profit that pays
   developers from sponsor money is not automatically "non-commercial" under
   such terms, so the owner confirms with IFES in writing before the scan
   reads it in production. Build it behind a switch that is off until then.

Filters: keep elections that fill an assembly with seats (national
legislatures; regional legislatures for an allowlist of federations, start
with Germany, Austria, Australia, Canada, Spain, Belgium, India); drop
presidential elections, referendums and local elections. De-duplicate by
`request_key` against tracked rows and by `find_by_place` against stored
elections. New rows start `status=upcoming`, `next_refresh_at=now`,
`added_by=calendar`. The first run of each costs one resolver call and the
poll reads; at a hundred elections that is a bounded one-off.

Oversight, so nothing silently piles up: the daily report gains a "Tracked
elections" section (added, refreshed, polls stored, results stored, finalised,
failing with their `last_error`, parked). The owner untracks or corrects via
the admin routes in A3.

### A6 — Keeping the bill small

These are requirements, not optimisations:

1. **No model call when the page did not change.** Before extraction, hash
   the condensed text (`wikipedia.condense_article` output, or the fetched
   page text otherwise) and compare with `source_digest`; equal means count
   the run as `skipped_unchanged`, increment `unchanged_reads` on the results
   path, and move on. Election night at 30-minute ticks is then a fetch every
   half hour and a model call only when the table changed.
2. **No resolver on a refresh** (A4 step 2).
3. **A cheaper model for refreshes**: `REFRESH_MODEL` environment variable,
   defaulting to the extractor's model, passed to `LlmElectionParser` for
   refresh runs only. The strict schema and the gates catch a weaker model's
   mistakes; try it on the poll path first.
4. **Caps**: `REFRESH_MAX_PER_TICK`, `polls.keep_newest`, `IMPORT_PAGE_LIMIT`
   as today, and the backoff.
5. Later, once a few countries' polling tables are familiar: parse Wikipedia
   polling tables deterministically and call the model only when parsing
   fails. Not part of this plan's first delivery.

### A7 — Rollout

1. Deploy with the cron in place but no tracked rows; the tick answers all
   zeros.
2. Add three elections by hand through `POST /api/admin/tracked`: one months
   away, one within two weeks, one already held this year (to exercise the
   results path immediately). Watch the report for a week.
3. Run the calendar scan once by hand, read its proposal in the report,
   untrack what does not belong, then let the monthly cron own it.

## B — The front door: search, grouping, a sensible default

The dropdown does not scale past a dozen entries and hides the one thing the
new funnel is about: finding any election. Files: `js/import-ui.js` (split
the picker out into `js/picker.js`), `index.html`, `js/strings.csv`,
`backend/app/main.py` (`ElectionSummary`).

1. `ElectionSummary` gains `election_key` (the identity hash computed without
   the forecast tuple, so every poll of one election shares it),
   `provenance`, and the list is sorted by `election_date` descending
   server-side.
2. A search box above the calculator filters the summaries client-side on
   nation, state, title, year and publisher, accent- and case-insensitive.
   Results are grouped by `election_key`: the election as the row, its
   newest poll or its result preselected, older polls one click deeper.
   Keyboard: arrows and enter. The current election stays rendered while
   searching.
3. No match: an inline "Not here yet? Ask for it" that reuses
   `import-form.js` to parse a year and a place out of the query and files
   the request (Plan 1 Phase A made that open to everyone). The owner in
   admin mode sees "Import it" instead.
4. Default election on a plain `/`: the nearest upcoming tracked election
   that has a stored poll; failing that, the most recent result. The bundled
   `providers/folketing-2026.js` stays as the render before the list arrives
   and as the offline fallback. Shared links (Plan 2) bypass this.
5. Tests: `test/picker.test.mjs` for filtering, grouping and keyboard;
   `test_api.py` for `election_key` and ordering.

## C — Funding and open source

Mostly writing; the conclusions are from the conversation that produced these
plans. No ads: they pay badly on political content, need a consent banner for
anything personalised, and cost the one differentiator the page has.

1. **Licence.** AGPL-3.0 for the code, decided by the owner (it is a hosted
   service, and AGPL keeps a fork that offers it as a service open). Add
   `LICENSE` and a licence line in the README. Note that a `LICENSE` file at
   the repo root must be added to `.assetsignore` or `test_cloudflare.py`
   fails.
2. **Data attribution.** Elections read from Wikipedia carry CC BY-SA
   obligations: keep `source_url` visible (it already is in the footer of
   each election), add a `data.attribution` string under the calculator
   ("Figures from {source}, CC BY-SA where Wikipedia") and a paragraph in the
   README about the data licence. This matters for any later commercial
   licensing of the data: derived data stays share-alike.
3. **Outreach.** The `outreach` plugin and
   [04-reddit-outreach.md](04-reddit-outreach.md) cover the one marketing
   channel this project has: answering coalition questions where they are
   asked, with disclosure and one-by-one approval. Read that plan's "Before you
   switch this on" before spending time on it.
4. **Donate and sponsor.** A `support.link` string and a single link in the
   footer (GitHub Sponsors, Ko-fi or MobilePay, owner's choice) and an empty
   `support.sponsor` slot ("Supported by …") that stays hidden until set.
5. **Grants to apply to**, with the open-source repository and the privacy
   stance as the pitch: NLnet (NGI Zero), Prototype Fund (Germany, needs a
   resident applicant), the EU's Next Generation Internet calls, and Danish
   democracy or digitisation foundations. Draft the one-page pitch from the
   README.
6. **Business-to-business door, kept open but not built.** The
   `ElectionSummary` and `Election` JSON are already the shape an embed or an
   API would serve; document the endpoints as stable and add a
   `Cache-Control` and CORS story (`ALLOWED_ORIGINS` exists) so a newsroom
   could embed. Build nothing further until somebody asks.
7. **README rewrite** as a project pitch: what it is, why a single tool for
   every election, how data gets in (requests, owner imports, the tracked
   calendar), how to run it, how to contribute an election or a country's
   seat rules, the licence and the data licence. Add
   `.github/ISSUE_TEMPLATE/election-request.md` with the `election-request`
   label that `wishlist.LABEL` files under, and `CONTRIBUTING.md` pointing at
   `doc/contribute.md`.

## D — Housekeeping and open questions

1. Remove `selected` from the store protocol, the three implementations and
   their tests once Plan 1 has been live for a while; leave the column.
2. Remove `Job.owner` and the `owner` parameter of `store.claim`.
3. Rename `USAGE_REPORT_SECRET` to `SCHEDULE_SECRET` when the secret is next
   rotated; until then only the Worker constant is renamed (A3).
4. Workers plan: `/e/*` (Plan 2) and the 30-minute cron add Worker requests.
   Watch the free plan's daily request count for a month after both ship;
   Workers Paid is the answer if it gets close.
5. Cloud Run: stay at zero minimum instances. The cron's cold starts are
   fine; only if the refresh tick regularly exceeds the request timeout
   should `REFRESH_MAX_PER_TICK` drop or the tick become more frequent.
6. Fonts for the preview image (Plan 2): add Noto Sans subsets for Arabic,
   Hebrew, Devanagari and CJK, chosen per string by script detection in
   `og_image.py`, once an election in one of those scripts is tracked.
7. A dark variant of the preview image is not worth it: platforms render
   the card on their own background and do not signal a theme to the image
   URL.
8. Static JSON offload (publishing each election as a file on Cloudflare so
   the public path never touches Cloud Run) is deferred. Revisit when Cloud
   Run cost or latency shows up in practice; the API and the Worker cache
   cover the expected load.
9. Older polls of a tracked election stay in the picker after the result is
   final, grouped under the election (Section B), because "what did the polls
   say" is part of the story. Decided by the owner.
