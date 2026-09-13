/**
 * Sign-in against Firebase, over its REST endpoints.
 *
 * The Firebase JS SDK is not used: the page has no build step and loads its own
 * modules, and the calls needed here — sign up, sign in, refresh, and asking
 * Firebase to mail a confirmation or reset link — are plain JSON. That keeps
 * the page dependency-free and the whole flow testable with a fake `fetch`.
 *
 * What is kept where matters. The *refresh* token is long-lived and goes to
 * storage so a reload does not sign the user out. The *ID token* is short-lived
 * and stays in memory, refreshed shortly before it expires, because it is the
 * credential every API call carries.
 *
 * Nothing here decides what the user may do. The ID token is only evidence of
 * who they are; the backend reads their tier and quota from its own store.
 */

const IDENTITY_BASE = 'https://identitytoolkit.googleapis.com/v1/accounts';
const TOKEN_BASE = 'https://securetoken.googleapis.com/v1/token';

const STORAGE_KEY = 'koalitionsberegner.session';

/** Refresh this long before expiry, so a request never carries a dead token. */
const REFRESH_MARGIN_MS = 60_000;

/** How Firebase refuses a return address it has not been told to allow. */
const CONTINUE_URL_REFUSALS = ['UNAUTHORIZED_DOMAIN', 'INVALID_CONTINUE_URI'];

export class AuthError extends Error {
  constructor(message, code) {
    super(message);
    this.name = 'AuthError';
    this.code = code ?? null;
  }
}

/** Firebase's error codes are shouty constants; these are what a user should read. */
const MESSAGES = {
  EMAIL_EXISTS: 'Der findes allerede en konto med den e-mailadresse.',
  EMAIL_NOT_FOUND: 'Vi kunne ikke finde en konto med den e-mailadresse.',
  INVALID_PASSWORD: 'Forkert adgangskode.',
  INVALID_LOGIN_CREDENTIALS: 'Forkert e-mailadresse eller adgangskode.',
  INVALID_EMAIL: 'Adressen ser ikke ud til at være en e-mailadresse.',
  WEAK_PASSWORD: 'Adgangskoden skal være mindst 6 tegn.',
  USER_DISABLED: 'Kontoen er deaktiveret.',
  TOO_MANY_ATTEMPTS_TRY_LATER: 'For mange forsøg. Prøv igen om lidt.',
  RESET_PASSWORD_EXCEED_LIMIT: 'For mange forsøg. Prøv igen om lidt.',
  TOKEN_EXPIRED: 'Din session er udløbet. Log ind igen.',
  USER_NOT_FOUND: 'Din session er udløbet. Log ind igen.',
};

