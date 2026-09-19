# koalitionsberegner.moritzmarcus.com on Cloudflare

The app runs on Cloud Run in one region. Cloudflare sits in front of it:

```
visitor ──▶ Cloudflare edge ──┬─ index.html, js/ ──▶ asset store (served at the edge)
                              └─ /api/*          ──▶ Worker ──▶ Cloud Run (+ x-origin-secret)
```

- **The page** is served from Cloudflare's asset store at the edge nearest the
  visitor, with the headers in [`_headers`](../_headers). It never touches Cloud Run.
- **The API** goes through the Worker in [`worker/index.js`](../worker/index.js).
  It adds `x-origin-secret`, and Cloud Run answers 403 without that header
  (`ORIGIN_SECRET`, see `app.main.require_origin_secret`). The `run.app`
  address still resolves, but it serves nothing to anyone who bypasses Cloudflare.
- **Cloudflare's protections run before the Worker**: rate limiting, WAF, TLS.

Nothing is published except `index.html`, `js/` and `_headers`. Wrangler's
assets directory is the repo root, so [`.assetsignore`](../.assetsignore)
excludes everything else, `.env` first. `backend/tests/test_cloudflare.py`
fails if a new top-level file would slip through.

## 1. Sign in

```sh
npx wrangler@latest login
```

`run_worker_first` in `wrangler.jsonc` needs a recent wrangler, which is why
these commands use `@latest`.

## 2. Deploy the Worker

```sh
npx wrangler@latest deploy
```

The `custom_domain` route creates the DNS record for
`koalitionsberegner.moritzmarcus.com` and its certificate by itself. Do not add
a DNS record by hand. Give it a minute, then:

```sh
curl -sI https://koalitionsberegner.moritzmarcus.com/ | head -1          # 200, the page
curl -s  https://koalitionsberegner.moritzmarcus.com/api/config          # 503: no secret yet
curl -sI https://koalitionsberegner.moritzmarcus.com/.env | head -1      # 404
curl -sI https://koalitionsberegner.moritzmarcus.com/wrangler.jsonc | head -1  # 404
```

## 3. One secret, in both places

Generate the value once and pipe it into both stores, so it is never printed
and never lands in shell history:

```sh
SECRET=$(openssl rand -hex 32)
printf %s "$SECRET" | gcloud secrets create ORIGIN_SECRET \
  --project koalitionsberegner --replication-policy automatic --data-file=-
printf %s "$SECRET" | npx wrangler@latest secret put ORIGIN_SECRET
unset SECRET

gcloud secrets add-iam-policy-binding ORIGIN_SECRET --project koalitionsberegner \
  --member serviceAccount:koalitionsberegner@koalitionsberegner.iam.gserviceaccount.com \
  --role roles/secretmanager.secretAccessor

# Same routine for the owner's import secret — importing is refused without it.
ADMIN=$(openssl rand -hex 32)
printf %s "$ADMIN" | gcloud secrets create ADMIN_SECRET \
  --project koalitionsberegner --replication-policy automatic --data-file=-
unset ADMIN

gcloud secrets add-iam-policy-binding ADMIN_SECRET --project koalitionsberegner \
  --member serviceAccount:koalitionsberegner@koalitionsberegner.iam.gserviceaccount.com \
  --role roles/secretmanager.secretAccessor
```

`/api/config` through the domain now answers 200. Cloud Run is not checking
the header yet, so nothing is locked down so far.

## 4. Lock the origin

`deploy.local.sh` already mounts `ORIGIN_SECRET` and `ADMIN_SECRET`, and sets
`PUBLIC_BASE_URL=https://koalitionsberegner.moritzmarcus.com`. Deploy the
backend:

If election requests are wanted (anyone asking for an election that has not
been imported, filed as an issue — see the README), the revision also needs
`GITHUB_ISSUES_TOKEN` mounted from Secret Manager the way `ORIGIN_SECRET` is,
and `GITHUB_ISSUES_REPO=mcklmo/koalitionsberegner` as a plain variable. The
token is a fine-grained PAT with **Issues: write** on that repository and
nothing else. Without them the app runs exactly as before and the page simply
does not offer requests.

```sh
./deploy.local.sh
```

It deploys the working tree, so do this with the tests green. Then:

