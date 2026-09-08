/**
 * Client for the election store API.
 *
 * The backend speaks snake_case; the renderer's canonical schema (election.js)
 * speaks camelCase. That translation lives here and nowhere else, and every
 * election crossing the boundary is validated before anything else touches it.
 *
 * Every request carries the caller's ID token when there is one, and none when
 * there is not — a signed-out visitor is a legitimate caller here, served the
 * curated selection. What that identity is *allowed* to do is decided by the
 * backend alone; nothing in this file gates anything.
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
    blocks: payload.blocks,
  });
}

function toSummary(row) {
  return {
    electionHash: row.election_hash,
    nation: row.nation,
    state: row.state ?? null,
    electionDate: row.election_date,
    title: row.title,
    totalSeats: row.total_seats,
    selected: Boolean(row.selected),
  };
}

/** The caller's tier and what is left of this month's import allowance. */
function toAccount(body) {
  return {
    uid: body.uid,
    email: body.email ?? null,
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
    authRequired: Boolean(body.auth_required),
    firebase: body.firebase ?? {},
    billingEnabled: Boolean(body.billing_enabled),
    tiers: (body.tiers ?? []).map((row) => ({
      tier: row.tier,
      monthlyImports: row.monthly_imports,
      purchasable: Boolean(row.purchasable),
    })),
  };
}

/** `state` on the wire is the import state; renamed so it cannot be confused
 *  with an election's `state` (its region). */
function toResult(body) {
  return {
    pageKey: body.page_key,
    electionHash: body.election_hash ?? null,
    status: body.status ?? body.state,
    election: body.election ? toElection(body.election) : null,
    error: body.error ?? null,
    reused: Boolean(body.reused),
    duplicate: Boolean(body.duplicate),
  };
}

/**
 * @param {{baseUrl?: string, fetch?: Function, getToken?: () => Promise<string|null>}} options
 *   `getToken` supplies the caller's ID token, or null when signed out.
 */
export function createApiClient({ baseUrl = '', fetch: fetchImpl, getToken } = {}) {
  const doFetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
  if (!doFetch) throw new TypeError('no fetch implementation available');

  async function authHeaders() {
    if (!getToken) return {};
    // A session that cannot produce a token is simply signed out as far as
    // this request is concerned; the visitor view is still worth serving.
    const token = await getToken().catch(() => null);
    return token ? { authorization: `Bearer ${token}` } : {};
  }

  async function request(path, options = {}) {
    let response;
    const authorization = await authHeaders();
    try {
      response = await doFetch(baseUrl + path, {
        ...options,
        headers: { 'content-type': 'application/json', ...authorization, ...options.headers },
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
    /** Public settings: how to sign in, and what is for sale. */
    async getConfig() {
      return toConfig(await request('/api/config'));
    },

    /** The signed-in caller's tier and allowance. Requires a token. */
    async getAccount() {
      return toAccount(await request('/api/me'));
    },

    async listElections() {
      return (await request('/api/elections')).map(toSummary);
    },

    /** Has this page been imported before? Answered without fetching it. */
    async lookup({ sourceUrl }) {
      return toResult(await request(`/api/elections/lookup?${query({ source_url: sourceUrl })}`));
    },

    async getElection(electionHash) {
      return toResult(await request(`/api/elections/${encodeURIComponent(electionHash)}`));
    },

    /** The agent identifies the election; we send only where to read it. */
    async importElection({ sourceUrl }, { waitSeconds = 25 } = {}) {
      const body = await request(`/api/elections/import?${query({ wait_seconds: waitSeconds })}`, {
        method: 'POST',
        body: JSON.stringify({ source_url: sourceUrl }),
      });
      return toResult(body);
    },

    /** The only path into storage. */
    async confirm(pageKey) {
      return toResult(
        await request(`/api/elections/pages/${encodeURIComponent(pageKey)}/confirm`, {
          method: 'POST',
        })
      );
    },

    async discardPreview(pageKey) {
      await request(`/api/elections/pages/${encodeURIComponent(pageKey)}/preview`, {
        method: 'DELETE',
      });
    },

    /** Curate an election into what signed-out visitors see. Administrators only. */
    async setSelected(electionHash, selected) {
      return toSummary(
        await request(`/api/elections/${encodeURIComponent(electionHash)}/selected`, {
          method: 'PUT',
          body: JSON.stringify({ selected }),
        })
      );
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
