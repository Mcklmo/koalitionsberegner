import test from 'node:test';
import assert from 'node:assert/strict';
import { MAX_YEAR, MIN_YEAR, parseYear, validateImportForm } from '../js/import-form.js';
import { setLanguage } from '../js/i18n.js';
import './strings.mjs';

// These tests read the Danish wording.
setLanguage('da', { remember: false });

test('accepts a year and a country, and trims them', () => {
  const result = validateImportForm({ year: ' 2026 ', nation: '  Danmark  ' });
  assert.ok(result.valid);
  assert.deepEqual(result.errors, {});
  assert.equal(result.values.year, 2026);
  assert.equal(result.values.nation, 'Danmark');
  assert.equal(result.values.subnation, null, 'no region means the national election');
});

test('a region is kept when one is given', () => {
  const { values } = validateImportForm({
    year: 2026, nation: 'Germany', subnation: ' Sachsen-Anhalt ',
  });
  assert.equal(values.subnation, 'Sachsen-Anhalt');
});

test('the form collects a year, a nation and a region, and nothing else', () => {
  const { values } = validateImportForm({ year: 2026, nation: 'Danmark' });
  assert.deepEqual(Object.keys(values), ['year', 'nation', 'subnation']);
});

test('extra input is ignored rather than submitted', () => {
  const { values } = validateImportForm({
    year: 2026, nation: 'Danmark', sourceUrl: 'https://x.org',
  });
  assert.equal(values.sourceUrl, undefined, 'which page is read is not the user’s to choose');
});

test('a misspelled place is accepted — reading it is the server’s job', () => {
  const spelled = ['Germny', 'sachen-anhalt', 'DANMARK', 'Österreich'];
  for (const nation of spelled) {
    const { valid, values } = validateImportForm({ year: 2026, nation });
    assert.ok(valid, `${nation} should be submitted, not refused`);
    assert.equal(values.nation, nation, 'and passed on exactly as typed');
  }
});

test('a year typed with a slip of the finger is read as what it means', () => {
  for (const year of ['2026', '2o26', '2O26', ' 2026 ']) {
    assert.equal(parseYear(year), 2026, year);
  }
});

for (const [name, year] of [
  ['empty', ''],
  ['whitespace only', '   '],
  ['words', 'sometime'],
  ['too many digits', '20226'],
  ['before the range', String(MIN_YEAR - 1)],
  ['after the range', String(MAX_YEAR + 1)],
  ['a decimal', '2026.5'],
]) {
  test(`rejects year: ${name}`, () => {
    const { valid, errors } = validateImportForm({ year, nation: 'Danmark' });
    assert.equal(valid, false);
    assert.ok(errors.year);
    assert.equal(errors.nation, undefined, 'and says nothing about the country');
  });
}

test('rejects a missing country', () => {
  const { valid, errors } = validateImportForm({ year: 2026, nation: '   ' });
  assert.equal(valid, false);
  assert.ok(errors.nation);
});
