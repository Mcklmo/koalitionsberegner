import test from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient } from '../js/api.js';
import { mountImportUi } from '../js/import-ui.js';
import { validateElection } from '../js/election.js';
import { setLanguage, t } from '../js/i18n.js';
import './strings.mjs';

// These tests read the Danish wording.
setLanguage('da', { remember: false });

// Asking without a year: the server names the previous election and, when it
// is less than a year off, the next one; the user picks, and the pick is an
// ordinary import with that election's year.

const wireCandidates = [
  { which: 'upcoming', year: 2027, election_date: '2027-06-30', title: 'Next Danish general election', nation: 'Denmark', state: null },
  { which: 'previous', year: 2022, election_date: '2022-11-01', title: 'Danish general election', nation: 'Denmark', state: null },
];

// --- the client -------------------------------------------------------------

function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method ?? 'GET', body: options.body });
    const next = queue.shift();
    return { ok: next.status < 400, status: next.status, statusText: 'x', json: async () => next.body };
  };
  return { fetchImpl, calls };
}

test('a yearless import sends no year, and the elections to pick come back in camelCase', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'pick', candidates: wireCandidates } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).importElection({ year: null, nation: 'Danmark' });

  assert.equal(JSON.parse(calls[0].body).year, null);
  assert.equal(result.status, 'pick');
  assert.deepEqual(result.candidates[1], {
    which: 'previous', year: 2022, electionDate: '2022-11-01',
    title: 'Danish general election', nation: 'Denmark', state: null,
  });
});

test('a yearless lookup leaves the year out of the query', async () => {
  const { fetchImpl, calls } = fakeFetch([{ status: 200, body: { request_key: 'r1', state: 'unknown' } }]);
  await createApiClient({ fetch: fetchImpl }).lookup({ year: null, nation: 'Danmark', subnation: null });
  assert.equal(calls[0].url, '/api/elections/lookup?nation=Danmark');
});

test('a candidate not shaped like one is dropped, not shown', async () => {
  const { fetchImpl } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'pick', candidates: [
      { ...wireCandidates[0], which: 'someday' },
      { ...wireCandidates[0], year: '2027' },
      { ...wireCandidates[0], election_date: 'soon' },
      { ...wireCandidates[0], nation: '' },
      'Denmark 2027',
      wireCandidates[1],
    ] } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).importElection({ nation: 'Danmark' });
  assert.deepEqual(result.candidates.map((c) => c.which), ['previous']);
});

// --- the page -----------------------------------------------------------------

