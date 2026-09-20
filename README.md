# Koalitionsberegner

Coalition-seat calculator, for every election rather than one country's.
Pick parties, see whether they reach a majority, share the result. One tool
instead of a spreadsheet per election is the whole pitch: the renderer knows
nothing about any particular country, assembly size or majority threshold —
every number and name it shows comes from an election that passed the same
schema check, whether it is the one bundled with the page, one a visitor
asked for, or one the app went and read on its own.

Elections get in three ways: a visitor names a year and a place and the
server reads the official results itself; the owner imports one directly,
poll by poll; or the app tracks an election ahead of time from a public
calendar and reads it on a schedule that tightens as the day approaches,
without anyone asking (`doc/plans/03-remaining-work.md`, Section A). Nothing
about who asked, or who is looking, is ever stored — see
[Usage reports and privacy](#usage-reports-and-privacy) below.

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

Viewing is open to everyone; importing is the one thing that spends money
(fetching a page and running the extraction agent), so it is the owner's
alone.

| Caller | Sees | May ask for an election | May import | May curate |
| --- | --- | --- | --- | --- |
| Anyone | every stored election | yes | no | no (curation is gone) |
| The owner, with `x-admin-secret` | every stored election | yes | yes | — |
| The schedule, with `x-report-secret` | `/api/internal/*` only | — | — | — |

### Asking for an election instead

Anyone who cannot import reaches the same form and presses the same button.
What they cannot do is make the server go and read pages, which is the part
that costs money — so the election they named is filed as an issue on this
repository instead (`label:election-request`) and imported by hand later. The
tracker *is* that queue: asking twice for the same election finds the open
issue and points at it rather than opening a second one. The issue names the
election and nothing about who asked: the tracker is public.

**No secret is needed to ask.** The reasoning that gates importing does not
apply here: nothing about asking searches, fetches, extracts or spends, and a
visitor is the person most likely to find an election missing. What it does
mean is a public write on behalf of a caller nobody authenticated, bounded by
one issue per election and by the rate limiting in front of the app; see
[T12](doc/threat-model.md) for what that does and does not cover.

Requests need `GITHUB_ISSUES_TOKEN` and `GITHUB_ISSUES_REPO`; without them
`/api/config` says so and the page does not offer it.

## Sharing a coalition

The share button next to *Clear all* copies a link of the shape
`/e/<id>?c=<parties>&s=<seats>`: `<id>` is a prefix of the election's stored
hash, `c` the ticked parties as positions in the order the page lists them,
and `s` the seat total at the time, used only to notice a later correction.
Opening the link restores exactly that selection.

Pasted into Slack, Discord, Signal, iMessage, Reddit or Facebook, the same
link unfurls into an image of the seat bar, the total and the verdict — built
server-side (`GET /api/og/<id>.png`, `backend/app/og_image.py`) from the same
wording as the page's `<meta>` tags (`GET /api/elections/<id>/card`,
`backend/app/share.py`), so the two never disagree. The Cloudflare Worker
(`worker/index.js`) injects those tags into the page for crawlers, and caches
the card and the image for an hour so a link pasted into a busy thread costs
one fetch to the origin rather than one per viewer or crawler. Each of those
fetches — at most one per link per hour — is counted in a shared-link-preview
counter alongside the other usage numbers; it is not a count of how many
people opened the link. The card is always in English: crawlers have no
language of their own.

## Usage reports and privacy

The owner gets a daily, weekly and monthly email about how the app is used:

- page loads by language
- elections picked
- imports started, saved and discarded, and how many failed
- requests filed
- shared-link previews built (`GET /api/elections/<id>/card`), at most once per link per hour, crawlers included — nothing about who opened it or which election

