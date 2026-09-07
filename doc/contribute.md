# Contributing

## Run everything

The page is served by the backend so the API is same-origin, which is what the
import UI expects.

```sh
cd backend
pip install -e '.[dev]'
ELECTION_STORE=memory uvicorn app.main:app --reload   # http://localhost:8000
pytest
```

Frontend on its own (bundled election only, no import):

```sh
python3 -m http.server 8000   # or: npx wrangler dev
node --test test/*.test.mjs
```

### Cloud setup (deployment only)

1. A GCP project with the **Firestore** and **Cloud Run** APIs enabled.
2. A Firestore database in **Native mode** — single-flight parsing relies on its
   transactions. Collections `elections` and `parse_jobs` are created on demand.
3. A service account for the Cloud Run revision holding `roles/datastore.user`.

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

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | yes | — | GCP project holding Firestore. If unset the app falls back to the in-memory store, which loses everything on restart and shares nothing between instances. |
| `FIRESTORE_DATABASE` | no | `(default)` | Named Firestore database, if not the default one. |
| `ELECTION_STORE` | no | `firestore` | Set to `memory` to run without Firestore. |
| `PARSE_LEASE_SECONDS` | no | `300` | How long a parse may run before a crashed job is reclaimed. |
| `IMPORT_MAX_WAIT_SECONDS` | no | `25` | Ceiling on `?wait_seconds=` long-polling. |
| `ALLOWED_ORIGINS` | no | — | Comma-separated origins allowed to call the API, when the page is hosted elsewhere. Unset means same-origin only. |
| `FRONTEND_DIR` | no | repo root | Directory holding `index.html`; served at `/` when present. |
| `PORT` | no | `8080` | Set by Cloud Run. |

Credentials come from Application Default Credentials — the attached service account
on Cloud Run, or `gcloud auth application-default login` locally.
