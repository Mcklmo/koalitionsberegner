import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ADMIN_SECRET_KEY, forgetSecret, mountAdmin, readSecret, saveSecret,
} from '../js/admin.js';
import { setLanguage } from '../js/i18n.js';
import './strings.mjs';

// These tests read the Danish wording.
setLanguage('da', { remember: false });

/** A sessionStorage double. */
function memoryStorage() {
  const data = new Map();
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
    removeItem: (key) => { data.delete(key); },
  };
}

/** Storage the browser refuses to hand over: every call throws. */
const blockedStorage = {
  getItem() { throw new Error('SecurityError'); },
  setItem() { throw new Error('SecurityError'); },
  removeItem() { throw new Error('SecurityError'); },
};

function node() {
  return {
    value: '',
    textContent: '',
    className: '',
    hidden: false,
    open: false,
    handlers: {},
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = { preventDefault() {} }) {
      return Promise.all((this.handlers[type] ?? []).map((fn) => fn(event)));
    },
  };
}

function harness({ hash = '#admin', storage = memoryStorage() } = {}) {
  const el = { panel: node(), form: node(), input: node(), forget: node(), message: node() };
  const changes = [];
  const location = { hash };
  const admin = mountAdmin({ elements: el, storage, location, onChange: (isAdmin) => changes.push(isAdmin) });
  return { el, admin, storage, changes, location };
}

// --- keeping the secret ------------------------------------------------------

test('the secret is kept in the session under one key, and forgotten again', () => {
  const storage = memoryStorage();

  assert.equal(readSecret(storage), null);
  assert.equal(saveSecret('s3cret', storage), true);
  assert.equal(storage.data.get(ADMIN_SECRET_KEY), 's3cret');
  assert.equal(ADMIN_SECRET_KEY, 'koalitionsberegner.adminSecret');
  assert.equal(readSecret(storage), 's3cret');

  forgetSecret(storage);
  assert.equal(readSecret(storage), null);
});

test('storage the browser refuses reads as no secret rather than breaking the page', () => {
  assert.equal(readSecret(blockedStorage), null);
  assert.equal(saveSecret('s3cret', blockedStorage), false);
  assert.doesNotThrow(() => forgetSecret(blockedStorage));
  assert.equal(readSecret(null), null);
  assert.equal(saveSecret('s3cret', null), false);
});

// --- the form ---------------------------------------------------------------

test('the form is hidden unless the address asks for it', () => {
  const { el } = harness({ hash: '' });

  assert.equal(el.form.hidden, true);
  assert.equal(el.panel.open, false, 'the import panel stays as it was');
});

test('#admin opens the import panel and the form', () => {
  const { el } = harness();

  assert.equal(el.form.hidden, false);
  assert.equal(el.panel.open, true);
  assert.equal(el.forget.hidden, true, 'nothing to forget yet');
});

test('saving keeps the secret, clears the field and switches the page to the owner', async () => {
  const { el, storage, changes } = harness();
  el.input.value = '  s3cret  ';

  await el.form.dispatch('submit');

  assert.equal(readSecret(storage), 's3cret');
  assert.equal(el.input.value, '', 'the secret does not linger on screen');
  assert.deepEqual(changes, [true]);
  assert.equal(el.forget.hidden, false);
  assert.equal(el.message.className, 'msg msg-ok');
});

test('an empty field saves nothing', async () => {
  const { el, storage, changes } = harness();
  el.input.value = '   ';

  await el.form.dispatch('submit');

  assert.equal(readSecret(storage), null);
  assert.deepEqual(changes, []);
});

test('where storage is refused the page says so and stays a visitor', async () => {
  const { el, changes } = harness({ storage: blockedStorage });
  el.input.value = 's3cret';

  await el.form.dispatch('submit');

  assert.deepEqual(changes, []);
  assert.match(el.message.textContent, /vil ikke gemme nøglen/);
  assert.equal(el.message.className, 'msg msg-error');
});

test('forgetting drops the secret and switches the page back', async () => {
  const storage = memoryStorage();
  saveSecret('s3cret', storage);
  const { el, changes } = harness({ storage });
  assert.equal(el.forget.hidden, false);

  await el.forget.dispatch('click');

  assert.equal(readSecret(storage), null);
  assert.deepEqual(changes, [false]);
  assert.equal(el.forget.hidden, true);
});

test('a secret the server refused is forgotten without a word from this form', () => {
  const storage = memoryStorage();
  saveSecret('wrong', storage);
  const { el, admin, changes } = harness({ storage });

  admin.forget();

  assert.equal(readSecret(storage), null);
  assert.equal(el.forget.hidden, true);
  assert.deepEqual(changes, [], 'the import panel already knows; it asked for this');
});
