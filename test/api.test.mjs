import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, createApiClient, toElection } from '../js/api.js';
import { isValidatedElection } from '../js/election.js';

const payload = {
  nation: 'Danmark',
  state: null,
  election_date: '2026-03-25',
  title: 'Folketing 2026',
  source_url: 'https://www.dst.dk/valg',
  total_seats: 10,
  majority_seats: 6,
  blocks: [
    { name: 'Left', parties: [{ name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' }] },
    { name: 'Right', parties: [{ name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' }] },
  ],
};

/** Records requests and replies with queued responses. */
function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method ?? 'GET', body: options.body });
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
    { electionHash: 'abc', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179 },
  ]);
});

test('lookup sends the metadata and omits an absent state', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { election_hash: 'abc', state: 'unknown', election: null } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).lookup({
    nation: 'Danmark',
    state: null,
    electionDate: '2026-03-25',
  });
  assert.match(calls[0].url, /^\/api\/elections\/lookup\?/);
  assert.ok(!calls[0].url.includes('state='), 'an absent state is not sent');
  assert.equal(result.status, 'unknown');
  assert.equal(result.election, null);
});

test('import posts snake_case and returns a validated preview', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { election_hash: 'abc', state: 'preview', election: payload, reused: false } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).importElection({
    nation: 'Danmark',
    state: null,
    electionDate: '2026-03-25',
    sourceUrl: 'https://www.dst.dk/valg',
  });

  assert.equal(calls[0].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].body), {
    nation: 'Danmark',
    state: null,
    election_date: '2026-03-25',
    source_url: 'https://www.dst.dk/valg',
  });
  assert.equal(result.status, 'preview');
  assert.ok(isValidatedElection(result.election));
});

test('confirm posts to the confirm endpoint', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { election_hash: 'abc', state: 'ready', election: payload } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).confirm('abc');
  assert.equal(calls[0].url, '/api/elections/abc/confirm');
  assert.equal(calls[0].method, 'POST');
  assert.equal(result.status, 'ready');
});

test('discarding a preview tolerates a 204 with no body', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 204, body: null }]);
  await createApiClient({ fetch: fetchImpl }).discardPreview('abc');
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