/** Firebase prefixes some codes with detail after a colon. */
export function readableAuthError(code) {
  const key = String(code ?? '').split(':')[0].trim().toUpperCase();
  if (MESSAGES[key]) return MESSAGES[key];
  if (key.startsWith('WEAK_PASSWORD')) return MESSAGES.WEAK_PASSWORD;
  return 'Log ind mislykkedes. Prøv igen.';
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
 * A signed-in session, or the absence of one.
 *
 * @param {{apiKey: string, projectId?: string, continueUrl?: string}} config the
 *   public Firebase config, plus where a mailed link should bring the user back to
 */
export function createAuth({
  apiKey,
  continueUrl,
  fetch: fetchImpl,
  storage,
  now = () => Date.now(),
} = {}) {
  const doFetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
  const store = storage ?? defaultStorage();
  const listeners = new Set();

  /** @type {{email: string|null, uid: string|null, refreshToken: string}|null} */
  let session = null;
  let idToken = null;
  let expiresAt = 0;
  /** One refresh at a time, however many callers want a token. */
  let refreshing = null;

  const enabled = Boolean(apiKey);

  function load() {
    try {
      const raw = store.getItem(STORAGE_KEY);
      const saved = raw ? JSON.parse(raw) : null;
      if (saved && typeof saved.refreshToken === 'string' && saved.refreshToken) {
        session = { email: saved.email ?? null, uid: saved.uid ?? null, refreshToken: saved.refreshToken };
      }
    } catch {
      session = null;
    }
  }

  function persist() {
    if (!session) {
      store.removeItem(STORAGE_KEY);
      return;
    }
    store.setItem(STORAGE_KEY, JSON.stringify(session));
  }

  function announce() {
    const state = user();
    for (const listener of listeners) listener(state);
  }

  function user() {
    return session ? { email: session.email, uid: session.uid } : null;
  }

  async function call(url, body) {
    if (!doFetch) throw new AuthError('no fetch implementation available');
    let response;
    try {
      response = await doFetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (cause) {
      throw new AuthError(`Kunne ikke nå login-tjenesten: ${cause.message}`);
    }
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const code = payload?.error?.message ?? String(response.status);
      throw new AuthError(readableAuthError(code), code);
    }
    return payload ?? {};
  }

  /** Adopt what an identitytoolkit sign-in/sign-up returned. */
  function adopt(payload) {
    session = {
      email: payload.email ?? null,
      uid: payload.localId ?? null,
      refreshToken: payload.refreshToken,
    };
    idToken = payload.idToken;
    expiresAt = now() + Number(payload.expiresIn ?? 3600) * 1000;
    persist();
    announce();
    return user();
  }

  async function refresh() {
    if (!session) return null;
    const payload = await call(`${TOKEN_BASE}?key=${encodeURIComponent(apiKey)}`, {
      grant_type: 'refresh_token',
      refresh_token: session.refreshToken,
    });
    idToken = payload.id_token;
    expiresAt = now() + Number(payload.expires_in ?? 3600) * 1000;
    session = {
      ...session,
      uid: payload.user_id ?? session.uid,
      refreshToken: payload.refresh_token ?? session.refreshToken,
    };
    persist();
    return idToken;
  }

  function forget() {
    session = null;
    idToken = null;
    expiresAt = 0;
    persist();
  }

  /**
   * The credential for the next API call, refreshed if it is about to expire.
   * `null` when signed out, which is a normal state, not an error.
   */
  async function getIdToken() {
    if (!session) return null;
    if (idToken && now() < expiresAt - REFRESH_MARGIN_MS) return idToken;
    // Collapse concurrent callers onto one refresh; several API calls start
    // together on page load and would otherwise each spend the refresh token.
    refreshing ??= refresh()
      .catch((error) => {
        // A refresh token that no longer works cannot be recovered from:
        // the session is over, and the page must show that rather than
        // retrying forever.
        forget();
        announce();
        throw error;
      })
      .finally(() => {
        refreshing = null;
      });
    return refreshing;
  }

  /**
   * Ask Firebase to mail a link. The link comes back to this page when its
   * domain is authorised in the Firebase project. When it is not, Firebase
   * refuses the whole request, so it is sent again without a return address and
   * the link ends on Firebase's own page instead — a mail that arrives beats one
   * that does not.
   */
  async function sendOobCode(body) {
    const url = `${IDENTITY_BASE}:sendOobCode?key=${encodeURIComponent(apiKey)}`;
    if (!continueUrl) return call(url, body);
    try {
      return await call(url, { ...body, continueUrl });
    } catch (error) {
      const code = String(error.code ?? '').split(':')[0].trim();
      if (!CONTINUE_URL_REFUSALS.includes(code)) throw error;
      return call(url, body);
    }
  }

  load();

  return {
    /** False when no Firebase project is configured; the page then stays signed out. */
    enabled,

    user,

    isSignedIn() {
      return session !== null;
    },

    /** Notifies on sign-in, sign-out, and an expiry we could not recover from. */
    onChange(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    async signUp(email, password) {
      return adopt(
        await call(`${IDENTITY_BASE}:signUp?key=${encodeURIComponent(apiKey)}`, {
          email,
          password,
          returnSecureToken: true,
        })
      );
    },

    async signIn(email, password) {
      return adopt(
        await call(`${IDENTITY_BASE}:signInWithPassword?key=${encodeURIComponent(apiKey)}`, {
          email,
          password,
          returnSecureToken: true,
        })
      );
    },

    signOut() {
      if (!session) return;
      forget();
      announce();
    },

    getIdToken,

    /**
     * A fresh ID token now, rather than when the current one expires. A token
     * states `email_verified` as it was when it was minted, so this is how the
     * server gets to see an address the user has just confirmed.
     */
    async reload() {
      if (!session) return null;
      idToken = null;
      expiresAt = 0;
      return getIdToken();
    },

    /** Mail the signed-in user the link that confirms their address. */
    async sendVerification() {
      const token = await getIdToken();
      if (!token) throw new AuthError('Log ind for at bekræfte din e-mailadresse.');
      await sendOobCode({ requestType: 'VERIFY_EMAIL', idToken: token });
    },

    /**
     * Mail a link for choosing a new password. Resolves the same whether or not
     * the address has an account: which addresses do is not the page's to reveal.
     */
    async sendPasswordReset(email) {
      try {
        await sendOobCode({ requestType: 'PASSWORD_RESET', email });
      } catch (error) {
        if (error instanceof AuthError && error.code === 'EMAIL_NOT_FOUND') return;
        throw error;
      }
    },
  };
}
