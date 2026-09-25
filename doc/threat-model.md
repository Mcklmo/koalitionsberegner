# Threat model: the election import pipeline

Importing an election means fetching a web page nobody here controls, handing it
to a language model, and rendering the result. This document says what we assume
about that page, what an attacker who controls one can and cannot do, and where
each defence lives in the code.

The short version: **the page is data, the model has no capabilities, the
validator is a hard gate, and the user is the last one.** A page that completely
succeeds at manipulating the model wins nothing but a candidate result — one
that must still pass schema validation and then be confirmed by a human before
it is stored, and that renders as inert text either way.

## The pipeline

```
 user types place (+ year)
        │
        ├─ no year ─▶ [0a] resolve_latest   resolver.py ── identities only: the
        │               previous and next    parser.py     previous and the next
        │               election; the user                 election, offered by a
        │               picks one, and it                  rule in code; nothing is
        │               comes back with a year             fetched or read
        ▼
 [0] resolver agent         resolver.py  ── the typed text and today's date, a
        │  one election,       search.py     hosted search, and an output of one
        │  candidate URLs                    identity plus candidate addresses
        ▼
 [0b] Wikipedia lookup      wikipedia.py ── the identified election searched for
        │  the article first                in one API, on one host, no model
        ▼
 [1] backend fetch          fetcher.py   ── SSRF checks, size/type/redirect caps
        │  HTML → text                      script/style/comments dropped
        │                   wikipedia.py    an article instead through its API,
        │                                   reduced to infobox, lead and tables
        ▼
 [2] extraction agent       extractor.py ── page fenced as data, no tools,
        │  structured output                one turn, one output schema
        ▼
 [3] validator              schema.py    ── allowlisted fields, types, bounds,
        │  Election                         seat/majority consistency, safe text
        ▼
 [3b] is this the election  parser.py    ── year must match what the user typed,
        │   that was asked for?             nation and region the resolution;
        │   no → next candidate, back to [1] otherwise it is discarded
        ▼
 [4] preview (not stored)   service.py   ── staged; the user sees it
        │
        ▼ user confirms
 [5] store                  store.py     ── first write wins per election identity
        │
        ▼
 [6] browser                election.js  ── the same validation again, client-side
                            app.js       ── textContent only, never markup
```

Steps 0–5 run server-side. The browser never fetches the imported page and never
sees the API key.

The user supplies no address at all now: the candidate pages come from the
resolver, which is given a few dozen characters of typed text and never reads a
results page, and from Wikipedia's search API, which is given the election the
resolver identified and answers with article titles on one fixed host. A page that states no seats, or turns out to be a different
election, simply moves the import on to the next candidate of that fixed list
(see T9) — no page is ever asked where to go next, and the links on a fetched
page are not read. The election is attributed to whichever page the numbers came
from, and the preview always names it.

## Trust boundaries

| Input | Trusted? | Notes |
| --- | --- | --- |
| What the user typed | Partly | A place and, optionally, a year, from our own user. Length-capped and stripped of control and direction-changing characters, then passed on as typed — misspellings are the resolver's job. |
| A URL from the resolver | No | Produced by a model that read search results. Only `http(s)` addresses survive `resolver.clean_sources`, and each is fetched under the same SSRF checks as anything else. |
| The fetched page | No | Fully attacker-controlled bytes. Every field of an imported election ultimately derives from it. |
| The model's output | No | It read untrusted input, so its answer is untrusted too. Constrained by the output schema, then re-validated. |
| A link on the fetched page | Not read | Nothing consults the links on an imported page any more; there is no path by which one becomes an address we fetch. |
| A web search result | No | Comes from outside the app entirely; filtered and fetched exactly as a link is. |
| A Wikipedia article | No | An encyclopedia anyone may edit is an untrusted page like any other: fenced as data, extracted by the same tool-less agent, checked by `parser.is_wanted`, confirmed by the user. What it does get is first place in the queue, because it is the page most likely to state seats at all. |
| A Wikipedia article title | No | From the MediaWiki search API. It only ever becomes a path under the one host `wikipedia.py` is configured for, and that URL is still put through `fetcher.assert_public_url`. |
| The stored election | Partly | It passed validation and a human confirmation. Still rendered as text only. |
| Our own prompts and code | Yes | Built server-side from constants; no part of the page reaches them as instructions. |

