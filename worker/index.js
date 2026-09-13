/**
 * The public front door at koalitionsberegner.moritzmarcus.com.
 *
 * The page itself never reaches this code: `index.html` and `js/` are served
 * straight from Cloudflare's asset store, at the edge nearest the visitor (see
 * `run_worker_first` in wrangler.jsonc). Only `/api/*` runs here, and it is
 * forwarded to Cloud Run carrying the secret without which the origin answers
 * 403 — so the `run.app` address serves nothing to anyone who goes around us.
 *
 * `ORIGIN_URL` is a plain var in wrangler.jsonc; `ORIGIN_SECRET` is set with
 * `wrangler secret put ORIGIN_SECRET` and must equal the backend's.
 */

export const ORIGIN_SECRET_HEADER = 'x-origin-secret';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) {
      // A static file that does not exist. Nothing else is ours to forward.
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
};
