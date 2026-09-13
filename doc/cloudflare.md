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
```

`/api/config` through the domain now answers 200. Cloud Run is not checking
the header yet, so nothing is locked down so far.

## 4. Lock the origin

`deploy.local.sh` already mounts `ORIGIN_SECRET` and sets
`PUBLIC_BASE_URL=https://koalitionsberegner.moritzmarcus.com`, which is where
Stripe sends people back to. Deploy the backend:

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

## 5. Point Stripe and Firebase at the domain

- **Stripe → Developers → Webhooks**: the endpoint URL becomes
  `https://koalitionsberegner.moritzmarcus.com/api/billing/webhook`. Editing an
  existing endpoint's URL keeps its signing secret. In live mode you create the
  endpoint fresh, and its `whsec_…` goes into `STRIPE_WEBHOOK_SECRET`.
- **Firebase → Authentication → Settings → Authorized domains**: add
  `koalitionsberegner.moritzmarcus.com`.
- **GCP → APIs & Services → Credentials**: if the Firebase browser key is
  restricted by HTTP referrer, add `https://koalitionsberegner.moritzmarcus.com/*`.

## 6. Harden the zone (dashboard, moritzmarcus.com)

- **SSL/TLS → Edge Certificates**: *Always Use HTTPS* on, minimum TLS 1.2.
- **Security → WAF → Rate limiting rules** (the free plan allows one rule):
  - If: `starts_with(http.request.uri.path, "/api/") and http.request.uri.path ne "/api/billing/webhook"`
  - Rate: 100 requests per 10 seconds, counted per IP
  - Action: block for 10 seconds

  This is roughly ten times what a person clicking around produces. Lower it once
  you have seen real traffic in *Security → Analytics*.
- **Leave Bot Fight Mode off.** It cannot be exempted for one path, and it
  would challenge Stripe's webhook deliveries. The rate limit, the origin secret
  and the app's own quotas cover what it would.

## Limits worth knowing on launch day

- **Workers Free allows 100,000 Worker requests a day**, and only `/api/*`
  counts. Static files are served without running the Worker. Each page view
  makes a handful of API calls, so a busy day can hit the cap, and past it API
  calls fail until midnight UTC. **Workers Paid** ($5/month, 10 million requests)
  removes that risk.
- **Multi-region later** changes one line: point `ORIGIN_URL` at a global
  Google load balancer with Cloud Run in several regions behind it. Nothing
  else here changes.
