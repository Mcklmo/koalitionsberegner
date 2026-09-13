import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, createApiClient, toElection } from '../js/api.js';
import { isValidatedElection } from '../js/election.js';

const payload = {
  nation: 'Danmark',
  state: null,
  election_date: '2026-03-25',
  title: 'Denmark — 2026',
  source_url: 'https://www.dst.dk/valg',
  total_seats: 10,
  majority_seats: 6,
  blocks: [
    { name: 'Left', parties: [{ name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' }] },
    { name: 'Right', parties: [{ name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' }] },
  ],
};

test('maps a party\'s local name onto the schema, and its absence onto null', () => {
  const bilingual = structuredClone(payload);
  bilingual.blocks[0].parties[0].local_name = 'Venstrepartiet';
  const [left] = toElection(bilingual).blocks[0].parties;
  assert.equal(left.localName, 'Venstrepartiet');
  assert.equal(toElection(payload).blocks[0].parties[0].localName, null);
  assert.throws(() => toElection({ ...bilingual, blocks: [{ ...bilingual.blocks[0], parties: [{ ...bilingual.blocks[0].parties[0], local_name: '' }] }] }),
    /localName: must not be empty/);
});

/** Records requests and replies with queued responses. */
function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method ?? 'GET', body: options.body, headers: options.headers ?? {} });
    const next = queue.shift();
    if (!next) throw new Error(`unexpected request to ${url}`);
    return {
      ok: next.status < 400,
      status: next.status,
      statusText: 'x',
      json: async () => next.body,
    };
  };
  return { fetchImpl, calls };
}

test('maps the API payload onto the canonical schema', () => {
  const election = toElection(payload);
  assert.ok(isValidatedElection(election), 'the API is not trusted; output is validated');
  assert.equal(election.electionDate, '2026-03-25');
  assert.equal(election.sourceUrl, 'https://www.dst.dk/valg');
  assert.equal(election.totalSeats, 10);
  assert.equal(election.majoritySeats, 6);
});

test('rejects an election the API should never have sent', () => {
  assert.throws(() => toElection({ ...payload, total_seats: 11 }), /seats sum to 10/);
  assert.throws(() => toElection({ ...payload, source_url: 'javascript:alert(1)' }), /http or https/);
  assert.throws(() => toElection(null), ApiError);
});

test('listElections maps summaries to camelCase', async () => {
  const { fetchImpl } = fakeFetch([
    {
      status: 200,
      body: [
        { election_hash: 'abc', nation: 'Danmark', state: null, election_date: '2026-03-25', title: 'T', total_seats: 179 },
      ],
    },
  ]);
  const listed = await createApiClient({ fetch: fetchImpl }).listElections();
  assert.deepEqual(listed, [
    { electionHash: 'abc', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179, selected: false, forecast: null },
  ]);
});

test('lookup asks about the year and the place', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'unknown', election: null } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).lookup({
    year: 2026, nation: 'Danmark', subnation: null,
  });
  assert.match(calls[0].url, /^\/api\/elections\/lookup\?year=2026&nation=Danmark$/);
  assert.equal(result.status, 'unknown');
  assert.equal(result.election, null);
  assert.equal(result.electionHash, null);
});

test('import sends the year and the place, and returns a validated preview', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', election_hash: 'abc', state: 'preview', election: payload, reused: false } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).importElection({
    year: 2026, nation: 'Danmark',
  });

  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].body), { year: 2026, nation: 'Danmark', subnation: null },
    'identity is the agent\'s to infer, so none is sent');
  assert.equal(result.status, 'preview');
  assert.equal(result.requestKey, 'r1');
  assert.equal(result.electionHash, 'abc');
  assert.ok(isValidatedElection(result.election));
});

test('an import under way is polled without starting another', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', election_hash: 'abc', state: 'preview', election: payload, reused: false } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).getImport('r1');

  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[0].url, '/api/elections/imports/r1?wait_seconds=25');
  assert.equal(result.status, 'preview');
  assert.ok(isValidatedElection(result.election));
});

test('confirm posts to the import it previewed', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', election_hash: 'abc', state: 'ready', election: payload, duplicate: false } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).confirm('r1');
  assert.equal(calls[0].url, '/api/elections/imports/r1/confirm');
  assert.equal(calls[0].method, 'POST');
  assert.equal(result.status, 'ready');
  assert.equal(result.duplicate, false);
});

test('discarding a preview tolerates a 204 with no body', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 204, body: null }]);
  await createApiClient({ fetch: fetchImpl }).discardPreview('r1');
  assert.equal(calls[0].url, '/api/elections/imports/r1/preview');
  assert.equal(calls[0].method, 'DELETE');
});

test('an error response becomes an ApiError carrying the detail', async () => {
  const { fetchImpl } = fakeFetch([{ status: 409, body: { detail: 'nothing awaiting confirmation' } }]);
  await assert.rejects(() => createApiClient({ fetch: fetchImpl }).confirm('abc'), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.match(error.message, /nothing awaiting/);
    return true;
  });
});

