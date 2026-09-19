# Plan 2 — Shareable links with a preview image of the coalition

Handover plan for a Claude Code session. Goal: a visitor picks parties, copies
one link, and that link (a) restores exactly that selection for whoever opens
it and (b) unfurls in Reddit, Facebook, Slack, Discord, Signal, iMessage and
the rest into an image that shows the seat distribution they built, the total,
and whether it is a majority.

Second of the four plans ([README.md](README.md) has the order). Depends on
[01-open-access.md](01-open-access.md) Phase B: link previews are fetched by
crawlers that never sign in, so the election behind a link must be readable
anonymously. The two pure modules in WP1 can be written before plan 1 lands.
[03-remaining-work.md](03-remaining-work.md) lists the follow-ups this plan
leaves open (glyph coverage, a dark image variant).

## How unfurling works, in three sentences

When a link is pasted, the platform's crawler requests the URL, reads the
`<meta property="og:...">` tags in the HTML it gets back, and then fetches the
image URL named in `og:image`. It sends the path and the query string but never
the `#fragment`, so the selection has to live in the path or the query. Most
platforms want PNG or JPEG at about 1200×630 and ignore SVG, so the image has
to be rasterised somewhere on our side.

## Decisions already made

1. **State in path and query, never in the hash.** `/e/<id>?c=<parties>&s=<seats>`.
2. **`<id>` is a prefix of the election hash**, the first 16 hex characters.
   Backend lookups accept any prefix of 12 to 64 characters and answer `409`
   if it is ambiguous (with a 12-character floor that is not going to happen).
   No slug table, nothing to keep unique, stable for the life of the election.
3. **`c` is the list of selected parties as positions** in the order the
   renderer lists them, block by block, flattened: `c=0,2,3,7`. Ascending,
   unique, comma-separated. Positions are what `js/app.js` already keys its
   selection on, and they are the only party identifier the schema guarantees
   (abbreviations can repeat).
4. **`s` is a checksum**: the seat total of the selection when the link was
   made. A result never changes and each poll is its own election, so a
   mismatch only happens after a correction via `store.replace_election`.
   On mismatch the page keeps the selection and shows one line saying the
   numbers changed since the link was made.
5. **The image is rendered by the backend with Pillow**, at
   `GET /api/og/<id>.png?c=&s=`, and cached by the Worker. Not in the Worker:
   a Satori plus resvg render costs far more CPU than the Workers free plan
   allows per request, and it would need a second copy of the election data
   and of the wording. The backend already holds both; Pillow is boring.
6. **The `<meta>` tags are injected by the Worker into the asset copy of
   `index.html`**, by plain string replacement on `</head>` and `<title>`
   (not `HTMLRewriter`, which does not exist in Node and would make the Worker
   tests need a Workers runtime). The wording comes from one backend endpoint,
   `GET /api/elections/<id>/card`, so the PNG and the tags never disagree and
   `index.html` never drifts from `js/` (the Cloud Run image carries its own
   copy of the page; the Worker must not serve that one).
7. **Card text is English** whatever the visitor's language. Crawlers have no
   language; the page itself stays translated.
8. **The bundled Denmark election is shareable through its stored twin.**
   `js/main.js` renders `providers/folketing-2026.js` first as the offline
   fallback; once the list is loaded, the summary with the same nation and
   election date and no forecast is its id for links. If the list has no
   twin, the share button is hidden for it.

## URL grammar

```
/e/{id}                     id: [0-9a-f]{12,64}
   ?c={i}(,{i})*            i: integer 0..(parties-1); missing or empty = nothing selected
   &s={n}                   n: integer 0..100000; optional
```

Parsing rules, same in `js/share.js` and `backend/app/share.py`: unknown
parameters are ignored; a malformed `c` is treated as empty and the election
still renders; an index past the last party is dropped; an unknown `id` makes
the page show its default election with a `share.unknown` message, and the
Worker serves the generic tags for it.

## Work packages

### WP1 — Backend: prefix lookup, card, PNG

Files: `backend/app/service.py`, `backend/app/main.py`, new
`backend/app/share.py`, new `backend/app/og_image.py`, new
`backend/app/fonts/`, `backend/pyproject.toml`, tests `test_share.py`,
`test_og_image.py`, additions to `test_api.py`.

