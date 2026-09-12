# Koalitionsberegner

Coalition-seat calculator. Pick parties, see whether they reach a majority.
Elections can be imported by naming a year and a place — the server finds the official results itself — and shared between users.

The renderer (`js/app.js`) is election-agnostic; elections come from an injected
provider and must pass the canonical schema validator in `js/election.js` before
being rendered. The backend (`backend/`) stores imported elections.

Where the results are read from is decided server-side: the election is looked up
in Wikipedia, whose article is read first where there is one, and the pages a
resolver agent found are read after it. An imported page is untrusted input
wherever it came from: it is fetched server-side, handed to a tool-less
extraction agent as fenced data, validated against the canonical schema, and
shown to the user for confirmation before anything is stored — and every
extracted string reaches the DOM as text, never as markup. See
[doc/threat-model.md](doc/threat-model.md).

An election that has not been held yet has no seats, so asking for one reads its
opinion polls instead and offers the newest of them as a list. The user picks a
poll, checks it, and saves it as a *forecast* — as many of them as they like. A
poll that publishes seat projections is used as stated; one that publishes only
vote shares has its seats allocated in code (`backend/app/seats.py`), and is
labelled as computed wherever it appears.

## Who may do what

Viewing is open; importing is what is sold, because an import is what makes the
server fetch a page and run the extraction agent.

| Caller | Sees | May import |
| --- | --- | --- |
| Signed out | the curated selection | no |
| Free account | every stored election | no |
| Basic subscriber | every stored election | a fixed number per month |
| Premium subscriber | every stored election | a larger number per month |

Accounts are free (Firebase Authentication, or a local SQLite store when there
is no Firebase project); subscriptions are Stripe, and a
tier only ever changes when Stripe says so over a signed webhook. Quotas reset
by calendar month with nothing scheduled — a counter labelled with a past month
simply reads as zero.

An import costs quota only when it starts a *new* extraction. An election
somebody already imported, or one being extracted right now, is served for
free — which is the point of a shared store. A failed extraction is refunded.

## Run locally

[uv](https://docs.astral.sh/uv/getting-started/installation/) is the only thing
to install — not even Python, which it fetches at the version
`backend/.python-version` pins:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # or: brew install uv
```

The import UI calls the API on the same origin, so run both from the backend:

```sh
cd backend
ELECTION_STORE=sqlite uv run uvicorn app.main:app --reload
```

`uv run` builds the environment from `backend/uv.lock` before running, so that
first command doubles as the install step, and there is no way to end up on
different versions than everyone else.

`sqlite` keeps imported elections in `./data/elections.db`, so restarting does not
re-fetch and re-extract pages you already imported. Use `memory` for a clean slate.
It also holds accounts, so tiers and quotas can be exercised locally with no
Firestore — see [Modes](doc/contribute.md#modes) for every switch, what it needs,
and the three combinations worth knowing.

Then open <http://localhost:8000>.

Frontend only — the bundled Folketing 2026 election renders, import is disabled:

```sh
uv run --no-project python -m http.server 8000   # or: npx wrangler dev
```

## Configure with `.env`

Rather than prefixing every command, put the switches in a `.env` at the repo
root — the backend reads it at startup, from wherever it was started:

```sh
cp .env.example .env
```

Anything already in the environment wins, so `LLM_MODE=live uv run …` still
overrides the file. `.env` is gitignored and excluded from the image; a
deployment gets its variables from Cloud Run and Secret Manager instead.
`ENV_FILE=path` names a different file, and `ENV_FILE=` loads none.

## Run with live LLM

With `ANTHROPIC_API_KEY` and `LLM_MODE=live` in `.env`, the plain command above
is enough. Otherwise:

```sh
cd backend
LLM_MODE=live ELECTION_STORE=sqlite ANTHROPIC_API_KEY=sk-ant-… \
  uv run uvicorn app.main:app --reload
```

## Test

```sh
node --test test/*.test.mjs      # frontend
cd backend && uv run pytest      # backend
```

The adversarial results pages in `test/adversarial/` drive the import pipeline's
prompt-injection and output-safety tests.

See [doc/contribute.md](doc/contribute.md) for cloud setup and environment
variables, [doc/stripe.md](doc/stripe.md) for a bare minimum Stripe account, and
[doc/threat-model.md](doc/threat-model.md) for the import pipeline's threat
model.
