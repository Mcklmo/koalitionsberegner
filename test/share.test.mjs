/**
 * js/share.js mirrors backend/app/share.py's parsing rules exactly. This file
 * lists the same edge cases as backend/tests/test_share.py so the two sides
 * agree on every link a visitor might paste.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildPath, decodeSelection, encodeSelection, flattenedCount, parseLocation, parseSeats,
} from '../js/share.js';

const COUNT = 10; // The Folketing example from the plan and test_share.py: A F B Ø M V C I Æ O.

// --- c -----------------------------------------------------------------------

test('missing or empty c selects nothing', () => {
  assert.deepEqual(decodeSelection(null, COUNT), []);
  assert.deepEqual(decodeSelection(undefined, COUNT), []);
  assert.deepEqual(decodeSelection('', COUNT), []);
});

test('c is read as positions', () => {
  assert.deepEqual(decodeSelection('0,2,3,7', COUNT), [0, 2, 3, 7]);
});

test('c is deduplicated and sorted', () => {
  assert.deepEqual(decodeSelection('7,3,3,0,7', COUNT), [0, 3, 7]);
});

test('an index at or past the last party is dropped on its own', () => {
  assert.deepEqual(decodeSelection('1,10,9,500', COUNT), [1, 9]);
});

test('leading zeros are the same number', () => {
  assert.deepEqual(decodeSelection('007', COUNT), [7]);
});

for (const c of [
  'a', '1,a', '1,,2', '1,', ',1', '-1', '+1', ' 1', '1 ', '1.0', '0x1', '1e2',
  '١', // an Arabic-Indic one: Number() reads it, the regex would not
  '123456', // wider than any position can be
]) {
  test(`a malformed c (${JSON.stringify(c)}) selects nothing`, () => {
    assert.deepEqual(decodeSelection(c, COUNT), []);
  });
}

test('c naming more parties than any election holds selects nothing', () => {
  const bound = 50 * 200;
  assert.deepEqual(decodeSelection(Array(bound + 1).fill('1').join(','), bound + 5), []);
  assert.deepEqual(decodeSelection(['1', ...Array(bound - 1).fill('1')].join(','), bound + 5), [1]);
});

test('an absurdly long c is refused before it is split', () => {
  assert.deepEqual(decodeSelection('1,'.repeat(1_000_000), COUNT), []);
});

test('without a known party count, only the length past the highest index is dropped', () => {
  assert.deepEqual(decodeSelection('1,10,9,500', Infinity), [1, 9, 10, 500]);
});

// --- s -------------------------------------------------------------------

for (const [s, expected] of [['79', 79], ['0', 0], ['100000', 100_000], ['0079', 79]]) {
  test(`s=${s} is a seat total`, () => {
    assert.equal(parseSeats(s), expected);
  });
}

for (const s of [null, undefined, '', '-1', '100001', '1234567', '7.5', 'x', ' 7', '٧']) {
  test(`a missing or malformed s (${JSON.stringify(s)}) is null`, () => {
    assert.equal(parseSeats(s), null);
  });
}

// --- encode / round trip ---------------------------------------------------

test('encodeSelection is ascending, unique and comma-separated', () => {
  assert.equal(encodeSelection([7, 0, 3, 3]), '0,3,7');
  assert.equal(encodeSelection([]), '');
});

test('encode and decode round-trip a selection', () => {
  const indices = [0, 3, 7];
  assert.deepEqual(decodeSelection(encodeSelection(indices), COUNT), indices);
});

// --- flattenedCount ----------------------------------------------------------

test('flattenedCount counts every party across every block', () => {
  const election = { blocks: [{ parties: [{}, {}] }, { parties: [{}] }] };
  assert.equal(flattenedCount(election), 3);
});

// --- buildPath ---------------------------------------------------------------

test('buildPath names the bare election with nothing selected', () => {
  assert.equal(buildPath({ id: 'abc123abc123', indices: [], total: 0 }), '/e/abc123abc123');
});

test('buildPath carries the selection and the seat total', () => {
  assert.equal(
    buildPath({ id: 'abc123abc123', indices: [7, 0, 3], total: 77 }),
    '/e/abc123abc123?c=0,3,7&s=77',
  );
});

test('buildPath names the root when there is nothing to share', () => {
  // main.js falls back to this when setShareTarget(null) hides the share
  // button, so an old `/e/<id>?...` never outlives the button that pointed
  // at it.
  assert.equal(buildPath({ id: null }), '/');
  assert.equal(buildPath({ id: null, indices: [1, 2], total: 9 }), '/', 'id wins over anything else');
});

// --- parseLocation -------------------------------------------------------

test('a path with no /e/ id is not a shared link', () => {
  assert.equal(parseLocation({ pathname: '/', search: '' }), null);
  assert.equal(parseLocation({ pathname: '/e/', search: '' }), null);
  assert.equal(parseLocation({ pathname: '/e/short', search: '' }), null);
  assert.equal(parseLocation({ pathname: '/e/0123456789AB', search: '' }), null, 'uppercase is not hex');
});

test('parseLocation reads the id, the selection and the seat total', () => {
  assert.deepEqual(
    parseLocation({ pathname: '/e/0123456789ab', search: '?c=0,2,3,7&s=79' }),
    { id: '0123456789ab', indices: [0, 2, 3, 7], seats: 79 },
  );
});

test('parseLocation accepts an id up to sixty-four hex characters, and a bare id', () => {
  const long = '3f'.repeat(32);
  assert.deepEqual(
    parseLocation({ pathname: `/e/${long}`, search: '' }),
    { id: long, indices: [], seats: null },
  );
});

test('an unknown query parameter is ignored', () => {
  assert.deepEqual(
    parseLocation({ pathname: '/e/0123456789ab', search: '?utm_source=reddit&c=1' }),
    { id: '0123456789ab', indices: [1], seats: null },
  );
});

test('a duplicated c or s reads as the first one', () => {
  // URLSearchParams.get already returns the first value of a repeated
  // parameter; backend/app/main.py's `_first_param` reads `?c=...&c=...` the
  // same way, so a stray duplicate is not read differently on the two sides.
  assert.deepEqual(
    parseLocation({ pathname: '/e/0123456789ab', search: '?c=1&c=2&s=5&s=9' }),
    { id: '0123456789ab', indices: [1], seats: 5 },
  );
});

test('a malformed c in the URL still opens the election, with nothing selected', () => {
  assert.deepEqual(
    parseLocation({ pathname: '/e/0123456789ab', search: '?c=a,b&s=100001' }),
    { id: '0123456789ab', indices: [], seats: null },
  );
});
