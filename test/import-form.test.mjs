import test from 'node:test';
import assert from 'node:assert/strict';
import { validateImportForm } from '../js/import-form.js';

test('accepts a URL and trims it', () => {
  const result = validateImportForm({ sourceUrl: '  https://www.dst.dk/valg  ' });
  assert.ok(result.valid);
  assert.deepEqual(result.errors, {});
  assert.equal(result.values.sourceUrl, 'https://www.dst.dk/valg');
});

test('the URL is the only thing the form collects', () => {
  const { values } = validateImportForm({ sourceUrl: 'https://www.dst.dk/valg' });
  assert.deepEqual(Object.keys(values), ['sourceUrl'],
    'nation, region and date are inferred from the page, not typed');
});

test('extra input is ignored rather than submitted', () => {
  const { values } = validateImportForm({ sourceUrl: 'https://x.org', nation: 'Danmark' });
  assert.equal(values.nation, undefined);
});

for (const [name, sourceUrl] of [
  ['empty', ''],
  ['whitespace only', '   '],
  ['not a url', 'dst.dk/valg'],
  ['javascript scheme', 'javascript:alert(1)'],
  ['data scheme', 'data:text/html,<script>x</script>'],
  ['no host', 'https://'],
]) {
  test(`rejects source url: ${name}`, () => {
    const { valid, errors } = validateImportForm({ sourceUrl });
    assert.equal(valid, false);
    assert.ok(errors.sourceUrl);
  });
}
