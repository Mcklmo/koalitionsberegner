/**
 * The public front door at koalitionsberegner.moritzmarcus.com.
 *
 * The page itself never reaches this code: `index.html` and `js/` are served
 * straight from Cloudflare's asset store, at the edge nearest the visitor (see
 * `run_worker_first` in wrangler.jsonc). Only `/api/*`, `/e/*` and `/approve/*`
 * run here: the first is forwarded to Cloud Run carrying the secret without
 * which the origin answers 403 — so the `run.app` address serves nothing to
 * anyone who goes around us; the second is the page itself, unfurled (see
 * `doc/plans/02-share-links.md`, WP2); the third is the outreach approval page
 * (see `doc/plans/04-reddit-outreach.md`, section 2) — never unfurled, never
 * indexed, so an approval link leaked to a crawler or a chat preview shows
 * nothing.
 *
 * Three crons in wrangler.jsonc run `scheduled`, dispatched by `controller.cron`
 * (doc/plans/03-remaining-work.md, A3): once a day, it asks the origin to email
 * the usage reports that are due; every 30 minutes, it asks the origin to
 * refresh whichever tracked elections are due; once a month, it asks the
 * origin to scan for new elections to track. All three endpoints live under
 * `/api/internal/`, which is never forwarded from the public side: the
 * schedule secret already guards them, and this keeps them off the internet
 * altogether.
 *
 * `ORIGIN_URL` is a plain var in wrangler.jsonc; `ORIGIN_SECRET` and
 * `USAGE_REPORT_SECRET` are set with `wrangler secret put` and must equal the
 * backend's. `USAGE_REPORT_SECRET` is kept as the variable's name to avoid
 * rotating it (doc/plans/03-remaining-work.md, D3) — only the header constant
 * below is renamed, for honesty: every scheduled call carries it now, not
 * only the usage reports.
 */

export const ORIGIN_SECRET_HEADER = 'x-origin-secret';
export const SCHEDULE_SECRET_HEADER = 'x-report-secret';
export const USAGE_REPORTS_PATH = '/api/internal/usage-reports';
export const REFRESH_PATH = '/api/internal/refresh';
export const CALENDAR_SCAN_PATH = '/api/internal/calendar-scan';

//: The three crons, exactly as wrangler.jsonc's `triggers.crons` spells them —
//: `controller.cron` is compared against these, not parsed, so a typo in
//: either place shows up as the wrong branch running rather than a silent
//: mismatch.
export const DAILY_CRON = '0 6 * * *';
export const REFRESH_CRON = '*/30 * * * *';
export const CALENDAR_SCAN_CRON = '0 5 1 * *';

/**
 * This year and the next two, comma-separated: the window
 * `app.calendar.CalendarScanner.scan` accepts (`MAX_YEARS_AHEAD`). Computed
 * here rather than pinned in wrangler.jsonc, so nobody has to remember to
 * bump it every December.
 */
export function calendarScanYears(now = new Date()) {
  const year = now.getUTCFullYear();
  return `${year},${year + 1},${year + 2}`;
}

//: A shared link's id: a prefix of an election hash, 12 to 64 hex characters
//: (see `backend/app/share.py`, `MIN_ID_LENGTH`/`ID_LENGTH`), with an optional
//: trailing slash — matching `js/share.js`'s `ID_IN_PATH` exactly, so a link
//: either side accepts, the other does too.
const SHARE_PATH = /^\/e\/([0-9a-f]{12,64})\/?$/;
const OG_IMAGE_PATH = /^\/api\/og\/[0-9a-f]{12,64}\.png$/;

//: An outreach approval link: `/approve/<token>`, the token being whatever
//: `secrets.token_urlsafe(32)` produced (`app.outreach.new_token`) — URL-safe
//: base64, so letters, digits, `-` and `_`. Anything else is not a link this
//: deployment ever sent, so it gets a plain 404 rather than the page.
const APPROVE_PATH = /^\/approve\/([A-Za-z0-9_-]+)\/?$/;

