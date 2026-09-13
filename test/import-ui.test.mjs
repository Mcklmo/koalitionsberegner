import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../js/api.js';
import { mountImportUi } from '../js/import-ui.js';
import { validateElection } from '../js/election.js';

const election = validateElection({
  nation: 'Danmark',
  electionDate: '2026-03-25',
  title: 'Denmark — 2026',
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

const SUBSCRIBER = {
  tier: 'basic', limit: 10, remaining: 7, used: 3, unlimited: false, mayImport: true,
};

function harness({ api, selected = [], account = SUBSCRIBER, config = {} } = {}) {
  globalThis.document = { createElement: node };
  const el = {
    form: node('form'),
    year: node('input'),
    nation: node('input'),
    subnation: node('input'),
    submit: node('button'),
    message: node(),
    availability: node(),
    fieldErrors: { year: node(), nation: node() },
    preview: node(),
    previewTitle: node(),
    previewMeta: node(),
    previewSource: node(),
    previewList: node(),
    confirm: node('button'),
    discard: node('button'),
    choices: node(),
    choicesTitle: node(),
    choicesList: node(),
    choicesDiscard: node('button'),
    picker: node('select'),
    pickerRow: node(),
    curate: node('input'),
    curateRow: node(),
  };
  const imported = [];
  const ui = mountImportUi({
    api,
    elements: el,
    bundled,
    config,
    onSelect: (e) => selected.push(e),
    onImported: () => imported.push(true),
  });
  // Most cases are about the import flow, not the paywall, so the harness
  // starts from an account that may import unless a test says otherwise.
  if (account !== undefined) ui.setAccount(account);
  return { el, ui, selected, imported };
}

function fillValidForm(el) {
  el.year.value = '2026';
  el.nation.value = 'Danmark';
}

/** An API double that counts calls and returns queued results. */
function fakeApi(overrides = {}) {
  const calls = { lookup: 0, importElection: 0, getImport: 0, confirm: 0, discardPreview: 0, getElection: 0, listElections: 0, requestElection: 0 };
  const api = {
    async listElections() { calls.listElections++; return overrides.summaries ?? []; },
    async lookup() { calls.lookup++; return overrides.lookup ?? { requestKey: 'p1', electionHash: null, status: 'unknown', election: null }; },
    async importElection() { calls.importElection++; return overrides.importElection ?? { requestKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false }; },
    async requestElection(values) {
      calls.requestElection++;
      calls.requestedWith = values;
      if (overrides.requestElection instanceof Error) throw overrides.requestElection;
      return overrides.requestElection ?? { url: 'https://github.test/issues/12', number: 12, duplicate: false };
    },
    async getImport(requestKey) { calls.getImport++; calls.polledWith = requestKey; return overrides.getImport ?? { requestKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false }; },
    async confirm(requestKey) { calls.confirm++; calls.confirmedWith = requestKey; return overrides.confirm ?? { requestKey: 'p1', electionHash: 'h', status: 'ready', election, duplicate: false }; },
    async discardPreview(requestKey) { calls.discardPreview++; calls.discardedWith = requestKey; },
    async getElection() { calls.getElection++; return overrides.getElection ?? { electionHash: 'h', status: 'ready', election }; },
  };
  return { api, calls };
}

// --- acceptance criteria ---------------------------------------------------

test('an incomplete request is never submitted for import', async () => {
  const { api, calls } = fakeApi();
  const { el } = harness({ api });
  el.year.value = 'sometime';
  el.nation.value = '';

  await el.form.dispatch('submit');

  assert.equal(calls.lookup, 0);
  assert.equal(calls.importElection, 0);
  assert.ok(el.fieldErrors.year.textContent, 'the year is reported');
  assert.ok(el.fieldErrors.nation.textContent, 'and so is the missing country');
});

test('a misspelled place is submitted rather than refused', async () => {
  const { api, calls } = fakeApi();
  const { el } = harness({ api });
  el.year.value = '2o26';
  el.nation.value = 'Danmrak';

  await el.form.dispatch('submit');

  assert.equal(calls.importElection, 1, 'reading it is the server’s job');
});

test('an election imported before is blocked with a message before any import', async () => {
  const { api, calls } = fakeApi({
    lookup: { requestKey: 'p1', electionHash: 'dup', status: 'ready', election },
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
  assert.equal(el.previewTitle.textContent, 'Denmark — 2026');
  assert.match(el.previewMeta.textContent, /10 mandater/);
  assert.equal(el.previewList.children.length, 2, 'every extracted party is shown');
  assert.deepEqual(selected, [], 'the renderer is untouched until the user confirms');
});

test('the preview names the page the numbers were read from', async () => {
  // Nobody chose that page: the server searched for it. It is the only way to
  // check the numbers against their source, so it is always shown.
  const elsewhere = validateElection({ ...election, sourceUrl: 'https://www.dst.dk/valg/mandater' });
  const { api } = fakeApi({
    importElection: { requestKey: 'r1', electionHash: 'h', status: 'preview', election: elsewhere, reused: false },
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(el.previewSource.hidden, false);
  assert.match(el.previewSource.textContent, /https:\/\/www\.dst\.dk\/valg\/mandater/);
});

test('the preview shows which election this turned out to be, for the user to check', () => {
  // The user typed only a URL, so this line is the agent's claim about which
  // election the page describes — the thing confirmation actually approves.
  const { api } = fakeApi();
  const { el } = harness({ api });
  fillValidForm(el);

  return el.form.dispatch('submit').then(() => {
    assert.match(el.previewMeta.textContent, /Danmark/);
    assert.match(el.previewMeta.textContent, /2026-03-25/);
    assert.match(el.message.textContent, /det rigtige valg/);
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
    importElection: { requestKey: 'p1', electionHash: null, status: 'failed', election: null, error: 'kunne ikke læses' },
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(el.preview.hidden, true);
  assert.equal(calls.confirm, 0);
  assert.match(el.message.textContent, /kunne ikke læses/);
  assert.equal(el.message.className, 'msg msg-error');
});

/** An importElection that stays open until the test settles it. */
function heldImport(api, calls) {
  const held = {};
  api.importElection = () => {
    calls.importElection++;
    return new Promise((resolve) => { held.finish = resolve; });
  };
  return held;
}

const settled = () => new Promise((resolve) => setImmediate(resolve));

test('a second submit while one is under way starts nothing', async () => {
  const { api, calls } = fakeApi();
  const held = heldImport(api, calls);
  const { el } = harness({ api });
  fillValidForm(el);

  const first = el.form.dispatch('submit');
  await settled();
  await el.form.dispatch('submit');
  held.finish({ requestKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false });
  await first;

  assert.equal(calls.lookup, 1);
  assert.equal(calls.importElection, 1);
  assert.equal(el.preview.hidden, false, 'the first import’s preview is still shown');
});

test('re-reading the account mid-import does not re-enable the button', async () => {
  const { api, calls } = fakeApi();
  const held = heldImport(api, calls);
  const { el, ui } = harness({ api });
  fillValidForm(el);

  const running = el.form.dispatch('submit');
  await settled();
  await ui.setAccount(SUBSCRIBER);
  assert.equal(el.submit.disabled, true);

  held.finish({ requestKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false });
  await running;
  assert.equal(el.submit.disabled, false);
});

test('an import still running is waited on, not paid for again', async () => {
  const { api, calls } = fakeApi({
    lookup: { requestKey: 'p1', electionHash: null, status: 'pending', election: null },
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(calls.getImport, 1);
  assert.equal(calls.polledWith, 'p1');
  assert.equal(calls.importElection, 0, 'joining the running import must not reserve quota');
  assert.equal(el.preview.hidden, false);
});

test('a preview left behind is shown again without a new import', async () => {
  const { api, calls } = fakeApi({
    lookup: { requestKey: 'p1', electionHash: 'h', status: 'preview', election, reused: false },
  });
  const { el } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(calls.importElection, 0);
  assert.equal(calls.getImport, 0);
  assert.equal(el.preview.hidden, false);

  await el.confirm.dispatch('click');
  assert.equal(calls.confirmedWith, 'p1', 'and it can still be saved');
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

test('the picker lists elections alphabetically', async () => {
  const { api } = fakeApi({
    summaries: [
      { electionHash: 'se', nation: 'Sweden', state: null, electionDate: '2026-09-11', title: 'T', totalSeats: 349 },
      { electionHash: 'de', nation: 'Germany', state: null, electionDate: '2025-02-23', title: 'T', totalSeats: 630 },
      { electionHash: 'fi', nation: 'Finland', state: null, electionDate: '2023-04-02', title: 'T', totalSeats: 200 },
    ],
  });
  const { el, ui } = harness({ api });

  await ui.start();

  assert.deepEqual(el.picker.children.map((o) => o.value), ['local', 'fi', 'de', 'se']);
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


// --- what the account is allowed to do -------------------------------------

test('a signed-out visitor is told to sign in and cannot submit', async () => {
  const { api, calls } = fakeApi();
  const { el, ui } = harness({ api, account: undefined });

  await ui.setAccount(null);

  assert.equal(el.submit.disabled, true);
  assert.match(el.availability.textContent, /Log ind/);
  assert.equal(calls.importElection, 0);
});

test('a free account is pointed at a subscription rather than at a dead form', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api });

  await ui.setAccount({ tier: 'free', limit: 0, remaining: 0, unlimited: false, mayImport: false });

  assert.equal(el.submit.disabled, true);
  assert.match(el.availability.textContent, /kræver et abonnement/);
});

test('a subscriber with a spent quota is told when it comes back', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api });

  await ui.setAccount({ tier: 'basic', limit: 10, remaining: 0, unlimited: false, mayImport: false });

  assert.equal(el.submit.disabled, true);
  assert.match(el.availability.textContent, /brugt op/);
});

test('a subscriber sees what is left and can submit', async () => {
  const { api } = fakeApi();
  const { el } = harness({ api });

  assert.equal(el.submit.disabled, false);
  assert.match(el.availability.textContent, /7 importer tilbage/);
});

test('with gating off nothing is said about allowances', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api });

  await ui.setAccount({ tier: 'free', limit: -1, remaining: -1, unlimited: true, mayImport: true });

  assert.equal(el.submit.disabled, false);
  assert.equal(el.availability.textContent, '');
});

test('an import that started an extraction refreshes the allowance', async () => {
  const { api } = fakeApi();
  const { el, imported } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(imported.length, 1, 'the extraction was charged for; re-read the account');
});

test('a refusal from the server is shown in the page language and re-reads the account', async () => {
  const { api } = fakeApi();
  api.importElection = async () => {
    throw new ApiError("this month's 10 imports are used up", 429);
  };
  const { el, imported } = harness({ api });
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.match(el.message.textContent, /brugt op/);
  assert.equal(el.message.className, 'msg msg-error');
  assert.equal(imported.length, 1);
});

test('signing in reloads the picker, because a visitor saw only the selection', async () => {
  const { api, calls } = fakeApi({
    summaries: [{ electionHash: 'a', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179 }],
  });
  const { ui, el } = harness({ api, account: undefined });
  await ui.start();
  const before = calls.listElections;

  await ui.setAccount(SUBSCRIBER);

  assert.equal(calls.listElections, before + 1);
  assert.deepEqual(el.picker.children.map((o) => o.value), ['local', 'a']);
});

// --- curation ----------------------------------------------------------------

const ADMIN = { tier: 'free', limit: -1, remaining: -1, unlimited: true, admin: true, mayImport: true };

const STORED = { electionHash: 'a', nation: 'Danmark', state: null, electionDate: '2026-03-25', title: 'T', totalSeats: 179, selected: false };

async function curating({ account = ADMIN, setSelected } = {}) {
  const { api, calls } = fakeApi({ summaries: [STORED] });
  calls.setSelected = [];
  api.setSelected = setSelected ?? (async (hash, selected) => {
    calls.setSelected.push([hash, selected]);
    return { ...STORED, selected };
  });
  const { el, ui } = harness({ api, account: undefined });
  await ui.setAccount(account);
  el.picker.value = 'a';
  await el.picker.dispatch('change');
  return { el, ui, calls };
}

test('an administrator can make the chosen election visible to signed-out visitors', async () => {
  const { el, calls } = await curating();
  assert.equal(el.curateRow.hidden, false);
  assert.equal(el.curate.checked, false);

  el.curate.checked = true;
  await el.curate.dispatch('change');

  assert.deepEqual(calls.setSelected, [['a', true]]);
  assert.equal(el.curate.checked, true);
  assert.equal(el.picker.value, 'a', 'the picker stays on the election just changed');
  assert.match(el.picker.children[1].textContent, /offentlig/);
  assert.equal(el.message.className, 'msg msg-ok');
});

test('nobody but an administrator is offered curation', async () => {
  const { el } = await curating({ account: SUBSCRIBER });
  assert.equal(el.curateRow.hidden, true);
});

test('the bundled election cannot be curated', async () => {
  const { el } = await curating();
  el.picker.value = 'local';
  await el.picker.dispatch('change');
  assert.equal(el.curateRow.hidden, true);
});

test('a refused curation puts the checkbox back and says why', async () => {
  const { el } = await curating({
    setSelected: async () => { throw new ApiError('this needs an administrator', 403); },
  });

  el.curate.checked = true;
  await el.curate.dispatch('change');

  assert.equal(el.curate.checked, false);
  assert.equal(el.curate.disabled, false);
  assert.match(el.message.textContent, /administrator/);
  assert.equal(el.message.className, 'msg msg-error');
});


// --- asking for an election, with no subscription to import it -------------

/** A confirmed account on a tier that buys no imports, where asking is on. */
const FREE_ACCOUNT = {
  tier: 'free', limit: 0, remaining: 0, unlimited: false, mayImport: false, emailVerified: true,
};

const asking = { requestsEnabled: true };

test('a free account is offered the form rather than a dead button', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api, config: asking });

  await ui.setAccount(FREE_ACCOUNT);

  assert.equal(el.submit.disabled, false, 'the button still does something');
  assert.match(el.availability.textContent, /ønske/, 'and says what');
});

test('submitting without a subscription writes the election down instead of importing it', async () => {
  const { api, calls } = fakeApi();
  const { el, ui, imported } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.equal(calls.importElection, 0, 'nothing was fetched or read');
  assert.equal(calls.lookup, 0, 'and the server answers 409 if it already has it');
  assert.deepEqual(calls.requestedWith, { year: 2026, nation: 'Danmark', subnation: null });
  assert.match(el.message.textContent, /#12/);
  assert.equal(el.message.className, 'msg msg-ok');
  assert.deepEqual(imported, [], 'no quota moved, so the account is not re-read');
  assert.equal(el.submit.disabled, false, 'and the form is usable again');
});

test('the filed request is linked, not just numbered', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  fillValidForm(el);

  await el.form.dispatch('submit');

  const [, link] = el.message.children;
  assert.equal(link.href, 'https://github.test/issues/12');
  assert.equal(link.rel, 'noreferrer noopener');
});

test('asking for something somebody already asked for says so', async () => {
  const { api } = fakeApi({
    requestElection: { url: 'https://github.test/issues/5', number: 5, duplicate: true },
  });
  const { el, ui } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.match(el.message.textContent, /allerede et ønske om \(#5\)/);
  assert.equal(el.message.className, 'msg msg-ok');
});

test('an election already imported is shown rather than wished for', async () => {
  const { api, calls } = fakeApi({
    requestElection: new ApiError('this election is already imported', 409),
    summaries: [{ electionHash: 'a', nation: 'Danmark', electionDate: '2026-03-25', title: 'T', totalSeats: 10, selected: false, forecast: null }],
  });
  const { el, ui } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.match(el.message.textContent, /allerede importeret/);
  assert.equal(el.message.className, 'msg msg-warn');
  assert.ok(calls.listElections > 0, 'the picker is refreshed so it can be chosen');
});

test('a refusal to file is reported in the page’s own words', async () => {
  const { api } = fakeApi({
    requestElection: new ApiError('election requests are not configured', 503),
  });
  const { el, ui } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  fillValidForm(el);

  await el.form.dispatch('submit');

  assert.match(el.message.textContent, /ikke tilgængelig/);
  assert.equal(el.message.className, 'msg msg-error');
  assert.equal(el.submit.disabled, false);
});

test('an incomplete form is not written down either', async () => {
  const { api, calls } = fakeApi();
  const { el, ui } = harness({ api, config: asking });
  await ui.setAccount(FREE_ACCOUNT);
  el.year.value = '';
  el.nation.value = '';

  await el.form.dispatch('submit');

  assert.equal(calls.requestElection, 0);
  assert.ok(el.fieldErrors.year.textContent);
});

test('a subscriber who ran out for the month waits rather than wishing', async () => {
  const { api, calls } = fakeApi();
  const { el, ui } = harness({ api, config: asking });

  await ui.setAccount({ tier: 'basic', limit: 10, remaining: 0, unlimited: false, mayImport: false, emailVerified: true });
  fillValidForm(el);
  await el.form.dispatch('submit');

  assert.equal(el.submit.disabled, true, 'they bought imports; the allowance comes back');
  assert.match(el.availability.textContent, /brugt op/);
  assert.equal(calls.requestElection, 0);
});

test('an account waiting on its confirmation link is not offered the wish either', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api, config: asking });

  await ui.setAccount({ ...FREE_ACCOUNT, emailVerified: false });

  assert.equal(el.submit.disabled, true, 'the endpoint would refuse it anyway');
});

test('a signed-out visitor is asked to sign in, since a wish needs an account', async () => {
  const { api, calls } = fakeApi();
  const { el, ui } = harness({ api, config: asking, account: undefined });

  await ui.setAccount(null);

  assert.equal(el.submit.disabled, true);
  assert.match(el.availability.textContent, /Log ind/);
  assert.equal(calls.requestElection, 0);
});

test('where the backend offers no tracker, a free account is pointed at a subscription', async () => {
  const { api } = fakeApi();
  const { el, ui } = harness({ api, config: { requestsEnabled: false } });

  await ui.setAccount(FREE_ACCOUNT);

  assert.equal(el.submit.disabled, true);
  assert.match(el.availability.textContent, /kræver et abonnement/);
});
