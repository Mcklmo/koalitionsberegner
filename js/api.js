/**
 * Client for the election store API.
 *
 * The backend speaks snake_case; the renderer's canonical schema (election.js)
 * speaks camelCase. That translation lives here and nowhere else, and every
 * election crossing the boundary is validated before anything else touches it.
 *
 * Nobody signs in. Every request goes out as it is, except that the owner's
 * secret rides along in `x-admin-secret` once the page's admin mode holds one
 * (js/admin.js). What a request is *allowed* to do is decided by the backend
 * alone; nothing in this file gates anything.
 */

import { validateElection } from './election.js';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Import states reported by the backend. */
export const ImportStatus = {
  READY: 'ready',
  PENDING: 'pending',
  PREVIEW: 'preview',
  // Not held yet: `forecasts` lists its polls, and one is confirmed by option.
  CHOOSE: 'choose',
  FAILED: 'failed',
  UNKNOWN: 'unknown',
};

/** Maps a stored/previewed election payload onto the canonical schema. */
export function toElection(payload) {
  if (payload === null || typeof payload !== 'object') {
    throw new ApiError('the API returned no election');
  }
  return validateElection({
    nation: payload.nation,
    state: payload.state,
    electionDate: payload.election_date,
    title: payload.title,
    sourceUrl: payload.source_url,
    totalSeats: payload.total_seats,
    majoritySeats: payload.majority_seats,
    blocks: toBlocks(payload.blocks),
    forecast: toForecast(payload.forecast),
  });
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Blocks' wire shape, where only a party's local name is spelled differently. */
function toBlocks(blocks) {
  if (!Array.isArray(blocks)) return blocks;
  return blocks.map((block) => (isObject(block) && Array.isArray(block.parties)
    ? { ...block, parties: block.parties.map(toParty) }
    : block));
}

function toParty(party) {
  if (!isObject(party) || !('local_name' in party)) return party;
  const { local_name: localName, ...rest } = party;
  return { ...rest, localName };
}

/** A forecast's wire shape; left for the validator to judge. */
function toForecast(forecast) {
  if (forecast === null || forecast === undefined) return null;
  if (typeof forecast !== 'object') return forecast;
  return {
    publisher: forecast.publisher,
    publishedOn: forecast.published_on,
    computed: forecast.computed,
  };
}

function toSummary(row) {
  return {
    electionHash: row.election_hash,
    nation: row.nation,
    state: row.state ?? null,
    electionDate: row.election_date,
    title: row.title,
    totalSeats: row.total_seats,
    forecast: toForecast(row.forecast),
  };
}

/** The caller's tier and what is left of this month's import allowance. */
function toAccount(body) {
  return {
    uid: body.uid,
    email: body.email ?? null,
    // Only an explicit false: a server that predates confirmation never asked
    // for one, and its accounts are not waiting on anything.
    emailVerified: body.email_verified !== false,
    tier: body.tier,
    admin: Boolean(body.admin),
    period: body.period,
    used: body.used,
    limit: body.limit,
    remaining: body.remaining,
    // A negative limit means "not on a tier at all", which is what a local run
    // with gating switched off reports.
    unlimited: body.limit < 0,
    mayImport: Boolean(body.may_import),
    subscriptionStatus: body.subscription_status ?? null,
    billingEnabled: Boolean(body.billing_enabled),
  };
}

function toConfig(body) {
  return {
    // Whether anyone may ask for an election to be imported later.
    requestsEnabled: Boolean(body.requests_enabled),
    // Whether this deployment imports at all; the owner still needs the secret.
    importsEnabled: Boolean(body.imports_enabled),
    // Whether importing needs no secret here: a local run with none configured.
    importsOpen: Boolean(body.imports_open),
  };
}

/** A session issued by this backend, when it is the one holding the passwords. */
function toSession(body) {
  return {
    token: body.token,
    uid: body.uid,
    email: body.email ?? null,
    expiresAt: body.expires_at,
  };
}

/** Where an election request was written down. */
function toFiledRequest(body) {
  return {
    url: body.url,
    number: body.number,
    // True when somebody had already asked for this election: the existing
    // issue is handed back rather than a second one opened.
    duplicate: Boolean(body.duplicate),
  };
}

/** `state` on the wire is the import state; renamed so it cannot be confused
 *  with an election's `state` (its region). */
function toResult(body) {
  return {
    requestKey: body.request_key,
    electionHash: body.election_hash ?? null,
    status: body.status ?? body.state,
    election: body.election ? toElection(body.election) : null,
    error: body.error ?? null,
    reused: Boolean(body.reused),
    duplicate: Boolean(body.duplicate),
    // Every forecast is an election like any other, and validated like one.
    forecasts: (body.forecasts ?? []).map(toElection),
  };
}

/** The header the owner's secret travels in; the backend's `ADMIN_SECRET_HEADER`. */
export const ADMIN_SECRET_HEADER = 'x-admin-secret';

/**
 * @param {{baseUrl?: string, fetch?: Function, getAdminSecret?: () => string|null}} options
 *   `getAdminSecret` supplies the owner's secret, or null for everyone else.
 */
export function createApiClient({ baseUrl = '', fetch: fetchImpl, getAdminSecret } = {}) {
  const doFetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
  if (!doFetch) throw new TypeError('no fetch implementation available');

  /** Read per request, so a secret saved or forgotten later takes effect at once. */
  function adminHeaders() {
    const secret = getAdminSecret?.();
    return secret ? { [ADMIN_SECRET_HEADER]: secret } : {};
  }

  async function request(path, options = {}) {
    let response;
    try {
      response = await doFetch(baseUrl + path, {
        ...options,
        headers: { 'content-type': 'application/json', ...adminHeaders(), ...options.headers },
      });
    } catch (cause) {
      throw new ApiError(`could not reach the election store: ${cause.message}`, 0);
    }
    if (response.status === 204) return null;
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = body && body.detail ? body.detail : response.statusText;
      throw new ApiError(String(detail), response.status);
    }
    return body;
  }

  const query = (params) =>
    new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
    ).toString();

  return {
    /** Public settings: whether the page may ask, and whether it may import. */
    async getConfig() {
      return toConfig(await request('/api/config'));
    },

    /** Create an account here and sign it in. Only the password provider has these. */
    async register({ email, password }) {
      return toSession(
        await request('/api/auth/register', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        })
      );
    },

    async login({ email, password }) {
      return toSession(
        await request('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        })
      );
    },

    /** End the session this client is carrying, server-side as well as here. */
    async logout() {
      await request('/api/auth/logout', { method: 'POST' });
    },

    /** The signed-in caller's tier and allowance. Requires a token. */
    async getAccount() {
      return toAccount(await request('/api/me'));
    },

    async listElections() {
      return (await request('/api/elections')).map(toSummary);
    },

    /** Has this election been imported before? Answered without looking it up. */
    async lookup({ year, nation, subnation }) {
      return toResult(
        await request(`/api/elections/lookup?${query({ year, nation, subnation })}`)
      );
    },

    async getElection(electionHash) {
      return toResult(await request(`/api/elections/${encodeURIComponent(electionHash)}`));
    },

    /** The server finds the election; we send only which one is wanted. */
    async importElection({ year, nation, subnation }, { waitSeconds = 25 } = {}) {
      const body = await request(`/api/elections/import?${query({ wait_seconds: waitSeconds })}`, {
        method: 'POST',
        body: JSON.stringify({ year, nation, subnation: subnation ?? null }),
      });
      return toResult(body);
    },

    /**
     * Ask for an election nobody has imported. Needs no secret: nothing is
     * searched for or read — the election is written down to be imported later.
     */
    async requestElection({ year, nation, subnation }) {
      return toFiledRequest(
        await request('/api/elections/requests', {
          method: 'POST',
          body: JSON.stringify({ year, nation, subnation: subnation ?? null }),
        })
      );
    },

    /** How an import already under way is getting on. Free: it never starts one. */
    async getImport(requestKey, { waitSeconds = 25 } = {}) {
      return toResult(
        await request(
          `/api/elections/imports/${encodeURIComponent(requestKey)}?${query({ wait_seconds: waitSeconds })}`
        )
      );
    },

    /** The only path into storage. `option` picks one of an upcoming election's forecasts. */
    async confirm(requestKey, { option } = {}) {
      const suffix = option === undefined || option === null ? '' : `?${query({ option })}`;
      return toResult(
        await request(`/api/elections/imports/${encodeURIComponent(requestKey)}/confirm${suffix}`, {
          method: 'POST',
        })
      );
    },

    async discardPreview(requestKey) {
      await request(`/api/elections/imports/${encodeURIComponent(requestKey)}/preview`, {
        method: 'DELETE',
      });
    },

    /** A Stripe Checkout URL for one tier. Nothing changes until Stripe says so. */
    async startCheckout(tier) {
      const body = await request('/api/billing/checkout', {
        method: 'POST',
        body: JSON.stringify({ tier }),
      });
      return body.url;
    },

    /** Stripe's own page for changing or cancelling the subscription. */
    async openBillingPortal() {
      return (await request('/api/billing/portal', { method: 'POST' })).url;
    },
  };
}
