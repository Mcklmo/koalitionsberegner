/**
 * Acceptance (#9): an imported election of a different shape renders in the
 * coalition UI. Uses the Sachsen-Anhalt 2021 Landtag result — 97 seats, six
 * parties, one block — against the same renderer the Folketing page uses.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCoalitionCalculator } from '../js/app.js';
import { toElection } from '../js/api.js';

/** Exactly what the backend serves for this election. */
const payload = {
  nation: 'Deutschland',
  state: 'Sachsen-Anhalt',
  election_date: '2021-06-06',
  title: 'Koalitionsberegner — Landtag Sachsen-Anhalt 2021',
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

function makeEl() {
  return {
    className: '', textContent: '', style: {}, children: [], onclick: null,
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
  };
}

function mount() {
  const byId = {};
  for (const id of ['title', 'subtitle', 'party-list', 'bar', 'total', 'total-of', 'verdict', 'footer-note']) {
    byId[id] = makeEl();
  }
  globalThis.document = { title: '', getElementById: (id) => byId[id], createElement: makeEl };
  const app = mountCoalitionCalculator(toElection(payload));
  const rows = () => byId['party-list'].children.filter((c) => c.className.startsWith('party-row'));
  return { byId, app, rows };
}

test('the imported election renders with its own parties and totals', () => {
  const { byId, rows } = mount();

  assert.equal(byId['title'].textContent, 'Koalitionsberegner — Landtag Sachsen-Anhalt 2021');
  assert.equal(byId['subtitle'].textContent,
    'Vælg partier og se om de tilsammen opnår flertal (49+ ud af 97 mandater)');
  assert.equal(byId['total-of'].textContent, 'af 97 mandater');
  assert.match(byId['footer-note'].textContent, /Flertal kræver 49 mandater · Endelig resultat, 6\. juni 2021/);
  assert.equal(rows().length, 6);
  assert.equal(byId['party-list'].children.filter((c) => c.className === 'blok-label').length, 1);
  assert.equal(byId['verdict'].textContent, 'Mangler 49');
});

test('the majority arithmetic follows the imported thresholds', () => {
  const { byId, rows, app } = mount();
  const click = (i) => rows()[i].onclick();

  click(0); // CDU 40
  assert.equal(byId['total'].textContent, 40);
  assert.equal(byId['verdict'].textContent, 'Mangler 9');

  click(3); // + SPD 9 = 49, exactly a majority
  assert.equal(byId['total'].textContent, 49);
  assert.equal(byId['verdict'].textContent, 'Flertal ✓ (+0)');
  assert.equal(byId['bar'].style.background, '#378ADD');

  click(5); // + Grüne 6 = 55, the actual governing coalition
  assert.equal(byId['total'].textContent, 55);
  assert.equal(byId['verdict'].textContent, 'Flertal ✓ (+6)');

  app.clearAll();
  assert.equal(byId['total'].textContent, 0);
  assert.equal(byId['bar'].style.width, '0%');
});

test('a two-thirds coalition is called a large majority', () => {
  const { byId, rows } = mount();
  // CDU 40 + AfD 23 + Linke 12 = 75 of 97; the supermajority line is floor(97*2/3) = 64.
  [0, 1, 2].forEach((i) => rows()[i].onclick());
  assert.equal(byId['total'].textContent, 75);
  assert.equal(byId['verdict'].textContent, 'Stort flertal ✓');
  assert.equal(byId['bar'].style.background, '#1D9E75');
});
