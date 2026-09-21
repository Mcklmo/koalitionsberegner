# Plan 4 — The outreach approval gate on the server

Handover plan for a Claude Code session. It is the server half of the
`outreach` plugin in [`plugins/outreach/`](../../plugins/outreach/): the plugin
scans Reddit on the owner's machine and queues reply drafts; this plan builds
the queue, the approval email, the one-time approval page, and the posting.

Depends on [01-open-access.md](01-open-access.md) Phase C, which introduces
`ADMIN_SECRET` and the `x-admin-secret` header. It reuses the SMTP mailer that
plan 1 keeps for the usage reports. [02-share-links.md](02-share-links.md) runs before
this plan, so `/e/<id>?c=…&s=…` already exists and every reply links to the
coalition being discussed rather than to the front page. [03-remaining-work.md](03-remaining-work.md) runs after this plan, so
expect the scan to report `missing_elections` until tracked elections land;
those are import candidates to work by hand in the meantime.

## Does this bring Firebase back? No.

The question that prompted this plan was whether the approval step forces the
account system to stay. It does not, and **plan 1 needs no edits** — everything
here is additive.

Approval needs two things: evidence that the person pressing send is the owner,
and a record of what was sent. Two factors already exist after plan 1:

| Factor | What it is | Where it comes from |
| --- | --- | --- |
| Something the owner has | the one-time token in the emailed link | this plan |
| Something the owner knows | `ADMIN_SECRET`, held in `sessionStorage` by the page's admin mode | plan 1 Phase C |

Viewing a draft needs only the token, so the email opens on a phone and shows
the Reddit thread and the proposed reply. **Sending** needs the secret as well,
pasted once per browser. Firebase would add an identity provider, a user
database, a password reset flow and a privacy section to authenticate exactly
one person who already holds a 32-character secret. It is strictly more moving
parts for strictly less.

Optional upgrade, worth doing if approving from a phone becomes routine: replace
the pasted secret with a **passkey** (WebAuthn). One credential registered per
device, verified with `py_webauthn`, and the send button is armed by a
fingerprint instead of a paste. It is perhaps a day's work and needs no accounts
either — a single stored credential id and public key. Do it after the rest
works, not before.

## Before you switch this on

This is the part of the project that can do real damage, so read it before
writing code.

- **Reddit removes and bans for undisclosed self-promotion.** The site-wide
  guidance is that self-promotion should be a small fraction of what an account
  does, and many subreddits ban links from a project's own author outright. A
  domain that gets reported enough can be blocked site-wide, which would cost
  the project its main organic channel permanently. Verify each subreddit's
  rules by hand and keep the allowlist small. There is no discovery at all:
  the scanner reads only the threads the owner saved by hand, and the server's
  `OUTREACH_ALLOWED_SUBREDDITS` is what a reply may actually be posted to.
- **Many subreddits require bots to be flaired, or ban them.** The plugin's
  footer discloses that an LLM drafted the reply and that the owner is behind
  the site. Do not remove it; the sanitiser appends it in code so no model can.
- **Reddit's API terms and the user agent.** The plugin no longer reads
  Reddit at all — the owner saves a thread from their own browser — so this is
  about the server's poster: identify it honestly in `REDDIT_USER_AGENT`,
  including a contact. Confirm the current terms
  and the comment endpoint's shape at implementation time rather than from
  memory; the details below are the shape to verify, not a citation.
- **A reply that does not answer the thread is spam even when it is honest.**
  Claude is instructed to reject a thread that is not really about coalition
  seats, and `reply.py` answers exactly one post per invocation — there is no
  batch to run away. Keep the per-subreddit ceiling in section 5 low anyway;
  that one is the server's and does not depend on the plugin behaving.
- **Start by hand.** Run `reply.py --dry-run` for a week, read
  every draft, and post the good ones yourself from your own account. Build the
  server side only once the drafts are consistently ones you would have written.

## What already exists

