/**
 * Which buttons the approval page offers for a draft's status.
 *
 * Its own module because `js/approve.js` fetches the string sheet as it loads,
 * which a test cannot import. The rule it holds is the frontend half of
 * `RETRYABLE_STATUSES` and `REJECTABLE_STATUSES` in `backend/app/main.py`: a
 * draft that was claimed but never confirmed posted stays at `approved`, and
 * the owner may still reject it — but never send it again, because the reply
 * may already be on Reddit.
 */

export const SENDABLE = ['pending', 'failed'];
export const REJECTABLE = ['pending', 'failed', 'approved'];

export function buttonStates(status) {
  return { send: SENDABLE.includes(status), reject: REJECTABLE.includes(status) };
}
