import test from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient, toElection } from '../js/api.js';
import { validateElection, ElectionValidationError, isValidatedElection } from '../js/election.js';
import { mountImportUi } from '../js/import-ui.js';
import { setLanguage } from '../js/i18n.js';

// These tests read the Danish wording.
setLanguage('da', { remember: false });

const forecastInput = (forecast = {}) => ({
  nation: 'Denmark',
  electionDate: '2027-10-31',
  title: 'Next Danish general election — Voxmeter',
  sourceUrl: 'https://polls.example/dk',
  totalSeats: 10,
  majoritySeats: 6,
  blocks: [{ name: 'Folketing', parties: [
    { name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' },
    { name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' },
  ] }],
  forecast: { publisher: 'Voxmeter', publishedOn: '2026-09-07', computed: false, ...forecast },
});

function errorsFor(input) {
  try {
    validateElection(input);
  } catch (err) {
    assert.ok(err instanceof ElectionValidationError);
    return err.errors.join('\n');
  }
  assert.fail('expected validation to fail');
}

// --- the schema -------------------------------------------------------------

test('a forecast is validated and frozen with the election', () => {
  const election = validateElection(forecastInput({ computed: true }));
  assert.deepEqual(election.forecast, { publisher: 'Voxmeter', publishedOn: '2026-09-07', computed: true });
  assert.ok(Object.isFrozen(election.forecast));
});

test('a forecast is refused when it smuggles a field, lies about its type, or postdates the vote', () => {
  assert.match(errorsFor({ ...forecastInput(), forecast: { ...forecastInput().forecast, href: 'x' } }), /unexpected field "href"/);
  assert.match(errorsFor(forecastInput({ computed: 'yes' })), /computed: expected a boolean/);
  assert.match(errorsFor(forecastInput({ publisher: 'Vox‮meter' })), /direction-changing/);
  assert.match(errorsFor(forecastInput({ publishedOn: '2027-11-01' })), /after the election/);
  assert.match(errorsFor({ ...forecastInput(), forecast: 'Voxmeter' }), /forecast: expected an object/);
});

// --- the client -------------------------------------------------------------

const wire = {
  nation: 'Denmark', state: null, election_date: '2027-10-31',
  title: 'Next — Voxmeter', source_url: 'https://polls.example/dk',
  total_seats: 10, majority_seats: 6,
  blocks: forecastInput().blocks,
  forecast: { publisher: 'Voxmeter', published_on: '2026-09-07', computed: true },
};

function fakeFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method ?? 'GET' });
    const next = queue.shift();
    return { ok: next.status < 400, status: next.status, statusText: 'x', json: async () => next.body };
  };
  return { fetchImpl, calls };
}

test('a forecast crosses the API boundary in camelCase', () => {
  assert.deepEqual(toElection(wire).forecast, { publisher: 'Voxmeter', publishedOn: '2026-09-07', computed: true });
});

test('an offered list is mapped, and every forecast in it validated', async () => {
  const { fetchImpl } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'choose', election: null, forecasts: [wire, wire] } },
  ]);
  const result = await createApiClient({ fetch: fetchImpl }).importElection({ year: 2027, nation: 'Danmark' });

  assert.equal(result.status, 'choose');
  assert.equal(result.forecasts.length, 2);
  assert.ok(result.forecasts.every(isValidatedElection));
});

test('a forecast in the list that fails validation fails the whole answer', async () => {
  const { fetchImpl } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'choose', forecasts: [{ ...wire, total_seats: 11 }] } },
  ]);
  await assert.rejects(
    createApiClient({ fetch: fetchImpl }).importElection({ year: 2027, nation: 'Danmark' }),
    /seats sum to 10/
  );
});

test('confirming a forecast names the option, including the first one', async () => {
  const { fetchImpl, calls } = fakeFetch([
    { status: 200, body: { request_key: 'r1', state: 'ready', election: wire } },
    { status: 200, body: { request_key: 'r1', state: 'ready', election: wire } },
  ]);
  const api = createApiClient({ fetch: fetchImpl });
  await api.confirm('r1', { option: 0 });
  await api.confirm('r1', { option: 3 });

  assert.equal(calls[0].url, '/api/elections/imports/r1/confirm?option=0');
  assert.equal(calls[1].url, '/api/elections/imports/r1/confirm?option=3');
});

// --- the page ---------------------------------------------------------------

