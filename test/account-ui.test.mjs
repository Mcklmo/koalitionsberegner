import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../js/api.js';
import {
  mountAccountUi,
  quotaSummary,
  tierLabel,
  validateCredentials,
  validateEmail,
} from '../js/account-ui.js';

// --- a DOM stub covering exactly what account-ui touches --------------------
function node(tag = 'div') {
  return {
    tag,
    value: '',
    textContent: '',
    className: '',
    type: '',
    autocomplete: '',
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
  emailVerified: true,
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

/** A Firebase sign-up whose owner has not opened the link yet. */
const unconfirmed = {
  ...basicAccount,
  email: 'new@example.org',
  emailVerified: false,
  tier: 'free',
  limit: 0,
  remaining: 0,
  mayImport: false,
  subscriptionStatus: null,
};

/**
 * An auth double: a session that can be driven directly from a test.
 * `mails: false` is the self-hosted provider, which cannot send any links.
 */
function fakeAuth({ signedIn = false, enabled = true, mails = true } = {}) {
  const listeners = new Set();
  let current = signedIn;
  const calls = {
    signIn: [], signUp: [], signOut: 0, sendVerification: 0, sendPasswordReset: [], reload: 0,
  };
  const auth = {
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
  if (mails) {
    Object.assign(auth, {
      async sendVerification() { calls.sendVerification += 1; },
      async sendPasswordReset(email) { calls.sendPasswordReset.push(email); },
      async reload() { calls.reload += 1; return 'fresh-token'; },
    });
  }
  return auth;
}

/** `accounts` answers each `/api/me` in turn, repeating the last one. */
function fakeApi(overrides = {}) {
  const calls = { getAccount: 0, startCheckout: [], openBillingPortal: 0 };
  const queue = overrides.accounts ? [...overrides.accounts] : null;
  return {
    calls,
    async getAccount() {
      calls.getAccount += 1;
      if (overrides.accountError) throw overrides.accountError;
      if (queue) return queue.length > 1 ? queue.shift() : queue[0];
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
  const focusHandlers = [];
  globalThis.document = { createElement: node };
  globalThis.location = { search: '', assign: (url) => redirects.push(url) };
  globalThis.addEventListener = (type, fn) => {
    if (type === 'focus') focusHandlers.push(fn);
  };
  const el = {
    panel: node(), signedOut: node(), signedIn: node(),
    form: node('form'), emailInput: node('input'), passwordInput: node('input'),
    fieldErrors: { email: node(), password: node() },
    submit: node('button'), switchMode: node('button'), switchText: node(), forgot: node('button'),
    signOut: node('button'),
    email: node(), tier: node(), quota: node(),
    verifyRow: node(), verifyDone: node('button'), verifyResend: node('button'),
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
  /** The window regaining focus, as when the user comes back from their mail. */
  const focus = () => Promise.all(focusHandlers.map((fn) => fn()));
  return { el, ui, api, auth, changes, redirects, focus };
}

/** Type into the form and submit it, the way pressing Enter or the button does. */
async function fillAndSubmit(el, email = 'a@example.org', password = 'hunter22') {
  el.emailInput.value = email;
  el.passwordInput.value = password;
  await el.form.dispatch('submit');
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
  assert.match(quotaSummary({ ...basicAccount, unlimited: true, admin: true }), /Ubegrænsede importer som administrator/);
  assert.equal(quotaSummary(null), '');
});

test('credentials are checked locally before a round trip', () => {
  assert.equal(validateCredentials({ email: 'a@example.org', password: 'hunter22' }).valid, true);
  assert.match(validateCredentials({ password: 'hunter22' }).errors.email, /e-mailadresse/);
  assert.match(validateCredentials({ email: 'nope', password: 'hunter22' }).errors.email, /ser ikke ud/);
  assert.match(validateCredentials({ email: 'a@example.org', password: 'short' }).errors.password, /mindst 6/);
});

test('an address is checked on its own, trimmed', () => {
  assert.deepEqual(validateEmail(' a@example.org '), { value: 'a@example.org', error: null });
  assert.match(validateEmail('').error, /Angiv din e-mailadresse/);
  assert.match(validateEmail('nope').error, /ser ikke ud/);
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

  await fillAndSubmit(el);

  assert.deepEqual(auth.calls.signIn, [['a@example.org', 'hunter22']]);
  assert.equal(el.signedIn.hidden, false);
  assert.equal(el.signedOut.hidden, true);
  assert.equal(el.verifyRow.hidden, true);
  assert.equal(el.email.textContent, 'a@example.org');
  assert.equal(el.tier.textContent, 'Basis');
  assert.match(el.quota.textContent, /7 af 10/);
  assert.equal(changes.at(-1).tier, 'basic');
});

test('the password is cleared once it has been used', async () => {
  const { el, ui } = harness();
  await ui.start();

  await fillAndSubmit(el);

  assert.equal(el.passwordInput.value, '');
});

test('an invalid form never reaches the identity service', async () => {
  const { el, ui, auth } = harness();
  await ui.start();

  await fillAndSubmit(el, 'nope', 'x');

  assert.deepEqual(auth.calls.signIn, []);
  assert.match(el.fieldErrors.email.textContent, /ser ikke ud/);
  assert.match(el.fieldErrors.password.textContent, /mindst 6/);
});

test('the form signs in unless the user chose to create an account', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  assert.equal(el.submit.textContent, 'Log ind');

  await fillAndSubmit(el);

  assert.equal(auth.calls.signIn.length, 1);
  assert.deepEqual(auth.calls.signUp, []);
});

test('creating an account uses sign-up, not sign-in', async () => {
  const { el, ui, auth } = harness();
  await ui.start();

  await el.switchMode.dispatch('click');
  await fillAndSubmit(el, 'new@example.org');

  assert.deepEqual(auth.calls.signUp, [['new@example.org', 'hunter22']]);
  assert.deepEqual(auth.calls.signIn, []);
});

test('the password field tells a password manager whether the password is a new one', async () => {
  const { el, ui } = harness();
  await ui.start();
  assert.equal(el.passwordInput.autocomplete, 'current-password');
  assert.equal(el.forgot.hidden, false);

  await el.switchMode.dispatch('click');

  assert.equal(el.passwordInput.autocomplete, 'new-password', 'what makes a manager offer to save it');
  assert.equal(el.submit.textContent, 'Opret konto');
  assert.equal(el.switchMode.textContent, 'Log ind');
  assert.equal(el.forgot.hidden, true, 'there is nothing to forget before there is an account');

  await el.switchMode.dispatch('click');

  assert.equal(el.passwordInput.autocomplete, 'current-password');
  assert.equal(el.submit.textContent, 'Log ind');
});

test('a rejected sign-in is reported and leaves the form up', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  auth.failNext(new Error('Forkert adgangskode.'));

  await fillAndSubmit(el);

  assert.match(el.message.textContent, /Forkert adgangskode/);
  assert.equal(el.signedOut.hidden, false);
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

test('after signing out the form is back to signing in', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  await el.switchMode.dispatch('click');
  await fillAndSubmit(el, 'new@example.org');

  auth.signOut();

  assert.equal(el.passwordInput.autocomplete, 'current-password');
  assert.equal(el.submit.textContent, 'Log ind');
});

// --- confirming the address -------------------------------------------------

test('a new account is mailed the confirmation link and told where it went', async () => {
  const { el, ui, auth } = harness({ api: fakeApi({ account: unconfirmed }) });
  await ui.start();
  await el.switchMode.dispatch('click');

  await fillAndSubmit(el, 'new@example.org');

  assert.equal(auth.calls.sendVerification, 1);
  assert.match(el.message.textContent, /sendt et link til new@example\.org/);
  assert.equal(el.verifyRow.hidden, false);
});

test('a confirmation mail that cannot be sent does not undo the sign-up', async () => {
  const auth = fakeAuth();
  auth.sendVerification = async () => { throw new Error('For mange forsøg. Prøv igen om lidt.'); };
  const { el, ui } = harness({ api: fakeApi({ account: unconfirmed }), auth });
  await ui.start();
  await el.switchMode.dispatch('click');

  await fillAndSubmit(el, 'new@example.org');

  assert.equal(el.signedIn.hidden, false);
  assert.match(el.message.textContent, /Kontoen er oprettet\. Vi kunne ikke sende bekræftelsesmailen lige nu/);
  assert.doesNotMatch(el.message.className, /msg-error/, 'the account exists; nothing has failed');
  assert.equal(el.verifyRow.hidden, false, 'where the link can be sent again');
});

test('with no way to mail a link, a new account is simply created', async () => {
  const { el, ui, auth } = harness({ auth: fakeAuth({ mails: false }) });
  await ui.start();
  assert.equal(el.forgot.hidden, true, 'and no reset link is offered either');
  await el.switchMode.dispatch('click');

  await fillAndSubmit(el, 'new@example.org');

  assert.equal(auth.calls.signUp.length, 1);
  assert.equal(el.message.textContent, 'Kontoen er oprettet.');
});

test('an unconfirmed account is asked to confirm instead of being shown a plan', async () => {
  const api = fakeApi({ account: unconfirmed });
  const { el, ui, changes } = harness({ api, auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  assert.equal(el.signedIn.hidden, false);
  assert.equal(el.verifyRow.hidden, false);
  assert.equal(el.email.textContent, 'new@example.org');
  assert.equal(el.tier.hidden, true);
  assert.equal(el.quota.textContent, '');
  assert.equal(el.upgradeRow.hidden, true, 'nothing is for sale to an address nobody has confirmed');
  assert.equal(changes.at(-1).mayImport, false);
});

test('confirming takes a fresh token and shows the account once the server agrees', async () => {
  const api = fakeApi({ accounts: [unconfirmed, basicAccount] });
  const { el, ui, auth } = harness({ api, auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await el.verifyDone.dispatch('click');

  assert.equal(auth.calls.reload, 1, 'the old token still says the address is unconfirmed');
  assert.equal(el.verifyRow.hidden, true);
  assert.equal(el.tier.hidden, false);
  assert.equal(el.tier.textContent, 'Basis');
  assert.match(el.message.textContent, /Tak, din e-mailadresse er bekræftet/);
});

test('confirming before the link was opened says so', async () => {
  const { el, ui } = harness({ api: fakeApi({ account: unconfirmed }), auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await el.verifyDone.dispatch('click');

  assert.equal(el.verifyRow.hidden, false);
  assert.match(el.message.textContent, /ikke bekræftet endnu/);
});

test('the confirmation link can be sent again', async () => {
  const { el, ui, auth } = harness({ api: fakeApi({ account: unconfirmed }), auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await el.verifyResend.dispatch('click');

  assert.equal(auth.calls.sendVerification, 1);
  assert.match(el.message.textContent, /nyt link til new@example\.org/);
});

test('asking for another link too soon points to the one already sent', async () => {
  const auth = fakeAuth({ signedIn: true });
  auth.sendVerification = async () => {
    throw Object.assign(new Error('For mange forsøg. Prøv igen om lidt.'), { code: 'TOO_MANY_ATTEMPTS_TRY_LATER' });
  };
  const { el, ui } = harness({ api: fakeApi({ account: unconfirmed }), auth });
  await ui.start();

  await el.verifyResend.dispatch('click');

  assert.match(el.message.textContent, /Tjek din indbakke/);
  assert.doesNotMatch(el.message.className, /msg-error/);
});

test('coming back to the tab picks up an address confirmed elsewhere', async () => {
  const api = fakeApi({ accounts: [unconfirmed, basicAccount] });
  const { el, ui, auth, focus } = harness({ api, auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await focus();

  assert.equal(auth.calls.reload, 1);
  assert.equal(el.verifyRow.hidden, true);
});

test('coming back to the tab costs nothing when there is nothing to confirm', async () => {
  const { ui, auth, focus } = harness({ auth: fakeAuth({ signedIn: true }) });
  await ui.start();

  await focus();

  assert.equal(auth.calls.reload, 0);
});

// --- a forgotten password ---------------------------------------------------

test('a forgotten password is reset by mail, without saying whether the address has an account', async () => {
  const { el, ui, auth } = harness();
  await ui.start();
  el.emailInput.value = ' someone@example.org ';

  await el.forgot.dispatch('click');

  assert.deepEqual(auth.calls.sendPasswordReset, ['someone@example.org']);
  assert.match(el.message.textContent, /Hvis der findes en konto for someone@example\.org/);
});

test('a reset needs an address first, and never a password', async () => {
  const { el, ui, auth } = harness();
  await ui.start();

  await el.forgot.dispatch('click');

  assert.deepEqual(auth.calls.sendPasswordReset, []);
  assert.match(el.fieldErrors.email.textContent, /Angiv din e-mailadresse/);
  assert.equal(el.fieldErrors.password.textContent, '');
});

test('a reset that fails is reported', async () => {
  const auth = fakeAuth();
  auth.sendPasswordReset = async () => { throw new Error('For mange forsøg. Prøv igen om lidt.'); };
  const { el, ui } = harness({ auth });
  await ui.start();
  el.emailInput.value = 'a@example.org';

  await el.forgot.dispatch('click');

  assert.match(el.message.textContent, /For mange forsøg/);
});

// --- upgrading --------------------------------------------------------------

test('a subscriber is offered no second checkout, only the portal', async () => {
  // A second checkout would be a second subscription; changing tier is the portal's job.
  const { el, ui } = harness({ auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  assert.deepEqual(el.upgrades.children, []);
  assert.equal(el.upgradeRow.hidden, true);
  assert.equal(el.manage.hidden, false);
});

test('an administrator is offered nothing to buy', async () => {
  const api = fakeApi({ account: { ...basicAccount, tier: 'free', admin: true, limit: -1, remaining: -1, unlimited: true, subscriptionStatus: null } });
  const { el, ui } = harness({ api, auth: fakeAuth({ signedIn: true }) });

  await ui.start();

  assert.deepEqual(el.upgrades.children, []);
  assert.equal(el.upgradeRow.hidden, true);
  assert.match(el.quota.textContent, /administrator/);
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
  const api = fakeApi({ account: { ...basicAccount, tier: 'free', limit: 0, remaining: 0, mayImport: false, subscriptionStatus: null } });
  const { el, ui, redirects, changes } = harness({ api, auth: fakeAuth({ signedIn: true }) });
  await ui.start();
  const before = changes.at(-1).tier;

  await el.upgrades.children[1].dispatch('click');

  assert.deepEqual(api.calls.startCheckout, ['premium']);
  assert.deepEqual(redirects, ['https://checkout.test/premium']);
  assert.equal(changes.at(-1).tier, before, 'the tier is Stripe\'s to change, not the page\'s');
});

test('a checkout that cannot be started is reported rather than redirected', async () => {
  const api = fakeApi({
    account: { ...basicAccount, tier: 'free', limit: 0, remaining: 0, mayImport: false, subscriptionStatus: null },
    checkoutError: new ApiError('subscriptions are not configured', 503),
  });
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
