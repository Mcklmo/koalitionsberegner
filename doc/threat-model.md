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
 user types year + place
        │
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
| What the user typed | Partly | A year and a place, from our own user. Length-capped and stripped of control and direction-changing characters, then passed on as typed — misspellings are the resolver's job. |
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

Importing is bought, and bounded twice over (see T11): by the monthly
allowance, and by a cap on how many refunded failures a month may add to it.
There is no request-rate limit inside the app (see
[Accepted risks](#accepted-risks)).

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
- **A preview is accepted or thrown away only by whoever paid for it.** A
  request key follows from the year and the place alone, so any account can
  know one. The job records the account that started the attempt, and only
  that account or an administrator may confirm or discard its preview
  (`main.require_importer`) — otherwise anyone could save numbers the importer
  would have rejected, or make a subscriber pay twice for the same import.

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

The modes that let a caller be anyone, `AUTH_MODE=off` and `stub`, refuse to
start on Cloud Run (`config.get_verifier`). `off` is also the default without a
project id, so a deploy that loses that variable fails to boot rather than
serving every caller as an unlimited administrator.

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
  nation and region to match the resolution. The neighbouring region and the
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

### T11 — Spending somebody else's money

The adversary here needs no page at all: a script, a free account, or the
cheapest subscription, and the goal is our bill or another user's payment.

- **Failed imports are refunded, but not without end.** Each failure ran the
  resolver and a model over pages. `accounts.attempt_limit` lets a month *start*
  its allowance plus as many again (at least 5) before it is spent, whether or
  not the failures were refunded. An import served from the store gives its
  attempt back, because it cost nothing.
- **Viewing costs a copy, not a read.** `cached_store.CachedElectionStore`
  keeps stored elections for 30 s in front of Firestore, so reloading the list
  in a loop is not one billed read per election per request. Jobs are never
  cached, and neither is a miss. Waiting on an import backs off to one look a
  second.
- **Bodies are bounded before they are read.** `main.LimitRequestBody` refuses
  anything over 64 KB (1 MB for the Stripe webhook) and any body that does not
  declare its length, so the unauthenticated webhook cannot be used to fill the
  container's memory.
- **One subscription per account.** Checkout is refused to an account that is
  already paying — a second checkout is a second charge — and tier changes go
  through Stripe's portal. A webhook about a subscription the account no longer
  pays through cannot drop it to free (`accounts._subscribed`), and a
  subscription event is applied as Stripe reads it back *now*, because Stripe
  does not deliver in order and a late `incomplete` would otherwise win.
- **The proxy cannot be walked around.** With `ORIGIN_SECRET` set, only
  requests carrying it are answered, so rate limits and bot checks at the proxy
  are not bypassed through the platform's own `run.app` address.
- **The page runs only our scripts.** Every response carries a
  `Content-Security-Policy` allowing scripts from this origin alone and the two
  Google sign-in endpoints, and refusing to be framed — a backstop behind T6.

## Accepted risks

These are known and deliberately not addressed here:

- **The model can simply be wrong.** Mis-reading a table needs no attacker. The
  confirmation step exists as much for this as for injection.
- **A user can confirm bad data.** The preview is a check, not a proof. The
  defence is that the data is inert and the identity is visible.
- **CSS-hidden text still reaches the model.** Text hidden with `display: none`
  is text; stripping it reliably would need a full CSS cascade. It arrives
  inside the fence with no more authority than the rest of the page.
- **No request-rate limiting in the app.** Importing needs a paid, confirmed
  account and is capped per month (T11); everything else is cheap and cached.
  Limiting requests per client belongs at the proxy in front, which sees real
  client addresses — the app behind Cloud Run's front end cannot reliably.
- **A DNS answer can change between the check and the connection.**
  `assert_public_url` resolves the host, and the HTTP client resolves it again.
  A rebinding host could point the second answer inward; on Cloud Run there is
  no private network to reach, and the metadata server refuses requests
  without its `Metadata-Flavor` header, which the fetcher never sends.
- **Two checkouts opened side by side both complete.** The refusal in T11
  needs the first subscription to have been reported; a user who pays twice in
  the same minute has two subscriptions, and the portal shows both.
- **`AUTH_MODE=sqlite` does not slow guessing down.** Where this app holds the
  passwords itself, nothing limits how fast sign-ins may be attempted; the
  scrypt cost of one attempt is the whole of the defence, and there is no
  lockout. A deployment reachable from the internet belongs behind something
  that rate-limits, or on Firebase, which does it for you.
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
| Failures are refunded only so often; only the importer confirms or discards a preview | `backend/tests/test_access.py`, `backend/tests/test_accounts.py`, `backend/tests/test_store.py` |
| Stored elections are read once per 30 s, never stale after a local write | `backend/tests/test_cached_store.py` |
| No second checkout; a replaced or out-of-order subscription cannot demote | `backend/tests/test_access.py`, `backend/tests/test_accounts.py`, `backend/tests/test_billing.py` |
| Security headers, body caps, the origin secret, nothing served beside the page | `backend/tests/test_edge.py` |
| Ungated auth modes refuse to start on Cloud Run | `backend/tests/test_config.py` |
| An internal failure reaches the user as one fixed sentence | `backend/tests/test_service.py` |

The adversarial fixtures themselves are `test/adversarial/`:
`injected-instructions.html` argues with the agent through five different
channels, and `markup-in-names.html` hides markup and a direction override in
party names.
