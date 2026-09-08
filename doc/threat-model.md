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
 user pastes URL
        │
        ▼
 [1] backend fetch          fetcher.py   ── SSRF checks, size/type/redirect caps
        │  HTML → text                      script/style/comments dropped
        ▼
 [2] extraction agent       extractor.py ── page fenced as data, no tools,
        │  structured output                one turn, one output schema
        ▼
 [3] validator              schema.py    ── allowlisted fields, types, bounds,
        │  Election                         seat/majority consistency, safe text
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

Steps 1–5 run server-side. The browser never fetches the imported page and never
sees the API key.

## Trust boundaries

| Input | Trusted? | Notes |
| --- | --- | --- |
| The pasted URL | No | Attacker-chosen address; it is the only thing the user supplies. |
| The fetched page | No | Fully attacker-controlled bytes. Every field of an imported election ultimately derives from it. |
| The model's output | No | It read untrusted input, so its answer is untrusted too. Constrained by the output schema, then re-validated. |
| The stored election | Partly | It passed validation and a human confirmation. Still rendered as text only. |
| Our own prompts and code | Yes | Built server-side from constants; no part of the page reaches them as instructions. |

The adversary is whoever controls a page a user pastes — including the operator
of a legitimate-looking results site, or anyone who can get a URL in front of a
user. They can serve any bytes, any redirects, any encoding, and can vary the
response per request.

Their goals, in the order we care about: make the agent do something other than
extract (act, browse, leak), get executable or deceptive content in front of
another user, poison the shared election store, or burn our budget.

## Threats and defences

### T1 — The URL as a server-side request forgery primitive

`http://169.254.169.254/` would hand cloud instance metadata to the model, and
`http://10.0.0.5/` would reach inside the network.

`fetcher._assert_public_url` resolves the hostname and rejects any address that
is not globally routable, before every request *and again for every redirect
hop* (redirects are not followed by the HTTP client; the fetcher re-checks each
`Location` itself). Only `http`/`https` are accepted, at most 3 redirects, a
20 s timeout, 5 MB of body, and only HTML/plain-text content types.

### T2 — Resource exhaustion

Response bodies are capped at 5 MB and the flattened text at 400 000 characters.
An over-long page is **rejected, never truncated**: a cut-off results table
yields a plausible but wrong election, which is worse than a failed import.

Extraction is single-flight per page (`store.claim`): concurrent imports of the
same URL share one model call, and a page already imported is served from
storage without fetching or extracting anything at all.

Residual: there is no per-user rate limit and no authentication (see
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
so "now go and read this other URL" has no fetcher to reach.

The model's only output channel is one `ExtractedElection` object with
`extra="forbid"`: a demand to "add a field called `exfiltrate`" cannot be
satisfied, because the object has nowhere to put it.

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

Party names, block names and titles are attacker-influenced strings that the UI
displays. They are never parsed as markup:

- The renderer builds elements with `document.createElement` and sets text with
  `textContent`. `innerHTML` is assigned only the empty string, to clear a
  container; `insertAdjacentHTML`, `outerHTML`, `document.write`, `eval` and
  `new Function` do not appear in `js/`. `test/injection.test.mjs` enforces both
  claims by scanning the sources, and renders an adversarial election through a
  DOM stub that throws if any markup is ever written.
- A party name of `<script>alert(1)</script>` is therefore a *wrong name*, not a
  vulnerability. It is stored verbatim and shown verbatim.
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

### T8 — Secrets and logs

The Anthropic key lives in Secret Manager, is mounted into the Cloud Run
revision, and is used only server-side; it never reaches the browser. Extraction
logs record sizes, token counts, stop reasons and durations — never page text
and never model output (`extractor.AnthropicExtractor.extract`). Pasted URLs
*are* logged, as the fetch target.

## Accepted risks

These are known and deliberately not addressed here:

- **The model can simply be wrong.** Mis-reading a table needs no attacker. The
  confirmation step exists as much for this as for injection.
- **A user can confirm bad data.** The preview is a check, not a proof. The
  defence is that the data is inert and the identity is visible.
- **CSS-hidden text still reaches the model.** Text hidden with `display: none`
  is text; stripping it reliably would need a full CSS cascade. It arrives
  inside the fence with no more authority than the rest of the page.
- **No authentication or rate limiting.** Anyone who can reach the API can
  import, and imports are visible to everyone. Cost is bounded by the caps in
  T2 and by single-flight extraction, not by identity.
- **The page can vary per request.** The page we fetched is the page we
  extracted; nothing guarantees a later visitor sees the same thing.

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

The adversarial fixtures themselves are `test/adversarial/`:
`injected-instructions.html` argues with the agent through five different
channels, and `markup-in-names.html` hides markup and a direction override in
party names.
