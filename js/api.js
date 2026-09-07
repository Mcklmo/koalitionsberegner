/**
 * Client for the election store API.
 *
 * The backend speaks snake_case; the renderer's canonical schema (election.js)
 * speaks camelCase. That translation lives here and nowhere else, and every
 * election crossing the boundary is validated before anything else touches it.
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

export function createApiClient({ baseUrl = '', fetch: fetchImpl } = {}) {
  const doFetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
  if (!doFetch) throw new TypeError('no fetch implementation available');

  async function request(path, options = {}) {
    let response;
    try {
      response = await doFetch(baseUrl + path, {
        headers: { 'content-type': 'application/json' },
        ...options,
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
  };
}