The plugin is two commands now, not one. `scripts/scan.py` triages a thread
saved from Reddit with the local model — three questions per item, three
comments at a time, stopping at the first yes — and writes the thread's blob
and the flagged items to its own SQLite file. `scripts/reply.py` is run by hand
on one post at a time, reads the *whole* thread back, and produces the draft
below. The split exists so the money is spent by a person, not by a loop, and
the link a draft carries is now a coalition (`/e/<id>?c=…&s=…`) rather than the
front page.

`plugins/outreach/scripts/reply.py` produces, per accepted find, a JSON object
with these fields, and posts it to `POST /api/admin/outreach/drafts` with the
`x-admin-secret` header — unchanged from what `scan.py` used to post, so
nothing on this side of the gate had to move:

| Field | Meaning |
| --- | --- |
| `source` | always `reddit` for now |
| `subreddit` | the subreddit's name, no `r/` |
| `thing_id` | Reddit's fullname, `t3_<id>` for a post or `t1_<id>` for a comment. The reply's parent, and the uniqueness key |
| `kind` | `post` or `comment` |
| `permalink` | absolute URL of the thing being replied to |
| `title` | the thread's title |
| `excerpt` | the first 500 characters of what was said |
| `election_hash` | the stored election the reply links to |
| `election_title`, `link` | what the reply points at |
| `reply_text` | the full reply, disclosure footer included, ready to post |
| `verification` | what Claude concluded: the election it named, the coalition it picked, its seats and the majority |
| `classifier_reason` | which of the local model's three questions were answered yes, and where |
| `created_at` | when the scan produced it |

The script expects `201` with `{"id": "..."}`, treats `409` as "already queued"
and `403` as a wrong secret, which ends the run. Keep those three.

## 1. Storage

New `outreach_drafts` table in `backend/app/sqlite_store.py` (add it to the
`CREATE TABLE IF NOT EXISTS` block and the column-backfill loop) and collection
in `backend/app/firestore_store.py`, behind an `OutreachStore` protocol in a new
`backend/app/outreach.py` with an in-memory implementation for tests. Wire it in
`config.get_store` beside the election store.

| Column | Notes |
| --- | --- |
| `id` | `draft_` plus 16 hex characters |
| `thing_id` | **unique**; a second draft for the same post or comment is the `409` |
| every field from the table above | `verification` as JSON text |
| `status` | `pending`, `approved`, `posted`, `rejected`, `failed`, `expired` |
| `token_hash` | SHA-256 of the approval token; the token itself is never stored |
| `token_expires_at` | 72 hours after the email is sent |
| `emailed_at`, `decided_at`, `posted_at` | timestamps |
| `posted_url` | the permalink of the comment that was created |
| `last_error` | why a post failed, for the report |
| `edited` | whether the owner changed the text before sending |

Use the same `hashlib.sha256` and `hmac.compare_digest` discipline the session
tokens in `sqlite_auth.py` use today: generate with `secrets.token_urlsafe(32)`,
store the hash, compare in constant time, and consume the token on a successful
send so a forwarded link cannot be replayed.

## 2. Routes

Add to `backend/app/main.py`. The first is for the plugin, the rest for the
owner's browser.

| Route | Guard | Does |
| --- | --- | --- |
| `POST /api/admin/outreach/drafts` | `require_admin` | Stores a draft, mints a token, emails the owner, answers `201 {"id": ...}`. `409` when `thing_id` is already queued. `503` when SMTP is not configured — a draft nobody can approve is not stored. |
| `GET /api/admin/outreach/drafts` | `require_admin` | The queue, for the daily report and for a second look. |
| `GET /api/outreach/approval/{token}` | the token itself | The draft as JSON: thread, excerpt, proposed reply, election, expiry. `404` for an unknown, consumed or expired token — never say which. |
| `POST /api/outreach/approval/{token}/send` | token **and** `x-admin-secret` | Posts the reply, consumes the token, records the outcome. Optional body `{"reply_text": "..."}` when the owner edited it; the disclosure footer is re-appended in code if the edit dropped it. |
| `POST /api/outreach/approval/{token}/reject` | token **and** `x-admin-secret` | Marks it rejected and consumes the token. |

