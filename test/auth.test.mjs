import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthError, createAuth, readableAuthError } from '../js/auth.js';

const API_KEY = 'AIza-test';

/** A storage double, so a "reload" is just building a session over the same map. */
function fakeStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

/** Records requests and replies with queued responses. */
function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, body: JSON.parse(options.body ?? '{}') });
    const next = queue.shift();
    if (!next) throw new Error(`unexpected request to ${url}`);
    return { ok: next.status < 400, status: next.status, json: async () => next.body };
  };
  return { fetchImpl, calls };
}

const signedIn = {
  status: 200,
  body: {
    idToken: 'id-1',
    refreshToken: 'refresh-1',
    localId: 'uid-1',
    email: 'a@example.org',
    expiresIn: '3600',
  },
};

function auth(responses, { storage = fakeStorage(), now = () => 0 } = {}) {
  const { fetchImpl, calls } = fakeFetch(responses);
  return { session: createAuth({ apiKey: API_KEY, fetch: fetchImpl, storage, now }), calls, storage };
}

test('signing in stores the session and yields an ID token', async () => {
  const { session, calls } = auth([signedIn]);

  const user = await session.signIn('a@example.org', 'hunter22');

  assert.deepEqual(user, { email: 'a@example.org', uid: 'uid-1' });
  assert.equal(session.isSignedIn(), true);
  assert.equal(await session.getIdToken(), 'id-1');
  assert.match(calls[0].url, /accounts:signInWithPassword\?key=AIza-test$/);
  assert.equal(calls[0].body.returnSecureToken, true);
  assert.equal(calls.length, 1, 'a fresh token is not refreshed');
});

test('signing up uses the sign-up endpoint', async () => {
  const { session, calls } = auth([signedIn]);
  await session.signUp('a@example.org', 'hunter22');
  assert.match(calls[0].url, /accounts:signUp\?key=/);
});

test('only the refresh token is persisted, never the ID token', async () => {
  const { session, storage } = auth([signedIn]);

  await session.signIn('a@example.org', 'hunter22');

  const saved = JSON.parse(storage.values.get('koalitionsberegner.session'));
  assert.equal(saved.refreshToken, 'refresh-1');
  assert.equal(saved.idToken, undefined, 'the short-lived credential stays in memory');
});

test('a session survives a reload by exchanging the stored refresh token', async () => {
  const storage = fakeStorage();
  const first = auth([signedIn], { storage });
  await first.session.signIn('a@example.org', 'hunter22');

  const reloaded = auth(
    [{ status: 200, body: { id_token: 'id-2', refresh_token: 'refresh-2', expires_in: '3600', user_id: 'uid-1' } }],
    { storage }
  );

  assert.equal(reloaded.session.isSignedIn(), true, 'the page reopens signed in');
  assert.equal(await reloaded.session.getIdToken(), 'id-2');
  assert.match(reloaded.calls[0].url, /securetoken\.googleapis\.com/);
  assert.equal(reloaded.calls[0].body.grant_type, 'refresh_token');
});

test('an ID token about to expire is refreshed before it is handed out', async () => {
  const clock = { now: 0 };
  const { session, calls } = auth(
    [signedIn, { status: 200, body: { id_token: 'id-2', expires_in: '3600' } }],
    { now: () => clock.now }
  );
  await session.signIn('a@example.org', 'hunter22');

  clock.now = 3_550_000; // inside the refresh margin before the 3600s expiry
  assert.equal(await session.getIdToken(), 'id-2');
  assert.equal(calls.length, 2);
});

test('concurrent callers share one refresh rather than spending the token twice', async () => {
  const clock = { now: 0 };
  const { session, calls } = auth(
    [signedIn, { status: 200, body: { id_token: 'id-2', expires_in: '3600' } }],
    { now: () => clock.now }
  );
  await session.signIn('a@example.org', 'hunter22');
  clock.now = 3_550_000;

  const tokens = await Promise.all([session.getIdToken(), session.getIdToken(), session.getIdToken()]);

  assert.deepEqual(tokens, ['id-2', 'id-2', 'id-2']);
  assert.equal(calls.length, 2, 'one sign-in and one refresh, however many callers');
});

