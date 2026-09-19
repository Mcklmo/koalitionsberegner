# Plan 1 — Remove accounts and subscriptions, open the access

Handover plan for a Claude Code session. Read it whole before starting; the
phases are ordered so that the tree is green and deployable after each one.

Related plans: [02-share-links.md](02-share-links.md) needs Phase B of this
plan (an election must be readable without signing in before a crawler can
unfurl a link to it). [03-remaining-work.md](03-remaining-work.md) builds the
automated imports that make the owner's manual imports rare.

## Why

The product decision, in one paragraph: importing was sold because an import
spends money (a resolver call and one extraction call per page, all on Opus).
But an import is a one-time cost that is then served to everyone for free, and
the whole upcoming calendar is a small, bounded bill. Accounts existed only to
gate imports and to file requests; with imports no longer sold and requests
open to everyone (the other branch already did that), accounts do nothing but
cost: Firebase, Stripe, confirmation mail, password reset, a two-year retention
cron, most of the threat model, and half of the privacy section. Removing them
is also the strongest privacy statement the page can make.

## Target state

| Caller | Sees | May ask for an election | May import | May curate |
| --- | --- | --- | --- | --- |
| Anyone | every stored election | yes | no | no (curation is gone) |
| The owner, with `x-admin-secret` | every stored election | yes | yes | — |
| The schedule, with `x-report-secret` | `/api/internal/*` only | — | — | — |

Routes that stay:

| Route | Guard after this plan |
| --- | --- |
| `GET /healthz` | none |
| `GET /api/config` | none (still where page loads are counted) |
| `GET /api/elections` | none, always the full list |
| `GET /api/elections/{election_hash}` | none |
| `GET /api/elections/lookup` | none (store reads only, cached) |
| `POST /api/elections/requests` | none (one issue per election, `409` if stored) |
| `POST /api/elections/import` | `require_admin` |
| `GET /api/elections/imports/{request_key}` | `require_admin` |
| `POST /api/elections/imports/{request_key}/confirm` | `require_admin` |
| `DELETE /api/elections/imports/{request_key}/preview` | `require_admin` |
| `GET /api/admin/usage` | `require_admin` |
| `POST /api/internal/usage-reports` | `require_schedule` (unchanged) |

Routes that go: `GET /api/me`, `POST /api/auth/register|login|logout`,
`POST /api/billing/checkout|portal|webhook`, `PUT /api/elections/{hash}/selected`,
`POST /api/internal/inactive-accounts`.

Environment after this plan. Removed: `AUTH_MODE`, `FIREBASE_PROJECT_ID`,
`FIREBASE_API_KEY`, `ADMIN_EMAILS`, `STRIPE_API_KEY`, `STRIPE_PRICE_BASIC`,
`STRIPE_PRICE_PREMIUM`, `STRIPE_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`,
`PAYMENTS_PAUSED`, `BASIC_MONTHLY_IMPORTS`, `PREMIUM_MONTHLY_IMPORTS`.
Added: `ADMIN_SECRET` (at least 32 characters, same rule as `ORIGIN_SECRET`).
Everything else is unchanged.

## Decisions already made

Do not reopen these; they were weighed in the conversation that produced this
plan.

1. **Admin is a shared secret header, not a login and not Cloudflare Access.**
   `x-admin-secret`, compared with `hmac.compare_digest`, exactly like
   `require_schedule` does with `x-report-secret` in `backend/app/main.py`.
   The owner pastes it once into the page; it lives in `sessionStorage`. With
   no `ADMIN_SECRET` configured a local run is fully open (this replaces
   `AUTH_MODE=off`), and Cloud Run refuses to boot (this replaces the
   `on_cloud_run()` guard in `config.get_verifier`).
2. **Curation is dropped, not inverted.** Everyone sees everything. Before the
   deploy the owner reviews the production list once (any signed-in account
   sees the full list today) and deletes junk documents straight in the
   Firestore console. The `selected` column stays in both stores, unread; no
   migration.
3. **Lookup opens to everyone.** It is store reads behind the 30 s cache.
4. **Requests open to everyone** by porting commit `3c9e1da` from
   `origin/claude/koalitionsberegner-github-issues-i6eu0z` rather than
   re-deriving it.
5. **Usage counting stops recording anything per account.** The per-day hashed
   account marker (`usage.account_marker`) goes; only counts remain. That
   deletes the one pseudonymous datum the privacy section had to explain.
6. **`Job.owner` stays in storage, always `None`.** `require_importer` goes;
   an import is the owner's by definition.
7. **Stripe subscribers are handled before code removal, by hand** (Phase E).
   Check the Stripe dashboard for live subscriptions first; if there are none
   the phase is short.
8. **Dependencies:** drop `stripe` from `backend/pyproject.toml` and run
   `uv lock`. `google-cloud-firestore` stays (the election store).

