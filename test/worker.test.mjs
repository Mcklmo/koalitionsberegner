import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  escapeAttribute,
  ORIGIN_SECRET_HEADER,
  SCHEDULE_SECRET_HEADER,
  USAGE_REPORTS_PATH,
  REFRESH_PATH,
  CALENDAR_SCAN_PATH,
  DAILY_CRON,
  REFRESH_CRON,
  CALENDAR_SCAN_CRON,
  calendarScanYears,
} from '../worker/index.js';

const ENV = {
  ORIGIN_URL: 'https://koalitionsberegner-origin.example.run.app',
  ORIGIN_SECRET: 's'.repeat(64),
};

const realFetch = globalThis.fetch;
const realCaches = globalThis.caches;
afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.caches = realCaches;
});

/** A `caches.default` stand-in: an in-memory map keyed by request URL. */
function recordCache() {
  const store = new Map();
  const cache = {
    match: async (key) => {
      const hit = store.get(String(key.url ?? key));
      return hit ? hit.clone() : undefined;
    },
    put: async (key, response) => { store.set(String(key.url ?? key), response); },
  };
  globalThis.caches = { default: cache };
  return store;
}

/** A minimal `env.ASSETS` binding: answers one HTML page for every fetch. */
function recordAssets(html, { status = 200, headers = {} } = {}) {
  const calls = [];
  return {
    calls,
    ASSETS: {
      fetch: async (request) => {
        calls.push(String(request.url ?? request));
        return new Response(html, { status, headers });
      },
    },
  };
}

/** Replace fetch with a recorder that answers 200, or `statusFor(url)` when given. */
function recordFetches(statusFor = () => 200) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response('{}', { status: statusFor(String(url)) });
  };
  return calls;
}

test('an API request is forwarded to the origin with the secret', async () => {
  const calls = recordFetches();
  const request = new Request(
    'https://koalitionsberegner.moritzmarcus.com/api/elections/lookup?year=2026&nation=Danmark',
    { headers: { authorization: 'Bearer token', host: 'koalitionsberegner.moritzmarcus.com' } }
  );

  const response = await worker.fetch(request, ENV);

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    `${ENV.ORIGIN_URL}/api/elections/lookup?year=2026&nation=Danmark`,
    'path and query arrive unchanged, on the origin host'
  );
  const headers = calls[0].init.headers;
  assert.equal(headers.get(ORIGIN_SECRET_HEADER), ENV.ORIGIN_SECRET);
  assert.equal(headers.get('authorization'), 'Bearer token', 'an authorization header still rides along');
  assert.equal(headers.get('host'), null, 'the origin routes by its own hostname');
  assert.equal(calls[0].init.redirect, 'manual');
  assert.equal(calls[0].init.body, undefined);
});

test('a POST body is passed on byte for byte', async () => {
  const calls = recordFetches();
  const payload = '{"url":"https://example.org/results"}';
  const request = new Request('https://koalitionsberegner.moritzmarcus.com/api/elections/import', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: payload,
  });

  await worker.fetch(request, ENV);

  assert.equal(calls[0].init.method, 'POST');
  const forwarded = await new Response(calls[0].init.body).text();
  assert.equal(forwarded, payload, 'nothing rewrites the body in transit');
});

test('a client cannot supply its own secret header to learn anything', async () => {
  const calls = recordFetches();
  const request = new Request('https://koalitionsberegner.moritzmarcus.com/api/elections', {
    headers: { [ORIGIN_SECRET_HEADER]: 'guess' },
  });

  await worker.fetch(request, ENV);

  assert.equal(calls[0].init.headers.get(ORIGIN_SECRET_HEADER), ENV.ORIGIN_SECRET);
});

test('anything that is not the API is not forwarded', async () => {
  const calls = recordFetches();
  for (const path of ['/.env', '/docs', '/openapi.json', '/backend/app/main.py', '/healthz']) {
    const response = await worker.fetch(
      new Request(`https://koalitionsberegner.moritzmarcus.com${path}`), ENV
    );
    assert.equal(response.status, 404, path);
  }
  assert.equal(calls.length, 0);
});