test('a refresh token the server rejects ends the session', async () => {
  const storage = fakeStorage({
    'koalitionsberegner.session': JSON.stringify({ refreshToken: 'stale', email: 'a@example.org' }),
  });
  const { session } = auth([{ status: 400, body: { error: { message: 'TOKEN_EXPIRED' } } }], { storage });
  const seen = [];
  session.onChange((user) => seen.push(user));

  await assert.rejects(() => session.getIdToken(), AuthError);

  assert.equal(session.isSignedIn(), false, 'a session that cannot be renewed is over');
  assert.deepEqual(seen, [null], 'the page is told, so it can show the form again');
  assert.equal(storage.values.has('koalitionsberegner.session'), false);
});

test('signing out forgets the session and announces it', async () => {
  const { session, storage } = auth([signedIn]);
  await session.signIn('a@example.org', 'hunter22');
  const seen = [];
  session.onChange((user) => seen.push(user));

  session.signOut();

  assert.equal(session.isSignedIn(), false);
  assert.equal(await session.getIdToken(), null);
  assert.deepEqual(seen, [null]);
  assert.equal(storage.values.has('koalitionsberegner.session'), false);
});

test('signing out twice announces once', async () => {
  const { session } = auth([signedIn]);
  await session.signIn('a@example.org', 'hunter22');
  const seen = [];
  session.onChange((user) => seen.push(user));

  session.signOut();
  session.signOut();

  assert.equal(seen.length, 1);
});

test('a signed-out page has no token to offer and says so without failing', async () => {
  const { session } = auth([]);
  assert.equal(session.isSignedIn(), false);
  assert.equal(await session.getIdToken(), null);
});

test('a rejected sign-in becomes a message a user can act on', async () => {
  const { session } = auth([{ status: 400, body: { error: { message: 'INVALID_LOGIN_CREDENTIALS' } } }]);

  await assert.rejects(() => session.signIn('a@example.org', 'wrong'), (error) => {
    assert.ok(error instanceof AuthError);
    assert.equal(error.code, 'INVALID_LOGIN_CREDENTIALS');
    assert.match(error.message, /Forkert e-mailadresse eller adgangskode/);
    return true;
  });
  assert.equal(session.isSignedIn(), false);
});

test('an unreachable identity service is reported as such', async () => {
  const failing = createAuth({
    apiKey: API_KEY,
    storage: fakeStorage(),
    fetch: async () => {
      throw new Error('connection refused');
    },
  });
  await assert.rejects(() => failing.signIn('a@example.org', 'hunter22'), /Kunne ikke nå login-tjenesten/);
});

test('without an API key the page simply stays signed out', async () => {
  const disabled = createAuth({ apiKey: '', storage: fakeStorage() });
  assert.equal(disabled.enabled, false);
  assert.equal(disabled.isSignedIn(), false);
  assert.equal(await disabled.getIdToken(), null);
});

test('a corrupt stored session is discarded rather than thrown', () => {
  const storage = fakeStorage({ 'koalitionsberegner.session': 'not json' });
  assert.equal(createAuth({ apiKey: API_KEY, storage }).isSignedIn(), false);
});

test('Firebase error codes become Danish sentences', () => {
  assert.match(readableAuthError('EMAIL_EXISTS'), /findes allerede/);
  assert.match(readableAuthError('WEAK_PASSWORD : Password should be at least 6'), /mindst 6 tegn/);
  assert.match(readableAuthError('SOMETHING_NEW'), /Log ind mislykkedes/);
  assert.match(readableAuthError(undefined), /Log ind mislykkedes/);
});
