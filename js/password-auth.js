/**
 * Sign-in against this app's own backend, for a deployment with no Firebase.
 *
 * The same shape as auth.js — `signUp`, `signIn`, `signOut`, `getIdToken` — so
 * the account panel and the API client cannot tell which one they were handed.
 * `/api/config` says which to build; nothing else in the page knows.
 *
 * The differences from the Firebase flow are all in what a token *is*. This one
 * is an opaque session the server issued and can revoke, so there is no refresh
 * token and nothing to renew: the session is stored as it came, used until the
 * expiry the server stated, and then the user signs in again. Signing out tells
 * the server, because here the server is the thing that remembers.
 *
 * Nothing here decides what the user may do. The token is only evidence of who
 * they are; the backend reads their tier and quota from its own store.
 */

import { AuthError, readableAuthError } from './auth.js';
import { ApiError } from './api.js';

const STORAGE_KEY = 'koalitionsberegner.local-session';

/** Treat a session as over slightly early, so a request never carries a dead token. */
const EXPIRY_MARGIN_MS = 60_000;

/** What the backend's refusals mean, in the vocabulary auth.js already speaks. */
function codeFor(error) {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409) return 'EMAIL_EXISTS';
  if (error.status === 401) return 'INVALID_LOGIN_CREDENTIALS';
  if (error.status === 422) {
    return /e-?mail|address/i.test(error.message) ? 'INVALID_EMAIL' : 'WEAK_PASSWORD';
  }
  return null;
}

/** localStorage is absent in some embeddings; the session then lasts one page view. */
function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

function defaultStorage() {
  try {
    const probe = '__probe__';
    globalThis.localStorage.setItem(probe, '1');
    globalThis.localStorage.removeItem(probe);
    return globalThis.localStorage;
  } catch {
    return memoryStorage();
  }
}

/**
 * A signed-in session against the app's own `/api/auth/*` endpoints.
 *
 * @param {{api: object, storage?: object, now?: () => number}} options
 */
export function createPasswordAuth({ api, storage, now = () => Date.now() } = {}) {
  const store = storage ?? defaultStorage();
  const listeners = new Set();

  /** @type {{token: string, uid: string|null, email: string|null, expiresAt: number}|null} */
  let session = null;

  function load() {
    try {
      const saved = JSON.parse(store.getItem(STORAGE_KEY) ?? 'null');
      if (saved && typeof saved.token === 'string' && saved.token) {
        session = {
          token: saved.token,
          uid: saved.uid ?? null,
          email: saved.email ?? null,
          // A stored session with no expiry is one we cannot reason about;
          // treating it as already over is the safe reading.
          expiresAt: Number(saved.expiresAt) || 0,
        };
      }
    } catch {
      session = null;
    }
  }

  function persist() {
    if (!session) store.removeItem(STORAGE_KEY);
    else store.setItem(STORAGE_KEY, JSON.stringify(session));
  }

  function user() {
    return session ? { email: session.email, uid: session.uid } : null;
  }

  function announce() {
    const state = user();
    for (const listener of listeners) listener(state);
  }

  function forget() {
    session = null;
    persist();
  }

  /** Adopt what `/api/auth/register` or `/api/auth/login` returned. */
  function adopt(payload) {
    session = {
      token: payload.token,
      uid: payload.uid ?? null,
      email: payload.email ?? null,
      // The server states the expiry in seconds since the epoch.
      expiresAt: Number(payload.expiresAt ?? 0) * 1000,
    };
    persist();
    announce();
    return user();
  }

  async function open(call) {
    try {
      return adopt(await call());
    } catch (error) {
      const code = codeFor(error);
      if (code) throw new AuthError(readableAuthError(code), code);
      throw new AuthError(`Kunne ikke nå login-tjenesten: ${error.message}`);
    }
  }

  load();

  return {
    /** Always: the endpoints are part of this backend, not a service to configure. */
    enabled: true,

    user,

    isSignedIn() {
      return session !== null;
    },

    /** Notifies on sign-in, sign-out, and a session that has run out. */
    onChange(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    async signUp(email, password) {
      return open(() => api.register({ email, password }));
    },

    async signIn(email, password) {
      return open(() => api.login({ email, password }));
    },

    signOut() {
      if (!session) return;
      // Before forgetting it locally: the request carries the token, and the
      // server is what makes it stop working for anyone else holding a copy.
      const ending = api.logout().catch(() => {});
      forget();
      announce();
      return ending;
    },

    /**
     * The credential for the next API call, or `null` when signed out — which
     * is a normal state, not an error. An expired session ends here rather
     * than on the server's 401, so the panel stops claiming to be signed in.
     */
    async getIdToken() {
      if (!session) return null;
      // A session with no stated expiry reads as zero, which is in the past:
      // a credential we cannot reason about is not one to keep sending.
      if (now() >= session.expiresAt - EXPIRY_MARGIN_MS) {
        forget();
        announce();
        return null;
      }
      return session.token;
    },
  };
}
