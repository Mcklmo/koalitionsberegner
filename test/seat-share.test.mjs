/**
 * Every party in the list carries its share of the assembly beside its seat
 * count. The share is seats over `totalSeats` — never a vote share, which
 * never reaches the renderer — shown to one decimal, in the page's own
 * notation: a comma and a non-breaking space in Danish and German, a point and
 * no space in English.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCoalitionCalculator } from '../js/app.js';
import { toElection } from '../js/api.js';
import { setLanguage, t } from '../js/i18n.js';
import './strings.mjs';

/** 97 seats over six parties: nothing here divides evenly. */
const uneven = {
  nation: 'Deutschland',
  state: 'Sachsen-Anhalt',
  election_date: '2021-06-06',
  title: 'Landtag Sachsen-Anhalt 2021',
  source_url: 'https://wahlergebnisse.sachsen-anhalt.de/',
  total_seats: 97,
  majority_seats: 49,
  blocks: [
    {
      name: 'Landtag von Sachsen-Anhalt',
      parties: [
        { name: 'Christlich Demokratische Union', abbr: 'CDU', seats: 40, color: '#000000' },
        { name: 'Alternative für Deutschland', abbr: 'AfD', seats: 23, color: '#009EE0' },
        { name: 'Die Linke', abbr: 'Linke', seats: 12, color: '#BE3075' },
        { name: 'Sozialdemokratische Partei Deutschlands', abbr: 'SPD', seats: 9, color: '#E3000F' },
        { name: 'Freie Demokratische Partei', abbr: 'FDP', seats: 7, color: '#FFED00' },
        { name: 'Bündnis 90/Die Grünen', abbr: 'Grüne', seats: 6, color: '#1AA037' },
      ],
    },
  ],
};

/** A party that won nothing still sits in the list, at nought per cent. */
const withEmptyHanded = {
  nation: 'Toyland',
  state: null,
  election_date: '2026-01-15',
  title: 'Toy election',
  source_url: 'https://example.org/results',
  total_seats: 10,
  majority_seats: 6,
  blocks: [
    {
      name: 'Toy parliament',
      parties: [
        { name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' },
        { name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' },
        { name: 'Nobody Voted Party', abbr: 'N', seats: 0, color: '#7F8C8D' },
      ],
    },
  ],
};

/** A poll whose seats were themselves worked out from vote shares. */
const forecast = {
  ...withEmptyHanded,
  title: 'Toy poll',
  forecast: { publisher: 'Voxmeter', published_on: '2025-12-01', computed: true },
};

function makeEl() {
  return {
    className: '', textContent: '', style: {}, children: [], onclick: null, title: '',
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
  };
}

/** Mounts `payload` and hands back each party row's spans, by class. */
function mount(payload) {
  const byId = {};
  for (const id of ['title', 'subtitle', 'party-list', 'bar', 'total', 'total-of', 'verdict', 'footer-note']) {
    byId[id] = makeEl();
  }
  globalThis.document = { title: '', getElementById: (id) => byId[id], createElement: makeEl };
  mountCoalitionCalculator(toElection(payload));
  const rows = byId['party-list'].children.filter((c) => c.className.startsWith('party-row'));
  const span = (row, className) => row.children.find((c) => c.className === className);
  return {
    shares: rows.map((row) => span(row, 'share').textContent),
    seats: rows.map((row) => span(row, 'seats').textContent),
    titles: rows.map((row) => span(row, 'share').title),
  };
}

test('every party shows its share of the seats beside its seat count', () => {
  setLanguage('da', { remember: false });
  const { shares, seats } = mount(uneven);
  assert.deepEqual(seats, [40, 23, 12, 9, 7, 6], 'the seat counts are untouched');
  assert.deepEqual(shares, ['41,2 %', '23,7 %', '12,4 %', '9,3 %', '7,2 %', '6,2 %']);
});

test('a share that does not divide evenly is rounded to one decimal, not truncated', () => {
  setLanguage('en', { remember: false });
  const { shares } = mount(uneven);
  // 12/97 is 12.371…, which rounds up; 40/97 is 41.237…, which rounds down.
  assert.deepEqual(shares, ['41.2%', '23.7%', '12.4%', '9.3%', '7.2%', '6.2%']);
  for (const share of shares) assert.match(share, /^\d+\.\d%$/, 'exactly one decimal, always');
});

test('each language writes the number its own way', () => {
  for (const [lang, expected] of [
    ['da', '60,0 %'],
    ['de', '60,0 %'],
    ['en', '60.0%'],
  ]) {
    setLanguage(lang, { remember: false });
    assert.equal(mount(withEmptyHanded).shares[0], expected, lang);
  }
});

test('a party with no seats reads as nought, rather than being left blank', () => {
  setLanguage('da', { remember: false });
  const { shares, seats } = mount(withEmptyHanded);
  assert.deepEqual(seats, [6, 4, 0]);
  assert.deepEqual(shares, ['60,0 %', '40,0 %', '0,0 %']);
});

test('a forecast shows the same seat share, vote shares being none of the renderer\'s business', () => {
  setLanguage('da', { remember: false });
  assert.deepEqual(mount(forecast).shares, mount(withEmptyHanded).shares);
});

test('the share says what it is a share of, in the page\'s language', () => {
  for (const [lang, expected] of [
    ['da', 'Andel af mandaterne'],
    ['en', 'Share of the seats'],
    ['de', 'Anteil der Sitze'],
  ]) {
    setLanguage(lang, { remember: false });
    const { titles } = mount(withEmptyHanded);
    assert.equal(t('calc.seatShare'), expected);
    for (const title of titles) assert.equal(title, expected);
  }
});

test('the shares of a whole assembly add up to about all of it', () => {
  setLanguage('en', { remember: false });
  const total = mount(uneven).shares
    .reduce((sum, share) => sum + Number(share.replace('%', '')), 0);
  // Six numbers each rounded to a tenth cannot land on exactly 100.
  assert.ok(Math.abs(total - 100) <= 0.3, `shares summed to ${total}`);
});