test('an unreachable backend becomes a clear ApiError', async () => {
  const fetchImpl = async () => {
    throw new Error('connection refused');
  };
  await assert.rejects(() => createApiClient({ fetch: fetchImpl }).listElections(), (error) => {
    assert.equal(error.status, 0);
    assert.match(error.message, /could not reach the election store/);
    return true;
  });
});

test('hashes are escaped into the path', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 404, body: { detail: 'no' } }]);
  await createApiClient({ fetch: fetchImpl }).getElection('../admin').catch(() => {});
  assert.equal(calls[0].url, '/api/elections/..%2Fadmin');
});


// --- carrying the caller's identity ----------------------------------------

test('a signed-in caller sends their ID token', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }]);
  const api = createApiClient({ fetch: fetchImpl, getToken: async () => 'id-token-1' });

  await api.listElections();

  assert.equal(calls[0].headers.authorization, 'Bearer id-token-1');
  assert.equal(calls[0].headers['content-type'], 'application/json');
});

test('a signed-out caller sends no credential and is still served', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }]);
  const api = createApiClient({ fetch: fetchImpl, getToken: async () => null });

  await api.listElections();

  assert.equal(calls[0].headers.authorization, undefined, 'a visitor is a legitimate caller');
});

test('a session that cannot produce a token falls back to the visitor view', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }]);
  const api = createApiClient({
    fetch: fetchImpl,
    getToken: async () => {
      throw new Error('refresh token rejected');
    },
  });

  await api.listElections();

  assert.equal(calls[0].headers.authorization, undefined);
});

test('the token is read per request, not captured once', async () => {
  const tokens = ['first', 'second'];
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }, { status: 200, body: [] }]);
  const api = createApiClient({ fetch: fetchImpl, getToken: async () => tokens.shift() });

  await api.listElections();
  await api.listElections();

  assert.deepEqual(calls.map((c) => c.headers.authorization), ['Bearer first', 'Bearer second']);
});

// --- the account and config endpoints --------------------------------------

test('the public config is mapped to camelCase', async () => {
  const { fetchImpl } = fakeFetch([
    {
      status: 200,
      body: {
        auth_required: true,
        firebase: { apiKey: 'AIza', projectId: 'demo' },
        billing_enabled: true,
        tiers: [{ tier: 'basic', monthly_imports: 10, purchasable: true }],
      },
    },
  ]);

  const config = await createApiClient({ fetch: fetchImpl }).getConfig();

  assert.equal(config.authRequired, true);
  assert.equal(config.firebase.apiKey, 'AIza');
  assert.deepEqual(config.tiers, [{ tier: 'basic', monthlyImports: 10, purchasable: true }]);
});

test('the account reports the tier and what is left', async () => {
  const { fetchImpl, calls } = fakeFetch([
    {
      status: 200,
      body: {
        uid: 'u1', email: 'a@example.org', tier: 'basic', admin: false, period: '2026-09',
        used: 3, limit: 10, remaining: 7, may_import: true,
        subscription_status: 'active', billing_enabled: true,
      },
    },
  ]);

  const account = await createApiClient({ fetch: fetchImpl }).getAccount();

  assert.equal(calls[0].url, '/api/me');
  assert.equal(account.tier, 'basic');
  assert.equal(account.remaining, 7);
  assert.equal(account.mayImport, true);
  assert.equal(account.unlimited, false);
  assert.equal(account.subscriptionStatus, 'active');
});

test('a negative limit reads as no tier at all', async () => {
  const { fetchImpl } = fakeFetch([
    {
      status: 200,
      body: { uid: 'local', email: null, tier: 'free', admin: true, period: '2026-09', used: 0, limit: -1, remaining: -1, may_import: true },
    },
  ]);

  const account = await createApiClient({ fetch: fetchImpl }).getAccount();

  assert.equal(account.unlimited, true);
  assert.equal(account.mayImport, true);
});

test('curation is a PUT carrying the new flag', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { election_hash: 'abc', nation: 'Danmark', state: null, election_date: '2026-03-25', title: 'T', total_seats: 179, selected: true } },
  ]);

  const summary = await createApiClient({ fetch: fetchImpl }).setSelected('abc', true);

  assert.equal(calls[0].method, 'PUT');
  assert.equal(calls[0].url, '/api/elections/abc/selected');
  assert.deepEqual(JSON.parse(calls[0].body), { selected: true });
  assert.equal(summary.selected, true);
});

test('starting a checkout returns the URL to send the user to', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: { url: 'https://checkout.test/x' } }]);

  const url = await createApiClient({ fetch: fetchImpl }).startCheckout('premium');

  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, '/api/billing/checkout');
  assert.deepEqual(JSON.parse(calls[0].body), { tier: 'premium' });
  assert.equal(url, 'https://checkout.test/x');
});

test('a refused import surfaces the status the page needs to explain it', async () => {
  const { fetchImpl } = fakeFetch([{ status: 402, body: { detail: 'subscribe to import new ones' } }]);

  await assert.rejects(
    () => createApiClient({ fetch: fetchImpl }).importElection({ year: 2026, nation: 'Danmark' }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 402);
      return true;
    }
  );
});