## Scope, so the session can pace itself

Backend modules that disappear: `accounts.py` (441 lines), `auth.py` (562),
`billing.py` (309), `firestore_accounts.py` (332), `sqlite_accounts.py` (305),
`sqlite_auth.py`, `retention.py`. Backend test files that disappear:
`test_accounts.py` (25 tests), `test_auth.py` (28), `test_billing.py` (24),
`test_retention.py` (32), `test_signin.py` (14), `test_sqlite_auth.py` (18),
and most of `test_access.py` (49). Frontend modules that disappear:
`js/account-ui.js` (410), `js/auth.js` (308), `js/password-auth.js`, with
`test/account-ui.test.mjs` (41), `test/auth.test.mjs` (22),
`test/password-auth.test.mjs` (13). Everything else is edited, not deleted.

Run after every phase:

```sh
cd backend && uv run pytest
node --test test/*.test.mjs
```

`backend/tests/test_cloudflare.py` fails if a new top-level file appears or
if `_headers` and `main.SECURITY_HEADERS` drift. Keep new files under
`backend/`, `js/`, `doc/` or `test/`.

## Phase A — Anyone may ask for an election

1. `git cherry-pick 3c9e1da`. Expect conflicts in `README.md`,
   `doc/threat-model.md`, `doc/contribute.md`, `js/import-ui.js`,
   `test/import-ui.test.mjs` and `backend/tests/test_wishlist.py`: HEAD moved
   every text into `js/strings.csv` (commit `88ae210`) and stopped naming the
   requester in issues (commit `6ab9808`), so `wishlist.file` on HEAD takes no
   `requester`. Resolve toward HEAD's shapes and keep the cherry-pick's intent:
   the route takes no principal, the button works signed out.
2. `backend/app/main.py`, `request_election`: no `principal`, no `_account`;
   the log line says `anonymous`. Keep the `409` when the election is stored
   and the `503` when the wishlist is not configured.
3. `js/import-ui.js`: `canRequest()` becomes
   `config.requestsEnabled && !allowed()`. `REQUEST_REFUSALS` keeps only `503`
   (the `401` and `403` keys and their strings go: `request.refusal.signIn`,
   `request.refusal.confirmEmail`). `renderAvailability()` shows
   `import.requestNote` to a visitor.
4. Reword `import.requestNote` in both columns of `js/strings.csv`: the
   election is written down and imported later, no account needed.
5. Tests: the API tests in `backend/tests/test_wishlist.py` (anonymous `201`,
   duplicate `200`, stored `409`), `test/import-ui.test.mjs` (signed-out submit
   files a request).

## Phase B — Everyone sees everything

1. `main.py`: `list_elections` calls `service.list_elections()` with no
   `selected_only`; `get_election` drops both principal checks; delete
   `is_member`; delete the `PUT /api/elections/{election_hash}/selected` route
   and `SelectedBody`; `ElectionSummary` drops `selected`.
2. `service.py`: drop `selected_only` from `list_elections` and delete
   `set_selected`. `cached_store.py`: `_lists` becomes a single entry instead
   of a dict keyed by the flag. Leave `store.ElectionStore.set_selected`, the
   three implementations and the `selected` column alone for now (removing
   them is housekeeping in Plan 3); `StoredElection.selected` stays as a field
   nothing reads.
3. Frontend: `js/import-ui.js` loses `renderCuration`, `curate`, the
   `picker.public` suffix in `optionLabel`, and the `curate`/`curateRow`
   elements; `js/api.js` loses `setSelected` and the `selected` field in
   `toSummary`; `index.html` loses `#curate-row`; `js/strings.csv` loses
   `curate.*` and `picker.public`.
4. Tests: in `backend/tests/test_access.py` keep and reword the tests that say
   the list and a single election are open to a caller with no header; delete
   the curated/member variants. `test/import-ui.test.mjs` loses the curation
   tests.

## Phase C — The owner's secret replaces sign-in for importing

1. `backend/app/config.py`: add

   ```python
   def admin_secret() -> str:
       return _env_str("ADMIN_SECRET")
   ```

   validate its length in `validate_configuration` next to `ORIGIN_SECRET`,
   and raise `ConfigError` when `on_cloud_run()` and it is empty (move the
   sentence from `get_verifier` here: a deployment where everyone is an
   administrator must not boot).
2. `main.py`: replace `current_principal`, `require_principal`,
   `require_verified`, `require_account`, `require_admin`, `require_importer`
   with one dependency:

   ```python
   ADMIN_SECRET_HEADER = "x-admin-secret"

   def require_admin(x_admin_secret: str | None = Header(default=None)) -> None:
       expected = admin_secret()
       if not expected:
           return  # a local run with no secret configured is the owner's own
       if not hmac.compare_digest((x_admin_secret or "").encode(), expected.encode()):
           raise HTTPException(403, detail="this needs the administrator's secret")
   ```

   Put it on `import_election`, `get_import`, `confirm_election`,
   `discard_preview` and `preview_usage_report`.
