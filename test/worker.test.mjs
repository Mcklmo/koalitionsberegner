import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  ORIGIN_SECRET_HEADER,
  REPORT_SECRET_HEADER,
  USAGE_REPORTS_PATH,
} from '../worker/index.js';

const ENV = {
  ORIGIN_URL: 'https://koalitionsberegner-origin.example.run.app',
  ORIGIN_SECRET: 's'.repeat(64),
};

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

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

// --- the daily cron -----------------------------------------------------------

const REPORT_ENV = { ...ENV, USAGE_REPORT_SECRET: 'r'.repeat(64) };

/** A stand-in for the scheduled event's context, keeping what it was told to await. */
function scheduledContext() {
  const pending = [];
  return { pending, waitUntil: (promise) => pending.push(promise) };
}

test('the cron asks the origin for the reports, carrying both secrets', async () => {
  const calls = recordFetches();
  const ctx = scheduledContext();

  await worker.scheduled({}, REPORT_ENV, ctx);
  await Promise.all(ctx.pending);

  assert.deepEqual(calls.map((call) => call.url), [`${ENV.ORIGIN_URL}${USAGE_REPORTS_PATH}`]);
  for (const call of calls) {
    assert.equal(call.init.method, 'POST');
    assert.equal(call.init.headers[ORIGIN_SECRET_HEADER], ENV.ORIGIN_SECRET);
    assert.equal(call.init.headers[REPORT_SECRET_HEADER], REPORT_ENV.USAGE_REPORT_SECRET);
  }
});

test('a report run the origin refused shows as a failed cron', async () => {
  globalThis.fetch = async () => new Response('{}', { status: 502 });
  const ctx = scheduledContext();

  await worker.scheduled({}, REPORT_ENV, ctx);

  await assert.rejects(Promise.all(ctx.pending), /HTTP 502/);
});

test('without a report secret the cron sends nothing', async (t) => {
  const calls = recordFetches();
  t.mock.method(console, 'warn', () => {});
  const ctx = scheduledContext();

  await worker.scheduled({}, ENV, ctx);

  assert.equal(ctx.pending.length, 0);
  assert.equal(calls.length, 0);
});

test('the endpoint only the cron calls is never forwarded from the public side', async () => {
  const calls = recordFetches();
  const response = await worker.fetch(
    new Request(`https://koalitionsberegner.moritzmarcus.com${USAGE_REPORTS_PATH}`, {
      method: 'POST',
      headers: { [REPORT_SECRET_HEADER]: REPORT_ENV.USAGE_REPORT_SECRET },
    }),
    REPORT_ENV
  );
  assert.equal(response.status, 404);
  assert.equal(calls.length, 0);
});
