# Contributing

Thanks for looking. The short version: run it locally with `uv`, keep the
tests green, and open a pull request. The long version — cloud setup,
environment variables, every mode the backend can run in, and the rollout
steps for each of the plans under `doc/plans/` — lives in
[doc/contribute.md](doc/contribute.md); this file only points at it and at
the two things people ask about most.

## Missing an election?

You do not need to read any of this to ask for one: the page itself has a
button for it (look for "ask for it" next to the search, or the import form
directly), open to everyone, no account needed. It searches this
repository's issues first, so asking twice for the same election finds the
existing request rather than opening a second one. See
[.github/ISSUE_TEMPLATE/election-request.md](.github/ISSUE_TEMPLATE/election-request.md)
if you would rather file one by hand — for instance, to add detail the page's
own form has no field for. Every such issue carries the `election-request`
label ([`app.wishlist.LABEL`](backend/app/wishlist.py)); that label *is* the
queue the owner imports from, so there is no separate list to keep in sync.

## Contributing a country's seat rules

An election whose poll or result publishes only vote shares needs its seats
allocated in code — see `backend/app/seats.py` for the method used so far
(D'Hondt with a threshold) and add another where a country's method differs.
Whatever you add needs a test against a real, published result: seat
allocation is exactly the kind of code that looks right and is off by one.

## Code

- `README.md` is the map of what the pieces are and how they fit; start
  there.
- [doc/threat-model.md](doc/threat-model.md) explains why imported text is
  handled the way it is (fetched server-side, extracted by a tool-less
  agent, validated against the canonical schema, and never sent to the DOM
  as markup) — read it before touching the import pipeline.
- [doc/contribute.md](doc/contribute.md) has the environment variables, the
  modes (`ELECTION_STORE`, `LLM_MODE`, …), and cloud setup for Cloudflare,
  Cloud Run and Firestore.
- Run the tests before opening a pull request:

  ```sh
  node --test test/*.test.mjs      # frontend
  cd backend && uv run pytest      # backend
  ```

  Both suites run offline — nothing here needs a live LLM key or a real
  Wikipedia fetch to pass.
- `js/strings.csv` and `index.html` change together: every `data-i18n`
  attribute and every `t('key')` call needs a row in the sheet, with a text
  in every language column — Danish, English and German
  (`test/i18n.test.mjs` checks this). Wording never lives in a `.js` file
  directly.
- Untrusted text — anything that came from an imported page — reaches the
  DOM as text, never as markup. This is load-bearing; see the threat model.

## Licence

The code is [AGPL-3.0](LICENSE). Data read from Wikipedia carries its own
[CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/) obligations,
independent of the code's licence — see the README's "Data and its licence"
section.

## Questions

Open an issue, or see [doc/privacy-requests.md](doc/privacy-requests.md) if
what you want is to see, correct or delete data about yourself rather than
to contribute code.
