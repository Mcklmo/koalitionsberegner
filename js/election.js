/**
 * Canonical election schema and its validator.
 *
 * This is the contract between the renderer and every election source —
 * a hardcoded provider or the LLM extraction agent. Nothing reaches the
 * renderer without passing `validateElection`, which is the client-side half of
 * the output gate described in `doc/threat-model.md`: an allowlist of fields,
 * checked types and bounds, and text that is safe to put in the DOM.
 *
 * @typedef {Object} Party
 * @property {string} name    Full party name.
 * @property {string} abbr    Short label shown in the list (e.g. "A").
 * @property {number} seats   Seats won; non-negative integer.
 * @property {string} color   `#rgb` or `#rrggbb` hex color for the party dot.
 *
 * @typedef {Object} Block
 * @property {string} name    Heading for the group (e.g. "Rød blok").
 * @property {Party[]} parties
 *
 * @typedef {Object} Election
 * @property {string} nation         Country the election belongs to.
 * @property {string|null} state     Sub-national region, or null.
 * @property {string} electionDate   ISO 8601 `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`.
 * @property {string} title          Heading shown above the calculator.
 * @property {string} sourceUrl      Absolute http(s) URL the results came from.
 * @property {number} totalSeats     Size of the assembly.
 * @property {number} majoritySeats  Seats needed for a majority.
 * @property {Block[]} blocks
 *
 * @typedef {Object} ElectionProvider
 * @property {() => Promise<Election>} getElection
 */

/** Thrown by {@link validateElection}; `.errors` lists every problem found. */
export class ElectionValidationError extends Error {
  /** @param {string[]} errors */
  constructor(errors) {
    super('Invalid election:\n  - ' + errors.join('\n  - '));
    this.name = 'ElectionValidationError';
    this.errors = errors;
  }
}

const ELECTION_FIELDS = ['nation', 'state', 'electionDate', 'title', 'sourceUrl', 'totalSeats', 'majoritySeats', 'blocks'];
const BLOCK_FIELDS = ['name', 'parties'];
const PARTY_FIELDS = ['name', 'abbr', 'seats', 'color'];

const MAX_TEXT = 200;
const MAX_BLOCKS = 50;
const MAX_PARTIES_PER_BLOCK = 200;
const MAX_SEATS = 100000;
const HEX_COLOR = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?$/;
// Control characters are rejected outright: extracted text goes straight into the DOM.
const CONTROL_CHARS = /[\u0000-\u001F\u007F]/;
// Invisible and direction-changing characters survive `textContent` intact, so a
// name carrying U+202E renders reversed and can be made to read as another
// party's. ZWJ/ZWNJ (U+200C/U+200D) are allowed: real scripts need them.
// Mirrors `_CONFUSABLE_CHARS` in `backend/app/schema.py`.
const CONFUSABLE_CHARS = /[\u00AD\u200B\u200E\u200F\u202A-\u202E\u2028\u2029\u2066-\u2069\uFEFF]/;

const isPlainObject = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Records unexpected keys so a source cannot smuggle fields past the allowlist. */
function checkAllowlist(errors, path, obj, allowed) {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) errors.push(`${path}: unexpected field "${key}"`);
  }
}

function describe(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'an array';
  if (typeof value === 'string') return `a string (${JSON.stringify(value.slice(0, 40))})`;
  if (typeof value === 'number') return `the number ${value}`;
  return `a ${typeof value}`;
}

function checkText(errors, path, value) {
  if (typeof value !== 'string') return errors.push(`${path}: expected a string, got ${describe(value)}`), false;
  if (value.trim() === '') return errors.push(`${path}: must not be empty`), false;
  if (value.length > MAX_TEXT) return errors.push(`${path}: must be at most ${MAX_TEXT} characters`), false;
  if (CONTROL_CHARS.test(value)) return errors.push(`${path}: must not contain control characters`), false;
  if (CONFUSABLE_CHARS.test(value)) {
    return errors.push(`${path}: must not contain invisible or direction-changing characters`), false;
  }
  return true;
}

function checkInteger(errors, path, value, { min, max }) {
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    return errors.push(`${path}: expected an integer, got ${describe(value)}`), false;
  }
  if (value < min || value > max) return errors.push(`${path}: must be between ${min} and ${max}, got ${value}`), false;
  return true;
}

/** ISO 8601 date or date-time that is also a real calendar moment. */
function checkElectionDate(errors, path, value) {
  if (typeof value !== 'string') return errors.push(`${path}: expected a string, got ${describe(value)}`), false;
  const m = ISO_DATE.exec(value);
  if (!m) {
    errors.push(`${path}: expected ISO 8601 "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM", got ${JSON.stringify(value)}`);
    return false;
  }
  const [, y, mo, d, hh = '00', mi = '00', ss = '00'] = m;
  const date = new Date(Date.UTC(+y, +mo - 1, +d, +hh, +mi, +ss));
  const real = date.getUTCFullYear() === +y && date.getUTCMonth() === +mo - 1 && date.getUTCDate() === +d
    && date.getUTCHours() === +hh && date.getUTCMinutes() === +mi && date.getUTCSeconds() === +ss;
  if (!real) return errors.push(`${path}: not a real date/time: ${JSON.stringify(value)}`), false;
  return true;
}

