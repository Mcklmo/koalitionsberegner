/**
 * Output safety (#10): extracted strings reach the DOM as text and nothing else.
 *
 * The election used here is what an agent would return for
 * `test/adversarial/markup-in-names.html` — party names that are a `<script>`
 * tag, an `onerror` handler and a `javascript:` link. The backend stores those
 * verbatim (they are wrong names, not markup), so the renderer is the thing
 * that has to keep them inert.
 *
 * The DOM stub below refuses any write of markup: `innerHTML` may only be
 * assigned the empty string, and no parsing sink exists at all. A renderer that
 * builds HTML by concatenation fails these tests instead of shipping.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { mountCoalitionCalculator } from '../js/app.js';
import { mountImportUi } from '../js/import-ui.js';
import { validateElection, ElectionValidationError } from '../js/election.js';

const SCRIPT_NAME = "<script>alert('xss')</script>";
const IMG_NAME = '<img src=x onerror="fetch(\'//evil.example\')">';
const LINK_NAME = '<a href="javascript:alert(1)">Free Party</a>';

const markupElection = validateElection({
  nation: '<b>Markupland</b>',
  state: null,
  electionDate: '2026-05-03',
  title: '<script>document.cookie</script>',
  sourceUrl: 'https://markupland.example/result',
  totalSeats: 15,
  majoritySeats: 8,
  blocks: [
    {
      name: '<iframe src="//evil.example"></iframe>',
      parties: [
        { name: SCRIPT_NAME, abbr: 'SCR', seats: 5, color: '#C0392B' },
        { name: IMG_NAME, abbr: 'IMG', seats: 4, color: '#2980B9' },
        { name: LINK_NAME, abbr: 'JS', seats: 2, color: '#1AA037' },
        { name: 'Ordinary Party', abbr: 'OP', seats: 4, color: '#8E44AD' },
      ],
    },
  ],
});

/** A node that accepts text and rejects markup. Any HTML sink is simply absent. */
function node(tag = 'div') {
  return {
    tag,
    value: '',
    textContent: '',
    className: '',
    hidden: false,
    disabled: false,
    style: {},
    children: [],
    handlers: {},
    onclick: null,
    get options() { return this.children; },
    set innerHTML(value) {
      if (value !== '') throw new Error(`markup written to innerHTML: ${value}`);
      this.children = [];
    },
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

/** Every string that ended up somewhere on screen. */
function allText(root) {
  return descendants(root).filter((n) => n.textContent).map((n) => n.textContent);
}

/** Every node below `root`, in creation order. */
function descendants(root) {
  const out = [];
  const walk = (n) => {
    out.push(n);
    n.children.forEach(walk);
  };
  root.children.forEach(walk);
  return out;
}

function mountCalculator(election) {
  const byId = {};
  for (const id of ['title', 'subtitle', 'party-list', 'bar', 'total', 'total-of', 'verdict', 'footer-note']) {
    byId[id] = node();
  }
  globalThis.document = { title: '', getElementById: (id) => byId[id], createElement: node };
  mountCoalitionCalculator(election);
  return byId;
}

test('party names that are markup render as text, unchanged and unparsed', () => {
  const byId = mountCalculator(markupElection);
  const names = allText(byId['party-list']);

  // Present verbatim: nothing was stripped, and nothing became an element.
  assert.ok(names.includes(SCRIPT_NAME), 'the script-tag name is shown as text');
  assert.ok(names.includes(IMG_NAME));
  assert.ok(names.includes(LINK_NAME));
  assert.ok(names.includes('<iframe src="//evil.example"></iframe>'), 'block heading too');
  // The stub throws on any markup write, so reaching here is the guarantee:
  // every one of those strings went through textContent, once each.
  assert.equal(names.filter((t) => t === SCRIPT_NAME).length, 1);
  assert.equal(descendants(byId['party-list']).filter((n) => n.className === 'party-row').length, 4);
});

test('the election title reaches the page and the tab as text', () => {
  const byId = mountCalculator(markupElection);
  assert.equal(byId['title'].textContent, '<script>document.cookie</script>');
  assert.equal(document.title, '<script>document.cookie</script>');
});

test('the only thing a party can colour is the dot, and only with a hex colour', () => {
  const byId = mountCalculator(markupElection);
  const dots = descendants(byId['party-list']).filter((c) => c.className === 'dot');

  assert.equal(dots.length, 4);
  for (const dot of dots) {
    assert.match(dot.style.background, /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
    assert.deepEqual(Object.keys(dot.style), ['background'], 'no other style is set');
  }
});

test('a colour that tries to escape the style property is rejected before rendering', () => {
  assert.throws(
    () => validateElection({
      ...markupElection,
      blocks: [{
        name: 'Assembly',
        parties: [{ name: 'P', abbr: 'P', seats: 15, color: 'red;background:url(//evil.example)' }],
      }],
    }),
    ElectionValidationError,
  );
});

test('invisible and direction-changing characters are rejected, joiners are not', () => {
  const withOverride = (name) => ({
    ...markupElection,
    blocks: [{ name: 'Assembly', parties: [{ name, abbr: 'OP', seats: 15, color: '#8E44AD' }] }],
  });

  for (const char of ['‮', '⁦', '​', '﻿', '­']) {
    assert.throws(
      () => validateElection(withOverride(`Ordinary${char} Party`)),
      /invisible or direction-changing/,
      `U+${char.codePointAt(0).toString(16).toUpperCase()} must not reach the DOM`,
    );
  }
  assert.doesNotThrow(() => validateElection(withOverride('م‌لی')), 'ZWNJ is legitimate text');
});

test('the import preview shows an adversarial extraction as text', async () => {
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
  globalThis.document = { createElement: node };
  const api = {
    async listElections() { return []; },
    async lookup() { return { pageKey: 'p1', status: 'unknown', election: null, electionHash: null }; },
    async importElection() {
      return { pageKey: 'p1', status: 'preview', election: markupElection, electionHash: 'h' };
    },
    async confirm() { return { pageKey: 'p1', status: 'ready', election: markupElection, electionHash: 'h' }; },
    async discardPreview() {},
    async getElection() { return { status: 'ready', election: markupElection }; },
  };
  mountImportUi({ api, elements: el, bundled: markupElection, onSelect() {} });

  el.sourceUrl.value = 'https://markupland.example/result';
  await el.form.dispatch('submit');

  assert.equal(el.preview.hidden, false, 'the user sees it before anything is saved');
  assert.equal(el.previewTitle.textContent, '<script>document.cookie</script>');
  assert.ok(allText(el.previewList).some((t) => t.includes(SCRIPT_NAME)));
});

test('an error message from the backend is shown as text, never as markup', async () => {
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
  globalThis.document = { createElement: node };
  const failure = "the extracted results are not valid: title: <img src=x onerror='alert(1)'>";
  const api = {
    async listElections() { return []; },
    async lookup() { return { pageKey: 'p1', status: 'unknown', election: null, electionHash: null }; },
    async importElection() { return { pageKey: 'p1', status: 'failed', error: failure, election: null }; },
    async confirm() { throw new Error('not reached'); },
    async discardPreview() {},
    async getElection() { throw new Error('not reached'); },
  };
  mountImportUi({ api, elements: el, bundled: markupElection, onSelect() {} });

  el.sourceUrl.value = 'https://markupland.example/result';
  await el.form.dispatch('submit');

  assert.ok(el.message.textContent.includes(failure));
});

// --- keeping it that way ----------------------------------------------------

/** Every source file the browser loads. */
function frontendSources() {
  const root = fileURLToPath(new URL('../js/', import.meta.url));
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.js'))
    .map((entry) => {
      const path = `${entry.parentPath ?? entry.path}/${entry.name}`;
      return { path: path.slice(root.length - 3), source: readFileSync(path, 'utf8') };
    });
}

test('no frontend source parses a string into markup', () => {
  // If a feature ever needs one of these, it needs a sanitiser and a rethink of
  // doc/threat-model.md first — which is what failing here is for.
  const sinks = [
    /\.outerHTML\s*=/,
    /insertAdjacentHTML/,
    /document\.write/,
    /\beval\s*\(/,
    /new\s+Function\s*\(/,
    /\bsrcdoc\b/,
    /\.setAttribute\(\s*['"]on/,
  ];
  for (const { path, source } of frontendSources()) {
    for (const sink of sinks) {
      assert.doesNotMatch(source, sink, `${path} uses a markup-parsing sink: ${sink}`);
    }
  }
});

test('innerHTML is only ever used to clear a container', () => {
  const assignment = /\.innerHTML\s*=\s*([^;\n]+)/g;
  for (const { path, source } of frontendSources()) {
    for (const [, rhs] of source.matchAll(assignment)) {
      assert.match(rhs.trim(), /^(''|""|``)$/, `${path} assigns ${rhs.trim()} to innerHTML`);
    }
  }
});
