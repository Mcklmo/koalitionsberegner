/**
 * The password provider: the same contract as auth.js, over this app's own API.
 *
 * What is worth testing here is the part that differs from Firebase — an opaque
 * session with no refresh, an expiry the page respects on its own, and a sign-out
 * that the server is told about — plus the interchangeability itself, since the
 * account panel is written against one shape and handed either.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, createApiClient } from '../js/api.js';
import { AuthError, createAuth } from '../js/auth.js';
import { createPasswordAuth } from '../js/password-auth.js';

const HOUR = 3600_000;

function fakeStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

/** A stand-in for the API client: records calls, replies from a queue. */
function fakeApi(responses = {}) {
  const calls = [];
  const reply = (name) => async (body) => {
    calls.push({ name, body });
    const next = responses[name];
    if (next instanceof Error) throw next;
    return next ?? { token: 'sess-1', uid: 'uid-1', email: 'a@example.org', expiresAt: 3600 };
  };
  return {
    calls,
    register: reply('register'),
    login: reply('login'),
    logout: reply('logout'),
  };
}

function session({ api = fakeApi(), storage = fakeStorage(), now = () => 0 } = {}) {
  return { auth: createPasswordAuth({ api, storage, now }), api, storage };
}

test('signing in stores the session and yields its token', async () => {
  const { auth, api } = session();

  const user = await auth.signIn('a@example.org', 'hunter22');

  assert.deepEqual(user, { email: 'a@example.org', uid: 'uid-1' });
  assert.equal(auth.isSignedIn(), true);
  assert.equal(await auth.getIdToken(), 'sess-1');
  assert.deepEqual(api.calls, [
    { name: 'login', body: { email: 'a@example.org', password: 'hunter22' } },
  ]);
});

test('registering signs the new account in, the same as Firebase sign-up does', async () => {
  const { auth, api } = session();

  await auth.signUp('a@example.org', 'hunter22');

  assert.equal(api.calls[0].name, 'register');
  assert.equal(await auth.getIdToken(), 'sess-1');
});

test('the session survives a reload', async () => {
  const storage = fakeStorage();
  await session({ storage }).auth.signIn('a@example.org', 'hunter22');

  const restored = session({ storage }).auth;

  assert.equal(restored.isSignedIn(), true);
  assert.equal(await restored.getIdToken(), 'sess-1');
});

test('a token is used as it came: there is nothing to refresh', async () => {
  const { auth, api } = session();
  await auth.signIn('a@example.org', 'hunter22');

  await auth.getIdToken();
  await auth.getIdToken();

  assert.equal(api.calls.length, 1, 'the session is not renewed behind the page');
});

test('an expired session signs the page out rather than sending a dead token', async () => {
  const clock = { now: 0 };
  const { auth } = session({
    api: fakeApi({ login: { token: 'sess-1', uid: 'uid-1', email: 'a@example.org', expiresAt: 2 * 3600 } }),
    now: () => clock.now,
  });
  await auth.signIn('a@example.org', 'hunter22');
  const changes = [];
  auth.onChange((user) => changes.push(user));

  clock.now = 2 * HOUR - 30_000;   // inside the margin: as good as expired

  assert.equal(await auth.getIdToken(), null);
  assert.equal(auth.isSignedIn(), false);
  assert.deepEqual(changes, [null], 'the panel is told, so it stops claiming a session');
});

test('a stored session with no expiry is treated as over', async () => {
  const storage = fakeStorage({
    'koalitionsberegner.local-session': JSON.stringify({ token: 'sess-1', uid: 'uid-1' }),
  });

  const { auth } = session({ storage, now: () => 1 });

  assert.equal(await auth.getIdToken(), null);
});

test('signing out tells the server before forgetting the session', async () => {
  const { auth, api, storage } = session();
  await auth.signIn('a@example.org', 'hunter22');

  await auth.signOut();

  assert.equal(api.calls.at(-1).name, 'logout');
  assert.equal(auth.isSignedIn(), false);
  assert.equal(storage.values.has('koalitionsberegner.local-session'), false);
});

test('a server that cannot be reached still signs the page out', async () => {
  const { auth } = session({ api: fakeApi({ logout: new ApiError('offline', 0) }) });
  await auth.signIn('a@example.org', 'hunter22');

  await auth.signOut();

  assert.equal(auth.isSignedIn(), false);
});

test('a taken address and a wrong password read as themselves, in Danish', async () => {
  const taken = session({ api: fakeApi({ register: new ApiError('there is already an account', 409) }) });
  const wrong = session({ api: fakeApi({ login: new ApiError('wrong email address or password', 401) }) });

  await assert.rejects(taken.auth.signUp('a@example.org', 'hunter22'), (error) => {
    assert.ok(error instanceof AuthError);
    assert.equal(error.code, 'EMAIL_EXISTS');
    assert.match(error.message, /allerede en konto/);
    return true;
  });
  await assert.rejects(wrong.auth.signIn('a@example.org', 'nope'), (error) => {
    assert.equal(error.code, 'INVALID_LOGIN_CREDENTIALS');
    assert.match(error.message, /Forkert e-mailadresse eller adgangskode/);
    return true;
  });
});

test('the server\'s own rules are reported as the field they are about', async () => {
  const weak = session({ api: fakeApi({ register: new ApiError('the password must be at least 6 characters', 422) }) });
  const bad = session({ api: fakeApi({ register: new ApiError('that does not look like an email address', 422) }) });

  await assert.rejects(weak.auth.signUp('a@example.org', 'short'), { code: 'WEAK_PASSWORD' });
  await assert.rejects(bad.auth.signUp('not-an-address', 'hunter22'), { code: 'INVALID_EMAIL' });
});

test('a failure that is not a refusal says the service could not be reached', async () => {
  const { auth } = session({ api: fakeApi({ login: new ApiError('could not reach the election store', 0) }) });

  await assert.rejects(auth.signIn('a@example.org', 'hunter22'), /Kunne ikke nå login-tjenesten/);
});

test('it is interchangeable with the Firebase provider', () => {
  const firebase = createAuth({ apiKey: 'AIza-test', storage: fakeStorage() });
  const local = createPasswordAuth({ api: fakeApi(), storage: fakeStorage() });

  // Mailing links is Firebase's alone; the panel checks for these before
  // offering them, so they are the one permitted difference.
  const MAIL_ONLY = new Set(['reload', 'sendPasswordReset', 'sendVerification']);
  const surface = (auth) => Object.keys(auth).filter((key) => !MAIL_ONLY.has(key)).sort();
  assert.deepEqual(surface(local), surface(firebase),
    'the account panel is written against one shape and handed either');
});

test('the token it produces is what the API client sends', async () => {
  const { auth } = session();
  await auth.signIn('a@example.org', 'hunter22');
  const seen = [];
  const api = createApiClient({
    fetch: async (url, options) => {
      seen.push(options.headers.authorization);
      return { ok: true, status: 200, json: async () => [] };
    },
    getToken: () => auth.getIdToken(),
  });

  await api.listElections();

  assert.deepEqual(seen, ['Bearer sess-1']);
});