test('without its secret the Worker says so instead of forwarding', async () => {
  const calls = recordFetches();
  const response = await worker.fetch(
    new Request('https://koalitionsberegner.moritzmarcus.com/api/config'),
    { ORIGIN_URL: ENV.ORIGIN_URL }
  );
  assert.equal(response.status, 503);
  assert.equal(calls.length, 0);
});

// --- shared links: /e/* and its preview image ----------------------------

const PAGE_HTML = '<!DOCTYPE html><html><head><title>Denmark — 2026</title></head><body></body></html>';
const ID = 'a'.repeat(16);

/** A `globalThis.fetch` stand-in that answers the origin's card endpoint. */
function recordCardFetch(card, { status = 200 } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(card), {
      status,
      headers: status === 200 ? { 'cache-control': 'public, max-age=3600' } : {},
    });
  };
  return calls;
}

test('escapeAttribute escapes the five characters HTML attributes need escaped', () => {
  assert.equal(escapeAttribute(`& < > " '`), '&amp; &lt; &gt; &quot; &#39;');
});

test('a shared link is worded from the origin\'s card', async () => {
  recordCardFetch({ title: 'A + F + B', description: '79 of 179 seats.', image_path: `/api/og/${ID}.png?c=0` });
  const assets = recordAssets(PAGE_HTML);
  recordCache();

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/e/${ID}?c=0`),
    { ...ENV, ...assets }
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'public, max-age=300');
  const html = await response.text();
  assert.match(html, /<title>A \+ F \+ B<\/title>/);
  assert.match(html, /<meta property="og:description" content="79 of 179 seats\.">/);
  assert.match(html, /<meta property="og:image" content="[^"]*\/api\/og\/a{16}\.png\?c=0">/);
  assert.match(html, /<meta property="og:image:width" content="1200">/);
  assert.match(html, /<meta name="twitter:card" content="summary_large_image">/);
});

test('a title with markup and quotes arrives escaped', async () => {
  recordCardFetch({
    title: '<script>alert(1)</script>',
    description: 'A "quoted" description',
    image_path: null,
  });
  const assets = recordAssets(PAGE_HTML);
  recordCache();

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/e/${ID}`), { ...ENV, ...assets }
  );

  const html = await response.text();
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /content="A &quot;quoted&quot; description"/);
  assert.doesNotMatch(html, /og:image/, 'no image_path means no image tags');
});

test('a title with $-patterns survives, unread as a replacement string', async () => {
  // `String.prototype.replace` reads `$&`, `$``, `$'`, `$<name>` out of a
  // *string* replacement; escaping turns a lone `$` into `$&lt;` etc., which
  // then contains `$&` (the whole match) if the replacement is a string
  // rather than a function. A function replacement must not do that.
  recordCardFetch({
    title: "$` and $& and $' and $<x> in one title",
    description: 'plain',
    image_path: null,
  });
  const assets = recordAssets(PAGE_HTML);
  recordCache();

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/e/${ID}`), { ...ENV, ...assets }
  );

  const html = await response.text();
  assert.match(
    html,
    /<title>\$` and \$&amp; and \$&#39; and \$&lt;x&gt; in one title<\/title>/,
    'the escaped title is inserted verbatim, not reinterpreted as a replacement pattern'
  );
  assert.match(html, /<meta property="og:title" content="\$` and \$&amp;/);
});

