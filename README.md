# Koalitionsberegner

Static coalition-seat calculator. Pick parties, see whether they reach a majority.

The renderer (`js/app.js`) is election-agnostic; the data comes from an injected
provider (`js/providers/folketing-2026.js`) and must pass the canonical schema
validator in `js/election.js` before it can be rendered.

## Run locally

Needs a server — the page uses ES modules, so `file://` won't work.

```sh
python3 -m http.server 8000    # then open http://localhost:8000
```

Or, closer to production:

```sh
npx wrangler dev
```

`http://localhost:8000/test/toy-election.html` renders a two-party toy election
through the same renderer — a manual check that the provider seam holds.

## Test

```sh
node --test test/election.test.mjs
```
