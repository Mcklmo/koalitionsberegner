import test from 'node:test';
import assert from 'node:assert/strict';
import { validateImportForm } from '../js/import-form.js';

const valid = {
  sourceUrl: 'https://www.dst.dk/valg',
  nation: 'Danmark',
  state: '',
  electionDate: '2026-03-25',
};

test('accepts a complete form and trims its values', () => {
  const result = validateImportForm({ ...valid, nation: '  Danmark  ' });
  assert.ok(result.valid);
  assert.deepEqual(result.errors, {});
  assert.equal(result.values.nation, 'Danmark');
});

test('state is optional and normalises to null', () => {
  assert.equal(validateImportForm(valid).values.state, null);
  assert.equal(validateImportForm({ ...valid, state: ' Nordjylland ' }).values.state, 'Nordjylland');
});

test('every required field is reported at once', () => {
  const { valid: ok, errors } = validateImportForm({});
  assert.equal(ok, false);
  assert.deepEqual(Object.keys(errors).sort(), ['electionDate', 'nation', 'sourceUrl']);
});

for (const [name, sourceUrl] of [
  ['empty', ''],
  ['not a url', 'dst.dk/valg'],
  ['javascript scheme', 'javascript:alert(1)'],
  ['data scheme', 'data:text/html,<script>x</script>'],
  ['no host', 'https://'],
]) {
  test(`rejects source url: ${name}`, () => {
    const { valid: ok, errors } = validateImportForm({ ...valid, sourceUrl });
    assert.equal(ok, false);
    assert.ok(errors.sourceUrl);
  });
}

for (const [name, electionDate] of [
  ['empty', ''],
  ['wrong format', '25/03/2026'],
  ['impossible day', '2026-02-30'],
  ['month 13', '2026-13-01'],
]) {
  test(`rejects election date: ${name}`, () => {
    const { valid: ok, errors } = validateImportForm({ ...valid, electionDate });
    assert.equal(ok, false);
    assert.ok(errors.electionDate);
  });
}

test('a valid form produces no network-facing surprises', () => {
  const { values } = validateImportForm(valid);
  assert.deepEqual(values, {
    sourceUrl: 'https://www.dst.dk/valg',
    nation: 'Danmark',
    state: null,
    electionDate: '2026-03-25',
  });
});
