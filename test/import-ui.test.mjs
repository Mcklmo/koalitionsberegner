import test from 'node:test';
import assert from 'node:assert/strict';
import { mountImportUi } from '../js/import-ui.js';
import { validateElection } from '../js/election.js';

const election = validateElection({
  nation: 'Danmark',
  electionDate: '2026-03-25',
  title: 'Folketing 2026',
  sourceUrl: 'https://www.dst.dk/valg',
  totalSeats: 10,
  majoritySeats: 6,
  blocks: [
    { name: 'Left', parties: [{ name: 'Left Party', abbr: 'L', seats: 6, color: '#C0392B' }] },
    { name: 'Right', parties: [{ name: 'Right Party', abbr: 'R', seats: 4, color: '#2980B9' }] },
  ],
});

const bundled = validateElection({ ...election, title: 'Bundled election' });

// --- a DOM stub covering exactly what import-ui touches ---------------------
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

function harness({ api, selected = [] }) {
  globalThis.document = { createElement: node };
  const el = {
    form: node('form'),
    sourceUrl: node('input'),
    submit: node('button'),
    message: node(),
    fieldErrors: { sourceUrl: node() },
    preview: node(),
    previewTitle: node(),
    previewMeta: node(),
    previewList: node(),
    confirm: node('button'),
    discard: node('button'),
    picker: node('select'),
    pickerRow: node(),
  };
  const ui = mountImportUi({ api, elements: el, bundled, onSelect: (e) => selected.push(e) });
  return { el, ui, selected };
}

function fillValidForm(el) {
  el.sourceUrl.value = 'https://www.dst.dk/valg';
}

/** An API double that counts calls and returns queued results. */
function fakeApi(overrides = {}) {
  const calls = { lookup: 0, importElection: 0, confirm: 0, discardPreview: 0, getElection: 0, listElections: 0 };
  const api = {
    async listElections() { calls.listElections++; return overrides.summaries ?? []; },
    async lookup() { calls.lookup++; return overrides.lookup ?? { pageKey: 'p1', electionHash: null, status: 'unknown', election: null }; },
    async importElection() { calls.importElection++; return overrides.importElection ?? { pageKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false }; },
    async confirm(pageKey) { calls.confirm++; calls.confirmedWith = pageKey; return overrides.confirm ?? { pageKey: 'p1', electionHash: 'h', status: 'ready', election, duplicate: false }; },
    async discardPreview(pageKey) { calls.discardPreview++; calls.discardedWith = pageKey; },
    async getElection() { calls.getElection++; return overrides.getElection ?? { electionHash: 'h', status: 'ready', election }; },
  };
  return { api, calls };
}

// --- acceptance criteria ---------------------------------------------------

test('an invalid URL is never submitted for extraction', async () => {
  const { api, calls } = fakeApi();
  const { el } = harness({ api });
  el.sourceUrl.value = 'javascript:alert(1)';

  await el.form.dispatch('submit');

  assert.equal(calls.lookup, 0);
  assert.equal(calls.importElection, 0);
  assert.ok(el.fieldErrors.sourceUrl.textContent, 'the bad URL is reported');
});

