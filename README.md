# Koalitionsberegner

Coalition-seat calculator. Pick parties, see whether they reach a majority.
Elections can be imported from an official results URL and shared between users.

The renderer (`js/app.js`) is election-agnostic; elections come from an injected
provider and must pass the canonical schema validator in `js/election.js` before
being rendered. The backend (`backend/`) stores imported elections.

An imported page is untrusted input: it is fetched server-side, handed to a
tool-less extraction agent as fenced data, validated against the canonical
schema, and shown to the user for confirmation before anything is stored — and
every extracted string reaches the DOM as text, never as markup. See
[doc/threat-model.md](doc/threat-model.md).

## Who may do what

Viewing is open; importing is what is sold, because an import is what makes the
server fetch a page and run the extraction agent.

| Caller | Sees | May import |
| --- | --- | --- |
| Signed out | the curated selection | no |
| Free account | every stored election | no |
| Basic subscriber | every stored election | a fixed number per month |
| Premium subscriber | every stored election | a larger number per month |

Accounts are free (Firebase Authentication); subscriptions are Stripe, and a
tier only ever changes when Stripe says so over a signed webhook. Quotas reset
by calendar month with nothing scheduled — a counter labelled with a past month
simply reads as zero.

An import costs quota only when it starts a *new* extraction. An election
somebody already imported, or one being extracted right now, is served for
free — which is the point of a shared store. A failed extraction is refunded.

## Run locally

The import UI calls the API on the same origin, so run both from the backend:

```sh
cd backend
ELECTION_STORE=sqlite uv run uvicorn app.main:app --reload
```

`uv run` builds the environment from `backend/uv.lock` on first use, so there is
no install step to remember and no way to end up on different versions than
everyone else. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
if you have not.

`sqlite` keeps imported elections in `./data/elections.db`, so restarting does not
re-fetch and re-extract pages you already imported. Use `memory` for a clean slate.

Then open <http://localhost:8000>.

Frontend only — the bundled Folketing 2026 election renders, import is disabled:

```sh
python3 -m http.server 8000   # or: npx wrangler dev
```

## Test

```sh
node --test test/*.test.mjs      # frontend
cd backend && uv run pytest      # backend
```

The adversarial results pages in `test/adversarial/` drive the import pipeline's
prompt-injection and output-safety tests.

See [doc/contribute.md](doc/contribute.md) for cloud setup and environment
variables, and [doc/threat-model.md](doc/threat-model.md) for the import
pipeline's threat model.
