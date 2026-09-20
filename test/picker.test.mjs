/**
 * The front door: search, grouping, keyboard nav, the default election and
 * the "ask for it"/"import it" fallback (plan 3, Section B).
 *
 * `groupSummaries`, `matchesQuery`, `pickDefaultElection` and
 * `parseQueryAsRequest` are pure and tested directly. `mountPicker` is tested
 * against the same DOM stub style as test/import-ui.test.mjs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  filterGroups, groupSummaries, LOCAL_VALUE, matchesQuery,
  mountPicker, parseQueryAsRequest, pickDefaultElection,
} from '../js/picker.js';
import { setLanguage } from '../js/i18n.js';
import './strings.mjs';

// These tests read the Danish wording.
setLanguage('da', { remember: false });

const poll = (over) => ({
  electionHash: 'h', electionKey: 'k', nation: 'Danmark', state: null,
  electionDate: '2026-03-25', title: 'T', totalSeats: 179, forecast: null, ...over,
});

// --- groupSummaries ----------------------------------------------------------

test('every summary sharing an electionKey becomes one group', () => {
  const groups = groupSummaries([
    poll({ electionHash: 'a', electionKey: 'k1' }),
    poll({ electionHash: 'b', electionKey: 'k2' }),
    poll({ electionHash: 'c', electionKey: 'k1' }),
  ]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.find((g) => g.key === 'k1').primary.electionHash, 'a');
  assert.deepEqual(groups.find((g) => g.key === 'k1').older.map((e) => e.electionHash), ['c']);
});

test('a result leads a group even when it arrived after its polls', () => {
  const groups = groupSummaries([
    poll({ electionHash: 'poll1', electionKey: 'k', forecast: { publisher: 'Voxmeter', publishedOn: '2026-03-01' } }),
    poll({ electionHash: 'result', electionKey: 'k', forecast: null }),
  ]);
  assert.equal(groups[0].primary.electionHash, 'result');
  assert.deepEqual(groups[0].older.map((e) => e.electionHash), ['poll1']);
});

test('among polls of the same election, the newest publishedOn leads', () => {
  const groups = groupSummaries([
    poll({ electionHash: 'old', electionKey: 'k', forecast: { publisher: 'A', publishedOn: '2026-01-01' } }),
    poll({ electionHash: 'new', electionKey: 'k', forecast: { publisher: 'B', publishedOn: '2026-03-01' } }),
  ]);
  assert.equal(groups[0].primary.electionHash, 'new');
  assert.deepEqual(groups[0].older.map((e) => e.electionHash), ['old']);
});

test('a summary with no electionKey falls back to grouping by its own hash', () => {
  const groups = groupSummaries([poll({ electionHash: 'a', electionKey: undefined })]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, 'a');
});

// --- matchesQuery / filterGroups ---------------------------------------------

test('a group matches on nation, state, title, year and a poll\'s publisher', () => {
  const group = { key: 'k', primary: poll({ nation: 'Deutschland', state: 'Sachsen-Anhalt', title: 'Landtag' }), older: [] };
  assert.ok(matchesQuery(group, 'deutschland'));
  assert.ok(matchesQuery(group, 'sachsen-anhalt'));
  assert.ok(matchesQuery(group, 'landtag'));
  assert.ok(matchesQuery(group, '2026'));
  assert.ok(!matchesQuery(group, '2027'));
});

test('a search matches an older poll\'s publisher too', () => {
  const group = {
    key: 'k',
    primary: poll({ forecast: null }),
    older: [poll({ electionHash: 'p', forecast: { publisher: 'Voxmeter', publishedOn: '2026-01-01' } })],
  };
  assert.ok(matchesQuery(group, 'voxmeter'));
});

test('matching is accent- and case-insensitive', () => {
  const group = { key: 'k', primary: poll({ nation: 'Danmark', state: 'Ærø' }), older: [] };
  assert.ok(matchesQuery(group, 'ærø'));
  assert.ok(matchesQuery(group, 'ærø'.toUpperCase()));
  assert.ok(matchesQuery(group, 'DANMARK'));
});

test('every word of a multi-word query must match, in any field', () => {
  const group = { key: 'k', primary: poll({ nation: 'Deutschland', state: 'Sachsen-Anhalt' }), older: [] };
  assert.ok(matchesQuery(group, 'deutschland sachsen-anhalt'));
  assert.ok(!matchesQuery(group, 'deutschland sverige'));
});

test('an empty query matches everything', () => {
  const group = { key: 'k', primary: poll(), older: [] };
  assert.ok(matchesQuery(group, ''));
  assert.ok(matchesQuery(group, '   '));
});

test('filterGroups keeps only the groups that match, in order', () => {
  const groups = [
    { key: 'a', primary: poll({ nation: 'Danmark' }), older: [] },
    { key: 'b', primary: poll({ nation: 'Deutschland' }), older: [] },
  ];
  assert.deepEqual(filterGroups(groups, 'deutsch').map((g) => g.key), ['b']);
});

// --- pickDefaultElection ------------------------------------------------------

const NOW = new Date('2026-06-01T00:00:00Z');

test('the soonest upcoming election with a stored poll is the default', () => {
  const summaries = [
    poll({ electionHash: 'far', electionDate: '2026-12-01', forecast: { publisher: 'X', publishedOn: '2026-05-01' } }),
    poll({ electionHash: 'near', electionDate: '2026-07-01', forecast: { publisher: 'X', publishedOn: '2026-05-01' } }),
  ];
  assert.equal(pickDefaultElection(summaries, NOW).electionHash, 'near');
});

test('an upcoming election with no poll yet is not a candidate', () => {
  const summaries = [poll({ electionHash: 'bare', electionDate: '2026-07-01', forecast: null })];
  assert.equal(pickDefaultElection(summaries, NOW), null);
});

test('failing an upcoming poll, the most recent past result is the default', () => {
  const summaries = [
    poll({ electionHash: 'old', electionDate: '2020-01-01', forecast: null }),
    poll({ electionHash: 'recent', electionDate: '2024-01-01', forecast: null }),
  ];
  assert.equal(pickDefaultElection(summaries, NOW).electionHash, 'recent');
});

test('nothing upcoming and nothing past yields no default', () => {
  assert.equal(pickDefaultElection([], NOW), null);
});

// --- parseQueryAsRequest -------------------------------------------------------

test('a year and a single-word place are pulled out of free text', () => {
  assert.deepEqual(parseQueryAsRequest('Danmark 2026'), { year: 2026, nation: 'Danmark', subnation: null });
  assert.deepEqual(parseQueryAsRequest('2026 Danmark'), { year: 2026, nation: 'Danmark', subnation: null });
});

test('a comma splits the place into a nation and a region', () => {
  assert.deepEqual(
    parseQueryAsRequest('Deutschland, Sachsen-Anhalt 2021'),
    { year: 2021, nation: 'Deutschland', subnation: 'Sachsen-Anhalt' },
  );
});

test('no year found leaves it null rather than guessing', () => {
  assert.deepEqual(parseQueryAsRequest('Danmark'), { year: null, nation: 'Danmark', subnation: null });
});

test('an empty query parses to nothing, for validateImportForm to reject', () => {
  assert.deepEqual(parseQueryAsRequest(''), { year: null, nation: '', subnation: null });
});

// --- mountPicker (DOM) --------------------------------------------------------

function node(tag = 'div') {
  const self = {
    tag,
    value: '',
    textContent: '',
    className: '',
    hidden: true,
    attrs: {},
    children: [],
    handlers: {},
    // Bound to `self`, not to the `classList` object itself, so `add` lands
    // on the node's own `className` the way a real element's would.
    classList: {
      add(name) {
        const names = self.className ? self.className.split(' ') : [];
        self.className = [...new Set([...names, name])].join(' ');
      },
    },
    setAttribute(name, value) { self.attrs[name] = value; },
    getAttribute(name) { return self.attrs[name]; },
    set innerHTML(v) { if (v === '') self.children = []; },
    get innerHTML() { return ''; },
    appendChild(child) { self.children.push(child); return child; },
    append(...cs) { self.children.push(...cs); },
    addEventListener(type, fn) { (self.handlers[type] ||= []).push(fn); },
    async dispatch(type, event = { preventDefault() {} }) {
      for (const fn of self.handlers[type] ?? []) await fn(event);
    },
  };
  return self;
}

const bundled = { title: 'Bundled election', nation: 'Danmark', state: null, electionDate: '2026-03-25' };

function harness({ summaries = [], bundled: bundledOverride = bundled, ...rest } = {}) {
  globalThis.document = { createElement: node };
  const el = { row: node(), input: node('input'), results: node('ul') };
  const selected = [];
  const asked = [];
  const api = {
    async listElections() { return summaries; },
    async getElection(hash) {
      const summary = summaries.find((s) => s.electionHash === hash);
      return { election: summary ? { ...summary, title: summary.title } : null, electionHash: hash };
    },
  };
  const picker = mountPicker({
    elements: el,
    bundled: bundledOverride,
    onSelect: (election, hash) => selected.push([election, hash]),
    onAskForIt: async (values) => { asked.push(values); return { valid: true }; },
    ...rest,
  });
  return { el, api, picker, selected, asked };
}

test('the bundled election alone hides the search row', async () => {
  const { el, api, picker } = harness({ summaries: [] });
  await picker.refresh(api);
  assert.equal(el.row.hidden, true, 'nothing to search but the one election already on screen');
});

test('a single stored election alongside the bundled one shows the search row', async () => {
  const { el, api, picker } = harness({ summaries: [poll({ electionHash: 'a' })] });
  await picker.refresh(api);
  assert.equal(el.row.hidden, false, 'two things to choose between is worth searching');
});

test('more than one distinct election shows the search row', async () => {
  const { el, api, picker } = harness({
    summaries: [poll({ electionHash: 'a', electionKey: 'k1' }), poll({ electionHash: 'b', electionKey: 'k2' })],
  });
  await picker.refresh(api);
  assert.equal(el.row.hidden, false);
});

test('focusing the input opens the list with every group as a row', async () => {
  const { el, api, picker } = harness({
    summaries: [
      poll({ electionHash: 'a', electionKey: 'k1', nation: 'Danmark' }),
      poll({ electionHash: 'b', electionKey: 'k2', nation: 'Deutschland' }),
    ],
  });
  await picker.refresh(api);
  await el.input.dispatch('focus');
  assert.equal(el.results.hidden, false);
  // Two groups plus the bundled election.
  assert.equal(el.results.children.length, 3);
});

test('typing narrows the list to matching groups', async () => {
  const { el, api, picker } = harness({
    summaries: [
      poll({ electionHash: 'a', electionKey: 'k1', nation: 'Danmark' }),
      poll({ electionHash: 'b', electionKey: 'k2', nation: 'Deutschland' }),
    ],
  });
  await picker.refresh(api);
  el.input.value = 'deutsch';
  await el.input.dispatch('input');
  assert.equal(el.results.children.length, 1);
  assert.match(el.results.children[0].textContent, /Deutschland/);
});

test('arrow keys move the active row and enter chooses it', async () => {
  const { el, api, picker, selected } = harness({
    summaries: [
      poll({ electionHash: 'a', electionKey: 'k1', nation: 'Danmark' }),
      poll({ electionHash: 'b', electionKey: 'k2', nation: 'Deutschland' }),
    ],
  });
  await picker.refresh(api);
  await el.input.dispatch('focus');
  await el.input.dispatch('keydown', { key: 'ArrowDown', preventDefault() {} });
  assert.ok(el.results.children[1].className.includes('active'), 'the second row is now active');

  await el.input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
  assert.equal(selected.length, 1);
  assert.equal(selected[0][1], 'b', 'the active row (Deutschland) was chosen');
});

test('choosing a stored group fetches the full election', async () => {
  const { el, api, picker, selected } = harness({ summaries: [poll({ electionHash: 'a', electionKey: 'k1' })] });
  await picker.refresh(api);
  await el.input.dispatch('focus');
  await el.results.children[0].handlers.click[0]();
  assert.equal(selected.length, 1);
  assert.equal(selected[0][1], 'a');
});

test('choosing the bundled row needs no fetch', async () => {
  const { el, picker, selected, api } = harness({ summaries: [] });
  await picker.refresh(api);
  await el.input.dispatch('focus');
  const bundledRow = el.results.children.find((row) => row.textContent === 'Bundled election');
  await bundledRow.handlers.click[0]();
  assert.deepEqual(selected, [[bundled, null]]);
});

test('older polls stay reachable one click deeper (plan 3, D9)', async () => {
  const { el, api, picker, selected } = harness({
    summaries: [
      poll({ electionHash: 'result', electionKey: 'k1', forecast: null }),
      poll({ electionHash: 'poll1', electionKey: 'k1', forecast: { publisher: 'Voxmeter', publishedOn: '2026-01-01' } }),
    ],
  });
  await picker.refresh(api);
  await el.input.dispatch('focus');
  // The group's row, its "older polls" toggle, then the bundled election.
  assert.equal(el.results.children.length, 3);
  const toggle = el.results.children[1];
  assert.match(toggle.textContent, /1/);
  await toggle.handlers.click[0]();
  const olderRow = el.results.children.find((row) => row !== toggle && row.textContent.includes('Voxmeter'));
  assert.ok(olderRow, 'the older poll is now a row of its own');
  await olderRow.handlers.click[0]();
  assert.equal(selected[0][1], 'poll1');
});

test('no match, and nobody may import or request, says so plainly', async () => {
  const { el, api, picker } = harness({ summaries: [poll({ electionHash: 'a' })] });
  await picker.refresh(api);
  el.input.value = 'nowhere';
  await el.input.dispatch('input');
  assert.match(el.results.children[0].textContent, /matcher/i);
});

test('no match, and requesting is allowed, offers "ask for it"', async () => {
  const { el, api, picker, asked } = harness({
    summaries: [poll({ electionHash: 'a' })],
    canRequestElection: () => true,
  });
  await picker.refresh(api);
  el.input.value = 'Sachsen-Anhalt 2021';
  await el.input.dispatch('input');
  assert.match(el.results.children[0].textContent, /Spørg efter det/);

  await el.results.children[0].handlers.click[0]();
  assert.deepEqual(asked, [{ year: 2021, nation: 'Sachsen-Anhalt', subnation: null }]);
  assert.match(el.results.children[0].textContent, /Bedt om/);
});

test('no match, and importing is allowed, offers "import it" instead', async () => {
  const { el, api, picker } = harness({
    summaries: [poll({ electionHash: 'a' })],
    canImport: () => true,
    canRequestElection: () => true,
  });
  await picker.refresh(api);
  el.input.value = 'nowhere 2030';
  await el.input.dispatch('input');
  assert.match(el.results.children[0].textContent, /Importér det/);
});
