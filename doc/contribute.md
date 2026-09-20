# Contributing

## Run everything

The page is served by the backend so the API is same-origin, which is what the
import UI expects.

```sh
cd backend
uv run uvicorn app.main:app --reload   # http://localhost:8000
uv run pytest
```

`uv run` resolves the environment from `uv.lock` before running, so the first
command doubles as the install step and nobody drifts onto a different version
of anything — including the interpreter: `backend/.python-version` pins it, and
uv downloads that Python if the machine has not got it. So
[uv](https://docs.astral.sh/uv/getting-started/installation/) is the whole
toolchain: nothing here asks you to match a system Python, create a virtualenv
or run `pip`. `ELECTION_STORE=sqlite` keeps imported elections across restarts.

Dependencies are edited through uv rather than by hand, so the lockfile stays in
step — `uv add httpx`, `uv add --dev pytest`, `uv remove …`. After changing
`pyproject.toml` directly, run `uv lock` and commit the result: the Dockerfile
builds with `--locked` and fails if the two disagree.

The interpreter is pinned the same way. `uv python pin 3.14` rewrites
`backend/.python-version`, and the `FROM` line in the root `Dockerfile` has to
move with it: the image builds with `UV_PYTHON_DOWNLOADS=never`, so uv must find
that exact version already in the base image, and a pin the base image cannot
satisfy fails the build rather than a revision. `requires-python` in
`pyproject.toml` is the wider range the code supports; the pin is the one
version everyone actually develops and ships on.

Editors get the same environment: `.vscode/settings.json` points the Python
extension at `backend/.venv` — the one uv builds — rather than at a system
interpreter, so what Pylance resolves is what `uv run pytest` imports.

Frontend on its own (bundled election only, no import):

```sh
uv run --no-project python -m http.server 8000   # or: npx wrangler dev
node --test test/*.test.mjs
```

`--no-project` because there is no project at the repo root to sync — it is only
uv's Python serving static files, so the frontend needs no toolchain of its own
either.

Three real election articles are checked in under `test/wikipedia/`, and
`backend/tests/test_articles.py` reads each of them the way an import does: the
document handed to the agent has to name the election, state every party's seats
in a row of its own, carry the colours the page publishes, and have dropped the
navigation, footnotes and tables that count something else. They are verbatim
slices of the live articles, so refresh them with
`uv run python tools/capture_articles.py` (from `backend/`) when Wikipedia
restructures something, and read the diff before committing it — a fixture that
quietly loses its seats column would take the test with it.

Before changing anything in the import path — the fetcher, the extraction
prompt, the schema, or how extracted strings are rendered — read
[threat-model.md](threat-model.md). It says which of those pieces is load-bearing
against a hostile results page, and which test holds each claim up.

### Modes

A handful of independent switches decide what the backend talks to. Each
defaults to whatever a bare checkout can actually do, so
`uv run uvicorn app.main:app` with no environment set at all starts and works:
in memory, every caller the owner, with a mocked extraction agent. Nothing
here needs a cloud account until you want one.

Set them on the command line, or keep them in a `.env` at the repo root —
`cp .env.example .env`. Both work at once, and the environment wins over the
file, so a one-off `LLM_MODE=live uv run …` overrides what `.env` says without
editing it. See [The .env file](#the-env-file) for the rules.

| Setting | What it does | Needs |
| --- | --- | --- |
| `ELECTION_STORE=firestore` | Elections and usage counts shared between instances. Default with a GCP project. | `GOOGLE_CLOUD_PROJECT`; `FIRESTORE_DATABASE` if not `(default)` |
| `ELECTION_STORE=sqlite` | One local file. Survives restarts, so you do not re-fetch and re-extract pages you already imported. | `SQLITE_PATH` (optional; defaults to `./data/elections.db`) |
| `ELECTION_STORE=memory` | Forgets everything on exit. Default with no GCP project. | — |
| `ADMIN_SECRET` set | Importing needs `x-admin-secret` to match; every other caller gets `403`. Required on Cloud Run — the boot refuses without it. | `ADMIN_SECRET` (32+ characters, like `ORIGIN_SECRET`) |
| `ADMIN_SECRET` unset | Every caller is the owner. The default locally; refused at boot on Cloud Run. | — |
| `LLM_MODE=mock` | Fetches the page, then returns a fixed Sachsen-Anhalt result instead of calling a model — or, for a year still to come, two fixed polls, one of them computed from vote shares. The default, and the whole pipeline except the model. | — |
| `LLM_MODE=live` | Runs the real extraction agent. | `ANTHROPIC_API_KEY` |
| `LLM_MODE=off` | Importing is refused outright. | — |
| `SEARCH_MODE=auto` | When a page states no seat counts, an import reads the pages it links to, then searches the web for one that does. Google if its keys are set, otherwise the Anthropic API's hosted search. The default. | `LLM_MODE=live` |
| `SEARCH_MODE=google` | The same, always through Google Programmable Search. | `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX` |
| `SEARCH_MODE=off` | An import reads only the pages the resolver named. | — |
| `WIKIPEDIA=on` | Each import looks the election up in Wikipedia and reads that article first, through Wikimedia's API. No key; the `User-Agent` identifies this project unless `WIKIPEDIA_CONTACT` names someone closer. The default. | `LLM_MODE=live` |
| `WIKIPEDIA=off` | No lookup: an import reads the resolver's candidates, and a Wikipedia URL among them goes to the ordinary fetcher — which Wikimedia answers with a 403. | — |
| Requests on | Anyone can ask for an election that has not been imported; it is filed as an issue and imported by hand. | `GITHUB_ISSUES_TOKEN`, `GITHUB_ISSUES_REPO` |
| Requests off | The page does not offer it. The default. | — |
| Reports on | Usage reports are emailed when the schedule calls `POST /api/internal/usage-reports`. Counting happens either way. | `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `REPORT_EMAIL_TO`; `USAGE_REPORT_SECRET` for the endpoint |
| Reports off | Nothing is emailed; an administrator reads a report at `GET /api/admin/usage?period=daily\|weekly\|monthly`. The default. | — |

Each variable named here is described in full under
[Environment variables](#environment-variables) below.

Half-configured search is refused at startup rather than silently disabled, and
so is any unrecognised value of the others — a misspelled `LLM_MODE` must not
quietly serve mock election data. `app.config.validate_configuration` builds
every configured dependency during startup so a typo stops the boot instead of
surfacing on the first real request.

Two combinations cover most local work:

```sh
cd backend

# Nothing to configure. Memory store, mocked agent, every caller the owner.
uv run uvicorn app.main:app --reload

# The real extraction agent against a real results page.
ELECTION_STORE=sqlite LLM_MODE=live ANTHROPIC_API_KEY=… \
  uv run uvicorn app.main:app --reload

# The same, reading only the first candidate — the Wikipedia article, unless
# WIKIPEDIA=off. Useful when you want to see exactly what one page yields.
ELECTION_STORE=sqlite LLM_MODE=live ANTHROPIC_API_KEY=… \
  IMPORT_PAGE_LIMIT=1 SEARCH_MODE=off uv run uvicorn app.main:app --reload
```

### From "Sachen-Anhalt 2026" to a seat distribution

A user types a year, a country, and — for a regional election — the region.
Nothing more, and not necessarily spelled correctly. Two agents turn that into
a stored election, and they are kept apart on purpose:

1. **`app.resolver`** is given only what the user typed, plus today's date. It
   searches, and may answer with one election's identity — nation, region, date,
   in English — and a list of candidate URLs. Reading "Germny" as Germany and
   knowing which day the election was held is its whole job. It never reads a
   results page, so nothing a hostile page wrote can reach it.
2. **`app.extractor`** is handed one candidate page at a time, with no tools,
   and reports what that page states. It is told which election is wanted, so
   it can answer "wrong_election" instead of extracting a different one.

`app.wikipedia` then looks that election up in Wikipedia's own search API and
puts its article at the front of the queue. An encyclopedia article is the one
page that reliably *states seats*: one table, the election named in its lead, and
the party colours in the markup — where an electoral authority publishes votes,
percentages, or a PDF at least as often as a seat allocation. The article is read
through the MediaWiki API rather than the ordinary fetcher, which is both what
Wikimedia's servers will answer and what lets BeautifulSoup cut a megabyte of
article down to the infobox, the lead and the result tables. It buys no trust:
the article is untrusted page content like any other, and the resolver's
candidates are still read, in order, when the article turns out not to be the
results.

`app.search` adds candidates on top of those, which matters for a deployment
whose `SEARCH_MODE` is a plain index rather than an agent.

Then `app.parser` checks, in code, that what came back is what was asked for:
the year must match the user's own input, and the nation and region must match
the resolution (compared with `identity.place_token`, so spelling and
punctuation do not matter). A page about the previous election, or the
neighbouring region, or the national result in place of a regional one, is
discarded and the next candidate read. That check is why this pipeline can be
pointed at a search engine at all — the models choose what to *read*, never what
counts as an answer.

Whatever page the numbers came from is what the stored election is attributed
to, and the preview names it, because nobody chose that address. Some hosts
refuse us whatever we do, and those candidates are passed over rather than worked
around — disguising the client is not something we do. Wikimedia is the one
exception, and not by disguise: it has an API for this, and it asks a caller to
identify itself rather than to look like a browser. So we do, in the `User-Agent`
— this project's URL by default, a deployment's own address through
`WIKIPEDIA_CONTACT`.

### An election that has not been held yet

When the resolver says an election is `upcoming` — or its date is after today,
whatever the flag says — there are no seats to read, and the import reads polls
instead:

1. The resolver lists pages that publish recent polls, and describes the
   assembly: its size, its threshold, and whether D'Hondt or Sainte-Laguë is the
   closer highest-averages method. `app.wikipedia` searches for the "Opinion
   polling for the next … election" article and puts it first.
2. `app.extractor` reads each page with a separate prompt and output schema
   (`ExtractedForecasts`): the newest polls, each one publisher's figures from
   one date, in `seats` or `percent` exactly as the page states them. It never
   converts one into the other.
3. `app.parser.forecast_election` turns each poll into an `Election` carrying a
   `forecast` — publisher, date, and `computed`. Seats are taken as stated;
   percentages go through `app.seats.allocate` over the resolver's assembly. A
   poll that cannot be made valid is dropped on its own, and reading carries on
   past the first good page until the page budget or ten forecasts is reached.
4. The job waits in `awaiting_choice` with the list, and the API answers
   `state: "choose"` with `forecasts`. `POST …/confirm?option=N` saves one;
   the job keeps offering the rest until its lease runs out, so more than one
   can be saved and asking again later reads newer polls.

A stored forecast is its own identity — the election's plus publisher and date
— so two polls never collide, and a result's hash is exactly what it was before
forecasts existed. `find_by_place` ignores forecasts, so a saved poll never stops
somebody importing a newer one, or the result once there is one.

The computed seats are an approximation: one nationwide allocation, with no
constituency seats, overhang, regional thresholds or reserved seats. That is why
they are labelled in the list, the preview and the calculator's footer.

### Cloud setup (deployment only)

1. A GCP project with the **Firestore** and **Cloud Run** APIs enabled.
2. A Firestore database in **Native mode** — single-flight parsing relies on its
   transactions. The `elections`, `extraction_jobs`, `pages`, `usage_daily`
   and `usage_reports` collections are created on demand.
3. Two composite indexes for `outreach_drafts`, before the first outreach send
   on this project — without them `FirestoreOutreachStore.count_posted` and
   `count_posted_total` 500 with `FAILED_PRECONDITION`, and the etiquette caps
   in `doc/plans/04-reddit-outreach.md` never run:
   ```sh
   gcloud firestore indexes composite create --collection-group=outreach_drafts \
     --field-config field-path=subreddit,order=ascending \
     --field-config field-path=status,order=ascending \
     --field-config field-path=posted_at,order=ascending

   gcloud firestore indexes composite create --collection-group=outreach_drafts \
     --field-config field-path=status,order=ascending \
     --field-config field-path=posted_at,order=ascending
   ```
   Add `--database=NAME` to both if `FIRESTORE_DATABASE` is not `(default)`.
   `thread_posted`'s query (`thread_id ==` and `status ==`, two equalities and
   no range filter) needs no composite index — Firestore serves that from its
   automatic single-field indexes, the one case exempt from this. The same two
   indexes are defined in `firestore.indexes.json` at the repo root, for a
   deploy that uses the Firebase CLI instead (`firebase deploy --only
   firestore:indexes`).
4. A service account for the Cloud Run revision holding `roles/datastore.user`,
   plus `roles/secretmanager.secretAccessor` once `LLM_MODE=live` or
   `ADMIN_SECRET` is mounted from Secret Manager.
5. For live extraction: an Anthropic API key in Secret Manager, mounted as
   `ANTHROPIC_API_KEY` (`--set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest`).
   The agent runs `claude-opus-5` server-side, so the key never reaches the browser.
6. `ADMIN_SECRET` in Secret Manager, mounted the same way. The boot refuses to
   start on Cloud Run without it — see [Modes](#modes).

The root `Dockerfile` builds one image serving both the API and the page:

```sh
gcloud run deploy koalitionsberegner \
  --source . --region europe-north1 \
  --service-account koalitionsberegner@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_CLOUD_PROJECT=PROJECT_ID
```

To host the page separately (e.g. Cloudflare) instead, point the page at the API
with `<meta name="api-base" content="https://…">` in `index.html` and set
`ALLOWED_ORIGINS` on the backend.

### The .env file

`app/env.py` reads a `.env` before anything is configured, so a local run needs
nothing exported. Four rules are worth knowing, because each one is there to
stop a specific failure:

- **The real environment always wins.** A variable already set is never
  replaced. `LLM_MODE=live uv run …` still overrides the file, and a `.env` that
  slips into an image cannot shadow what Cloud Run set.
- **The file is found by walking up** from the working directory, so the
  repo-root file is picked up whether the server was started in `backend/` or at
  the root. `ENV_FILE=path` names one explicitly; `ENV_FILE=` loads none, which
  is what `backend/tests/conftest.py` does so a developer's keys never reach the
  suite.
- **A file it cannot read stops the boot** — a malformed line, or an `ENV_FILE`
  that does not exist. Same reason an unrecognised `LLM_MODE` is fatal: a typo
  that silently leaves `ANTHROPIC_API_KEY` unset is found by the first user
  instead of by whoever made it.
- **Only names are logged, never values.** The startup line says which file was
  used and which variables came from it.

Syntax is `NAME=value`, one per line, with `#` comments, a tolerated leading
`export`, and quotes when a value has spaces. No line continuations and no
`$VAR` expansion — this is a file of literals, and a shell is what expands
things.

`.env` is in both `.gitignore` and `.dockerignore`; a deployment gets its
variables from `--set-env-vars` and Secret Manager.

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | with `ELECTION_STORE=firestore` | — | GCP project holding Firestore. |
| `FIRESTORE_DATABASE` | no | `(default)` | Named Firestore database, if not the default one. |
| `ELECTION_STORE` | no | `firestore`, or `memory` with no GCP project | `firestore` shares imports between instances; `sqlite` keeps them in a local file across restarts; `memory` forgets everything on exit. |
| `SQLITE_PATH` | no | `./data/elections.db` | Database file for `ELECTION_STORE=sqlite`. Created with its parent directory on first use. |
| `LLM_MODE` | no | `mock` | `mock` returns a fixed Sachsen-Anhalt result without calling Anthropic; `live` runs the real extraction agent; `off` disables importing. |
| `ANTHROPIC_API_KEY` | with `LLM_MODE=live` | — | Anthropic API key. Store it in Secret Manager and mount it as this env var; never put it in the image or in client code. |
| `PARSE_LEASE_SECONDS` | no | `300` | How long a parse may run before a crashed job is reclaimed. |
| `IMPORT_PAGE_LIMIT` | no | `3` | Candidate pages one import may read. Each is a fetch and a model call. `1` reads only the resolver's best answer. |
| `IMPORT_SEARCH_LIMIT` | no | `3` | Candidates a search engine may contribute beyond the ones the resolver named. `0` disables searching without touching `SEARCH_MODE`. |
| `SEARCH_MODE` | no | `auto` | Which engine that search uses: `google` is Programmable Search; `anthropic` is the search tool the Anthropic API hosts; `off` reads only the resolver's candidates. `auto` picks Google where it is configured, otherwise `off` — the resolver has already searched, and paying twice for the same hosted tool is not a useful default. Always off unless `LLM_MODE=live`. |
| `GOOGLE_SEARCH_API_KEY` / `GOOGLE_SEARCH_CX` | with `SEARCH_MODE=google` | — | API key and the id of a [Programmable Search engine](https://programmablesearchengine.google.com/) set to search the whole web. Named explicitly, both are required at startup even under a mocked agent. |
| `WIKIPEDIA` | no | `on` | Whether an import looks the election up in Wikipedia and reads that article before the resolver's own candidates — an article states seats where an electoral authority often states only votes. Always off unless `LLM_MODE=live`. |
| `WIKIPEDIA_LANGUAGE` | no | `en` | Which Wikipedia to search, as a language code. English has an article for an election held anywhere; another language often has the better one for its own country. Validated at startup in every mode, because it becomes a hostname. |
| `WIKIPEDIA_CONTACT` | no | this project's repository URL | The address in the `User-Agent` of our Wikipedia requests, as [Wikimedia's policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy) requires — an email or a URL. There is always one, because a client that identifies nobody is answered `403 Please respect our robot policy`; set this so a Wikimedia administrator reaches *you* and not the project. |
| `IMPORT_MAX_WAIT_SECONDS` | no | `25` | Ceiling on `?wait_seconds=` long-polling. |
| `ALLOWED_ORIGINS` | no | — | Comma-separated origins allowed to call the API, when the page is hosted elsewhere. Unset means same-origin only. |
| `ORIGIN_SECRET` | no | — | When set (at least 32 characters), only requests carrying it in `X-Origin-Secret` are answered, `/healthz` aside. Set it when a proxy such as Cloudflare fronts the app, so the `run.app` address stops answering on its own. |
| `FRONTEND_DIR` | no | repo root | Directory holding `index.html`; served at `/` when present. |
| `ADMIN_SECRET` | required on Cloud Run | — | At least 32 characters, same rule as `ORIGIN_SECRET`. Importing needs `x-admin-secret` to match this value; every other caller gets `403`. With none set, every caller is the owner — right for a local run, refused at boot on Cloud Run. |
| `PUBLIC_BASE_URL` | for the outreach approval email | — | Where this site is reachable, as an absolute URL, no trailing slash. Used to build the `/approve/{token}` link a queued draft is emailed with ([04-reddit-outreach.md](plans/04-reddit-outreach.md)); without it the email carries a relative link instead. |
| `GITHUB_ISSUES_TOKEN` | for election requests | — | Fine-grained PAT with **Issues: write** on `GITHUB_ISSUES_REPO` and nothing else. Unset means the page is told not to offer requests. Not named `GITHUB_TOKEN` on purpose: GitHub Actions and several agent runtimes export that name, and a token that happens to be in the environment is not a decision to open issues with it. |
| `GITHUB_ISSUES_REPO` | with `GITHUB_ISSUES_TOKEN` | — | Repository the requests are filed on, as `owner/name`. Set without a token, or in any other shape, it is a startup error rather than a guess. |
| `SMTP_HOST` | for emailed usage reports | — | Mail server the reports are submitted to, e.g. `smtp.gmail.com`. Set together with the three below, or not at all; a partial set is a startup error. |
| `SMTP_PORT` | no | `587` | `465` is TLS from the first byte; any other port is upgraded with STARTTLS before the password is sent. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | with `SMTP_HOST` | — | The login. With Gmail, an app password. Put the password in Secret Manager. |
| `REPORT_EMAIL_TO` | with `SMTP_HOST` | — | Comma-separated addresses the reports go to. |
| `REPORT_EMAIL_FROM` | no | `SMTP_USERNAME` | The sender address, where the server allows a different one. |
| `USAGE_REPORT_SECRET` | for scheduled usage reports | — | At least 32 characters. The Cloudflare Worker's cron presents it in `X-Report-Secret`. If it is unset, `POST /api/internal/usage-reports` does not exist. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USERNAME` / `REDDIT_PASSWORD` / `REDDIT_USER_AGENT` | for posting approved outreach replies | — | A script-app OAuth login, all five or none ([04-reddit-outreach.md](plans/04-reddit-outreach.md)). Missing any one leaves the approval queue and email working; only `POST /api/outreach/approval/{token}/send` answers `503`. Never logged, never in a `repr`. |
| `OUTREACH_ALLOWED_SUBREDDITS` | for posting | — (nothing is allowed) | Comma-separated subreddit names this deployment may post to. Its own list, not the scanner's `OUTREACH_SUBREDDITS`. |
| `OUTREACH_SUBREDDIT_WEEKLY_CAP` | no | `2` | Posted replies per subreddit per rolling seven days, enforced server-side regardless of what the scanner queued. |
| `OUTREACH_DAILY_CAP` | no | `3` | Posted replies in total per rolling 24 hours. |
| `LOG_LEVEL` | no | `INFO` | Level for the `app.*` loggers. Every call out — page fetch, extraction agent, Firestore — logs a `start` line and a matching `ok`/`failed` line with a duration; `WARNING` keeps only the failures. |
| `ENV_FILE` | no | nearest `.env` walking up from the working directory | A different file to read variables from. Empty loads none. A path that does not exist is a startup error. |
| `PORT` | no | `8080` | Set by Cloud Run. |

Credentials come from Application Default Credentials — the attached service account
on Cloud Run, or `gcloud auth application-default login` locally.
