/**
 * Composition root: picks the election source and wires the UI together.
 *
 * Order matters here. The bundled election renders first, so the page is
 * useful before any network call resolves; then the public config says whether
 * this deployment gates anything; then the session, if any, is restored and the
 * account and import panels follow it.
 */

import { createApiClient } from './api.js';
import { mountAccountUi } from './account-ui.js';
import { mountCoalitionCalculator } from './app.js';
import { createAuth } from './auth.js';
import { createPasswordAuth } from './password-auth.js';
import { mountImportUi } from './import-ui.js';
import { Folketing2026Provider } from './providers/folketing-2026.js';

const apiBase = document.querySelector('meta[name="api-base"]')?.content ?? '';

const byId = (id) => document.getElementById(id);

let auth = null;
// The token is read per request rather than captured, because the session is
// restored after the client is built and can end at any point after that.
const api = createApiClient({
  baseUrl: apiBase,
  getToken: () => auth?.getIdToken() ?? Promise.resolve(null),
});

let calculator = null;

/** Re-render the calculator for a different election. */
function render(election) {
  calculator = mountCoalitionCalculator(election);
}

// The bundled election renders immediately, so the page works with no backend.
const bundled = await Folketing2026Provider.getElection();
render(bundled);

byId('reset').addEventListener('click', () => calculator?.clearAll());

// Without a reachable backend there is nothing to sign in to and nothing to
// import; the bundled election still works, which is the point of the fallback.
const config = await api.getConfig().catch(() => ({
  authRequired: false,
  authProvider: 'none',
  firebase: {},
  billingEnabled: false,
  // With no backend there is nothing to pay and nothing to ask for; both
  // panels then offer neither rather than guessing.
  paymentsPaused: false,
  requestsEnabled: false,
  tiers: [],
}));

// The backend says which flow it can satisfy; the panel and the API client are
// handed one of these and never learn which.
auth = config.authProvider === 'password'
  ? createPasswordAuth({ api })
  : createAuth({
    apiKey: config.firebase.apiKey,
    // Where a confirmation or reset link brings the user back to.
    continueUrl: `${globalThis.location.origin}${globalThis.location.pathname}`,
  });

const importUi = mountImportUi({
  api,
  bundled,
  config,
  onSelect: render,
  onImported: () => accountUi.refresh(),
  elements: {
    form: byId('import-form'),
    year: byId('f-year'),
    nation: byId('f-nation'),
    subnation: byId('f-subnation'),
    submit: byId('f-submit'),
    message: byId('import-message'),
    availability: byId('import-availability'),
    fieldErrors: { year: byId('e-year'), nation: byId('e-nation') },
    preview: byId('preview'),
    previewTitle: byId('preview-title'),
    previewMeta: byId('preview-meta'),
    previewSource: byId('preview-source'),
    previewList: byId('preview-list'),
    confirm: byId('preview-confirm'),
    discard: byId('preview-discard'),
    choices: byId('choices'),
    choicesTitle: byId('choices-title'),
    choicesList: byId('choices-list'),
    choicesDiscard: byId('choices-discard'),
    picker: byId('picker'),
    pickerRow: byId('picker-row'),
    curate: byId('curate'),
    curateRow: byId('curate-row'),
  },
});

const accountUi = mountAccountUi({
  api,
  auth,
  config,
  onChange: (account) => importUi.setAccount(account),
  elements: {
    panel: byId('account'),
    signedOut: byId('account-signed-out'),
    signedIn: byId('account-signed-in'),
    form: byId('auth-form'),
    emailInput: byId('f-email'),
    passwordInput: byId('f-password'),
    fieldErrors: { email: byId('e-email'), password: byId('e-password') },
    submit: byId('auth-submit'),
    switchMode: byId('auth-switch'),
    switchText: byId('auth-switch-text'),
    forgot: byId('auth-forgot'),
    signOut: byId('account-signout'),
    email: byId('account-email'),
    tier: byId('account-tier'),
    quota: byId('account-quota'),
    verifyRow: byId('verify-row'),
    verifyDone: byId('verify-done'),
    verifyResend: byId('verify-resend'),
    upgradeRow: byId('upgrade-row'),
    upgrades: byId('upgrades'),
    upgradeNote: byId('upgrade-note'),
    manage: byId('account-manage'),
    message: byId('account-message'),
  },
});

await accountUi.start();
await importUi.start();

// Coming back from Stripe: the subscription is only ours once the webhook has
// landed, which is usually but not always before the redirect. Re-reading the
// account is the cheapest way to pick it up.
if (new URLSearchParams(globalThis.location.search).get('checkout') === 'success') {
  await accountUi.refresh();
}