```sh
RUN=https://koalitionsberegner-711803377000.europe-north1.run.app
curl -s -o /dev/null -w "%{http_code}\n" $RUN/api/config      # 403
curl -s -o /dev/null -w "%{http_code}\n" $RUN/                # 403
curl -s -o /dev/null -w "%{http_code}\n" $RUN/healthz         # 200, the platform's probe
curl -s -o /dev/null -w "%{http_code}\n" https://koalitionsberegner.moritzmarcus.com/api/config  # 200
```

To undo it, `gcloud run services update koalitionsberegner --region europe-north1
--project koalitionsberegner --remove-secrets ORIGIN_SECRET` makes `run.app`
answer directly again.

## 5. Harden the zone (dashboard, moritzmarcus.com)

- **SSL/TLS → Edge Certificates**: *Always Use HTTPS* on, minimum TLS 1.2.
- **Security → WAF → Rate limiting rules** (the free plan allows one rule):
  - If: `starts_with(http.request.uri.path, "/api/")`
  - Rate: 100 requests per 10 seconds, counted per IP
  - Action: block for 10 seconds

  This is roughly ten times what a person clicking around produces. Lower it once
  you have seen real traffic in *Security → Analytics*.

  It matters more than it used to: `POST /api/elections/requests` takes an
  election request from anyone, with no secret, and opens a GitHub issue for it
  (doc/threat-model.md T12). One election is one issue, so a repeated form is
  already a no-op — but this rule is what bounds a caller working through made-up
  ones. The free plan allows a single rule, so if the tracker ever does get
  spammed, the move is to narrow this rule to that path at a much lower rate
  (a handful per minute is generous for a form somebody fills in by hand).
- **Leave Bot Fight Mode off.** It cannot be exempted for one path, and it
  would challenge the schedule's own calls to `/api/internal/*`. The rate
  limit, the origin secret and the admin secret cover what it would.

## 6. Usage reports by email

The Worker's cron (`triggers.crons` in `wrangler.jsonc`, 06:00 UTC daily) calls
`POST /api/internal/usage-reports` on the origin. The backend emails yesterday's
report, plus last week's on a Monday and last month's on the 1st. A report is
sent once however often the cron fires. `/api/internal/*` is never forwarded
from the public side.

The same one-value-in-both-places routine as step 3:

```sh
SECRET=$(openssl rand -hex 32)
printf %s "$SECRET" | gcloud secrets create USAGE_REPORT_SECRET --project koalitionsberegner --replication-policy automatic --data-file=-
printf %s "$SECRET" | npx wrangler@latest secret put USAGE_REPORT_SECRET
unset SECRET

printf %s "$SMTP_APP_PASSWORD" | gcloud secrets create smtp-password --project koalitionsberegner --replication-policy automatic --data-file=-
```

Grant the service account `roles/secretmanager.secretAccessor` on both, as for
`ORIGIN_SECRET`. Then add these to `deploy.local.sh`:

- secrets `USAGE_REPORT_SECRET=USAGE_REPORT_SECRET:latest` and
  `SMTP_PASSWORD=smtp-password:latest`
- plain variables `SMTP_HOST`, `SMTP_USERNAME` and `REPORT_EMAIL_TO`

Redeploy the backend and the Worker. To try it without waiting for 06:00, make
the call the cron makes, straight to Cloud Run. The secrets are piped in as
headers, so they never appear in the command line or the shell history:

```sh
RUN=https://koalitionsberegner-711803377000.europe-north1.run.app
{
  printf 'x-origin-secret: %s\n' "$(gcloud secrets versions access latest --secret ORIGIN_SECRET --project koalitionsberegner)"
  printf 'x-report-secret: %s\n' "$(gcloud secrets versions access latest --secret USAGE_REPORT_SECRET --project koalitionsberegner)"
} | curl -s -X POST -H @- "$RUN/api/internal/usage-reports?period=daily"
# {"sent":["daily:…"],"skipped":[]} and an email; run it again and it is "skipped"
```

A report sent this way is not sent again: the 06:00 run lists that day's daily
report under `skipped`.

What is counted, and why it is only counts, is in `backend/app/usage.py`.
Page loads come from `/api/config`, so they include bots that run the page's
scripts.

## Limits worth knowing on launch day

- **Workers Free allows 100,000 Worker requests a day**, and only `/api/*`
  counts. Static files are served without running the Worker. Each page view
  makes a handful of API calls, so a busy day can hit the cap, and past it API
  calls fail until midnight UTC. **Workers Paid** ($5/month, 10 million requests)
  removes that risk.
- **Multi-region later** changes one line: point `ORIGIN_URL` at a global
  Google load balancer with Cloud Run in several regions behind it. Nothing
  else here changes.
