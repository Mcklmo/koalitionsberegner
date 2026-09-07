# Contributing

## Frontend

Static files at the repo root, deployed to Cloudflare Workers. No cloud setup needed.

```sh
python3 -m http.server 8000   # or: npx wrangler dev
node --test test/election.test.mjs
```

## Backend

FastAPI on Cloud Run, Firestore as the datastore.

```sh
cd backend
pip install -e '.[dev]'
ELECTION_STORE=memory uvicorn app.main:app --reload   # no cloud needed
pytest
```

### Cloud setup (deployment only)

1. A GCP project with the **Firestore** and **Cloud Run** APIs enabled.
2. A Firestore database in **Native mode** — single-flight parsing relies on its
   transactions. Collections `elections` and `parse_jobs` are created on demand.
3. A service account for the Cloud Run revision holding `roles/datastore.user`.

```sh
gcloud run deploy koalitionsberegner-api \
  --source backend --region europe-north1 \
  --service-account koalitionsberegner@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_CLOUD_PROJECT=PROJECT_ID
```

### Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | yes | — | GCP project holding Firestore. If unset the app falls back to the in-memory store, which loses everything on restart and shares nothing between instances. |
| `FIRESTORE_DATABASE` | no | `(default)` | Named Firestore database, if not the default one. |
| `ELECTION_STORE` | no | `firestore` | Set to `memory` to run without Firestore. |
| `PARSE_LEASE_SECONDS` | no | `300` | How long a parse may run before a crashed job is reclaimed. |
| `IMPORT_MAX_WAIT_SECONDS` | no | `25` | Ceiling on `?wait_seconds=` long-polling. |
| `PORT` | no | `8080` | Set by Cloud Run. |

Credentials come from Application Default Credentials — the attached service account
on Cloud Run, or `gcloud auth application-default login` locally.
