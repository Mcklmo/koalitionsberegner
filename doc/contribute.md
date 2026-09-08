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

### Working on accounts, tiers and quotas

`AUTH_MODE=off` — the default without a Firebase project — means *no gating at
all*: every request is a local developer with no quota, so the import flow
behaves as it did before accounts existed. That is deliberate, so the rest of
the app stays workable without a Firebase project.

To exercise the gated behaviour, run with `AUTH_MODE=stub` and send the identity
as the bearer token — `uid`, `uid:email`, or `uid:email:admin`:

```sh
ELECTION_STORE=sqlite AUTH_MODE=stub BASIC_MONTHLY_IMPORTS=2 \
  uv run uvicorn app.main:app --reload

curl localhost:8000/api/me -H 'Authorization: Bearer u1:a@example.org'
curl -X PUT localhost:8000/api/elections/HASH/selected \
  -H 'Authorization: Bearer boss:b@example.org:admin' \
  -H 'content-type: application/json' -d '{"selected": true}'
```

The stub verifies nothing — it believes whatever the caller types — so
`app.config.get_verifier` logs a warning whenever it is selected and never
chooses it by default. The browser cannot mint stub tokens, so the sign-in panel
stays hidden; drive the API with `curl` instead.

There is deliberately no endpoint that grants a tier: Stripe is the only thing
that changes one, and a way around that would be a way around paying. To try the
paid flow end to end, use Stripe test mode and forward its webhooks:

```sh
stripe listen --forward-to localhost:8000/api/billing/webhook
```

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
5. Grant an administrator by setting `ADMIN_EMAILS`, or by setting a custom
   `admin` claim on the Firebase user. Administrators are the only callers that
   can curate which elections signed-out visitors see.

Stripe is optional: with no `STRIPE_API_KEY` the app starts, serves free
accounts, and sells nothing. Half-configured billing is refused at startup
rather than silently disabled.

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
| `IMPORT_MAX_WAIT_SECONDS` | no | `25` | Ceiling on `?wait_seconds=` long-polling. |
| `ALLOWED_ORIGINS` | no | — | Comma-separated origins allowed to call the API, when the page is hosted elsewhere. Unset means same-origin only. |
| `FRONTEND_DIR` | no | repo root | Directory holding `index.html`; served at `/` when present. |
| `AUTH_MODE` | no | `firebase` with a project, else `off` | `firebase` verifies real Firebase ID tokens; `off` disables gating entirely for local runs; `stub` trusts the token's text and is for local testing only. |
| `FIREBASE_PROJECT_ID` | with `AUTH_MODE=firebase` | `GOOGLE_CLOUD_PROJECT` | The project whose ID tokens are accepted. A token minted for another project is refused. |
| `FIREBASE_API_KEY` | for browser sign-in | — | The project's public Web API key, served to the page by `/api/config`. Not a secret. |
| `ADMIN_EMAILS` | no | — | Comma-separated addresses allowed to curate the selection visible to signed-out visitors. A custom `admin` claim works too. |
| `BASIC_MONTHLY_IMPORTS` | no | `10` | Imports a Basic subscriber gets per calendar month. |
| `PREMIUM_MONTHLY_IMPORTS` | no | `200` | Imports a Premium subscriber gets per calendar month. |
| `STRIPE_API_KEY` | for subscriptions | — | Stripe secret key. Unset means nothing is for sale; free accounts still work. |
| `STRIPE_PRICE_BASIC` / `STRIPE_PRICE_PREMIUM` | with `STRIPE_API_KEY` | — | Recurring price IDs. The price on a subscription is what decides the tier. |
| `STRIPE_WEBHOOK_SECRET` | with `STRIPE_API_KEY` | — | Signing secret for `/api/billing/webhook`. The signature is the only authentication that endpoint has. |
| `PUBLIC_BASE_URL` | with `STRIPE_API_KEY` | — | Where Stripe returns the user after checkout, e.g. `https://koalitionsberegner.example`. |
| `LOG_LEVEL` | no | `INFO` | Level for the `app.*` loggers. Every call out — page fetch, extraction agent, Firestore — logs a `start` line and a matching `ok`/`failed` line with a duration; `WARNING` keeps only the failures. |
| `PORT` | no | `8080` | Set by Cloud Run. |

Credentials come from Application Default Credentials — the attached service account
on Cloud Run, or `gcloud auth application-default login` locally.