/** Absolute http(s) only — blocks `javascript:` and `data:` from untrusted sources. */
function checkSourceUrl(errors, path, value) {
  if (typeof value !== 'string') return errors.push(`${path}: expected a string, got ${describe(value)}`), false;
  let url;
  try {
    url = new URL(value);
  } catch {
    return errors.push(`${path}: not a well-formed URL: ${JSON.stringify(value)}`), false;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return errors.push(`${path}: must use http or https, got "${url.protocol}"`), false;
  }
  return true;
}

function validateParty(errors, path, party) {
  if (!isPlainObject(party)) return errors.push(`${path}: expected an object, got ${describe(party)}`), 0;
  checkAllowlist(errors, path, party, PARTY_FIELDS);
  checkText(errors, `${path}.name`, party.name);
  checkText(errors, `${path}.abbr`, party.abbr);
  const seatsOk = checkInteger(errors, `${path}.seats`, party.seats, { min: 0, max: MAX_SEATS });
  if (typeof party.color !== 'string' || !HEX_COLOR.test(party.color)) {
    errors.push(`${path}.color: expected a hex color like "#1a2b3c", got ${describe(party.color)}`);
  }
  return seatsOk ? party.seats : 0;
}

function validateBlock(errors, path, block) {
  if (!isPlainObject(block)) return errors.push(`${path}: expected an object, got ${describe(block)}`), 0;
  checkAllowlist(errors, path, block, BLOCK_FIELDS);
  checkText(errors, `${path}.name`, block.name);
  if (!Array.isArray(block.parties) || block.parties.length === 0) {
    return errors.push(`${path}.parties: expected a non-empty array, got ${describe(block.parties)}`), 0;
  }
  if (block.parties.length > MAX_PARTIES_PER_BLOCK) {
    return errors.push(`${path}.parties: at most ${MAX_PARTIES_PER_BLOCK} parties per block`), 0;
  }
  return block.parties.reduce((sum, p, i) => sum + validateParty(errors, `${path}.parties[${i}]`, p), 0);
}

/**
 * The single gate. Returns a deep-frozen election containing only allowlisted
 * fields, or throws {@link ElectionValidationError} listing every problem.
 *
 * @param {unknown} input
 * @returns {Election}
 */
export function validateElection(input) {
  /** @type {string[]} */
  const errors = [];

  if (!isPlainObject(input)) throw new ElectionValidationError([`election: expected an object, got ${describe(input)}`]);
  checkAllowlist(errors, 'election', input, ELECTION_FIELDS);

  checkText(errors, 'election.nation', input.nation);
  const hasState = input.state !== undefined && input.state !== null;
  if (hasState) checkText(errors, 'election.state', input.state);
  checkElectionDate(errors, 'election.electionDate', input.electionDate);
  checkText(errors, 'election.title', input.title);
  checkSourceUrl(errors, 'election.sourceUrl', input.sourceUrl);

  const totalOk = checkInteger(errors, 'election.totalSeats', input.totalSeats, { min: 1, max: MAX_SEATS });
  const majorityOk = checkInteger(errors, 'election.majoritySeats', input.majoritySeats, { min: 1, max: MAX_SEATS });
  // A majority must be more than half the assembly and no more than all of it.
  if (totalOk && majorityOk
    && (input.majoritySeats <= input.totalSeats / 2 || input.majoritySeats > input.totalSeats)) {
    errors.push(`election.majoritySeats: ${input.majoritySeats} is not a majority of ${input.totalSeats} seats `
      + `(expected ${Math.floor(input.totalSeats / 2) + 1} to ${input.totalSeats})`);
  }

  if (!Array.isArray(input.blocks) || input.blocks.length === 0) {
    errors.push(`election.blocks: expected a non-empty array, got ${describe(input.blocks)}`);
  } else if (input.blocks.length > MAX_BLOCKS) {
    errors.push(`election.blocks: at most ${MAX_BLOCKS} blocks`);
  } else {
    const before = errors.length;
    const seatSum = input.blocks.reduce((sum, b, i) => sum + validateBlock(errors, `election.blocks[${i}]`, b), 0);
    // Only meaningful once every individual seat count is known to be a valid integer.
    if (totalOk && errors.length === before && seatSum !== input.totalSeats) {
      errors.push(`election.blocks: party seats sum to ${seatSum}, but totalSeats is ${input.totalSeats}`);
    }
  }

  if (errors.length > 0) throw new ElectionValidationError(errors);

  return freeze({
    nation: input.nation,
    state: hasState ? input.state : null,
    electionDate: input.electionDate,
    title: input.title,
    sourceUrl: input.sourceUrl,
    totalSeats: input.totalSeats,
    majoritySeats: input.majoritySeats,
    blocks: input.blocks.map((b) => ({
      name: b.name,
      parties: b.parties.map((p) => ({ name: p.name, abbr: p.abbr, seats: p.seats, color: p.color })),
    })),
  });
}

/** Objects that came out of {@link validateElection}; membership cannot be forged by copying. */
const validated = new WeakSet();

function freeze(election) {
  election.blocks.forEach((b) => {
    b.parties.forEach(Object.freeze);
    Object.freeze(b.parties);
    Object.freeze(b);
  });
  Object.freeze(election.blocks);
  Object.freeze(election);
  validated.add(election);
  return election;
}

/** True only for the exact object returned by {@link validateElection}. */
export function isValidatedElection(value) {
  return validated.has(value);
}

/**
 * Wraps a raw election source in the provider seam, validating on every read
 * so no unvalidated data can be handed to the renderer.
 *
 * @param {() => unknown | Promise<unknown>} load
 * @returns {ElectionProvider}
 */
export function createElectionProvider(load) {
  return {
    async getElection() {
      return validateElection(await load());
    },
  };
}