1. `ImportService.resolve_id(prefix) -> StoredElection | None`, raising a
   dedicated `AmbiguousId` when more than one stored election starts with
   it. Implement over `list_elections()` (already behind the 30 s cache); an
   exact 64-character hash short-circuits to `get_stored`. `GET
   /api/elections/{election_hash}` uses it (`409` on ambiguity, `422` on a
   prefix under 12 characters or non-hex).
2. `share.py`, pure functions, no I/O:
   - `parse_selection(c: str | None, election: Election) -> list[int]` with
     the rules above, bounded by `MAX_BLOCKS * MAX_PARTIES_PER_BLOCK` from
     `schema.py`.
   - `parse_seats(s: str | None) -> int | None`.
   - `flatten(election) -> list[Party]` in renderer order.
   - `Card` dataclass: `title`, `description`, `image_path`, `page_path`,
     `total`, `majority`, `stale: bool`, `parties: list[tuple[abbr, seats,
     color]]`.
   - `card(election, election_hash, selection, seats_claimed) -> Card`.
     Description wording (English, one line, at most 200 characters):
     - nothing selected: `Pick parties and see whether they reach the 90 seats a majority needs. Final result, 25 Mar 2026.`
     - short: `A + F + B + Ø: 79 of 179 seats, 11 short of a majority. Final result, 25 Mar 2026.`
     - majority: `A + M + V + C: 95 of 179 seats, a majority (+5). Forecast: Voxmeter, 12 Sep 2026, seats computed.`
     - more than eight parties: name eight, then `+3 more`.
3. `GET /api/elections/{id}/card?c=&s=` returns the `Card` as JSON with
   `Cache-Control: public, max-age=3600`. Record `UsageEvent.LINK_OPENED` on
   it (a new counter; the Worker calls it once per `/e/*` page view, crawlers
   included, so the report line should say so).