//: What a link unfurls into when its card cannot be read — an unknown id, or
//: the origin not answering. The page still loads either way.
const GENERIC_CARD = {
  title: 'Koalitionsberegner',
  description: 'Pick parties and see whether they add up to a majority.',
  image_path: null,
};

/** `& < > " '` escaped, for text dropped into an HTML attribute or body. */
export function escapeAttribute(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

/**
 * Fetch a static asset (the asset copy, never the Cloud Run image's own) and
 * return it with `transform` applied to its HTML and `headers` merged over
 * the asset response's own — which already carries `_headers`. Shared by
 * every Worker route that answers with a page rather than the API.
 * `path` defaults to `/` (`index.html`, what `/e/*` unfurls); `/approve/*`
 * passes its own small page instead.
 */
export async function servePage(env, request, { path = '/', transform, headers } = {}) {
  const asset = await env.ASSETS.fetch(new Request(new URL(path, request.url)));
  let body = await asset.text();
  if (transform) body = transform(body);
  const responseHeaders = new Headers(asset.headers);
  for (const [name, value] of Object.entries(headers ?? {})) {
    responseHeaders.set(name, value);
  }
  return new Response(body, { status: asset.status, headers: responseHeaders });
}

/** The `<meta>` tags a shared link's card is worded into, `</head>`-ready. */
function shareTags(card, pageUrl) {
  const title = escapeAttribute(card.title);
  const description = escapeAttribute(card.description);
  const tags = [
    '<meta property="og:type" content="website">',
    '<meta property="og:site_name" content="Koalitionsberegner">',
    `<meta property="og:title" content="${title}">`,
    `<meta property="og:description" content="${description}">`,
    `<meta property="og:url" content="${escapeAttribute(pageUrl)}">`,
    `<meta name="twitter:title" content="${title}">`,
    `<meta name="twitter:description" content="${description}">`,
  ];
  if (card.image_path) {
    const imageUrl = escapeAttribute(new URL(card.image_path, pageUrl).toString());
    tags.push(
      `<meta property="og:image" content="${imageUrl}">`,
      '<meta property="og:image:width" content="1200">',
      '<meta property="og:image:height" content="630">',
      `<meta property="og:image:alt" content="${description}">`,
      '<meta name="twitter:card" content="summary_large_image">',
      `<meta name="twitter:image" content="${imageUrl}">`,
    );
  } else {
    tags.push('<meta name="twitter:card" content="summary">');
  }
  return tags.join('\n  ');
}

/** The card a shared link unfurls into, cached under its own public URL. */
async function fetchCard(env, request, id) {
  if (!env.ORIGIN_URL || !env.ORIGIN_SECRET) return null;
  const url = new URL(request.url);
  const cacheKey = new Request(new URL(`/api/elections/${id}/card${url.search}`, request.url));
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) return cached.json();

  let response;
  try {
    response = await fetch(new URL(`/api/elections/${id}/card${url.search}`, env.ORIGIN_URL), {
      headers: { [ORIGIN_SECRET_HEADER]: env.ORIGIN_SECRET },
    });
  } catch {
    return null; // the origin did not answer at all
  }
  if (!response.ok) return null; // 404 (unknown id), 409 (ambiguous), ...
  if (response.headers.get('cache-control')) {
    await cache.put(cacheKey, response.clone());
  }
  return response.json();
}

async function serveSharedLink(request, env, id) {
  // `id` is null for an `/e/*` path the Worker cannot resolve to an id of its
  // own (a bad length, uppercase letters, ...): the generic card still loads
  // the page rather than a bare 404, and the frontend's own parsing decides
  // what to say about it.
  const card = (id ? await fetchCard(env, request, id) : null) ?? GENERIC_CARD;
  const pageUrl = request.url;
  return servePage(env, request, {
    // A function, not a string, as the replacement: a string replacement is
    // read for `$&`/`$``/`$'`/`$<name>` patterns, which an escaped title or
    // party abbreviation can easily contain (e.g. a literal `$&` in a name).
    // A function's return value is inserted verbatim.
    transform: (html) => html
      .replace(/<title>.*?<\/title>/s, () => `<title>${escapeAttribute(card.title)}</title>`)
      .replace('</head>', () => `${shareTags(card, pageUrl)}\n</head>`),
    headers: { 'cache-control': 'public, max-age=300' },
  });
}

