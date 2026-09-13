/**
 * The public front door at koalitionsberegner.moritzmarcus.com.
 *
 * The page itself never reaches this code: `index.html` and `js/` are served
 * straight from Cloudflare's asset store, at the edge nearest the visitor (see
 * `run_worker_first` in wrangler.jsonc). Only `/api/*` runs here, and it is
 * forwarded to Cloud Run carrying the secret without which the origin answers
 * 403 — so the `run.app` address serves nothing to anyone who goes around us.
 *
 * Once a day the cron in wrangler.jsonc runs `scheduled`, which asks the origin
 * to email the usage reports that are due. That endpoint lives under
 * `/api/internal/`, which is never forwarded from the public side: the report
 * secret already guards it, and this keeps it off the internet altogether.
 *
 * `ORIGIN_URL` is a plain var in wrangler.jsonc; `ORIGIN_SECRET` and
 * `USAGE_REPORT_SECRET` are set with `wrangler secret put` and must equal the
 * backend's.
 */

export const ORIGIN_SECRET_HEADER = 'x-origin-secret';
export const REPORT_SECRET_HEADER = 'x-report-secret';
export const USAGE_REPORTS_PATH = '/api/internal/usage-reports';

async function sendUsageReports(env) {
  const response = await fetch(new URL(USAGE_REPORTS_PATH, env.ORIGIN_URL), {
    method: 'POST',
    headers: {
      [ORIGIN_SECRET_HEADER]: env.ORIGIN_SECRET,
      [REPORT_SECRET_HEADER]: env.USAGE_REPORT_SECRET,
    },
  });
  // Thrown rather than logged, so the run shows as failed in the dashboard.
  if (!response.ok) throw new Error(`usage reports: HTTP ${response.status}`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/internal/')) {
      // A static file that does not exist, or an endpoint only the cron calls.
      return new Response('Not found', { status: 404 });
    }
    if (!env.ORIGIN_URL || !env.ORIGIN_SECRET) {
      // Forwarding without the secret would only earn a 403 from the origin;
      // saying so here makes a missing `wrangler secret put` obvious.
      return Response.json({ detail: 'the API is not configured' }, { status: 503 });
    }

    const headers = new Headers(request.headers);
    // The origin routes by its own hostname, not ours.
    headers.delete('host');
    headers.set(ORIGIN_SECRET_HEADER, env.ORIGIN_SECRET);

    const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
    return fetch(new URL(url.pathname + url.search, env.ORIGIN_URL), {
      method: request.method,
      headers,
      // Streamed, not buffered: the Stripe webhook's signature covers the exact
      // bytes, and the origin enforces the size limit.
      body: hasBody ? request.body : undefined,
      duplex: hasBody ? 'half' : undefined,
      redirect: 'manual',
    });
  },

  async scheduled(controller, env, ctx) {
    if (!env.ORIGIN_URL || !env.ORIGIN_SECRET || !env.USAGE_REPORT_SECRET) {
      // Reports are optional; a deployment without them has nothing to send.
      console.warn('usage reports are not configured; skipping');
      return;
    }
    ctx.waitUntil(sendUsageReports(env));
  },
};
