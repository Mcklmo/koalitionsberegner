/**
 * The public front door at koalitionsberegner.moritzmarcus.com.
 *
 * The page itself never reaches this code: `index.html` and `js/` are served
 * straight from Cloudflare's asset store, at the edge nearest the visitor (see
 * `run_worker_first` in wrangler.jsonc). Only `/api/*` and `/e/*` run here:
 * the first is forwarded to Cloud Run carrying the secret without which the
 * origin answers 403 — so the `run.app` address serves nothing to anyone who
 * goes around us; the second is the page itself, unfurled (see
 * `doc/plans/02-share-links.md`, WP2).
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

//: A shared link's id: a prefix of an election hash, 12 to 64 hex characters
//: (see `backend/app/share.py`, `MIN_ID_LENGTH`/`ID_LENGTH`).
const SHARE_PATH = /^\/e\/([0-9a-f]{12,64})$/;
const OG_IMAGE_PATH = /^\/api\/og\/[0-9a-f]{12,64}\.png$/;

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
 * Fetch the static page (the asset copy, never the Cloud Run image's own) and
 * return it with `transform` applied to its HTML and `headers` merged over
 * the asset response's own — which already carries `_headers`. Shared by
 * every Worker route that answers with the page rather than the API.
 */
export async function servePage(env, request, { transform, headers } = {}) {
  const asset = await env.ASSETS.fetch(new Request(new URL('/', request.url)));
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
  const card = (await fetchCard(env, request, id)) ?? GENERIC_CARD;
  const pageUrl = request.url;
  return servePage(env, request, {
    transform: (html) => html
      .replace(/<title>.*?<\/title>/s, `<title>${escapeAttribute(card.title)}</title>`)
      .replace('</head>', `${shareTags(card, pageUrl)}\n</head>`),
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
      [REPORT_SECRET_HEADER]: env.USAGE_REPORT_SECRET,
    },
  });
  // Thrown rather than logged, so the run shows as failed in the dashboard.
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method;

    const shared = (method === 'GET' || method === 'HEAD') && url.pathname.match(SHARE_PATH);
    if (shared) return serveSharedLink(request, env, shared[1]);

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
      // A deployment without the secret has no daily job to run.
      console.warn('the daily job is not configured; skipping');
      return;
    }
    // If it fails, the run is marked failed.
    ctx.waitUntil(callOrigin(env, USAGE_REPORTS_PATH));
  },
};