The approval page is served at `/approve/{token}` the same way plan 2 serves
`/e/{id}`: the Worker fetches the asset copy of a small new `approve.html` and
forwards the two API calls. Add `/approve/*` to `run_worker_first`. The page
gets **no** Open Graph tags and an `X-Robots-Tag: noindex` header, because an
approval link must never unfurl or be indexed.

Two things about that page trip over `backend/tests/test_cloudflare.py`, which
is why they are spelled out rather than left to discover:

- **Set `X-Robots-Tag` on the Worker's response, not in `_headers`.**
  `test_cloudflare_sends_the_same_security_headers_as_the_backend` flattens
  every indented `name: value` line in `_headers` into one dictionary and
  asserts it equals `main.SECURITY_HEADERS`. A second path block would add a key
  and fail. The Worker already builds this response, so add the header there and
  leave `_headers` alone.
- **`approve.html` is a new top-level file, so widen the published set.**
  `test_nothing_but_the_page_is_published` asserts the repo root publishes
  nothing but `index.html`, `js` and `_headers`. Add `approve.html` to that set
  in the test, in the same commit that adds the file. Do not add it to
  `.assetsignore`: the Worker fetches it through `env.ASSETS`, so it has to be
  published.

Guards on the send path, all of them:

1. The token verifies, is unconsumed and unexpired.
2. `x-admin-secret` matches, compared with `hmac.compare_digest`.
3. The draft is `pending`.
4. The per-subreddit ceiling in section 5 is not reached.
5. `reply_text` is at most 1,200 characters, contains no URL but the site's own
   (reuse the plugin's `URL_PATTERN` rule, reimplemented server-side — never
   trust the client's sanitising), and ends with the disclosure.

## 3. The email

Reuse `app.mailer.SmtpMailer`. One message per draft, plain text, subject like
`Approve a reply in r/denmark — Denmark 2026`. Body: the thread title, the
permalink, the excerpt, the proposed reply in full, the election it links to,
and the approval URL. Say that the link expires in 72 hours and works once.

The email needs an absolute URL. Plan 1 keeps `PUBLIC_BASE_URL` for exactly
this reason, re-documented from "where Stripe returns the user" to "where this
site is reachable". If you find it gone, plan 1 was followed too literally; put
it back as a plain variable rather than a secret.

## 4. Posting to Reddit

New `backend/app/reddit.py`, one `RedditPoster` protocol with an
`httpx`-backed implementation and a fake for tests. The script-app OAuth flow:
exchange the app credentials and the owner's login for a bearer token at
Reddit's access-token endpoint, then create the comment against the OAuth host
with the parent's fullname and the text. **Verify both endpoints, the grant type
and the response shape against Reddit's current API documentation before
writing them** — this is the one place in the plan written from memory. Cache
the bearer token until shortly before it expires.

Credentials, all from Secret Manager, none in the image: `REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`,
`REDDIT_USER_AGENT`. With any of them missing the send route answers `503` and
the approval page says posting is not configured, so the rest can be built and
tested first.

Failures: a Reddit error sets `status=failed` with `last_error`, does **not**
consume the token, and the page offers one retry. Reddit's own words never reach
the browser — log the status, show one fixed sentence, exactly as
`app.wishlist` already does for GitHub.

**Alternative worth knowing:** the dev machine could do the posting instead, by
polling `GET /api/admin/outreach/drafts?status=approved` at the start of the
next scan. That keeps Reddit credentials off the cloud entirely, at the cost of
up to a day between approval and the comment appearing, which is usually too
long for a live thread. Server-side posting is the default for that reason.

## 5. Etiquette limits, enforced in code

Not configuration the owner can forget:

- At most one reply per thread, ever. The unique `thing_id` covers the same
  post; also refuse when a draft for the same `thread_id` is already `posted`.
- At most `OUTREACH_SUBREDDIT_WEEKLY_CAP` (default 2) posted replies per
  subreddit per rolling seven days.
- At most `OUTREACH_DAILY_CAP` (default 3) posted replies in total per day.
- Nothing posts to a subreddit that is not in the deployment's own
  `OUTREACH_ALLOWED_SUBREDDITS`, which is the server's list and need not equal
  the scanner's.

A refused send is an explicit message on the page saying which ceiling stopped
it, not a silent no.

## 6. Reporting and privacy

- The daily usage report gains an "Outreach" section: drafts queued, approved,
  rejected, posted, failed, expired unapproved. Add the counters to
  `usage.UsageEvent` and a section to `usage.SECTIONS`.
- The privacy section gains a sentence, in both languages and in the Danish
  markup: public Reddit content the owner may reply to is stored — the link,
  the title and an excerpt — and no Reddit usernames are collected. The scanner
  deliberately does not read author names, and the drafts have no field for
  them. Keep it that way.
- `doc/threat-model.md` gains T14: the approval gate. What an attacker who
  obtains an approval link can do (read one draft) and cannot (send it, without
  the secret); why Reddit text is untrusted input in both prompts and is fenced;
  why the reply is sanitised server-side even though the plugin already did it;
  why a token is single-use and short-lived.

## 7. Running this beside plan 2

The owner runs this plan and [02-share-links.md](02-share-links.md) at the same
time. This half needs nothing plan 2 builds; the plugin's reply half does, since
the link it writes is a coalition on the `/e/<id>` route.

What the two plans share is five files. Most of each plan does not touch them,
so start with the work that does not, and keep the shared edits small and late.

| File | Plan 2 does | This plan does | How to keep it cheap |
| --- | --- | --- | --- |
| `backend/app/main.py` | adds the card and PNG routes | adds five outreach routes | Append each plan's routes as one contiguous block near the end, above the static mount. Two blocks in different places merge cleanly; two interleaved edits do not. |
| `worker/index.js` | adds the `/e/*` branch | adds the `/approve/*` branch | See below. |
| `wrangler.jsonc` | `run_worker_first` gains `/e/*` | gains `/approve/*` | A one-line conflict. Whoever merges second re-adds their entry by hand. |
| `doc/threat-model.md` | adds T13 | adds T14 | Whoever merges second renumbers their section and the "Where this is tested" row. |
| `js/strings.csv`, `index.html` | a privacy paragraph about links | a privacy paragraph about outreach | Different keys are different rows, so the sheet merges. Keep the Danish markup edit in the same commit as the sheet edit, or `test/i18n.test.mjs` fails. |

**The Worker is worth coordinating rather than merging.** Both branches do the
same thing: match a dynamic path, fetch the asset page through `env.ASSETS`, and
return it with changes. Let plan 2 land its branch first and factor out a
helper, something like `servePage(env, request, { transform, headers })`. This
plan then adds four lines that call it instead of a second copy of the same
fetch. If this plan is ready first, write the helper here and say so, so plan 2
uses it. Two hand-merged copies of the same twenty lines is the outcome to
avoid.

## 8. Order of work

1. Store, `POST /api/admin/outreach/drafts`, and the email. Verify by running
   the plugin with `--submit server` against a local backend
   (`ELECTION_STORE=sqlite`, SMTP pointed at a test inbox).
2. The approval page and the two token routes, with posting stubbed by the fake
   poster. Approve a draft end to end and watch the status change.
3. `reddit.py` and the real credentials, in a test subreddit of your own first.
4. The caps, the report section, the privacy and threat-model text.

## Acceptance

- The plugin queues a draft, an email arrives, the link opens on a phone and
  shows the thread and the reply, and pressing send without the secret is
  refused.
- The same link a second time is `404`.
- A link older than 72 hours is `404`.
- A second draft for the same `thing_id` is `409` and the plugin reports
  "already queued".
- `curl -sI https://<site>/approve/<token>` carries `X-Robots-Tag: noindex` and
  no `og:` tags.
- Both test suites green, `backend/tests/test_outreach.py` covering the token
  lifecycle, the two-factor send, the caps and the sanitiser.
