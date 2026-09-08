import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../js/api.js';
import { mountAccountUi, quotaSummary, tierLabel, validateCredentials } from '../js/account-ui.js';

// --- a DOM stub covering exactly what account-ui touches --------------------
function node(tag = 'div') {
  return {
    tag,
    value: '',
    textContent: '',
    className: '',
    type: '',
    hidden: false,
    disabled: false,
    children: [],
    handlers: {},
    set innerHTML(v) { if (v === '') this.children = []; },
    get innerHTML() { return ''; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = { preventDefault() {} }) {
      return Promise.all((this.handlers[type] ?? []).map((fn) => fn(event)));
    },
  };
}

const TIERS = [
  { tier: 'free', monthlyImports: 0, purchasable: false },
  { tier: 'basic', monthlyImports: 10, purchasable: true },
  { tier: 'premium', monthlyImports: 200, purchasable: true },
];

const basicAccount = {
  uid: 'uid-1',
  email: 'a@example.org',
  tier: 'basic',
  admin: false,
  period: '2026-09',
  used: 3,
  limit: 10,
  remaining: 7,
  unlimited: false,
  mayImport: true,
  subscriptionStatus: 'active',
  billingEnabled: true,
};

/** An auth double: a session that can be driven directly from a test. */
function fakeAuth({ signedIn = false, enabled = true } = {}) {
  const listeners = new Set();
  let current = signedIn;
  const calls = { signIn: [], signUp: [], signOut: 0 };
  return {
    enabled,
    calls,
    isSignedIn: () => current,
    user: () => (current ? { email: 'a@example.org', uid: 'uid-1' } : null),
    onChange(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    async signIn(email, password) {
      calls.signIn.push([email, password]);
      current = true;
      await Promise.all([...listeners].map((fn) => fn(this.user())));
    },
    async signUp(email, password) {
      calls.signUp.push([email, password]);
      current = true;
      await Promise.all([...listeners].map((fn) => fn(this.user())));
    },
    signOut() {
      calls.signOut += 1;
      current = false;
      for (const fn of listeners) fn(null);
    },
    /** Drive a rejected sign-in without changing state. */
    failNext(error) {
      this.signIn = async () => { throw error; };
    },
  };
}

function fakeApi(overrides = {}) {
  const calls = { getAccount: 0, startCheckout: [], openBillingPortal: 0 };
  return {
    calls,
    async getAccount() {
      calls.getAccount += 1;
      if (overrides.accountError) throw overrides.accountError;
      return overrides.account ?? basicAccount;
    },
    async startCheckout(tier) {
      calls.startCheckout.push(tier);
      if (overrides.checkoutError) throw overrides.checkoutError;
      return `https://checkout.test/${tier}`;
    },
    async openBillingPortal() {
      calls.openBillingPortal += 1;
      return 'https://portal.test/cus_1';
    },
  };
}

function harness({ api = fakeApi(), auth = fakeAuth(), config = {} } = {}) {
  const redirects = [];
  globalThis.document = { createElement: node };
  globalThis.location = { search: '', assign: (url) => redirects.push(url) };
  const el = {
    panel: node(), signedOut: node(), signedIn: node(),
    form: node('form'), emailInput: node('input'), passwordInput: node('input'),
    fieldErrors: { email: node(), password: node() },
    signIn: node('button'), signUp: node('button'), signOut: node('button'),
    email: node(), tier: node(), quota: node(),
    upgradeRow: node(), upgrades: node(), manage: node('button'),
    message: node(),
  };
  const changes = [];
  const ui = mountAccountUi({
    api,
    auth,
    config: { authRequired: true, billingEnabled: true, tiers: TIERS, ...config },
    elements: el,
    onChange: (account) => changes.push(account),
  });
  return { el, ui, api, auth, changes, redirects };
}

// --- the pure bits ----------------------------------------------------------

test('tiers are named in the page language', () => {
  assert.equal(tierLabel('premium'), 'Premium');
  assert.equal(tierLabel('free'), 'Gratis');
  assert.equal(tierLabel('mystery'), 'mystery', 'an unknown tier is shown, not hidden');
});

test('the quota line says what is left, in the singular when it should', () => {
  assert.match(quotaSummary(basicAccount), /7 af 10 importer tilbage/);
  assert.match(quotaSummary({ ...basicAccount, remaining: 1 }), /1 af 10 import tilbage/);
  assert.match(quotaSummary({ ...basicAccount, tier: 'free', limit: 0 }), /ikke adgang til at importere/);
  assert.match(quotaSummary({ ...basicAccount, unlimited: true }), /Ubegrænsede/);
  assert.equal(quotaSummary(null), '');
});

test('credentials are checked locally before a round trip', () => {
  assert.equal(validateCredentials({ email: 'a@example.org', password: 'hunter22' }).valid, true);
  assert.match(validateCredentials({ password: 'hunter22' }).errors.email, /e-mailadresse/);
  assert.match(validateCredentials({ email: 'nope', password: 'hunter22' }).errors.email, /ser ikke ud/);
  assert.match(validateCredentials({ email: 'a@example.org', password: 'short' }).errors.password, /mindst 6/);
});

// --- the panel --------------------------------------------------------------

test('a signed-out visitor is shown the form and nothing about tiers', async () => {
  const { el, ui, changes } = harness();

  await ui.start();

  assert.equal(el.panel.hidden, false);
  assert.equal(el.signedOut.hidden, false);
  assert.equal(el.signedIn.hidden, true);
  assert.equal(el.upgradeRow.hidden, true);
  assert.deepEqual(changes, [null], 'the rest of the page is told there is no account');
});

test('signing in shows the tier and what is left of the allowance', async () => {
  const { el, ui, auth, changes } = harness();
  await ui.start();

  el.emailInput.value = 'a@example.org';
  el.passwordInput.value = 'hunter22';
  await el.signIn.dispatch('click');

  assert.deepEqual(auth.calls.signIn, [['a@example.org', 'hunter22']]);
  assert.equal(el.signedIn.hidden, false);
  assert.equal(el.signedOut.hidden, true);
  assert.equal(el.email.textContent, 'a@example.org');
  assert.equal(el.tier.textContent, 'Basis');
  assert.match(el.quota.textContent, /7 af 10/);
  assert.equal(changes.at(-1).tier, 'basic');
});

test('the password is cleared once it has been used', async () => {
  const { el, ui } = harness();
  await ui.start();
  el.emailInput.value = 'a@example.org';
  el.passwordInput.value = 'hunter22';

  await el.signIn.dispatch('click');

  assert.equal(el.passwordInput.value, '');
});

test('an invalid form never reaches the identity service', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  el.emailInput.value = 'nope';
  el.passwordInput.value = 'x';

  await el.signIn.dispatch('click');

  assert.deepEqual(auth.calls.signIn, []);
  assert.match(el.fieldErrors.email.textContent, /ser ikke ud/);
  assert.match(el.fieldErrors.password.textContent, /mindst 6/);
});

