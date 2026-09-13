/**
 * The page speaks Danish or English. Both must word the same things, every text
 * the markup or the scripts ask for must exist, and the calculator must
 * actually come out in English for a visitor who is not Danish.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { STRINGS, detectLanguage, setLanguage, t } from '../js/i18n.js';
import { mountCoalitionCalculator } from '../js/app.js';
import { toElection } from '../js/api.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('both languages word the same things', () => {
  assert.deepEqual(Object.keys(STRINGS.en).sort(), Object.keys(STRINGS.da).sort());
});

test('every text the markup asks for exists', () => {
  const html = read('../index.html');
  const keys = [...html.matchAll(/data-i18n(?:-placeholder|-title)?="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(keys.length > 20, 'the markup is translated at all');
  for (const key of keys) assert.ok(Object.hasOwn(STRINGS.da, key), key);
});

test('every text the scripts ask for by name exists', () => {
  const scripts = readdirSync(new URL('../js/', import.meta.url)).filter((f) => f.endsWith('.js') && f !== 'i18n.js');
  const asked = new Set();
  for (const file of scripts) {
    const source = read(`../js/${file}`);
    // Direct lookups, and the refusal/mode/message tables that hold keys for later.
    for (const [, key] of source.matchAll(/\bt\(\s*'([^']+)'/g)) asked.add(key);
    for (const [, key] of source.matchAll(/:\s*\[?'((?:import|request|auth|email|password)\.[A-Za-z.]+)'/g)) asked.add(key);
  }
  assert.ok(asked.size > 50, 'the scripts are translated at all');
  for (const key of asked) assert.ok(Object.hasOwn(STRINGS.da, key), key);
  // The keys built from a template.
  for (const key of ['tier.free', 'tier.basic', 'tier.premium', 'saved.election', 'saved.electionAlready',
    'saved.forecast', 'saved.forecastAlready']) {
    assert.ok(Object.hasOwn(STRINGS.da, key), key);
  }
});

test('a Danish browser gets Danish, any other English, and a saved choice wins', () => {
  assert.equal(detectLanguage({ browser: 'da-DK' }), 'da');
  assert.equal(detectLanguage({ browser: 'da' }), 'da');
  assert.equal(detectLanguage({ browser: 'en-US' }), 'en');
  assert.equal(detectLanguage({ browser: 'de-DE' }), 'en');
  assert.equal(detectLanguage({}), 'en');
  assert.equal(detectLanguage({ saved: 'da', browser: 'en-GB' }), 'da');
  assert.equal(detectLanguage({ saved: 'en', browser: 'da-DK' }), 'en');
  assert.equal(detectLanguage({ saved: 'fr', browser: 'da-DK' }), 'da', 'an unknown choice is ignored');
});

test('a text nobody wrote shows as its key rather than breaking the page', () => {
  assert.equal(t('no.such.text'), 'no.such.text');
});

test('the calculator speaks English', () => {
  setLanguage('en', { remember: false });
  const byId = {};
  const makeEl = () => ({
    className: '', textContent: '', style: {}, children: [], onclick: null,
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
  });
  for (const id of ['title', 'subtitle', 'party-list', 'bar', 'total', 'total-of', 'verdict', 'footer-note']) {
    byId[id] = makeEl();
  }
  globalThis.document = { title: '', getElementById: (id) => byId[id], createElement: makeEl };

  mountCoalitionCalculator(toElection({
    nation: 'Germany',
    state: null,
    election_date: '2025-02-23',
    title: 'Bundestag 2025',
    source_url: 'https://en.wikipedia.org/wiki/2025_German_federal_election',
    total_seats: 10,
    majority_seats: 6,
    blocks: [{ name: 'Bundestag', parties: [
      { name: 'Alternative for Germany', abbr: 'AfD', seats: 6, color: '#009EE0' },
      { name: 'The Left', abbr: 'Linke', seats: 4, color: '#BE3075' },
    ] }],
  }));

  assert.equal(byId.subtitle.textContent, 'Pick parties and see whether together they reach a majority (6+ of 10 seats)');
  assert.equal(byId['total-of'].textContent, 'of 10 seats');
  assert.equal(byId['footer-note'].textContent, 'A majority needs 6 seats · Final result, 23 February 2025');
  assert.equal(byId.verdict.textContent, '6 short');
});

test('a chosen language is remembered, and a test can choose without leaving a trace', () => {
  const items = new Map();
  const saved = globalThis.localStorage;
  globalThis.localStorage = { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, String(v)) };
  try {
    assert.equal(setLanguage('da'), 'da');
    assert.equal(items.get('koalitionsberegner.language'), 'da');
    assert.equal(t('reset'), 'Ryd alle');
    setLanguage('en', { remember: false });
    assert.equal(items.get('koalitionsberegner.language'), 'da');
    assert.equal(t('reset'), 'Clear all');
    assert.equal(setLanguage('fr', { remember: false }), 'en', 'an unknown language falls back to English');
  } finally {
    globalThis.localStorage = saved;
  }
});