function node(tag = 'div') {
  return {
    tag, value: '', textContent: '', className: '', hidden: false, disabled: false, type: '',
    children: [], handlers: {},
    get options() { return this.children; },
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

const stated = validateElection(forecastInput());
const computed = validateElection(forecastInput({ publisher: 'Epinion', publishedOn: '2026-08-30', computed: true }));
const CHOOSE = { requestKey: 'r1', electionHash: null, status: 'choose', election: null, forecasts: [stated, computed] };

function harness(overrides = {}) {
  globalThis.document = { createElement: node };
  const calls = { importElection: 0, confirm: [], discardPreview: 0 };
  const api = {
    async listElections() { return []; },
    async lookup() { return overrides.lookup ?? { requestKey: 'r1', status: 'unknown', election: null }; },
    async importElection() { calls.importElection++; return CHOOSE; },
    async confirm(requestKey, options) {
      calls.confirm.push({ requestKey, options });
      return { requestKey, electionHash: 'h', status: 'ready', election: computed, duplicate: false };
    },
    async discardPreview() { calls.discardPreview++; },
    async getElection() { return { election: stated }; },
  };
  const el = Object.fromEntries([
    'form', 'year', 'nation', 'subnation', 'submit', 'message', 'availability', 'preview',
    'previewTitle', 'previewMeta', 'previewSource', 'previewList', 'confirm', 'discard',
    'choices', 'choicesTitle', 'choicesList', 'choicesDiscard', 'picker', 'pickerRow',
    'curate', 'curateRow',
  ].map((name) => [name, node()]));
  el.fieldErrors = { year: node(), nation: node() };
  el.year.value = '2027';
  el.nation.value = 'Danmark';
  const selected = [];
  const ui = mountImportUi({ api, elements: el, bundled: stated, onSelect: (e) => selected.push(e) });
  ui.setAccount({ tier: 'basic', limit: 10, remaining: 5, unlimited: false, mayImport: true });
  return { el, calls, selected };
}

test('an election not yet held is offered as a list of polls, computed ones marked', async () => {
  const { el, calls } = harness();
  await el.form.dispatch('submit');

  assert.equal(el.choices.hidden, false);
  assert.equal(el.preview.hidden, true, 'nothing is previewed until one is chosen');
  const [first, second] = el.choicesList.children;
  assert.match(first.children[0].textContent, /Voxmeter · 2026-09-07/);
  assert.doesNotMatch(first.children[1].textContent, /beregnet/);
  assert.match(second.children[1].textContent, /beregnet ud fra stemmeandele/);
  assert.match(el.message.textContent, /ikke afholdt endnu/);
  assert.equal(calls.confirm.length, 0);
});

test('a list somebody already read is shown without paying for an import', async () => {
  const { el, calls } = harness({ lookup: CHOOSE });
  await el.form.dispatch('submit');

  assert.equal(calls.importElection, 0);
  assert.equal(el.choicesList.children.length, 2);
});

test('choosing a poll previews it, says the seats were computed, and saves it by option', async () => {
  const { el, calls, selected } = harness();
  await el.form.dispatch('submit');

  await el.choicesList.children[1].dispatch('click');
  assert.equal(el.preview.hidden, false);
  assert.equal(el.choices.hidden, true);
  assert.match(el.previewMeta.textContent, /Prognose fra Epinion · 2026-08-30/);
  assert.match(el.previewSource.textContent, /beregnet ud fra dem og er et skøn/);

  await el.confirm.dispatch('click');
  assert.deepEqual(calls.confirm, [{ requestKey: 'r1', options: { option: 1 } }]);
  assert.match(el.message.textContent, /Prognosen er gemt/);
  assert.deepEqual(selected, [computed]);
});

test('stepping back from a chosen poll returns to the list and throws nothing away', async () => {
  const { el, calls } = harness();
  await el.form.dispatch('submit');
  await el.choicesList.children[0].dispatch('click');
  assert.equal(el.discard.textContent, 'Tilbage til listen');

  await el.discard.dispatch('click');

  assert.equal(el.preview.hidden, true);
  assert.equal(el.choices.hidden, false);
  assert.equal(calls.discardPreview, 0);
});

test('discarding the list tells the backend and saves nothing', async () => {
  const { el, calls } = harness();
  await el.form.dispatch('submit');

  await el.choicesDiscard.dispatch('click');

  assert.equal(el.choices.hidden, true);
  assert.equal(calls.discardPreview, 1);
  assert.equal(calls.confirm.length, 0);
});
