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
    { electionHash: 'abc', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179, forecast: null },
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


// --- carrying the owner's secret ------------------------------------------

test('the owner sends their secret with every request', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }]);
  const api = createApiClient({ fetch: fetchImpl, getAdminSecret: () => 's3cret' });

  await api.listElections();

  assert.equal(calls[0].headers['x-admin-secret'], 's3cret');
  assert.equal(calls[0].headers['content-type'], 'application/json');
  assert.equal(calls[0].headers.authorization, undefined, 'nobody signs in');
});

test('everyone else sends no secret and is still served', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }, { status: 200, body: [] }]);

  await createApiClient({ fetch: fetchImpl }).listElections();
  await createApiClient({ fetch: fetchImpl, getAdminSecret: () => null }).listElections();

  assert.deepEqual(calls.map((c) => c.headers['x-admin-secret']), [undefined, undefined]);
});

test('the secret is read per request, so forgetting it takes effect at once', async () => {
  const secrets = ['first', null];
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: [] }, { status: 200, body: [] }]);
  const api = createApiClient({ fetch: fetchImpl, getAdminSecret: () => secrets.shift() });

  await api.listElections();
  await api.listElections();

  assert.deepEqual(calls.map((c) => c.headers['x-admin-secret']), ['first', undefined]);
});

// --- the config endpoint ----------------------------------------------------

test('the public config is mapped to camelCase', async () => {
  const { fetchImpl } = fakeFetch([
    { status: 200, body: { requests_enabled: true, imports_enabled: true, imports_open: false } },
  ]);

  const config = await createApiClient({ fetch: fetchImpl }).getConfig();

  assert.deepEqual(config, { requestsEnabled: true, importsEnabled: true, importsOpen: false });
});

test('a refused import surfaces the status the page needs to explain it', async () => {
  const { fetchImpl } = fakeFetch([{ status: 403, body: { detail: "this needs the administrator's secret" } }]);

  await assert.rejects(
    () => createApiClient({ fetch: fetchImpl }).importElection({ year: 2026, nation: 'Danmark' }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 403);
      return true;
    }
  );
});
