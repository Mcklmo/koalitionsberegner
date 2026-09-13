/**
 * The page speaks every language strings.csv has a column for. The sheet must
 * parse, anything malformed in it must stop the page rather than slip through,
 * every text the markup or the scripts ask for must exist, and the calculator
 * must actually come out in English for a visitor who is not Danish.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import './strings.mjs';
import { detectLanguage, languageName, languages, parseStrings, setLanguage, t } from '../js/i18n.js';
import { mountCoalitionCalculator } from '../js/app.js';
import { toElection } from '../js/api.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const shipped = parseStrings(read('../js/strings.csv'));

/** Whether every language in the shipped sheet has `key`. */
const everywhere = (key) => languages().every((locale) => key in shipped.strings[locale]);

test('the shipped sheet parses, with Danish and English, each naming itself', () => {
  assert.deepEqual(languages().slice().sort(), ['da', 'en']);
  assert.equal(languageName('da'), 'Dansk');
  assert.equal(languageName('en'), 'English');
});

test('every text the markup asks for exists', () => {
  const html = read('../index.html');
  const keys = [...html.matchAll(/data-i18n(?:-placeholder|-title)?="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(keys.length > 20, 'the markup is translated at all');
  for (const key of keys) assert.ok(everywhere(key), key);
});

test('the privacy section reads in Danish before the scripts run, word for word', () => {
  const inline = [...read('../index.html').matchAll(/data-i18n="(privacy\.[^"]+)">([^<]*)</g)];
  assert.ok(inline.length >= 10, 'the paragraphs carry their text');
  for (const [, key, text] of inline) assert.equal(text, shipped.strings.da[key](), key);
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
  for (const key of asked) assert.ok(everywhere(key), key);
  // The keys built from a template.
  for (const key of ['tier.free', 'tier.basic', 'tier.premium', 'saved.election', 'saved.electionAlready',
    'saved.forecast', 'saved.forecastAlready', 'date', ...Array.from({ length: 12 }, (_, i) => `month.${i + 1}`)]) {
    assert.ok(everywhere(key), key);
  }
});

test('a sheet reads quoted commas, doubled quotes, line breaks in a cell, CRLF and blank lines', () => {
  const { locales, strings } = parseStrings(
    'key,da,en\r\n\r\ngreet,"Hej, {name}","Say ""hi"", {name}"\r\ntwo,"en\nto",one two\r\n',
  );
  assert.deepEqual(locales, ['da', 'en']);
  assert.equal(strings.da.greet({ name: 'Bo' }), 'Hej, Bo');
  assert.equal(strings.en.greet({ name: 'Bo' }), 'Say "hi", Bo');
  assert.equal(strings.da.two(), 'en\nto');
});

test('a plural follows each language\'s own rules', () => {
  const { strings } = parseStrings('key,da,en\nn,"{n} {n, plural, one {stol} other {stole}}","{n, plural, one {a chair} other {# chairs}}"');
  assert.equal(strings.da.n({ n: 1 }), '1 stol');
  assert.equal(strings.da.n({ n: 0 }), '0 stole');
  assert.equal(strings.en.n({ n: 1 }), 'a chair');
  assert.equal(strings.en.n({ n: 3 }), '# chairs', 'a branch is plain text');

  setLanguage('en', { remember: false });
  assert.equal(t('seats', { count: 1 }), '1 seat');
  assert.equal(t('seats', { count: 2 }), '2 seats');
  setLanguage('da', { remember: false });
  assert.equal(t('import.remaining', { remaining: 1 }), '1 import tilbage denne måned.');
  assert.equal(t('import.remaining', { remaining: 3 }), '3 importer tilbage denne måned.');
});

for (const [what, sheet, error] of [
  ['an empty sheet', '', /line 1: the sheet is empty/],
  ['a quote that is never closed', 'key,da,en\na,"x,y', /line 2: a quote that is never closed/],
  ['text after a closing quote', 'key,da,en\na,"x"y,z', /line 2: text after a closing quote/],
  ['a quote inside an unquoted cell', 'key,da,en\na,x"y,z', /line 2: a quote inside an unquoted cell/],
  ['a carriage return on its own', 'key,da,en\ra,x,y', /line 1: a carriage return without a line feed/],
  ['a row that is too short', 'key,da,en\na,x', /line 2: 2 cells where the header has 3/],
  ['a row that is too long', 'key,da,en\na,x,y,z', /line 2: 4 cells where the header has 3/],
  ['a line break in a cell miscounting later lines', 'key,da,en\na,"x\ny",z\nb,x', /line 4: 2 cells/],
  ['a header not starting with "key"', 'id,da,en\na,x,y', /line 1: the first column must be headed "key"/],
  ['a column with no locale', 'key,da,,en\na,x,y,z', /line 1: a column with no locale code/],
  ['a locale twice', 'key,en,en\na,x,y', /line 1: the locale "en" appears twice/],
  ['something that is not a locale', 'key,da_DK!,en\na,x,y', /line 1: "da_DK!" is not a locale code/],
  ['no English', 'key,da,de\na,x,y', /line 1: the sheet needs a "en" column/],
  ['a row with no key', 'key,da,en\n,x,y', /line 2: a row with no key/],
  ['a key twice', 'key,da,en\na,x,y\na,x,y', /line 3: the key "a" appears twice/],
  ['a missing translation', 'key,da,en\na,x,', /line 2: "a" has no en text/],
  ['a "}" with no "{"', 'key,da,en\na,x},y', /line 2: "a" in da, column 2: a "}" with no "{"/],
  ['a "{" that is not a placeholder', 'key,da,en\na,x,{y', /line 2: "a" in en, column 1: a "\{" that is neither/],
  ['a plural with no "other"', 'key,da,en\na,"{n, plural, one {x}}",y', /no "other"/],
  ['an unknown plural category', 'key,da,en\na,"{n, plural, single {x} other {y}}",y', /"single" is not a plural category/],
  ['a plural category twice', 'key,da,en\na,"{n, plural, one {x} one {y} other {z}}",y', /"one" is given twice/],
  ['a plural that is not closed', 'key,da,en\na,"{n, plural, other {x}",y', /the plural for "n" is not closed properly/],
]) {
  test(`a sheet with ${what} is refused`, () => {
    assert.throws(() => parseStrings(sheet), error);
  });
}

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

test('a language added to the sheet is picked for its browsers', () => {
  assert.equal(detectLanguage({ browser: 'de-AT', languages: ['da', 'en', 'de'] }), 'de');
  assert.equal(detectLanguage({ browser: 'pt-BR', languages: ['pt-PT', 'pt-BR', 'en'] }), 'pt-BR');
  assert.equal(detectLanguage({ browser: 'pt', languages: ['pt-BR', 'en'] }), 'pt-BR');
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
