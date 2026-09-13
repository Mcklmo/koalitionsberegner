import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import worker, { ORIGIN_SECRET_HEADER } from '../worker/index.js';

const ENV = {
  ORIGIN_URL: 'https://koalitionsberegner-origin.example.run.app',
  ORIGIN_SECRET: 's'.repeat(64),
};

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Replace fetch with a recorder that answers 200. */
function recordFetches() {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response('{}', { status: 200 });
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
  assert.equal(headers.get('authorization'), 'Bearer token', 'the Firebase token still rides along');
  assert.equal(headers.get('host'), null, 'the origin routes by its own hostname');
  assert.equal(calls[0].init.redirect, 'manual');
  assert.equal(calls[0].init.body, undefined);
});

test('a webhook body is passed on byte for byte', async () => {
  const calls = recordFetches();
  const payload = '{"id":"evt_1","type":"customer.subscription.updated"}';
  const request = new Request('https://koalitionsberegner.moritzmarcus.com/api/billing/webhook', {
    method: 'POST',
    headers: { 'stripe-signature': 't=1,v1=abc', 'content-type': 'application/json' },
    body: payload,
  });

  await worker.fetch(request, ENV);

  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.get('stripe-signature'), 't=1,v1=abc');
  const forwarded = await new Response(calls[0].init.body).text();
  assert.equal(forwarded, payload, 'the signature covers these exact bytes');
});

test('a client cannot supply its own secret header to learn anything', async () => {
  const calls = recordFetches();
  const request = new Request('https://koalitionsberegner.moritzmarcus.com/api/me', {
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