Only counts are stored: a day is a handful of numbers, and nothing says who,
which election, or from where. There are no accounts, so there is nothing per
person to count either. Nothing new runs in the browser: page loads are
counted from the config request the page already makes. The page's
*Privacy* section says what is processed and why. See `backend/app/usage.py`,
and [doc/cloudflare.md](doc/cloudflare.md#6-usage-reports-by-email) for the
schedule.

When someone asks to see, correct or delete their data, follow
[doc/privacy-requests.md](doc/privacy-requests.md).

Try it locally. Ungated, so every caller is an administrator; add the four
`SMTP_*`/`REPORT_EMAIL_TO` variables from `.env.example` to really send email:

```sh
cd backend
ELECTION_STORE=sqlite uv run uvicorn app.main:app --reload

# in a second terminal: open http://localhost:8000 and click around, then
TOMORROW=$(date -u -d tomorrow +%F 2>/dev/null || date -u -v+1d +%F)
curl -s "localhost:8000/api/admin/usage?period=weekly&before=$TOMORROW" | python3 -c 'import json,sys; print(json.load(sys.stdin)["body"])'
curl -s -X POST -H 'x-report-secret: local-report-secret-at-least-32-chars' 'localhost:8000/api/internal/usage-reports?period=daily'   # 502 until SMTP is set

uv run pytest tests/test_usage.py       # the counting, the reports and the endpoints
```

## Licence

The code is [AGPL-3.0](LICENSE), chosen because this is a hosted service: the
AGPL's network clause keeps a fork that offers it as a service to others open
too, not just a fork that stays on someone's own laptop.

## Data and its licence

Where results and polls are read from is named on every election — the
figures under the calculator say "Figures from *hostname*" — and the source
page itself stays one click away for anyone who wants to check. Data read
from Wikipedia carries the site's own
[CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/) licence, which
requires that attribution to stay visible and that anything derived from it
stays share-alike; the note under the calculator (`data.attribution` in
`js/strings.csv`) exists for exactly that reason. This matters beyond the
page itself: any later commercial licensing of this project's *data* (as
opposed to the code) has to keep the Wikipedia-derived parts share-alike,
whatever licence the rest of it carries.

## Funding

No ads: they pay badly on political content, would need a consent banner for
anything personalised, and would cost the one thing that sets this page
apart from one with a sponsor's thumb on the scale.

Instead, a single donate/sponsor link in the footer — GitHub Sponsors, Ko-fi
or MobilePay, whichever the deployment's owner sets as `SUPPORT_LINK` — and
an optional "Supported by …" line (`SUPPORT_SPONSOR`) that stays hidden until
someone is named. Both are documented in `.env.example` and
[doc/contribute.md](doc/contribute.md). Beyond that, this README is written
to double as the pitch for the grants an open-source, privacy-respecting
civic tool can apply to: NLnet (NGI Zero), the Prototype Fund (Germany),
the EU's Next Generation Internet calls, and Danish democracy or
digitisation foundations.

## Embedding and the API

`GET /api/elections` (every stored election, newest `election_date` first)
and `GET /api/elections/<hash>` (one election) are open to anyone, need no
secret, and are meant to be stable enough to embed — a newsroom's page, for
instance. Both send `Cache-Control: public, max-age=60`, and `ALLOWED_ORIGINS`
on the backend is the CORS half of that story: set it to the calling origin
and the browser will allow the read. Nothing further is built on top of this
today — no client library, no versioned API path — until somebody asks for
it; the shape is already the `ElectionSummary`/election JSON the page itself
uses; see `backend/app/main.py`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — in short: everyone may ask for a
missing election from the page itself, no account needed; code changes are
pull requests against a green `node --test test/*.test.mjs` and
`cd backend && uv run pytest`; and
[doc/contribute.md](doc/contribute.md) has cloud setup and every environment
variable.

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
See [Modes](doc/contribute.md#modes) for every switch, what it needs, and the
combinations worth knowing.

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
variables, and [doc/threat-model.md](doc/threat-model.md) for the import
pipeline's threat model.