const held = validateElection({
  nation: 'Denmark',
  electionDate: '2022-11-01',
  title: 'Danish general election',
  sourceUrl: 'https://www.dst.dk/valg',
  totalSeats: 10,
  majoritySeats: 6,
  blocks: [
    { name: 'Left', parties: [{ name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' }] },
    { name: 'Right', parties: [{ name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' }] },
  ],
});

const candidates = wireCandidates.map((c) => ({
  which: c.which, year: c.year, electionDate: c.election_date, title: c.title, nation: c.nation, state: c.state,
}));

function node(tag = 'div') {
  return {
    tag,
    value: '',
    textContent: '',
    className: '',
    hidden: false,
    disabled: false,
    children: [],
    handlers: {},
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(child) { this.children.push(child); return child; },
    append(...cs) { this.children.push(...cs); },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = { preventDefault() {} }) {
      return Promise.all((this.handlers[type] ?? []).map((fn) => fn(event)));
    },
    reset() { this.value = ''; },
  };
}

/** Answers each yearless import with `pick`, and each yeared one with a preview. */
function fakeApi({ pick = candidates, lookup = 'unknown' } = {}) {
  const calls = { lookup: [], importElection: [], discardPreview: [] };
  const api = {
    async listElections() { return []; },
    async lookup(values) {
      calls.lookup.push(values);
      if (values.year === null && lookup === 'pick') {
        return { requestKey: 'latest', status: 'pick', candidates: pick };
      }
      return { requestKey: 'k', status: 'unknown', election: null };
    },
    async importElection(values) {
      calls.importElection.push(values);
      if (values.year === null) return { requestKey: 'latest', status: 'pick', candidates: pick };
      return { requestKey: `k${values.year}`, electionHash: 'h', status: 'preview', election: held };
    },
    async discardPreview(requestKey) { calls.discardPreview.push(requestKey); },
  };
  return { api, calls };
}

function harness(api) {
  globalThis.document = { createElement: node };
  const el = {
    form: node('form'), year: node('input'), nation: node('input'), subnation: node('input'),
    submit: node('button'), message: node(), availability: node(),
    fieldErrors: { year: node(), nation: node() },
    preview: node(), previewTitle: node(), previewMeta: node(), previewSource: node(),
    previewList: node(), confirm: node('button'), discard: node('button'),
    choices: node(), choicesTitle: node(), choicesHint: node(), choicesList: node(),
    choicesDiscard: node('button'),
  };
  const ui = mountImportUi({ api, elements: el, config: { importsEnabled: true }, onSelect: () => {} });
  ui.setAdmin(true);
  return el;
}

async function askForTheLatest(el) {
  el.year.value = '';
  el.nation.value = 'Danmark';
  await el.form.dispatch('submit');
}

test('with no year and a near next election, the user picks between its polls and the last result', async () => {
  const { api, calls } = fakeApi();
  const el = harness(api);

  await askForTheLatest(el);

  assert.deepEqual(calls.importElection, [{ year: null, nation: 'Danmark', subnation: null }]);
  assert.equal(el.choices.hidden, false);
  assert.equal(el.choicesHint.hidden, true, 'the hint about picking a poll is not this question');
  assert.equal(el.message.textContent, t('pick.message'));
  const labels = el.choicesList.children.map((button) => button.children.map((s) => s.textContent));
  assert.deepEqual(labels, [
    [t('pick.next', { date: '30. juni 2027' }), 'Next Danish general election'],
    [t('pick.previous', { date: '1. november 2022' }), 'Danish general election'],
  ]);
  assert.equal(el.preview.hidden, true, 'nothing is read until the user picks');
});

test('picking the previous election imports it with its year, as if typed', async () => {
  const { api, calls } = fakeApi();
  const el = harness(api);
  await askForTheLatest(el);

  await el.choicesList.children[1].dispatch('click');

  assert.deepEqual(calls.importElection.at(-1), { year: 2022, nation: 'Denmark', subnation: null });
  assert.deepEqual(calls.lookup.at(-1), { year: 2022, nation: 'Denmark', subnation: null },
    'looked up first, so an election already stored is not read again');
  assert.deepEqual([el.year.value, el.nation.value], ['2022', 'Denmark']);
  assert.equal(el.choices.hidden, true);
  assert.equal(el.preview.hidden, false);
});

test('picking the next election asks for its year, which the server answers with polls', async () => {
  const { api, calls } = fakeApi();
  const el = harness(api);
  await askForTheLatest(el);

  await el.choicesList.children[0].dispatch('click');

  assert.equal(calls.importElection.at(-1).year, 2027);
});

test('with only the previous election to offer, the page picks it by itself', async () => {
  const { api, calls } = fakeApi({ pick: [candidates[1]] });
  const el = harness(api);

  await askForTheLatest(el);

  assert.deepEqual(calls.importElection.map((c) => c.year), [null, 2022]);
  assert.equal(el.choices.hidden, true);
  assert.equal(el.preview.hidden, false);
});

test('a pick somebody just looked up is offered again without a new import', async () => {
  const { api, calls } = fakeApi({ lookup: 'pick' });
  const el = harness(api);

  await askForTheLatest(el);

  assert.equal(calls.importElection.length, 0);
  assert.equal(el.choicesList.children.length, 2);
});

test('discarding the pick drops it on the server too', async () => {
  const { api, calls } = fakeApi();
  const el = harness(api);
  await askForTheLatest(el);

  await el.choicesDiscard.dispatch('click');

  assert.deepEqual(calls.discardPreview, ['latest']);
  assert.equal(el.choices.hidden, true);
});

test('a later list of polls shows its hint again', async () => {
  const { api } = fakeApi();
  api.importElection = async (values) => (values.year === null
    ? { requestKey: 'latest', status: 'pick', candidates }
    : { requestKey: 'k', status: 'choose', forecasts: [validateElection({
      ...held, electionDate: '2027-06-30',
      forecast: { publisher: 'Voxmeter', publishedOn: '2026-09-07', computed: false },
    })] });
  const el = harness(api);
  await askForTheLatest(el);

  await el.choicesList.children[0].dispatch('click');

  assert.equal(el.choicesHint.hidden, false);
});