test('creating an account uses sign-up, not sign-in', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  el.emailInput.value = 'new@example.org';
  el.passwordInput.value = 'hunter22';

  await el.signUp.dispatch('click');

  assert.deepEqual(auth.calls.signUp, [['new@example.org', 'hunter22']]);
  assert.deepEqual(auth.calls.signIn, []);
});

test('a rejected sign-in is reported and leaves the form up', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  auth.failNext(new Error('Forkert adgangskode.'));
  el.emailInput.value = 'a@example.org';
  el.passwordInput.value = 'hunter22';

  await el.signIn.dispatch('click');

  assert.match(el.message.textContent, /Forkert adgangskode/);
  assert.equal(el.signedOut.hidden, false);
});

test('submitting the form is the same as pressing log ind', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  el.emailInput.value = 'a@example.org';
  el.passwordInput.value = 'hunter22';

  await el.form.dispatch('submit');

  assert.equal(auth.calls.signIn.length, 1);
});

test('signing out returns the panel to the form and tells the page', async () => {
  const { el, ui, auth, changes } = harness({ auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await el.signOut.dispatch('click');

  assert.equal(auth.calls.signOut, 1);
  assert.equal(el.signedOut.hidden, false);
  assert.equal(el.signedIn.hidden, true);
  assert.equal(changes.at(-1), null);
});

// --- upgrading --------------------------------------------------------------

test('only tiers that are for sale and are not the current one are offered', async () => {
  const { el, ui } = harness({ auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  const labels = el.upgrades.children.map((child) => child.textContent);
  assert.deepEqual(labels, ['Premium — 200 importer/md.'], 'free is not for sale, basic is current');
  assert.equal(el.upgradeRow.hidden, false);
});

test('a free account is offered both paid tiers', async () => {
  const api = fakeApi({ account: { ...basicAccount, tier: 'free', limit: 0, remaining: 0, mayImport: false, subscriptionStatus: null } });
  const { el, ui } = harness({ api, auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  assert.deepEqual(
    el.upgrades.children.map((child) => child.textContent),
    ['Basis — 10 importer/md.', 'Premium — 200 importer/md.']
  );
  assert.equal(el.manage.hidden, true, 'nothing to manage without a subscription');
});

test('choosing a tier sends the user to Stripe and changes nothing locally', async () => {
  const { el, ui, api, redirects, changes } = harness({ auth: fakeAuth({ signedIn: true }) });
  await ui.start();
  const before = changes.at(-1).tier;

  await el.upgrades.children[0].dispatch('click');

  assert.deepEqual(api.calls.startCheckout, ['premium']);
  assert.deepEqual(redirects, ['https://checkout.test/premium']);
  assert.equal(changes.at(-1).tier, before, 'the tier is Stripe\'s to change, not the page\'s');
});

test('a checkout that cannot be started is reported rather than redirected', async () => {
  const api = fakeApi({ checkoutError: new ApiError('subscriptions are not configured', 503) });
  const { el, ui, redirects } = harness({ api, auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await el.upgrades.children[0].dispatch('click');

  assert.deepEqual(redirects, []);
  assert.match(el.message.textContent, /Kunne ikke starte betalingen/);
});

test('an existing subscriber can reach the billing portal', async () => {
  const { el, ui, api, redirects } = harness({ auth: fakeAuth({ signedIn: true }) });
  await ui.start();
  assert.equal(el.manage.hidden, false);

  await el.manage.dispatch('click');

  assert.equal(api.calls.openBillingPortal, 1);
  assert.deepEqual(redirects, ['https://portal.test/cus_1']);
});

// --- degraded cases ---------------------------------------------------------

test('a token the API rejects ends the session instead of showing a stale tier', async () => {
  const api = fakeApi({ accountError: new ApiError('sign in to use this', 401) });
  const auth = fakeAuth({ signedIn: true });
  const { el, ui } = harness({ api, auth });

  await ui.start();

  assert.equal(auth.calls.signOut, 1);
  assert.equal(el.signedIn.hidden, true);
});

test('an unreachable account endpoint says so without pretending to be signed out', async () => {
  const api = fakeApi({ accountError: new ApiError('could not reach the election store', 0) });
  const { el, ui, auth } = harness({ api, auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  assert.equal(auth.calls.signOut, 0);
  assert.match(el.message.textContent, /Kunne ikke hente kontoen/);
});

test('with gating switched off the panel is not shown at all', async () => {
  const { el, ui, api } = harness({ config: { authRequired: false } });

  await ui.start();

  assert.equal(el.panel.hidden, true);
  assert.equal(api.calls.getAccount, 0, 'there is no account to fetch');
});

test('a deployment with no Firebase says so rather than showing a dead form', async () => {
  const { el, ui } = harness({ auth: fakeAuth({ enabled: false }) });

  await ui.start();

  assert.equal(el.panel.hidden, false);
  assert.equal(el.signedOut.hidden, true);
  assert.match(el.message.textContent, /ikke konfigureret/);
});