The adversary is whoever controls a page an import reads — including the operator
of a legitimate-looking results site, or anyone who can get a page ranked for an
election's name. They can serve any bytes, any redirects, any encoding, and can vary the
response per request.

Their goals, in the order we care about: make the agent do something other than
extract (act, browse, leak), get executable or deceptive content in front of
another user, poison the shared election store, or burn our budget.

## Threats and defences

### T1 — The URL as a server-side request forgery primitive

`http://169.254.169.254/` would hand cloud instance metadata to the model, and
`http://10.0.0.5/` would reach inside the network.

`fetcher.assert_public_url` resolves the hostname and rejects any address that
is not globally routable, before every request *and again for every redirect
hop* (redirects are not followed by the HTTP client; the fetcher re-checks each
`Location` itself). Only `http`/`https` are accepted, at most 3 redirects, a
20 s timeout per read and 45 s for the whole fetch, 5 MB of body — counted while
it streams, so an endless response is cut off rather than buffered — and only
HTML/plain-text content types, checked before the body is downloaded.

### T2 — Resource exhaustion

Response bodies are capped at 5 MB and the flattened text at 400 000 characters.
An over-long page is **rejected, never truncated**: a cut-off results table
yields a plausible but wrong election, which is worse than a failed import.

Extraction is single-flight per page (`store.claim`): concurrent imports of the
same URL share one model call, and a page already imported is served from
storage without fetching or extracting anything at all.

