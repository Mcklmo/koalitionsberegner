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

| Caller | Sees | May import | May ask for an election |
| --- | --- | --- | --- |
| Signed out | the curated selection | no | yes |
| Free account | every stored election | no | yes |
| Basic subscriber | every stored election | a fixed number per month | — it imports |
| Premium subscriber | every stored election | a larger number per month | — it imports |

Accounts are free (Firebase Authentication, or a local SQLite store when there
is no Firebase project); subscriptions are Stripe, and a
tier only ever changes when Stripe says so over a signed webhook. Quotas reset
by calendar month with nothing scheduled — a counter labelled with a past month
simply reads as zero.

An import costs quota only when it starts a *new* extraction. An election
somebody already imported, or one being extracted right now, is served for
free — which is the point of a shared store. A failed extraction is refunded.

### Asking for an election instead

Anyone who cannot import reaches the same form and presses the same button.
What they cannot do is make the server go and read pages, which is the part
that costs money — so the election they named is filed as an issue on this
repository instead (`label:election-request`) and imported by hand later. The
tracker *is* that queue: asking twice for the same election finds the open
issue and points at it rather than opening a second one.

**No account is needed**, and that is the one route in the import flow where
none is — `/api/elections/lookup` and everything past it need one. The
reasoning that gates the rest does not apply here: nothing about asking
searches, fetches, extracts or spends, and a visitor who never signs in is the
person most likely to find an election missing. What it does mean is a public
write on behalf of a caller nobody authenticated, bounded by one issue per
election and by the rate limiting in front of the app; see
[T12](doc/threat-model.md) for what that does and does not cover.

A signed-in requester is named in the issue so they can be told when the
election lands — but only if their address is confirmed. An unconfirmed one was
merely typed by somebody, and naming it would publish a stranger's address on a
public issue, so those are filed anonymously like a signed-out request.

A subscriber who has merely run out for the month is not sent here — the
allowance comes back at the month's end, which is a better answer than a
hand-written issue.

Requests need `GITHUB_ISSUES_TOKEN` and `GITHUB_ISSUES_REPO`; without them
`/api/config` says so and the page does not offer it.

### Payments are closed right now

Checkout is turned off (`PAYMENTS_PAUSED`, on by default): the card form does
not work, so nothing is offered for sale, `POST /api/billing/checkout` answers
`503`, and the account panel asks people to come back tomorrow and to request
the election they wanted in the meantime. Nothing about an existing
subscription changes — tiers keep working and Stripe's portal stays open, so
nobody is trapped in a subscription they cannot cancel. Set `PAYMENTS_PAUSED`
to `false` to sell again; it is the only thing that has to change.

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

Frontend only — the bundled Denmark — 2026 election renders, import is disabled:

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