test('an unknown id still answers 200 with the generic tags', async () => {
  recordCardFetch({}, { status: 404 });
  const assets = recordAssets(PAGE_HTML);
  recordCache();

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/e/${ID}`), { ...ENV, ...assets }
  );

  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Koalitionsberegner<\/title>/);
  assert.match(html, /<meta property="og:title" content="Koalitionsberegner">/);
});

test('/e/ with a bad id still serves the page, with the generic tags', async () => {
  // A bad-length or uppercase id, or one followed by an extra path segment,
  // is not a link the Worker can resolve — but it is still `/e/*`, so the
  // frontend must get the real page (and its own parsing) rather than a bare
  // Worker 404 it never runs against.
  const calls = recordFetches();
  const assets = recordAssets(PAGE_HTML);
  recordCache();
  for (const bad of ['short12345', 'ThisIsSixteenNOT', 'a'.repeat(16) + '/extra']) {
    const response = await worker.fetch(
      new Request(`https://koalitionsberegner.moritzmarcus.com/e/${bad}`), { ...ENV, ...assets }
    );
    assert.equal(response.status, 200, bad);
    const html = await response.text();
    assert.match(html, /<title>Koalitionsberegner<\/title>/, bad);
  }
  assert.equal(calls.length, 0, 'an id the Worker cannot even parse is never asked about');
});

test('/e/<id>/ with a trailing slash resolves the same as /e/<id>', async () => {
  recordCardFetch({ title: 'A + F + B', description: '79 of 179 seats.', image_path: null });
  const assets = recordAssets(PAGE_HTML);
  recordCache();

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/e/${ID}/`), { ...ENV, ...assets }
  );

  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>A \+ F \+ B<\/title>/);
});

test('a second request for the same preview image is served from the cache', async () => {
  const store = recordCache();
  const calls = recordFetches();
  const request = () => new Request(
    `https://koalitionsberegner.moritzmarcus.com/api/og/${ID}.png?c=0`
  );

  const first = await worker.fetch(request(), ENV);
  assert.equal(first.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(store.size, 1, 'the response was cached under its own public URL');

  const second = await worker.fetch(request(), ENV);
  assert.equal(second.status, 200);
  assert.equal(calls.length, 1, 'the second request never reached the origin');
});

// --- the outreach approval page: /approve/* -------------------------------

const APPROVE_HTML = '<!DOCTYPE html><html><head><title>Koalitionsberegner — outreach</title></head><body></body></html>';
const TOKEN = 'abc123_-XYZ';

test('an approval link serves approve.html, never index.html, marked noindex and uncached', async () => {
  const assets = recordAssets(APPROVE_HTML);

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/approve/${TOKEN}`), { ...ENV, ...assets }
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-robots-tag'), 'noindex');
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(await response.text(), APPROVE_HTML);
  assert.deepEqual(assets.calls, ['https://koalitionsberegner.moritzmarcus.com/approve.html']);
});

test('an approval path that is not a bare token is not the page', async () => {
  const assets = recordAssets(APPROVE_HTML);
  for (const bad of ['/approve/', '/approve/has a space', '/approve/tok/en']) {
    const response = await worker.fetch(
      new Request(`https://koalitionsberegner.moritzmarcus.com${bad}`), { ...ENV, ...assets }
    );
    assert.equal(response.status, 404, bad);
  }
  assert.equal(assets.calls.length, 0);
});

test('the approval page never carries Open Graph tags', async () => {
  const assets = recordAssets(APPROVE_HTML);

  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com/approve/${TOKEN}`), { ...ENV, ...assets }
  );

  const html = await response.text();
  assert.doesNotMatch(html, /og:/);
});

// --- the three crons ------------------------------------------------------

const REPORT_ENV = { ...ENV, USAGE_REPORT_SECRET: 'r'.repeat(64) };

/** A stand-in for the scheduled event's context, keeping what it was told to await. */
function scheduledContext() {
  const pending = [];
  return { pending, waitUntil: (promise) => pending.push(promise) };
}

test('the daily cron asks the origin for the reports, carrying both secrets', async () => {
  const calls = recordFetches();
  const ctx = scheduledContext();

  await worker.scheduled({ cron: DAILY_CRON }, REPORT_ENV, ctx);
  await Promise.all(ctx.pending);

  assert.deepEqual(calls.map((call) => call.url), [`${ENV.ORIGIN_URL}${USAGE_REPORTS_PATH}`]);
  for (const call of calls) {
    assert.equal(call.init.method, 'POST');
    assert.equal(call.init.headers[ORIGIN_SECRET_HEADER], ENV.ORIGIN_SECRET);
    assert.equal(call.init.headers[SCHEDULE_SECRET_HEADER], REPORT_ENV.USAGE_REPORT_SECRET);
  }
});

test('an unrecognised cron is logged and does nothing', async (t) => {
  const calls = recordFetches();
  t.mock.method(console, 'warn', () => {});
  const ctx = scheduledContext();

  await worker.scheduled({ cron: 'not one of ours' }, REPORT_ENV, ctx);
  await Promise.all(ctx.pending);

  // A cron string this file does not recognise must not be guessed at — it
  // is a configuration mistake, not a signal to run the daily report early
  // (plan 3 review, finding 8): the wrong guess would email reports every
  // time the mismatched cron fires and silently never refresh anything.
  assert.equal(calls.length, 0);
  assert.equal(ctx.pending.length, 0);
  assert.ok(
    console.warn.mock.calls.some((call) => call.arguments[0].includes('not one of ours')),
  );
});

test('the 30-minute cron asks the origin to refresh tracked elections', async () => {
  const calls = recordFetches();
  const ctx = scheduledContext();

  await worker.scheduled({ cron: REFRESH_CRON }, REPORT_ENV, ctx);
  await Promise.all(ctx.pending);

  assert.deepEqual(calls.map((call) => call.url), [`${ENV.ORIGIN_URL}${REFRESH_PATH}`]);
  assert.equal(calls[0].init.headers[SCHEDULE_SECRET_HEADER], REPORT_ENV.USAGE_REPORT_SECRET);
});

test('the monthly cron asks the origin to scan for new elections, years included', async () => {
  const calls = recordFetches();
  const ctx = scheduledContext();

  await worker.scheduled({ cron: CALENDAR_SCAN_CRON }, REPORT_ENV, ctx);
  await Promise.all(ctx.pending);

  assert.equal(calls.length, 1);
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, CALENDAR_SCAN_PATH);
  assert.equal(url.searchParams.get('years'), calendarScanYears());
});

test('calendarScanYears names this year and the following two', () => {
  assert.equal(calendarScanYears(new Date('2026-12-31T23:59:59Z')), '2026,2027,2028');
  assert.equal(calendarScanYears(new Date('2027-01-01T00:00:00Z')), '2027,2028,2029');
});

test('a report run the origin refused shows as a failed cron', async () => {
  globalThis.fetch = async () => new Response('{}', { status: 502 });
  const ctx = scheduledContext();

  await worker.scheduled({ cron: DAILY_CRON }, REPORT_ENV, ctx);

  await assert.rejects(Promise.all(ctx.pending), /HTTP 502/);
});

test('without a report secret no cron sends anything', async (t) => {
  const calls = recordFetches();
  t.mock.method(console, 'warn', () => {});
  const ctx = scheduledContext();

  for (const cron of [DAILY_CRON, REFRESH_CRON, CALENDAR_SCAN_CRON]) {
    await worker.scheduled({ cron }, ENV, ctx);
  }

  assert.equal(ctx.pending.length, 0);
  assert.equal(calls.length, 0);
});

test('the endpoints only the crons call are never forwarded from the public side', async () => {
  const calls = recordFetches();
  for (const path of [USAGE_REPORTS_PATH, REFRESH_PATH, CALENDAR_SCAN_PATH]) {
    const response = await worker.fetch(
      new Request(`https://koalitionsberegner.moritzmarcus.com${path}`, {
        method: 'POST',
        headers: { [SCHEDULE_SECRET_HEADER]: REPORT_ENV.USAGE_REPORT_SECRET },
      }),
      REPORT_ENV
    );
    assert.equal(response.status, 404, path);
  }
  assert.equal(calls.length, 0);
});