3. `import_election` loses the whole allowance block (reserve, refund,
   `402`/`429`, `UsageEvent.REFUSED_*`). It becomes: `peek`, then
   `service.submit(request, on_parse_failed=...)`, then `_answer`. Pass
   `owner=None` or drop the parameter from `submit`; either is fine as long as
   `store.claim` keeps its signature.
4. `/api/config` slims to `requests_enabled: bool` and `imports_enabled: bool`
   (`LLM_MODE != "off"`). Keep the page-load counting on it. `PublicConfig`
   loses `auth_required`, `auth_provider`, `firebase`, `billing_enabled`,
   `payments_paused`, `tiers`; `TierInfo` goes.
5. Frontend admin mode. New `js/admin.js`, tested in `test/admin.test.mjs`:
   - `readSecret()` / `saveSecret()` / `forgetSecret()` over
     `sessionStorage['koalitionsberegner.adminSecret']`, wrapped in try/catch
     (storage can be unavailable).
   - `#admin` in the URL opens a one-field form (a password input) inside the
     import panel; saving stores the secret and re-renders. Wrong secret: the
     first `403` clears it and shows `admin.wrongSecret`.
   - `js/api.js`: `createApiClient({ baseUrl, getAdminSecret })` sends the
     header when a secret is set. Remove `getToken`.
   - `js/import-ui.js`: `setAccount(account)` becomes `setAdmin(boolean)`;
     `allowed()` is `admin || config.importsOpen`; `REFUSALS` shrinks to `403`.
     One form serves both roles: a visitor's submit files a request, the
     owner's submit imports. The button label follows the role
     (`import.submit` vs a new `request.submit`).
   - `index.html`: the import `<details>` is shown to everyone (the request
     path needs it); the preview and choices blocks only ever render for the
     owner.
6. Tests: `backend/tests/test_api.py` gains the three `require_admin` cases
   (no header is `403` when a secret is configured, the right header passes,
   no secret configured passes locally). Use `monkeypatch.setenv` and
   `config.admin_secret.cache_clear()` if it is cached.

## Phase D — Delete accounts, auth, billing, retention

Backend:

1. Delete the modules and test files listed under Scope. `test_access.py`:
   move the surviving open-listing tests into `test_api.py`, then delete it.
2. `config.py`: remove `AUTH_MODES`, `auth_mode`, `firebase_project_id`,
   `firebase_web_config`, `admin_emails`, `get_accounts`, `get_password_store`,
   `get_identity_remover`, `get_verifier`, `get_quota_policy`, `get_billing`,
   `payments_paused`, `public_base_url`, and their lines in
   `describe_configuration` and `validate_configuration`. `LOCAL_PRINCIPAL`
   and everything importing `app.auth` goes with it.
3. `main.py`: prune the imports; `LimitRequestBody` loses the webhook special
   case (`MAX_WEBHOOK_BYTES`, `WEBHOOK_PATH`); `CONTENT_SECURITY_POLICY`
   `connect-src` becomes `'self'` alone, and `_headers` changes in lockstep
   (`test_cloudflare.py` enforces it).
4. `usage.py`: remove `REFUSED_NO_SUBSCRIPTION`, `REFUSED_LIMIT_REACHED`,
   `CHECKOUT_STARTED`, `SUBSCRIPTION_STARTED`, `SUBSCRIPTION_ENDED`; remove
   `account_marker`, `marker_expiry`, `UsageRecorder.active`,
   `UsageStore.active_accounts`, `forget_active_before`, `ACTIVE_RETENTION_DAYS`,
   `CountsCreated`, and the `active_accounts` / `new_accounts` fields of
   `UsageFigures`; `gather(store, period)`. Delete the "Paywall" section of
   `SECTIONS`; rename "Requests from accounts without a subscription" to
   "Requests". Mirror in `firestore_usage.py`, `sqlite_usage.py`,
   `InMemoryUsageStore`, `test_usage.py`. `send_usage_reports` loses the
   `forget_active_before` call and the `accounts` dependency.
5. `backend/pyproject.toml`: remove `stripe`; `uv lock`. The Dockerfile needs
   no change.

Worker:

6. `worker/index.js`: remove `INACTIVE_ACCOUNTS_PATH` and its call in
   `scheduled`; `test/worker.test.mjs` accordingly. `wrangler.jsonc` comment.

Frontend:

7. Delete the modules and tests listed under Scope. `js/api.js`: remove
   `register`, `login`, `logout`, `getAccount`, `startCheckout`,
   `openBillingPortal`, `toAccount`, `toSession`; `toConfig` maps the two
   remaining fields. `js/main.js`: no `auth`, no `accountUi`, no `checkout`
   query handling; `importUi.setAdmin(Boolean(readSecret()))` after `start()`.
8. `index.html`: remove `<details id="account">` and everything inside it.
9. `js/strings.csv`: delete `account.*`, `auth.*`, `checkout.*`, `portal.*`,
   `password.*`, `quota.*`, `signup.*`, `verify.*`, `tier.*`, `upgrade.*`,
   `payments.paused`, `login.notConfigured`, `email.*`, `reset.sent`,
   `import.refusal.signIn`, `import.refusal.subscription`,
   `import.refusal.usedUp`. `test/i18n.test.mjs` fails on a key used in markup
   but missing from the sheet and on Danish markup that differs from the
   sheet, so edit both together.
10. Privacy section, both languages, and the Danish copies in `index.html`:
    delete `privacy.account` and `privacy.payments`; `privacy.statistics`
    drops the pseudonymised account id sentence; `privacy.browser` becomes
    display choices only; `privacy.imports` drops the sentence tying an import
    to an account; `privacy.transfers` drops Stripe and "your account";
    `privacy.updated` gets the day of the change. `privacy.controller` keeps
    the name and the address; the postal-address and CVR obligations in the
    README applied to selling online and no longer apply.

Docs:

11. `README.md`: rewrite "Who may do what" to the table at the top of this
    plan; delete "Payments are closed right now"; the usage-report list drops
    the account and subscription lines; "Run locally" drops `AUTH_MODE`.
    Delete `doc/stripe.md`. Rewrite `doc/privacy-requests.md` to the new
    reality (nothing about a visitor is stored; requests are public issues
    that name no one; logs at Cloudflare and Google Cloud).
    `doc/contribute.md`: the Modes table and the environment variable table
    lose every row named above and gain `ADMIN_SECRET`; delete "Working on
    accounts, tiers and quotas" and "Accounts and subscriptions (deployment
    only)". `doc/threat-model.md`: T11 becomes "spending the owner's money"
    (only the owner and, after Plan 3, the schedule can trigger a model call);
    T12 says the endpoint is open and why that is bounded (one issue per
    election, `409` when stored, proxy rate limit); Accepted risks drop the
    `AUTH_MODE=sqlite` and double-checkout items. `doc/cloudflare.md`: delete
    step 5 (Stripe and Firebase), the webhook exemption in the rate-limit rule
    of step 6, and the account-deletion half of step 7; add `ADMIN_SECRET` to
    step 3's routine. `.env.example` follows.

## Phase E — Cloud cleanup, by the owner, by hand

The session cannot do these; list them in the final message.

1. **Stripe.** Cancel every live subscription with a prorated refund and email
   those customers from the dashboard. Delete the webhook endpoint. Archive
   the prices. Bookkeeping records stay in Stripe as the law requires.
2. **Firebase.** Delete all users (Authentication → Users). Remove the
   authorized domain. Delete the browser API key restriction entry.
3. **Firestore.** Delete the `accounts` collection and every
   `usage_daily/{day}/active` subcollection. Remove the TTL policy on
   `active`. Review `elections` and delete junk before Phase B goes live.
4. **Secret Manager.** Delete `STRIPE_*` and `FIREBASE_API_KEY`. Create
   `ADMIN_SECRET` with the same one-value routine as `ORIGIN_SECRET` in
   `doc/cloudflare.md` step 3 and grant the service account access.
5. **IAM.** Remove `roles/firebaseauth.admin` from the service account.
6. **`deploy.local.sh`** (personal, gitignored): drop the removed variables and
   secrets, mount `ADMIN_SECRET`.
7. **Deploy order.** Worker first (`npx wrangler@latest deploy`, so the cron
   stops calling the account-deletion endpoint), then the backend. Then:

   ```sh
   D=https://koalitionsberegner.moritzmarcus.com
   curl -s $D/api/elections | head -c 300                       # the full list, no header
   curl -s -o /dev/null -w "%{http_code}\n" -X POST $D/api/elections/import \
     -H 'content-type: application/json' -d '{"year":2025,"nation":"Norway"}'   # 403
   curl -s -o /dev/null -w "%{http_code}\n" $D/api/me            # 404
   ```

## Acceptance

- Both test suites green; `grep -rniE "firebase|stripe|bearer|/api/me|checkout" js backend/app index.html _headers wrangler.jsonc worker` finds nothing.
- A signed-out `curl` lists everything, reads any election, files a request,
  and is refused an import with `403`; the same import with the header is
  `202` then `200`.
- The privacy section, in both languages, describes only what still happens.
- The Cloud Run revision refuses to start without `ADMIN_SECRET` (check the
  revision log once, on purpose, before mounting it).
