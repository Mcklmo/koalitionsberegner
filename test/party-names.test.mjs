/**
 * A party has a name in English and, where the election's own language differs,
 * a local one. The calculator shows the local one unless asked for English,
 * remembers the choice, and offers no choice where there is nothing to switch.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCoalitionCalculator } from '../js/app.js';
import { toElection } from '../js/api.js';

const payload = (parties) => ({
  nation: 'Germany',
  state: null,
  election_date: '2025-02-23',
  title: 'Bundestag 2025',
  source_url: 'https://en.wikipedia.org/wiki/2025_German_federal_election',
  total_seats: 10,
  majority_seats: 6,
  blocks: [{ name: 'Bundestag', parties }],
});

const bilingual = payload([
  { name: 'Alternative for Germany', local_name: 'Alternative für Deutschland', abbr: 'AfD', seats: 6, color: '#009EE0' },
  { name: 'The Left', local_name: 'Die Linke', abbr: 'Linke', seats: 4, color: '#BE3075' },
]);

/** Stored before parties had a local name. */
const englishOnly = payload([
  { name: 'Alternative for Germany', abbr: 'AfD', seats: 6, color: '#009EE0' },
  { name: 'The Left', abbr: 'Linke', seats: 4, color: '#BE3075' },
]);

function makeEl() {
  return {
    className: '', textContent: '', style: {}, children: [], onclick: null, onchange: null, value: '', hidden: false,
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
  };
}

function memoryStorage() {
  const items = new Map();
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, String(v)) };
}

globalThis.localStorage = memoryStorage();

function mount(data) {
  const byId = {};
  for (const id of ['title', 'subtitle', 'party-list', 'bar', 'total', 'total-of', 'verdict', 'footer-note', 'names', 'names-row']) {
    byId[id] = makeEl();
  }
  globalThis.document = { title: '', getElementById: (id) => byId[id], createElement: makeEl };
  mountCoalitionCalculator(toElection(data));
  const shownNames = () => byId['party-list'].children
    .filter((c) => c.className.startsWith('party-row'))
    .map((row) => row.children.find((c) => c.className === 'party-name').textContent);
  const choose = (value) => {
    byId.names.value = value;
    byId.names.onchange();
  };
  return { byId, shownNames, choose };
}

test('local names are shown by default, and English on request, which is remembered', () => {
  const first = mount(bilingual);
  assert.equal(first.byId['names-row'].hidden, false);
  assert.equal(first.byId.names.value, 'local');
  assert.deepEqual(first.shownNames(), ['Alternative für Deutschland', 'Die Linke']);

  first.choose('english');
  assert.deepEqual(first.shownNames(), ['Alternative for Germany', 'The Left']);
  assert.equal(globalThis.localStorage.getItem('koalitionsberegner.partyNames'), 'english');

  // Another election mounted later keeps the choice.
  const second = mount(bilingual);
  assert.equal(second.byId.names.value, 'english');
  assert.deepEqual(second.shownNames(), ['Alternative for Germany', 'The Left']);
  second.choose('local');
});

test('an election without local names hides the choice and shows the one name it has', () => {
  const { byId, shownNames } = mount(englishOnly);
  assert.equal(byId['names-row'].hidden, true);
  assert.deepEqual(shownNames(), ['Alternative for Germany', 'The Left']);
});

test('storage that throws does not stop the calculator or the choice', () => {
  const working = globalThis.localStorage;
  globalThis.localStorage = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); },
  };
  try {
    const { shownNames, choose } = mount(bilingual);
    choose('english');
    assert.deepEqual(shownNames(), ['Alternative for Germany', 'The Left']);
    choose('local');
  } finally {
    globalThis.localStorage = working;
  }
});