/** Forward one request to Cloud Run, carrying the origin secret. */
async function forwardToOrigin(request, env, url) {
  const headers = new Headers(request.headers);
  // The origin routes by its own hostname, not ours.
  headers.delete('host');
  headers.set(ORIGIN_SECRET_HEADER, env.ORIGIN_SECRET);

  const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
  return fetch(new URL(url.pathname + url.search, env.ORIGIN_URL), {
    method: request.method,
    headers,
    // Streamed, not buffered: the origin enforces the size limit itself.
    body: hasBody ? request.body : undefined,
    duplex: hasBody ? 'half' : undefined,
    redirect: 'manual',
  });
}

async function callOrigin(env, path) {
  const response = await fetch(new URL(path, env.ORIGIN_URL), {
    method: 'POST',
    headers: {
      [ORIGIN_SECRET_HEADER]: env.ORIGIN_SECRET,
      [SCHEDULE_SECRET_HEADER]: env.USAGE_REPORT_SECRET,
    },
  });
  // Thrown rather than logged, so the run shows as failed in the dashboard.
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method;

    // Every `/e/*` path is the shared-link page, whether or not its id is one
    // the Worker recognises — an unresolved id still gets the page, with the
    // generic card, rather than a bare 404 the frontend never runs against.
    if ((method === 'GET' || method === 'HEAD') && url.pathname.startsWith('/e/')) {
      const match = url.pathname.match(SHARE_PATH);
      return serveSharedLink(request, env, match ? match[1] : null);
    }

    // The approval page: never unfurled, never indexed, never cached — an
    // approval link is single-use and every fetch of the page itself must be
    // the one the owner is looking at (doc/plans/04-reddit-outreach.md,
    // section 2). Its own script calls the API routes below for the draft
    // and to send or reject it.
    if ((method === 'GET' || method === 'HEAD') && url.pathname.startsWith('/approve/')) {
      if (!APPROVE_PATH.test(url.pathname)) return new Response('Not found', { status: 404 });
      return servePage(env, request, {
        path: '/approve.html',
        headers: { 'X-Robots-Tag': 'noindex', 'Cache-Control': 'no-store' },
      });
    }

    if (!url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/internal/')) {
      // A static file that does not exist, or an endpoint only the cron calls.
      return new Response('Not found', { status: 404 });
    }
    if (!env.ORIGIN_URL || !env.ORIGIN_SECRET) {
      // Forwarding without the secret would only earn a 403 from the origin;
      // saying so here makes a missing `wrangler secret put` obvious.
      return Response.json({ detail: 'the API is not configured' }, { status: 503 });
    }

    if ((method === 'GET' || method === 'HEAD') && OG_IMAGE_PATH.test(url.pathname)) {
      // A link pasted into a busy thread renders its image once, not once per
      // viewer. Everything else under /api/* stays uncached.
      const cache = caches.default;
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await forwardToOrigin(request, env, url);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    }

    return forwardToOrigin(request, env, url);
  },

  async scheduled(controller, env, ctx) {
    if (!env.ORIGIN_URL || !env.ORIGIN_SECRET || !env.USAGE_REPORT_SECRET) {
      // A deployment without the secret has no scheduled job to run.
      console.warn('the schedule is not configured; skipping');
      return;
    }
    // If it fails, the run is marked failed — for all three, alike.
    if (controller.cron === REFRESH_CRON) {
      ctx.waitUntil(callOrigin(env, REFRESH_PATH));
      return;
    }
    if (controller.cron === CALENDAR_SCAN_CRON) {
      ctx.waitUntil(callOrigin(env, `${CALENDAR_SCAN_PATH}?years=${calendarScanYears()}`));
      return;
    }
    // The daily cron (or an unrecognised one): the usage reports, whichever
    // are due — the schedule this Worker has run the longest, so it is what
    // an unexpected `controller.cron` falls back to rather than doing nothing.
    ctx.waitUntil(callOrigin(env, USAGE_REPORTS_PATH));
  },
};