test('a page imported before is blocked with a message before any extraction', async () => {
  const { api, calls } = fakeApi({
    lookup: { pageKey: 'p1', electionHash: 'dup', status: 'ready', election },
    summaries: [{ electionHash: 'dup', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 10 }],
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(calls.lookup, 1);
  assert.equal(calls.importElection, 0, 'extraction must not run for a known page');
  assert.match(el.message.textContent, /allerede importeret/);
  assert.equal(el.message.className, 'msg msg-warn');
});

test('an extraction is previewed and nothing is saved yet', async () => {
  const { api, calls } = fakeApi();
  const { el, selected } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(calls.importElection, 1);
  assert.equal(calls.confirm, 0, 'nothing is saved without confirmation');
  assert.equal(el.preview.hidden, false);
  assert.equal(el.previewTitle.textContent, 'Folketing 2026');
  assert.match(el.previewMeta.textContent, /10 mandater/);
  assert.equal(el.previewList.children.length, 2, 'every extracted party is shown');
  assert.deepEqual(selected, [], 'the renderer is untouched until the user confirms');
});

test('the preview shows the identity the agent inferred, for the user to check', () => {
  // The user typed only a URL, so this line is the agent's claim about which
  // election the page describes — the thing confirmation actually approves.
  const { api } = fakeApi();
  const { el } = harness({ api });
  fillValidForm(el);

  return el.form.dispatch('submit').then(() => {
    assert.match(el.previewMeta.textContent, /Danmark/);
    assert.match(el.previewMeta.textContent, /2026-03-25/);
    assert.match(el.message.textContent, /identificeret rigtigt/);
  });
});

test('confirming saves the election and renders it', async () => {
  const { api, calls } = fakeApi();
  const { el, selected } = harness({ api });
  fillValidForm(el);
  await el.form.dispatch('submit');

  await el.confirm.dispatch('click');

  assert.equal(calls.confirm, 1);
  assert.equal(calls.confirmedWith, 'p1', 'confirmation targets the previewed page');
  assert.equal(el.preview.hidden, true);
  assert.match(el.message.textContent, /gemt/);
  assert.deepEqual(selected, [election], 'the saved election becomes the rendered one');
});

test('discarding saves nothing and tells the backend to drop the preview', async () => {
  const { api, calls } = fakeApi();
  const { el, selected } = harness({ api });
  fillValidForm(el);
  await el.form.dispatch('submit');

  await el.discard.dispatch('click');

  assert.equal(calls.confirm, 0);
  assert.equal(calls.discardPreview, 1);
  assert.equal(el.preview.hidden, true);
  assert.deepEqual(selected, []);
});

test('a failed extraction is reported and nothing is previewed', async () => {
  const { api, calls } = fakeApi({
    importElection: { pageKey: 'p1', electionHash: null, status: 'failed', election: null, error: 'kunne ikke læses' },
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(el.preview.hidden, true);
  assert.equal(calls.confirm, 0);
  assert.match(el.message.textContent, /kunne ikke læses/);
  assert.equal(el.message.className, 'msg msg-error');
});

test('the picker lists the bundled election plus everything stored', async () => {
  const { api } = fakeApi({
    summaries: [
      { electionHash: 'a', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179 },
      { electionHash: 'b', nation: 'Danmark', state: 'Nordjylland', electionDate: '2026-03-25', title: 'T', totalSeats: 71 },
    ],
  });
  const { el, ui } = harness({ api });

  await ui.start();

  assert.equal(el.pickerRow.hidden, false);
  assert.deepEqual(el.picker.children.map((o) => o.value), ['local', 'a', 'b']);
  assert.match(el.picker.children[2].textContent, /Nordjylland/);
});

test('choosing a stored election re-renders it', async () => {
  const { api, calls } = fakeApi({
    summaries: [{ electionHash: 'a', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 10 }],
  });
  const { el, ui, selected } = harness({ api });
  await ui.start();

  el.picker.value = 'a';
  await el.picker.dispatch('change');

  assert.equal(calls.getElection, 1);
  assert.deepEqual(selected, [election]);
});

test('choosing the bundled election needs no backend call', async () => {
  const { api, calls } = fakeApi();
  const { el, ui, selected } = harness({ api });
  await ui.start();

  el.picker.value = 'local';
  await el.picker.dispatch('change');

  assert.equal(calls.getElection, 0);
  assert.deepEqual(selected, [bundled]);
});

test('an unreachable backend leaves the bundled election usable', async () => {
  const api = {
    async listElections() { throw new Error('connection refused'); },
  };
  const { el, ui } = harness({ api });

  await ui.start();

  assert.match(el.message.textContent, /kan ikke nås/);
  assert.equal(el.pickerRow.hidden, true, 'no picker when there is nothing to pick');
});
