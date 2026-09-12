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
of anything. `ELECTION_STORE=sqlite` keeps imported elections across restarts.

Dependencies are edited through uv rather than by hand, so the lockfile stays in
step — `uv add httpx`, `uv add --dev pytest`, `uv remove …`. After changing
`pyproject.toml` directly, run `uv lock` and commit the result: the Dockerfile
builds with `--locked` and fails if the two disagree.

Frontend on its own (bundled election only, no import):

```sh
python3 -m http.server 8000   # or: npx wrangler dev
node --test test/*.test.mjs
```

Before changing anything in the import path — the fetcher, the extraction
prompt, the schema, or how extracted strings are rendered — read
[threat-model.md](threat-model.md). It says which of those pieces is load-bearing
against a hostile results page, and which test holds each claim up.

### Modes

Four independent switches decide what the backend talks to. Each defaults to
whatever a bare checkout can actually do, so `uv run uvicorn app.main:app` with
no environment set at all starts and works: in memory, ungated, with a mocked
extraction agent. Nothing here needs a cloud account until you want one.

Set them on the command line, or keep them in a `.env` at the repo root —
`cp .env.example .env`. Both work at once, and the environment wins over the
file, so a one-off `LLM_MODE=live uv run …` overrides what `.env` says without
editing it. See [The .env file](#the-env-file) for the rules.

The one coupling worth knowing: **accounts live wherever the elections do**.
`ELECTION_STORE` picks the database for both, so `sqlite` gives you real
accounts, tiers and quotas — persisted across restarts — with no Firestore
anywhere. `AUTH_MODE` is a separate question, about who a request *is*.

| Setting | What it does | Needs |
| --- | --- | --- |
| `ELECTION_STORE=firestore` | Elections and accounts shared between instances. Default with a GCP project. | `GOOGLE_CLOUD_PROJECT`; `FIRESTORE_DATABASE` if not `(default)` |
| `ELECTION_STORE=sqlite` | One local file. Survives restarts, so you do not re-fetch and re-extract pages you already imported. | `SQLITE_PATH` (optional; defaults to `./data/elections.db`) |
| `ELECTION_STORE=memory` | Forgets everything on exit. Default with no GCP project. | — |
| `AUTH_MODE=firebase` | Verifies real Firebase ID tokens against Google's certificates. Default once a project id is set. | `FIREBASE_PROJECT_ID` (falls back to `GOOGLE_CLOUD_PROJECT`); `FIREBASE_API_KEY` for browser sign-in |
| `AUTH_MODE=sqlite` | Real gating with no identity provider: this app holds the passwords and issues its own session tokens, in the same file as everything else. Sign-in works in the browser. | `SQLITE_PATH` (optional); pair it with `ELECTION_STORE=sqlite` |
| `AUTH_MODE=stub` | The bearer token *is* the identity — `uid`, `uid:email`, `uid:email:admin`. Nothing is verified. Exercises signed-in state, quota accounting and admin curation; the browser cannot mint these tokens, so drive the API with `curl`. | — |
| `AUTH_MODE=off` | No gating at all: every request is one admin developer with no quota. Default with no project id. | — |
| `LLM_MODE=mock` | Fetches the page, then returns a fixed Sachsen-Anhalt result instead of calling a model. The default, and the whole pipeline except the model. | — |
| `LLM_MODE=live` | Runs the real extraction agent. | `ANTHROPIC_API_KEY` |
| `LLM_MODE=off` | Importing is refused outright. | — |
| Billing on | Subscriptions are for sale; Stripe is the only thing that grants a tier. | `STRIPE_API_KEY`, `STRIPE_PRICE_BASIC` and/or `STRIPE_PRICE_PREMIUM`, `STRIPE_WEBHOOK_SECRET`, `PUBLIC_BASE_URL` |
| Billing off | Free accounts work, nothing is for sale. The default. | — |

Each variable named here is described in full under
[Environment variables](#environment-variables) below.

`firebase` and `sqlite` differ only in where the credential comes from. The
rules applied to it afterwards are one object either way — a subject is
mandatory, `ADMIN_EMAILS` is what grants curation rights — so behaviour you
verify under `sqlite` is the behaviour a Firebase deployment has. The seam is
`app.auth.StoreBackedVerifier`: a credential store plus
`app.auth.PrincipalRules`, wired in `app.config.get_verifier`. Adding a third
backend means writing a store, not another verifier.

One sharp edge shared by `sqlite` and `stub`: the account they create is on the
free tier, and free imports nothing. `POST /api/imports` answers `402` until a
subscription raises the tier, and the only thing that raises a tier is a Stripe
webhook — there is deliberately no endpoint that grants one. So those are the
modes for watching the gate *refuse*, and for admin curation; `off` is the mode
for driving a successful import; a successful *paid* import needs Stripe test
mode, below.

Billing is the one switch with no mode variable: it follows `STRIPE_API_KEY`.
Half-configured billing is refused at startup rather than silently disabled, and
so is any unrecognised value of the other three — a misspelled `LLM_MODE` must
not quietly serve mock election data. `app.config.validate_configuration` builds
every configured dependency during startup so a typo stops the boot instead of
surfacing on the first real request.

Three combinations cover most local work:

```sh
cd backend

# Nothing to configure. Memory store, no gating, mocked agent.
uv run uvicorn app.main:app --reload

# Real accounts and gating, persisted, no cloud anything: sign up in the page
# itself. Imports are then refused with 402 — that is the free tier working,
# not a misconfiguration.
ELECTION_STORE=sqlite AUTH_MODE=sqlite uv run uvicorn app.main:app --reload

# The real extraction agent against a real results page.
ELECTION_STORE=sqlite LLM_MODE=live ANTHROPIC_API_KEY=… \
  uv run uvicorn app.main:app --reload
```

### Working on accounts, tiers and quotas

Take the `sqlite` + `sqlite` line from [Modes](#modes) above. The sign-in panel
works as it does in a deployment — sign up in the page, or from the shell:

```sh
curl -X POST localhost:8000/api/auth/register \
  -H 'content-type: application/json' \
  -d '{"email": "boss@example.org", "password": "hunter22"}'   # → {"token": …}

curl localhost:8000/api/me -H "Authorization: Bearer $TOKEN"
curl -X PUT localhost:8000/api/elections/HASH/selected \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"selected": true}'
```

`/api/auth/register`, `/api/auth/login` and `/api/auth/logout` exist only in
this mode; every other mode answers `404`, because there is no account here to
create. The token is an opaque session, stored as its SHA-256 and revoked by
signing out; the password is stored as a salted scrypt hash and never leaves the
process in any other form. Everything behind it is the same machinery Firebase
sign-ins reach: the account row, its tier and the quota counter all live in
`./data/elections.db`. `ADMIN_EMAILS=boss@example.org` makes the curation call
above work, exactly as it would against a Firebase project.

For a quick identity with no sign-up at all, `AUTH_MODE=stub` takes the identity
straight out of the bearer token — `uid`, `uid:email`, or `uid:email:admin`:

```sh
curl localhost:8000/api/me -H 'Authorization: Bearer u1:a@example.org'
```

It verifies nothing — it believes whatever the caller types — so
`app.config.get_verifier` logs a warning whenever it is selected and never
chooses it by default. The browser cannot mint stub tokens, so the sign-in panel
stays hidden there; drive the API with `curl` instead.

The curation call above needs an election to curate, which neither gated mode
can import on a free account. Every mode reads the same file, so import one
under `AUTH_MODE=off` first, then restart with gating on and curate it.

There is deliberately no endpoint that grants a tier: Stripe is the only thing
that changes one, and a way around that would be a way around paying. To try the
paid flow end to end — including an import that is actually allowed — use Stripe
test mode and forward its webhooks:

```sh
stripe listen --forward-to localhost:8000/api/billing/webhook
```

[doc/stripe.md](stripe.md) is the fifteen-minute version of getting a test-mode
account, two prices and that signing secret.

`BASIC_MONTHLY_IMPORTS=2` is worth setting alongside it: the allowance is then
small enough to spend, so the `429` at the end of a month is reachable in a
minute rather than ten.

### Cloud setup (deployment only)

1. A GCP project with the **Firestore** and **Cloud Run** APIs enabled.
2. A Firestore database in **Native mode** — single-flight parsing relies on its
   transactions. The `elections`, `extraction_jobs`, `pages` and `accounts`
   collections are created on demand.
3. A service account for the Cloud Run revision holding `roles/datastore.user`,
   plus `roles/secretmanager.secretAccessor` once `LLM_MODE=live`.
4. For live extraction: an Anthropic API key in Secret Manager, mounted as
   `ANTHROPIC_API_KEY` (`--set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest`).
   The agent runs `claude-opus-5` server-side, so the key never reaches the browser.

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

### Accounts and subscriptions (deployment only)

1. Enable **Firebase Authentication** on the same GCP project and turn on the
   email/password provider. The page signs in against Firebase's REST endpoints,
   so there is no SDK to bundle and no build step to add.
2. Set `FIREBASE_API_KEY` to the project's Web API key. It is an identifier, not
   a secret — it names the project to Google's identity endpoints, and every
   authorisation decision is made server-side from the ID token it produces.
   Without it the API still verifies tokens, but nothing in the browser can
   obtain one, so startup logs a warning.
3. ID tokens are verified locally against Google's public certificates, which
   are cached for as long as Google says they are good for. A verified request
   costs no network call.
4. In **Stripe**, create one recurring price per paid tier and a webhook
   endpoint pointing at `/api/billing/webhook` subscribed to
   `checkout.session.completed` and `customer.subscription.*`. Put the secret
   key and the webhook signing secret in Secret Manager and mount them.
   [doc/stripe.md](stripe.md) walks the Dashboard steps.
5. Grant an administrator by setting `ADMIN_EMAILS`, or by setting a custom
   `admin` claim on the Firebase user. Administrators are the only callers that
   can curate which elections signed-out visitors see.

A deployment that does not want a Firebase project at all can run
`AUTH_MODE=sqlite` instead of steps 1–3: the accounts, passwords and sessions
then live in `SQLITE_PATH`, and `/api/auth/register`, `/api/auth/login` and
`/api/auth/logout` become real endpoints that the page signs in against. Know
what it does not do before choosing it — the sessions are in a file, so it is
one instance only, and there is no password reset, no email verification, and
no rate limiting on the sign-in endpoint. Put it behind something that has
those, or use Firebase, which does.

Stripe is optional: with no `STRIPE_API_KEY` the app starts, serves free
accounts, and sells nothing.

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
| `SQLITE_PATH` | no | `./data/elections.db` | Database file for `ELECTION_STORE=sqlite`, and for the accounts and sessions of `AUTH_MODE=sqlite`. Created with its parent directory on first use. |
| `LLM_MODE` | no | `mock` | `mock` returns a fixed Sachsen-Anhalt result without calling Anthropic; `live` runs the real extraction agent; `off` disables importing. |
| `ANTHROPIC_API_KEY` | with `LLM_MODE=live` | — | Anthropic API key. Store it in Secret Manager and mount it as this env var; never put it in the image or in client code. |
| `PARSE_LEASE_SECONDS` | no | `300` | How long a parse may run before a crashed job is reclaimed. |
| `IMPORT_MAX_WAIT_SECONDS` | no | `25` | Ceiling on `?wait_seconds=` long-polling. |
| `ALLOWED_ORIGINS` | no | — | Comma-separated origins allowed to call the API, when the page is hosted elsewhere. Unset means same-origin only. |
| `FRONTEND_DIR` | no | repo root | Directory holding `index.html`; served at `/` when present. |
| `AUTH_MODE` | no | `firebase` with a project, else `off` | Which credential store is behind the same gating rules: `firebase` verifies real Firebase ID tokens; `sqlite` holds passwords and sessions in `SQLITE_PATH` itself; `off` disables gating entirely for local runs; `stub` trusts the token's text and is for local testing only. |
| `FIREBASE_PROJECT_ID` | with `AUTH_MODE=firebase` | `GOOGLE_CLOUD_PROJECT` | The project whose ID tokens are accepted. A token minted for another project is refused. |
| `FIREBASE_API_KEY` | for browser sign-in | — | The project's public Web API key, served to the page by `/api/config`. Not a secret. |
| `ADMIN_EMAILS` | no | — | Comma-separated addresses allowed to curate the selection visible to signed-out visitors. Applied whatever signed the credential; with Firebase a custom `admin` claim works too. |
| `BASIC_MONTHLY_IMPORTS` | no | `10` | Imports a Basic subscriber gets per calendar month. |
| `PREMIUM_MONTHLY_IMPORTS` | no | `200` | Imports a Premium subscriber gets per calendar month. |
| `STRIPE_API_KEY` | for subscriptions | — | Stripe secret key. Unset means nothing is for sale; free accounts still work. |
| `STRIPE_PRICE_BASIC` / `STRIPE_PRICE_PREMIUM` | with `STRIPE_API_KEY` | — | Recurring price IDs. The price on a subscription is what decides the tier. |
| `STRIPE_WEBHOOK_SECRET` | with `STRIPE_API_KEY` | — | Signing secret for `/api/billing/webhook`. The signature is the only authentication that endpoint has. |
| `PUBLIC_BASE_URL` | with `STRIPE_API_KEY` | — | Where Stripe returns the user after checkout, e.g. `https://koalitionsberegner.example`. |
| `LOG_LEVEL` | no | `INFO` | Level for the `app.*` loggers. Every call out — page fetch, extraction agent, Firestore — logs a `start` line and a matching `ok`/`failed` line with a duration; `WARNING` keeps only the failures. |
| `ENV_FILE` | no | nearest `.env` walking up from the working directory | A different file to read variables from. Empty loads none. A path that does not exist is a startup error. |
| `PORT` | no | `8080` | Set by Cloud Run. |

Credentials come from Application Default Credentials — the attached service account
on Cloud Run, or `gcloud auth application-default login` locally.