Importing is the owner's alone (see T11), so nobody else can run this pipeline
at all, repeatedly or otherwise. There is no request-rate limit inside the app
(see [Accepted risks](#accepted-risks)).

### T3 — Prompt injection: instructions embedded in the page

Three of the page's channels are closed before the prompt exists.
`fetcher.html_to_text` drops `<script>`, `<style>`, `<noscript>`, `<template>`,
`<svg>` and `<head>` contents, and `HTMLParser` discards comments — so
instructions hidden in a script block or an HTML comment never reach the model.

What remains is visible (and CSS-hidden) text, and it is presented as data:

- `extractor.build_user_message` wraps the page in a `<document>` … `</document>`
  fence, with the source URL stated *before* the fence.
- `extractor.fence_page` neutralises any fence marker the page itself carries
  (`</document>`, any case or spacing) by swapping the angle brackets for
  look-alikes. The page cannot end the data section early and continue as if it
  were the operator. This is the one *structural* attack, and it is closed.
- `extractor.SYSTEM_PROMPT` states that the document is untrusted data, that
  embedded instructions are an attack, and that the fence markers inside it have
  been neutralised.

We do not claim the model is unpersuadable. We claim persuasion buys nothing —
see T4 and T5.

### T4 — Capability abuse: making the agent *do* something

The agent has nothing to do anything with. `extractor.build_request` is the
complete argument list of the extraction call: model, token cap, thinking,
system prompt, one user message, one output format. There are no tools, no
server-side tool blocks, no conversation history, no retrieval, and no second
turn. Injected instructions to browse, fetch, POST, or read a file address
capabilities that do not exist.

The page is fetched *before* the model runs, and the model is never asked again,
so "now go and read this other URL" has no fetcher to reach. The importer does
sometimes read a second page — see T9 — but the extraction agent has no say in
which, and is handed the result as one more fenced document.

The model's only output channel is one `ExtractedElection` object with
`extra="forbid"`: a demand to "add a field called `exfiltrate`" cannot be
satisfied, because the object has nowhere to put it. The one field whose value
reaches the user as prose rather than as election data — `no_results_reason`,
which explains an empty extraction — is a three-value enum, and
`parser.NO_RESULTS_MESSAGES` owns the wording. A page can steer which of three
sentences appears; it cannot write one.

### T5 — Hostile or nonsense extraction output

Whatever the model was talked into saying is validated as if it were hostile,
by `schema.Election`:

- **Allowlist**: `extra="forbid"` on every model; unknown fields are errors.
- **Types**: `strict=True`, so `"10"` is not silently a number.
- **Bounds**: 1–100 000 seats, ≤ 50 blocks, ≤ 200 parties per block, text ≤ 200
  characters.
- **Sanity**: party seats must sum *exactly* to `total_seats`; `majority_seats`
  must be more than half the assembly and no more than all of it. "400 of the
  120 seats for the Loyal Party" fails here.
- **Shape**: `election_date` must be a real calendar date; `source_url` must be
  an absolute `http(s)` URL; `color` must match `#rgb`/`#rrggbb`.
- **Text safety**: no control characters, and no invisible or direction-changing
  characters (see T6).

A failure stores nothing, reports a message to the user, and leaves the page
free to retry. Validation messages quote the value that failed — so they are
clipped to 200 characters per problem (`parser._clip`), because that value came
from the page.

One field is beyond the model's reach entirely: `source_url` is set from the
request, not from the extraction, so a page cannot make an import claim to have
come from somewhere else.

### T6 — Extracted strings reaching the DOM

Party names — English and local — block names and titles are attacker-influenced
strings that the UI displays. They are never parsed as markup:

- The renderer builds elements with `document.createElement` and sets text with
  `textContent`. `innerHTML` is assigned only the empty string, to clear a
  container; `insertAdjacentHTML`, `outerHTML`, `document.write`, `eval` and
  `new Function` do not appear in `js/`. `test/injection.test.mjs` enforces both
  claims by scanning the sources, and renders an adversarial election through a
  DOM stub that throws if any markup is ever written.
- A party name of `<script>alert(1)</script>` is therefore a *wrong name*, not a
  vulnerability. It is stored verbatim and shown verbatim.
- A party's local name may come from the extraction agent's own knowledge when
  the page states only one of its names, so it can be wrong with no attack at
  all. It passes the same text rules, and the preview shows both names side by
  side for the user to check before anything is stored.
- `color` is the only extracted value that reaches a style property, and it must
  be a hex colour, so it cannot escape into further CSS declarations.
- `sourceUrl` must be `http(s)`, which excludes `javascript:` and `data:`.
- Invisible and direction-changing characters (U+202A–U+202E, U+2066–U+2069,
  U+200B, U+200E/F, U+00AD, U+FEFF, U+2028/9) are **rejected**, not escaped:
  they survive `textContent` intact and can make one party's label read as
  another's. Zero-width joiners (U+200C/D) are allowed, because real scripts
  need them. Enforced identically in `backend/app/schema.py` and
  `js/election.js`.

The client re-runs the whole validation on everything the API returns
(`api.toElection` → `validateElection`), so a compromised or buggy backend does
not get a free pass with the renderer.

### T7 — Poisoning the shared store

Elections are shared between users, so a bad import is a bad import for
everyone. Three things bound it:

- **Nothing is stored without a human.** Extraction ends in a `PREVIEW` state
  (`service._extract` → `store.stage`). The preview shows the numbers *and* the
  identity the agent inferred — nation, region, date — because the user supplied
  none of it. `store.confirm` is the only path into storage.
- **No overwrites.** Identity is `election_hash(nation, state, date)`. If that
  identity is already stored, a confirmation records a duplicate and returns the
  existing election; it does not replace it. A page claiming to be the Danish
  2026 election cannot rewrite one already held.
- **Failures are not sticky.** A failed or discarded import leaves the page
  claimable again, so a transient attack cannot lock a URL.
- **A preview is accepted or thrown away only by the owner.** A request key
  follows from the year and the place alone, so anyone who can compute it could
  otherwise confirm or discard someone else's preview. Confirming and
  discarding sit behind the same `require_admin` that starting an import does,
  so both halves of the decision are the owner's.

### T8 — Secrets and logs

The Anthropic key lives in Secret Manager, is mounted into the Cloud Run
revision, and is used only server-side; it never reaches the browser. Extraction
logs record sizes, token counts, stop reasons and durations — never page text
and never model output (`extractor.AnthropicExtractor.extract`). The typed
request and the addresses we fetch *are* logged, as the targets. Only the
notable calls are logged at all (`observability.NOTABLE_SYSTEMS`), so an
import's log is a handful of lines and a failure stands out in it.

What a failed import *shows* is narrower than what it logs. Only a `ParseError`
is worded for the user; any other failure — an API error body, a database
message — reaches them as one fixed sentence (`service.IMPORT_FAILED`,
`parser.READ_FAILED`).

The GitHub PAT behind election requests (T12) is handled the same way: it is
read from the environment into `app.wishlist` alone, never logged, and never
described to a caller — what a failed filing shows is one fixed sentence. It is
read from `GITHUB_ISSUES_TOKEN` rather than `GITHUB_TOKEN`, so a token another
tool left in the environment is never picked up and used to open issues.

Running with no `ADMIN_SECRET` makes every caller the owner, which is right for
a local checkout and never for a deployment: `config.validate_configuration`
refuses to boot on Cloud Run without one, so a deploy that loses that variable
fails to start rather than serving every caller as the administrator.

The five `REDDIT_*` credentials (T14) get the same treatment as the Anthropic
key: read from Secret Manager into `app.reddit` alone, never logged and kept
out of every `repr` (`RedditCredentials`, `field(repr=False)` on the secret and
the password). A login failure or a refused comment is worded as one of a
handful of fixed sentences (`reddit.NOT_CONFIGURED`, `LOGIN_FAILED`,
`REFUSED`, `RATE_LIMITED`, `UNREACHABLE`, `UNCERTAIN`); Reddit's own response
body and error codes are logged, never returned. `config.get_reddit_poster`
mirrors `config.get_wishlist`: any credential missing is `DisabledRedditPoster`
rather than a half-configured client, and `describe_configuration` reports only
`"posting"` or `"off"`.

### T9 — Every page is one the user did not name

Nobody supplies an address any more. The pages an import reads come from
`resolver.py` and `search.py`, which is a search engine's opinion turned into a
fetch, so the narrowing is where the safety is:

- **The reading agent never chooses what to read.** The extractor has no tools
  and no second turn. The list of candidates is fixed before the first page is
  fetched, and the links on a fetched page are not read at all — a page cannot
  nominate its successor.
- **Wikipedia is a preference, not a dependency.** `wikipedia.py` looks the
  identified election up and puts its article at the front of the queue, because
  an encyclopedia article states seats where an electoral authority often states
  votes. It is no more trusted for it: the article is read by the same agent,
  checked by the same `parser.is_wanted`, and confirmed by the same user. The
  only addresses it can produce are `/wiki/<title>` on the one host it is
  configured for, and a lookup that fails simply leaves the resolver's own
  candidates to be read.
- **Only the resolver has a tool**, and it is a search engine
  (`resolver.SEARCH_TOOL`). Its input is the few dozen characters the user typed
  plus today's date — no page text can reach it, because no page has been read
  when it runs. Its output is put through `resolver.clean_sources`, which keeps
  `http(s)` URLs and discards everything else, including any instruction a
  search result talked it into repeating.
- **Every candidate is fetched the same way.** Same `assert_public_url`, same
  size, type and redirect caps. A candidate pointing at `169.254.169.254` is
  refused — being named by a model earns an address nothing.
- **A page that is not the election asked for is discarded in code.**
  `parser.is_wanted` requires the year to match the user's own input and the
  nation and region to match the resolution. Asked for without a year, the
  year is instead one the user picked from the resolver's short list
  (`parser.choose_candidates`, whose rule is code, not the model's): the
  follow-up request carries it, and every check here applies to it unchanged.
  That list is only names and dates — `resolver.LatestElections` has no field
  for an address — and reaches the page as text. The neighbouring region and the
  previous election are the wrong answers that do not look wrong, so they are
  refused before anything is staged, however cleanly they extracted.
- **Nothing is stored silently.** The election is attributed to the page it was
  read from, the preview names that page, and the user confirms.
- **The budget is small and configurable.** `IMPORT_PAGE_LIMIT` (3) and
  `IMPORT_SEARCH_LIMIT` (3) keep an import to a handful of pages, not a crawl;
  `SEARCH_MODE=off` leaves only the resolver's own candidates, and
  `WIKIPEDIA=off` takes the article out of the queue.

What this does *not* defend against is a wrong-but-plausible page: a search can
return an outdated or unofficial page whose numbers differ from the official
ones. The confirmation step, with the source page named, is the check.

### T10 — Polls of an election not yet held

An upcoming election is imported from its opinion polls, which changes what a
page's numbers are *for* but none of the defences above:

- **Same fence, same capability.** `extractor.build_forecast_request` carries
  exactly the arguments `build_request` does, with its own system prompt that
  opens with the same untrusted-data paragraph (`DOCUMENT_IS_DATA`) and its own
  closed output schema, `ExtractedForecasts`. `no_results_reason` is again a
  code, and `parser.NO_POLLS_MESSAGES` owns the wording.
- **The identity is the resolver's.** A forecast is filed under the nation,
  region and date the resolver gave, never the page's; the page's own claim of
  which election its polls are for is checked against the request by
  `parser.polls_are_wanted` first, and a page for another place or year is
  discarded whole.
- **Seats are never the model's arithmetic.** The agent reports seats or vote
  shares as stated and is told not to convert. Converting is
  `seats.allocate`, in code, from the resolver's assembly size, threshold and
  method — a model's reading of the election, not of the page. Every poll
  passes `schema.Election` like a result does, a poll dated after today or
  after the election is refused, and a bad poll is dropped rather than repaired.
- **No overwrite, and no squatting.** A forecast's identity adds its publisher
  and date to the election's, so it can never take a result's place in the
  store; `store.select_by_place` ignores forecasts, so a saved poll cannot block
  the result, or a newer poll, from being imported. A forecast is saved only by
  a user choosing it, and the store accepts only a forecast that is actually on
  offer (`confirm_forecast` compares the election itself, not its position).
- **Labelled.** A computed forecast says so in the list, the preview and the
  calculator's footer, and the page it was read from is named.

### T11 — Spending the owner's money

Importing is what costs money — a fetch and a model call — and it is the
owner's alone: `require_admin` gates every route that starts, follows, saves
or discards an import, and, after
[03-remaining-work.md](plans/03-remaining-work.md), the schedule that triggers
automated imports is checked the same way. The adversary here has no page and
no account to abuse; the goal is a leaked or guessed `ADMIN_SECRET`.

- **The secret is compared in constant time.** `require_admin` uses
  `hmac.compare_digest`, so a wrong guess cannot be narrowed down byte by byte
  from how long the comparison took.
- **A placeholder is a boot failure.** `ADMIN_SECRET`, like `ORIGIN_SECRET`,
  must be at least 32 characters, and Cloud Run refuses to start without one at
  all (`app.config.validate_configuration`). A local run with none configured
  is the owner's own, which is right on a laptop and nowhere else.
- **Viewing costs a copy, not a read.** `cached_store.CachedElectionStore`
  keeps stored elections for 30 s in front of Firestore, so reloading the list
  in a loop is not one billed read per election per request. Jobs are never
  cached, and neither is a miss. Waiting on an import backs off to one look a
  second.
- **Bodies are bounded before they are read.** `main.LimitRequestBody` refuses
  anything over 64 KB and any body that does not declare its length.
- **The proxy cannot be walked around.** With `ORIGIN_SECRET` set, only
  requests carrying it are answered, so rate limits and bot checks at the proxy
  are not bypassed through the platform's own `run.app` address.
- **The page runs only our scripts.** Every response carries a
  `Content-Security-Policy` allowing scripts and connections to this origin
  alone, and refusing to be framed — a backstop behind T6.

### T12 — Writing into the issue tracker

Anyone can ask for an election, and that ask becomes a
GitHub issue on this repository (`app.wishlist`). It is the app's only outbound
*write*, and the only one where a user's own words end up somewhere other than
the store, so it is worth its own section.

- **It is an open endpoint, deliberately.** `POST /api/elections/requests`
  needs no `x-admin-secret` — the one route in the import flow that does not.
  The reasoning that gates the rest does not apply: nothing here searches,
  fetches, extracts or spends, and the visitor most likely to find an election
  missing is not the owner. The cost is that this is a public write on behalf
  of a caller nobody authenticated, and the secret that gates every other path
  is not available to bound this one. What is left is the bullets below, plus
  the rate limiting in front of the app — which is where this app puts
  per-client limits generally. The residual is accepted below.
- **It names nobody.** The route reads no credential at all, and the issue
  carries the election and nothing about who asked: the tracker is public.
- **One election is one issue.** A request is keyed by `identity.request_key`,
  the same key an import is, and the key is written into the issue as an HTML
  comment. Asking again finds the open issue and points at it, so the tracker
  cannot be filled by resubmitting the same form. What is left is a caller
  enumerating *distinct* elections, which nothing in the app bounds — see the
  accepted risk below.
- **An election already stored is never filed.** The endpoint looks it up first
  and answers `409`, so the queue holds only elections that are actually
  missing.
- **The user's words cannot restructure the issue.** Everything interpolated
  goes through `wishlist._as_code`: backticks and pipes are removed and
  whitespace is collapsed, so a place name cannot close its code span, add a
  table row, or reach the title as more than one line. Same principle as T6 —
  text from outside decides nothing about the shape of what surrounds it.
- **GitHub's answer stays on our side of the boundary.** A refusal is logged
  with its status and reaches the caller as one fixed sentence
  (`WishlistUnavailable("could not file the request")`), so a mis-scoped or
  expired token cannot describe itself into a browser. The PAT is used in
  `app.wishlist` and nowhere else, and wants **Issues: write** on one repository
  and nothing else.
- **Failing to check for a duplicate is not failing to file.** A search that
  errors lets the filing go ahead: a second issue is a far better outcome than
  refusing somebody who asked for something reasonable.

### T13 — Shared links

A link's `id`, `c` and `s` ([02-share-links.md](plans/02-share-links.md)) are
read by a stranger's browser or by a crawler with no account and no secret, so
they are the only inputs here a visitor fully controls. Nothing they can carry
is more than a handful of small integers.

- **Every integer is bounded before it does anything.** `app.share.parse_selection`
  rejects a `c` longer than the widest selection any election allows before it
  is even split, caps how many tokens it will split into, and drops an index
  once it is not less than the election's own party count; `parse_seats`
  rejects anything over `MAX_SEATS` outright. Neither ever raises: a malformed
  value is read as an empty selection or a missing seat total, so a mangled
  link still opens the election rather than erroring.
- **Rendering the image is cheap and cached.** `app.og_image.render` draws a
  fixed layout from numbers already bounded above; one image is tens of
  milliseconds of work. `GET /api/og/<id>.png` and `GET
  /api/elections/<id>/card` both answer with an hour's `Cache-Control`, and the
  Worker puts both in `caches.default` keyed on the full request URL, so
  repeated requests for the same link — a crawler re-fetching, a link pasted
  into a busy channel — cost one render, not one per request. What is left is
  bounded by the same per-client limiting at the proxy that the rest of `/api/*`
  relies on (see the accepted risk on rate limiting, below).
- **Nothing in a title or an abbreviation gets a second chance to be markup.**
  Party names and abbreviations were already cleaned to plain text by
  `schema.clean_text` when the election was imported; the Worker's
  `escapeAttribute` (`worker/index.js`) escapes `& < > " '` again wherever the
  card's wording is written into an HTML attribute or a `<meta>` tag, so a
  title cannot close a tag or add an attribute even if something upstream ever
  let one through.
- **An unknown or ambiguous id costs a page, not an error.** The Worker treats
  a `404` (no such prefix) or a `409` (an ambiguous one) from the origin's card
  endpoint the same as any other failure: it serves the page anyway, with the
  generic tags rather than an election's. Trying prefixes to see which exist
  gets a crawler nothing but the same `200` every time.

### T14 — The outreach approval gate

[04-reddit-outreach.md](plans/04-reddit-outreach.md) adds a queue the
`outreach` plugin fills from Reddit, an emailed one-time link, and a route that
posts a reply. Two things are untrusted here that are not untrusted anywhere
else in the app: **Reddit's own text**, read by the plugin and shown back to
the owner and to the extraction prompt that verified it; and **the approval
link itself**, which travels by email and can be forwarded, leaked or guessed.

- **An approval link is not an account.** Viewing a draft needs only the token
  in the link (`GET /api/outreach/approval/{token}`); *sending* it needs
  `ADMIN_SECRET` as well, in `x-admin-secret`, checked with
  `hmac.compare_digest` exactly as every other admin route checks it
  (`main.require_admin`). Someone who intercepts or is forwarded a link can
  read one draft — a Reddit thread, an excerpt, a proposed reply, nothing about
  any other user — and can never post it, reject it, or learn whether it was
  already used, without also holding the secret.
- **The token sits in the URL, so Cloud Run's request logs carry it.** All
  three routes name it in the path (`GET /api/outreach/approval/{token}`, and
  the same path with `/send` or `/reject`), and Cloud Run records the full
  request path for every call, as would any proxy in front of it. The worst
  case of a leaked token, on its own, is exactly what "not an account" above
  already grants: one draft's contents — the reply, the excerpt, the thread it
  answers, what it links to — readable by repeating `GET` as many times as
  wanted, until the token is used or the 72-hour expiry claims it; `GET`
  itself changes nothing except on that expiry. It gets nothing else: `send`
  and `reject` both still need `x-admin-secret`, checked the same way as every
  other admin route, and that secret is already worth more than a token — it
  is what gates `/api/elections/import`, which spends money, and every other
  admin route in this file. Accepted rather than fixed: a bare token
  discloses a drafted reply to a public Reddit thread, never a way to post,
  reject, or spend anything.
- **The token is single-use and short-lived.** Only its SHA-256 hash is stored
  (`app.outreach.hash_token`, `new_token`); the token itself is never written
  down, the same discipline the (now-removed) session tokens used. Sending,
  rejecting, or the token simply expiring after `TOKEN_LIFETIME_SECONDS` (72
  hours) all clear the stored hash (`consume_token=True`), so
  `get_by_token_hash` cannot find it again — a forwarded or reused link is a
  `404`, indistinguishable from one that never existed.
- **Reddit text is fenced, twice.** The plugin's own verification prompt fences
  the thread the same way an imported page is fenced (T3); nothing it read is
  ever treated as an instruction. On the server, nothing from the plugin is
  trusted a second time either: `app.outreach.clean` re-applies the control-
  and invisible-character checks `schema.clean_text` applies to everything
  else, and `validate_reply_text` re-checks the reply's length, its one
  permitted link (the draft's own, never an arbitrary one) and its disclosure
  footer — because the owner may have edited the text in the approval page
  before sending, and the plugin's own sanitising never reaches the server.
  Every field a draft carries reaches the DOM as text (`js/approve.js`), the
  same rule T6 states for extracted election text.
- **No Reddit username is ever stored.** The scanner deliberately does not read
  author names, and `OutreachDraft` has no field for one — see the privacy
  section's outreach paragraph.
- **A send that might have gone through is never offered a blind retry.**
  `app.reddit` distinguishes a request that never reached Reddit (safe to
  retry) from one whose answer was lost after it was sent
  (`RedditUnavailable(maybe_posted=True)` — a timeout, a dropped connection, a
  gateway error). The route consumes the token on that outcome exactly as it
  does on a confirmed post, so the approval page has nothing left to offer but
  "check the thread by hand" — never a second click that could double-post.
  Reddit's own words never reach the page either way; the caller sees one of a
  handful of fixed sentences (T8).
- **Etiquette is enforced in code, not left to the owner to remember.** One
  reply per thread ever (`OutreachStore.thread_posted`), a weekly cap per
  subreddit and a daily cap in total (`config.outreach_subreddit_weekly_cap`,
  `outreach_daily_cap`), and nothing posts to a subreddit outside this
  deployment's own `OUTREACH_ALLOWED_SUBREDDITS` — the scanner keeps no list of
  its own, so this is the only place a subreddit is allowed or retired. Every refusal is a distinct message
  naming which limit stopped it, not a silent no.
- **The page itself is inert.** `/approve/{token}` gets no Open Graph tags and
  `X-Robots-Tag: noindex` (the Worker sets it on the response; see
  `worker/index.js`), so a link pasted anywhere, or fetched by a crawler,
  never unfurls and is never indexed — unlike `/e/*`, which is built to do
  exactly that.

## Accepted risks

These are known and deliberately not addressed here:

- **The model can simply be wrong.** Mis-reading a table needs no attacker. The
  confirmation step exists as much for this as for injection.
- **A user can confirm bad data.** The preview is a check, not a proof. The
  defence is that the data is inert and the identity is visible.
- **CSS-hidden text still reaches the model.** Text hidden with `display: none`
  is text; stripping it reliably would need a full CSS cascade. It arrives
  inside the fence with no more authority than the rest of the page.
- **Anyone can put an issue in the tracker.** Asking for an election needs no
  secret (T12), so a caller willing to invent distinct year-and-place pairs can
  open an issue per pair. One election is one issue, an election already stored
  is refused, and every field is length-bounded and escaped, so what this buys
  an attacker is noise in a queue somebody reads by hand — not a write to the
  store, not a fetch, not a model call, and nothing that costs money. The bound
  on the rest of it is the proxy's, as below.
- **No request-rate limiting in the app.** Importing needs the owner's secret
  (T11); asking for an election is one issue per election (T12); everything
  else is cheap and cached. Limiting requests per client belongs at the proxy
  in front, which sees real client addresses — the app behind Cloud Run's front
  end cannot reliably.
- **A DNS answer can change between the check and the connection.**
  `assert_public_url` resolves the host, and the HTTP client resolves it again.
  A rebinding host could point the second answer inward; on Cloud Run there is
  no private network to reach, and the metadata server refuses requests
  without its `Metadata-Flavor` header, which the fetcher never sends.
- **The page can vary per request.** The page we fetched is the page we
  extracted; nothing guarantees a later visitor sees the same thing.
- **A found page may be unofficial or out of date.** Every page is one a search
  turned up; the alternative is no import at all. The source URL is shown in the
  preview and stored with the election, so what it was read from is always
  visible.
- **Some hosts refuse this fetcher.** A site may block us by policy or by
  fingerprint, and those candidates are passed over rather than worked around —
  disguising the client is out of scope. Wikimedia used to be the case that hurt
  most; it is now read through its own API, which is the supported way in, with a
  `User-Agent` that says who we are (`WIKIPEDIA_CONTACT`).
- **Computed seats are an approximation.** A poll in percent is allocated as one
  nationwide proportional vote; real systems add constituency seats, overhang,
  regional thresholds and reserved seats. The error is honest, not hostile, and
  the label is the mitigation.
- **Wikipedia can be edited by the attacker too.** Putting its article first
  means an import usually reads the page an adversary would have to edit
  publicly, in the open, to poison — which is a trade we are making deliberately,
  not a defence. Every check downstream is unchanged.

## Where this is tested

| Claim | Test |
| --- | --- |
| Injected instructions cannot leave the data fence | `backend/tests/test_injection.py` |
| Script, style and comment instructions never reach the model | `backend/tests/test_injection.py` |
| The extraction call carries no tools or extra channels | `backend/tests/test_injection.py` |
| An agent that obeys an injection produces nothing storable | `backend/tests/test_injection.py` |
| An adversarial page stops at a preview | `backend/tests/test_injection.py` |
| Extracted strings render inert; no markup sinks in `js/` | `test/injection.test.mjs` |
| Schema bounds, text safety, seat consistency | `backend/tests/test_schema.py`, `test/election.test.mjs` |
| SSRF, size and redirect limits | `backend/tests/test_fetcher.py` |
| A page cannot nominate the next page to read | `backend/tests/test_injection.py` |
| A page reporting another year or region is discarded | `backend/tests/test_extraction.py` |
| A search returns URLs and nothing else; a broken search is not a failed import | `backend/tests/test_search.py` |
| Only `wikipedia.org` articles reach the Wikipedia API, and a broken lookup is not a failed import | `backend/tests/test_wikipedia.py` |
| An article is read first, but checked against the request like any other page | `backend/tests/test_extraction.py` |
| Real articles reduce to their results, and to nothing that is not a result | `backend/tests/test_articles.py` |
| Candidate pages go through the fetcher, and only for pages that stated no seats | `backend/tests/test_extraction.py` |
| Polls are fenced, tool-less, checked against the request, and dropped when invalid | `backend/tests/test_forecasts.py` |
| Seats from vote shares are computed in code, deterministically | `backend/tests/test_seats.py` |
| A forecast cannot take a result's identity, block an import, or be saved unless offered | `backend/tests/test_forecast_schema.py`, `backend/tests/test_forecast_store.py`, `backend/tests/test_forecast_api.py` |
| An offered list is validated whole; computed seats are labelled in the page | `test/forecast.test.mjs` |
| An endless or dripping page is cut off; a PDF is refused before download | `backend/tests/test_fetcher.py` |
| Only the owner's secret starts, follows, confirms or discards an import | `backend/tests/test_api.py` |
| Stored elections are read once per 30 s, never stale after a local write | `backend/tests/test_cached_store.py` |
| Security headers, body caps, the origin secret, nothing served beside the page | `backend/tests/test_edge.py` |
| Cloud Run refuses to start without `ADMIN_SECRET` | `backend/tests/test_config.py` |
| An internal failure reaches the user as one fixed sentence | `backend/tests/test_service.py` |
| A request needs no secret, files one issue per election, and never repeats GitHub's words | `backend/tests/test_wishlist.py` |
| A place name cannot restructure the issue it is written into | `backend/tests/test_wishlist.py` |
| A consumed, unknown or expired approval token is a `404`, indistinguishably; sending needs the token and the admin secret both | `backend/tests/test_outreach.py` |
| The etiquette caps, the subreddit allowlist and one-reply-per-thread are enforced server-side | `backend/tests/test_outreach.py` |
| A reply's length, its one permitted link and its disclosure footer are re-checked on the server, on the (possibly edited) text that is actually sent | `backend/tests/test_outreach.py` |
| A send that might have posted consumes the token instead of offering a retry; Reddit's own words never reach a caller | `backend/tests/test_reddit.py`, `backend/tests/test_outreach.py` |
| Reddit credentials are never logged or printed; posting is off, not half-configured, with any one missing | `backend/tests/test_reddit.py` |

The adversarial fixtures themselves are `test/adversarial/`:
`injected-instructions.html` argues with the agent through five different
channels, and `markup-in-names.html` hides markup and a direction override in
party names.