4. `og_image.py`: `render(card: Card) -> bytes`, a 1200×630 PNG. Fonts:
   Noto Sans Regular and Bold, OFL, committed under `backend/app/fonts/` with
   their licence file, declared as package data in `pyproject.toml`
   (`[tool.setuptools.package-data] app = ["fonts/*"]`); the Dockerfile
   already copies `backend/app` whole. Add `pillow>=10` and `uv lock`.
   Layout, all coordinates in pixels, white background (unfurl cards sit on
   the platform's own chrome, white is the convention):
   - Title at (64, 56), Bold 44, ellipsised to 1000 px; the domain
     `koalitionsberegner.moritzmarcus.com` right-aligned at the same baseline,
     Regular 22, `#888`.
   - Subtitle at (64, 124), Regular 26, `#666`: `Final result · 25 Mar 2026`
     or `Forecast · Voxmeter · 12 Sep 2026 · seats computed`.
   - Bar: track from x 64 to 1136, y 200 to 284, `#EEEEEE`, radius 12.
     Selected parties as segments left to right in list order, width
     `seats / total_seats * 1072`, in the party colour. Majority marker: a
     3 px `#111` line at `majority / total_seats`, with `Majority 90` in
     Regular 18 under it. The unselected remainder stays grey.
   - Total at (64, 332): the number in Bold 96, then `of 179 seats` in
     Regular 32, `#333`, on the same baseline. Right-aligned pill, Regular 30,
     with the page's colours: short `#666` on `#F0F0F0` (`11 short`); majority
     `#185FA5` on `#E6F1FB` (`Majority ✓ +5`); over two thirds `#3B6D11` on
     `#EAF3DE` (`Large majority ✓`).
   - Chips from y 470: a 14 px dot in the party colour, `A 38`, Regular 26,
     wrapping to at most two rows, then `+N more`. With nothing selected the
     chips row says `Open the link and pick parties.`
   - Glyphs Noto Sans lacks render as boxes; accepted for now, listed in Plan
     3 (party abbreviations are almost always Latin; titles are English).
5. `GET /api/og/{id}.png?c=&s=` returns `Response(render(card),
   media_type="image/png")` with `Cache-Control: public, max-age=3600` and an
   `ETag` built from the full hash, `c`, `s` and `stored_at`. Bound the work:
   parsing rejects anything over the schema limits, and rendering one image
   is tens of milliseconds; the Worker cache and the existing `/api/*` rate
   limit at Cloudflare do the rest.
6. Tests. `test_share.py`: round trips, de-duplication and ordering of `c`,
   out-of-range indices dropped, `s` mismatch sets `stale`, the three
   description shapes, the eight-party cap, length under 200. `test_og_image.py`:
   the bytes decode with Pillow to 1200×630 RGB; a pixel inside the first
   segment has that party's colour; a pixel in the empty remainder is
   `#EEEEEE`; no golden-file comparison (text rasterisation differs across
   FreeType builds). `test_api.py`: the prefix routes, `409` on ambiguity,
   both new endpoints' cache headers.

### WP2 — Worker: `/e/*` pages and image caching

Files: `wrangler.jsonc`, `worker/index.js`, `test/worker.test.mjs`,
`doc/cloudflare.md`.

1. `wrangler.jsonc`: `"run_worker_first": ["/api/*", "/e/*"]` and
   `"assets": { "directory": ".", "binding": "ASSETS", ... }`. The root page
   and `js/` stay Worker-free and off the request budget; only shared links
   and API calls count.
2. `worker/index.js`, new branch for `GET`/`HEAD` on `^/e/([0-9a-f]{12,64})$`:
   - Fetch the card from the origin (`/api/elections/{id}/card` plus the
     query, with `x-origin-secret`) through `caches.default`, keyed on the
     public URL, honouring the origin's `Cache-Control`. A `404` or any
     failure yields the generic card (title `Koalitionsberegner`, the default
     description, no image) so the page still loads.
   - Fetch the page with `env.ASSETS.fetch(new Request(new URL('/', request.url)))`.
     The asset response already carries `_headers`; keep its headers.
   - Replace `<title>…</title>` and insert before `</head>`:
     `og:type=website`, `og:site_name=Koalitionsberegner`, `og:title`,
     `og:description`, `og:url` (absolute `/e/...` with its query),
     `og:image` (absolute `/api/og/...png` with the same query),
     `og:image:width=1200`, `og:image:height=630`, `og:image:alt`
     (= description), `twitter:card=summary_large_image`, `twitter:title`,
     `twitter:description`, `twitter:image`. Every value goes through an
     `escapeAttribute()` that replaces `& < > " '`; export it and test it.
   - Return with `Cache-Control: public, max-age=300` (the page changes with
     every deploy; five minutes bounds staleness for crawlers that re-fetch).
3. `GET /api/og/*`: forward as today, wrapped in `caches.default` so a link
   pasted into a busy thread renders once, not once per viewer. Everything
   else under `/api/*` stays uncached. `/api/internal/*` stays blocked.
4. Tests, with `env.ASSETS.fetch` and `globalThis.fetch` and `caches.default`
   replaced by recorders: tags are injected with the card's wording; a title
   containing `<script>` and `"` arrives escaped; an unknown id still answers
   `200` with the generic tags; the second request for the same image is
   served from the cache recorder; `/e/` with a bad id falls through to `404`.
5. `doc/cloudflare.md`: a section on shared links (what counts against the
   Worker budget now, how to purge one cached image: Caching → Purge by URL).

### WP3 — Frontend: selection to URL and back, share button

Files: `js/app.js`, new `js/share.js`, `js/main.js`, `js/import-ui.js`,
`index.html`, `js/strings.csv`, new `test/share.test.mjs`, additions to
`test/other-election.test.mjs`, `backend/app/main.py` (one local route).

1. `js/app.js`: `mountCoalitionCalculator(election, elements, { initialSelection = [], onChange } = {})`.
   `initialSelection` is flattened positions; convert to the existing
   `block:party` keys. Call `onChange(indices, total)` at the end of
   `update()`. Return `{ clearAll, selection(), total() }`.
2. `js/share.js`, pure: `encodeSelection(indices)`, `decodeSelection(c,
   partyCount)`, `parseLocation(location) -> { id, indices, seats } | null`,
   `buildPath({ id, indices, total })`, `flattenedCount(election)`. Mirror the
   backend rules exactly; the test file lists the same cases as
   `test_share.py`.
3. `js/main.js`:
   - On boot, `parseLocation(globalThis.location)`. If it names an id, wait
     for `importUi.start()` (the list), then `api.getElection(id)`, render
     with `initialSelection`, select it in the picker. `404`/`409` shows
     `share.unknown` and leaves the default election. If `seats` is set and
     differs from the rendered total, show `share.stale` under the sticky
     bar.
   - `onChange` and picker changes call `history.replaceState(null, '',
     buildPath(...))`. With nothing selected the path is still `/e/<id>` so a
     reload keeps the election. The bundled election maps to its twin, found
     in the summaries by `nation` and the date part of `electionDate` with no
     forecast; with no twin, no URL is written and the share button hides.
   - Language switch already reloads the page; the URL survives it.
4. Share button in the sticky bar next to `reset`: `navigator.share({ url })`
   when present (mobile), otherwise `navigator.clipboard.writeText(url)` and a
   two-second `share.copied` state. Strings: `share.button`, `share.copied`,
   `share.stale`, `share.unknown`, `share.unavailable` in both columns; the
   Danish also in `index.html` markup where the button lives
   (`test/i18n.test.mjs` checks the pair).
5. `index.html`: the page now also loads at `/e/<id>`, so the module script
   must be `src="/js/main.js"` (absolute). `strings.csv` and the bundled
   provider resolve against `import.meta.url` and need no change. Check
   there is no other document-relative URL in the markup.
6. Local parity: `backend/app/main.py` serves `FRONTEND_DIR / "index.html"`
   for `GET /e/{id}` when the frontend directory is present, with no tags.
   Production never hits it (the Worker owns `/e/*`), so no drift.
7. Tests: `test/share.test.mjs` for the pure functions;
   `test/other-election.test.mjs` gains a boot-with-link case and the stale
   note; a `clipboard` stub for the button.

### WP4 — Docs and privacy

1. `README.md`: a "Sharing a coalition" section (URL shape, what unfurls
   where, the image endpoint).
2. Privacy, both languages and the Danish in `index.html`: a link holds the
   election's id and the chosen parties, nothing about the person; opening
   one makes the browser fetch the page and the preview image like any page
   load, and the count of opened links is a number per day. Bump
   `privacy.updated`.
3. `doc/threat-model.md`: a T13 on share links: the only user-controlled
   inputs are integers, bounded before use; the image render is bounded and
   cached; titles and abbreviations were schema-cleaned at import and are
   escaped again in the Worker; crawler traffic hits the Worker cache before
   the origin.

## Running this beside plan 4

The owner runs this plan and [04-reddit-outreach.md](04-reddit-outreach.md) at
the same time. That plan's section "Running this beside plan 2" holds the shared
file table and the protocol; read it before touching `worker/index.js`,
`backend/app/main.py`, `wrangler.jsonc`, `doc/threat-model.md` or the strings
sheet.

The one thing to do for the other plan rather than for this one: when WP2 adds
the `/e/*` branch, factor the asset fetch and response rewrite into a helper such
as `servePage(env, request, { transform, headers })`. Plan 4's approval page is
the same shape and should call it instead of copying it.

## Acceptance

- Open a shared link in a fresh browser: the same parties are ticked and the
  same verdict shows. Edit `s` in the URL by hand: the note appears, the
  selection stays.
- Paste a link into Slack or Discord (instant unfurl) and check it with the
  Facebook Sharing Debugger and a Reddit link post in a test subreddit; the
  image shows the bar, the total and the pill. Facebook caches per URL; use
  "Scrape again" after changes.
- `curl -sI https://koalitionsberegner.moritzmarcus.com/e/<id> | grep -i content-security-policy`
  prints the same header as for `/`.
- `curl -s https://koalitionsberegner.moritzmarcus.com/api/og/<id>.png?c=0 | file -`
  says PNG 1200 x 630.
- Both test suites green; `test_cloudflare.py` still passes (no new top-level
  files: fonts live under `backend/app/fonts/`).
