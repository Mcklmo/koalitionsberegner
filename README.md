# Koalitionsberegner

Coalition-seat calculator. Pick parties, see whether they reach a majority.
Elections can be imported from an official results URL and shared between users.

The renderer (`js/app.js`) is election-agnostic; elections come from an injected
provider and must pass the canonical schema validator in `js/election.js` before
being rendered. The backend (`backend/`) stores imported elections.

## Run locally

The import UI calls the API on the same origin, so run both from the backend:

```sh
cd backend && pip install -e '.[dev]'
ELECTION_STORE=memory uvicorn app.main:app --reload
```

Then open <http://localhost:8000>.

Frontend only — the bundled Folketing 2026 election renders, import is disabled:

```sh
python3 -m http.server 8000   # or: npx wrangler dev
```

## Test

```sh
node --test test/*.test.mjs   # frontend
cd backend && pytest          # backend
```

See [doc/contribute.md](doc/contribute.md) for cloud setup and environment variables.
