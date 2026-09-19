/**
 * A shared link's selection, read from and written to the URL.
 *
 * A link to a coalition is `/e/<id>?c=<parties>&s=<seats>` (see
 * `doc/plans/02-share-links.md`): `<id>` a prefix of the election hash, `c`
 * the selected parties as positions in the order the page lists them, block
 * by block, and `s` the seat total when the link was made.
 *
 * Every function here mirrors a rule in `backend/app/share.py` exactly —
 * `decodeSelection` mirrors `parse_selection`, `parseSeats` mirrors
 * `parse_seats` — so a link parses the same way whichever side reads it.
 * `test/share.test.mjs` lists the same edge cases as `backend/tests/test_share.py`.
 *
 * `parseLocation` runs before the election is known, so it cannot yet drop an
 * index past the last party — that happens naturally once the caller applies
 * `indices` to the actual election (a position with no matching party selects
 * nothing). It still applies every other rule: a malformed `c` is empty, `s`
 * out of range is missing.
 */

// Mirrors backend/app/schema.py: as many parties as an election can hold.
const MAX_BLOCKS = 50;
const MAX_PARTIES_PER_BLOCK = 200;
const MAX_SELECTION = MAX_BLOCKS * MAX_PARTIES_PER_BLOCK;
const MAX_SEATS = 100000;

// ASCII digits only, same widths as backend/app/share.py's _INDEX and _SEATS:
// a regex without \d, which would also match non-ASCII digits int() accepts.
const INDEX = new RegExp(`^[0-9]{1,${String(MAX_SELECTION - 1).length}}$`);
const SEATS = new RegExp(`^[0-9]{1,${String(MAX_SEATS).length}}$`);

// Longest `c` worth splitting: every index at its widest, plus the commas.
const MAX_C_LENGTH = MAX_SELECTION * (String(MAX_SELECTION - 1).length + 1);

const ID_IN_PATH = /^\/e\/([0-9a-f]{12,64})(?:[/?#]|$)/;

// Mirrors backend/app/share.py's ID_LENGTH: long enough not to collide in a
// store of tens of elections.
const ID_LENGTH = 16;

/** The id a new link to this election carries: the front of its full hash. */
export function shareId(electionHash) {
  return electionHash.slice(0, ID_LENGTH);
}

/** How many parties `election` has, in the order the page lists them: block by block. */
export function flattenedCount(election) {
  return election.blocks.reduce((total, block) => total + block.parties.length, 0);
}

/**
 * The selected positions `c` names, ascending and unique.
 *
 * Missing or empty means nothing selected. So does a malformed `c` — a token
 * that is not a plain non-negative integer, an empty token, or more tokens
 * than any election has parties — because a mangled link should still open
 * the election. An index at or past `partyCount` is dropped on its own; pass
 * `Infinity` to skip that bound (before the election is known).
 */
export function decodeSelection(c, partyCount) {
  if (!c || c.length > MAX_C_LENGTH) return [];
  const tokens = c.split(',');
  if (tokens.length > MAX_SELECTION || !tokens.every((token) => INDEX.test(token))) return [];
  const unique = new Set(tokens.map(Number).filter((i) => i < partyCount));
  return [...unique].sort((a, b) => a - b);
}

/** The `c` value for these positions: ascending, unique, comma-separated. */
export function encodeSelection(indices) {
  return [...new Set(indices)].sort((a, b) => a - b).join(',');
}

/** The seat total `s` claims, or null when missing or malformed. */
export function parseSeats(s) {
  if (!s || !SEATS.test(s)) return null;
  const seats = Number(s);
  return seats <= MAX_SEATS ? seats : null;
}

/**
 * What a shared link's URL names, or null when the path names no election.
 * `location` is anything with `pathname` and `search`, such as
 * `globalThis.location` or a `URL`.
 */
export function parseLocation(location) {
  const match = ID_IN_PATH.exec(location.pathname ?? '');
  if (!match) return null;
  const [, id] = match;
  const params = new URLSearchParams(location.search ?? '');
  return {
    id,
    indices: decodeSelection(params.get('c'), Infinity),
    seats: parseSeats(params.get('s')),
  };
}

/**
 * The path a link to this selection carries; `/e/<id>` alone with nothing
 * selected. `id` is null when there is nothing to share (the bundled election
 * with no stored twin) — the root, so an old `/e/<id>?...` never outlives the
 * button that pointed at it.
 */
export function buildPath({ id, indices, total }) {
  if (!id) return '/';
  const c = encodeSelection(indices);
  return c ? `/e/${id}?c=${c}&s=${total}` : `/e/${id}`;
}
