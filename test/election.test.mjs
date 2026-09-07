import test from 'node:test';
import assert from 'node:assert/strict';
import { validateElection, ElectionValidationError, isValidatedElection, createElectionProvider } from '../js/election.js';
import { Folketing2026Provider } from '../js/providers/folketing-2026.js';

/** A minimal election that passes; each case below breaks exactly one thing. */
const valid = () => ({
  nation: 'Toyland',
  electionDate: '2026-01-15',
  title: 'Toy election',
  sourceUrl: 'https://example.org/results',
  totalSeats: 10,
  majoritySeats: 6,
  blocks: [
    { name: 'Left', parties: [{ abbr: 'L', name: 'Left Party', seats: 6, color: '#C0392B' }] },
    { name: 'Right', parties: [{ abbr: 'R', name: 'Right Party', seats: 4, color: '#2980B9' }] },
  ],
});

/** Returns the error list produced by validating `mutate(valid())`. */
function errorsFor(mutate) {
  const input = valid();
  mutate(input);
  try {
    validateElection(input);
  } catch (err) {
    assert.ok(err instanceof ElectionValidationError, `expected ElectionValidationError, got ${err}`);
    return err.errors;
  }
  assert.fail('expected validation to fail, but it passed');
}

test('the Folketing 2026 provider data passes validation', async () => {
  const election = await Folketing2026Provider.getElection();
  assert.equal(election.nation, 'Danmark');
  assert.equal(election.totalSeats, 179);
  assert.equal(election.majoritySeats, 90);
  assert.equal(election.state, null, 'omitted state normalises to null');
  const seats = election.blocks.flatMap((b) => b.parties).reduce((n, p) => n + p.seats, 0);
  assert.equal(seats, 179, 'party seats sum to the declared total');
});

test('a valid election is normalised and deep-frozen', () => {
  const election = validateElection(valid());
  assert.ok(Object.isFrozen(election));
  assert.ok(Object.isFrozen(election.blocks[0].parties[0]));
  assert.deepEqual(Object.keys(election).sort(),
    ['blocks', 'electionDate', 'majoritySeats', 'nation', 'sourceUrl', 'state', 'title', 'totalSeats']);
});

test('optional state is accepted when present', () => {
  const election = validateElection({ ...valid(), state: 'North Province' });
  assert.equal(election.state, 'North Province');
});

const rejections = [
  ['extra top-level field', (e) => { e.extra = 'nope'; }, /unexpected field "extra"/],
  ['extra party field', (e) => { e.blocks[0].parties[0].onclick = 'alert(1)'; }, /unexpected field "onclick"/],
  ['extra block field', (e) => { e.blocks[0].rogue = 1; }, /unexpected field "rogue"/],
  ['missing nation', (e) => { delete e.nation; }, /nation: expected a string/],
  ['empty title', (e) => { e.title = '   '; }, /title: must not be empty/],
  ['wrong type for totalSeats', (e) => { e.totalSeats = '10'; }, /totalSeats: expected an integer/],
  ['fractional seats', (e) => { e.blocks[0].parties[0].seats = 6.5; }, /seats: expected an integer/],
  ['negative seats', (e) => { e.blocks[0].parties[0].seats = -1; }, /seats: must be between 0/],
  ['seat sum mismatch', (e) => { e.blocks[0].parties[0].seats = 5; }, /seats sum to 9, but totalSeats is 10/],
  ['majority below half', (e) => { e.majoritySeats = 5; }, /is not a majority of 10 seats/],
  ['majority above total', (e) => { e.majoritySeats = 11; }, /is not a majority of 10 seats/],
  ['malformed date', (e) => { e.electionDate = '15/01/2026'; }, /expected ISO 8601/],
  ['impossible date', (e) => { e.electionDate = '2026-02-30'; }, /not a real date/],
  ['malformed url', (e) => { e.sourceUrl = 'not a url'; }, /not a well-formed URL/],
  ['javascript: url', (e) => { e.sourceUrl = 'javascript:alert(1)'; }, /must use http or https/],
  ['non-hex color', (e) => { e.blocks[0].parties[0].color = 'red; background:url(x)'; }, /expected a hex color/],
  ['empty blocks', (e) => { e.blocks = []; }, /blocks: expected a non-empty array/],
  ['empty parties', (e) => { e.blocks[0].parties = []; }, /parties: expected a non-empty array/],
  ['control characters in a name', (e) => { e.blocks[0].parties[0].name = 'Left\u0007Party'; }, /must not contain control characters/],
];

for (const [name, mutate, expected] of rejections) {
  test(`rejects: ${name}`, () => {
    const errors = errorsFor(mutate);
    assert.ok(errors.some((m) => expected.test(m)),
      `no error matched ${expected}\ngot:\n  ${errors.join('\n  ')}`);
  });
}

test('rejects non-objects outright', () => {
  for (const input of [null, undefined, 42, 'election', []]) {
    assert.throws(() => validateElection(input), ElectionValidationError);
  }
});

test('reports every problem at once, with paths', () => {
  const errors = errorsFor((e) => {
    e.totalSeats = -1;
    e.sourceUrl = 'ftp://example.org';
    e.blocks[1].parties[0].color = 'blue';
  });
  assert.equal(errors.length, 3);
  assert.ok(errors.some((m) => m.startsWith('election.totalSeats:')));
  assert.ok(errors.some((m) => m.startsWith('election.sourceUrl:')));
  assert.ok(errors.some((m) => m.startsWith('election.blocks[1].parties[0].color:')));
});

test('validation is the only way to be marked validated', () => {
  const election = validateElection(valid());
  assert.ok(isValidatedElection(election));
  assert.ok(!isValidatedElection(valid()), 'raw input is not validated');
  assert.ok(!isValidatedElection({ ...election }), 'a copy of a validated election is not validated');
});

test('createElectionProvider validates on every read', async () => {
  const good = createElectionProvider(() => valid());
  assert.ok(isValidatedElection(await good.getElection()));

  const bad = createElectionProvider(() => ({ ...valid(), totalSeats: 3 }));
  await assert.rejects(() => bad.getElection(), ElectionValidationError);

  const asyncSource = createElectionProvider(async () => valid());
  assert.ok(isValidatedElection(await asyncSource.getElection()));
});
