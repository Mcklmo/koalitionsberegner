/**
 * The account panel: sign in, see your tier, see what is left, upgrade.
 *
 * Three states, never two at once: a sign-in form, a request to confirm the
 * address, or a summary of the account. The summary is the only place the page
 * says what a user may do, and it says it from `/api/me` rather than from
 * anything it worked out locally — the server owns the tier, the allowance and
 * whether the address is confirmed, and a page that guessed would eventually
 * offer a button that 402s.
 *
 * The form is one real form in one of two modes, signing in or creating an
 * account, and either way it is *submitted*. That is what a password manager
 * waits for before offering to save, and the password field's `autocomplete`
 * is how it tells a `current-password` from a `new-password` — the one worth
 * saving, and in Safari the one worth generating.
 *
 * Signing in is not required to use the page. A visitor sees the curated
 * elections and this panel invites them in; nothing else changes.
 */

import { ApiError } from './api.js';
import { t } from './i18n.js';

/** The tiers the page has a name for; any other is shown as the server spells it. */
const NAMED_TIERS = ['free', 'basic', 'premium'];

const MIN_PASSWORD_LENGTH = 6;

/**
 * Said wherever a plan could have been bought, while checkout is closed.
 *
 * It points at the one thing that still works for somebody who wanted to pay:
 * the import form writes the election down as a request instead (see
 * import-ui.js). The server refuses checkout regardless — this is the page
 * being honest about it rather than the page deciding it.
 */
export const paymentsPausedNote = () => t('payments.paused');

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** What the form says in each mode. `autocomplete` is the part a password manager reads. */
const MODES = {
  signin: {
    submit: 'auth.signin.submit',
    autocomplete: 'current-password',
    switchText: 'auth.signin.switchText',
    switchLabel: 'auth.signin.switchLabel',
  },
  signup: {
    submit: 'auth.signup.submit',
    autocomplete: 'new-password',
    switchText: 'auth.signup.switchText',
    switchLabel: 'auth.signup.switchLabel',
  },
};

export function tierLabel(tier) {
  return NAMED_TIERS.includes(tier) ? t(`tier.${tier}`) : tier;
}

/** What the quota line says, given what the server reported. */
export function quotaSummary(account) {
  if (!account) return '';
  if (account.unlimited) {
    return account.admin
      ? t('quota.adminUnlimited')
      : t('quota.unlimited');
  }
  if (account.limit <= 0) {
    return t('quota.none');
  }
  return t('quota.remaining', { remaining: account.remaining, limit: account.limit });
}

/** One address, checked locally. */
export function validateEmail(email) {
  const value = (email ?? '').trim();
  if (!value) return { value, error: t('email.missing') };
  if (!EMAIL_PATTERN.test(value)) {
    return { value, error: t('email.invalid') };
  }
  return { value, error: null };
}

/** Local checks only, so an obvious mistake costs no round trip. */
export function validateCredentials({ email, password } = {}) {
  const address = validateEmail(email);
  const values = { email: address.value, password: password ?? '' };
  const errors = {};
  if (address.error) errors.email = address.error;
  if (!values.password) {
    errors.password = t('password.missing');
  } else if (values.password.length < MIN_PASSWORD_LENGTH) {
    errors.password = t('password.short', { min: MIN_PASSWORD_LENGTH });
  }
  return { valid: Object.keys(errors).length === 0, values, errors };
}

export function mountAccountUi({ api, auth, config, elements, onChange = () => {} }) {
  const el = elements;
  let account = null;
  let mode = 'signin';

  // Only a provider that can mail links has these; the self-hosted one cannot,
  // and its accounts are never waiting on a confirmation.
  const canResetPassword = typeof auth.sendPasswordReset === 'function';
  const canVerifyEmail = typeof auth.sendVerification === 'function';

  const show = (node, visible) => {
    node.hidden = !visible;
  };

  function setMessage(text, kind = 'info') {
    el.message.textContent = text ?? '';
    el.message.className = 'msg' + (text ? ' msg-' + kind : '');
  }

  function setFieldErrors(errors = {}) {
    for (const [field, node] of Object.entries(el.fieldErrors)) {
      node.textContent = errors[field] ?? '';
    }
  }

  function busy(isBusy) {
    for (const node of [el.submit, el.switchMode, el.forgot, el.verifyDone, el.verifyResend]) {
      node.disabled = isBusy;
    }
  }

  function setMode(next) {
    mode = next;
    const copy = MODES[mode];
    el.submit.textContent = t(copy.submit);
    el.passwordInput.autocomplete = copy.autocomplete;
    el.switchText.textContent = t(copy.switchText);
    el.switchMode.textContent = t(copy.switchLabel);
    show(el.forgot, mode === 'signin' && canResetPassword);
    setFieldErrors({});
    setMessage('');
  }

  /**
   * One button per tier that is actually for sale and is not the current one.
   * An account with no limit has nothing to buy.
   */
  function renderUpgrades() {
    el.upgrades.innerHTML = '';
    // A subscriber changes tier in Stripe's portal: a second checkout would be
    // a second subscription, and the server refuses one.
    const subscribed = account?.tier !== 'free'
      && ['active', 'trialing'].includes(account?.subscriptionStatus);
    const buyer = !account?.unlimited && !subscribed;
    const sellable = buyer ? (config.tiers ?? []).filter(
      (row) => row.purchasable && row.tier !== account?.tier
    ) : [];
    for (const row of sellable) {
      const button = document.createElement('button');
      button.className = 'secondary';
      button.type = 'button';
      button.textContent = t('upgrade.button', { tier: tierLabel(row.tier), imports: row.monthlyImports });
      button.addEventListener('click', () => checkout(row.tier));
      el.upgrades.appendChild(button);
    }
    // While checkout is closed the server marks every tier unpurchasable, so
    // there are no buttons to render and the row would simply vanish. Somebody
    // who came here to pay is owed better than silence: the note says when to
    // come back and what works meanwhile.
    const paused = buyer && Boolean(config.paymentsPaused);
    el.upgradeNote.textContent = paused ? paymentsPausedNote() : '';
    show(el.upgradeRow, sellable.length > 0 || paused);
    // Only somebody who has paid before has a subscription to manage.
    show(el.manage, Boolean(account?.billingEnabled && account?.subscriptionStatus));
  }

  function render() {
    const signedIn = account !== null;
    const confirmed = signedIn && account.emailVerified !== false;
    show(el.signedOut, !signedIn);
    show(el.signedIn, signedIn);
    show(el.verifyRow, signedIn && !confirmed);
    if (!confirmed) {
      el.upgrades.innerHTML = '';
      el.upgradeNote.textContent = '';
      show(el.upgradeRow, false);
      show(el.manage, false);
    }
    if (!signedIn) return;
    el.email.textContent = account.email ?? '';
    el.tier.textContent = tierLabel(account.tier);
    show(el.tier, confirmed);
    // Until the address is confirmed the account can do nothing, so there is
    // no allowance worth stating and nothing worth selling.
    el.quota.textContent = confirmed ? quotaSummary(account) : '';
    if (confirmed) renderUpgrades();
  }

  /** Re-read the account from the server and tell the rest of the page. */
  async function refresh() {
    if (!auth.isSignedIn()) {
      account = null;
      render();
      onChange(null);
      return null;
    }
    try {
      account = await api.getAccount();
    } catch (error) {
      // A rejected token means the session is over; showing the form again is
      // more honest than leaving a stale summary on screen.
      if (error instanceof ApiError && error.status === 401) {
        auth.signOut();
        return null;
      }
      account = null;
      setMessage(t('account.loadFailed', { message: error.message }), 'error');
    }
    render();
    onChange(account);
    return account;
  }

  async function submit() {
    setMessage('');
    const { valid, values, errors } = validateCredentials({
      email: el.emailInput.value,
      password: el.passwordInput.value,
    });
    setFieldErrors(errors);
    if (!valid) return;

    const creating = mode === 'signup';
    try {
      busy(true);
      await (creating
        ? auth.signUp(values.email, values.password)
        : auth.signIn(values.email, values.password));
      el.passwordInput.value = '';
    } catch (error) {
      setMessage(error.message, 'error');
      return;
    } finally {
      busy(false);
    }

    if (!creating) return;
    if (!canVerifyEmail) {
      setMessage(t('signup.created'), 'ok');
      return;
    }
    try {
      await auth.sendVerification();
      setMessage(t('signup.linkSent', { email: values.email }), 'ok');
    } catch {
      // The account exists either way, and the panel is already offering to
      // send the link again: a hiccup to mention, not a failure to report.
      setMessage(t('signup.linkFailed'), 'warn');
    }
  }

  async function resetPassword() {
    setMessage('');
    const { value, error } = validateEmail(el.emailInput.value);
    setFieldErrors(error ? { email: error } : {});
    if (error) return;
    try {
      busy(true);
      await auth.sendPasswordReset(value);
      // Worded the same whether or not the address has an account: which ones
      // do is not something this form should answer.
      setMessage(t('reset.sent', { email: value }), 'ok');
    } catch (failure) {
      setMessage(failure.message, 'error');
    } finally {
      busy(false);
    }
  }

  /**
   * Ask again whether the address is confirmed. The answer is in the ID token,
   * which states it as it was when it was minted, so this takes a fresh one
   * before asking the server. `quiet` is for checks the user did not ask for.
   */
  async function confirmVerified({ quiet = false } = {}) {
    if (!quiet) setMessage('');
    try {
      if (!quiet) busy(true);
      await auth.reload?.();
    } catch (error) {
      // A session that cannot be renewed has already ended itself.
      if (!quiet) setMessage(error.message, 'error');
      return;
    } finally {
      if (!quiet) busy(false);
    }
    const next = await refresh();
    if (!next) return;
    if (next.emailVerified !== false) {
      setMessage(t('verify.confirmed'), 'ok');
    } else if (!quiet) {
      setMessage(t('verify.notYet'), 'warn');
    }
  }

  async function resendVerification() {
    setMessage('');
    try {
      busy(true);
      await auth.sendVerification();
      setMessage(t('verify.resent', { email: account?.email ?? t('verify.yourAddress') }), 'ok');
    } catch (error) {
      if (error.code === 'TOO_MANY_ATTEMPTS_TRY_LATER') {
        // Firebase refuses a new link shortly after the last one, which by
        // then is usually in the inbox already.
        setMessage(t('verify.recentlySent'), 'info');
      } else {
        setMessage(error.message, 'error');
      }
    } finally {
      busy(false);
    }
  }

  async function checkout(tier) {
    if (config.paymentsPaused) {
      setMessage(paymentsPausedNote(), 'warn');
      return;
    }
    setMessage(t('checkout.opening'), 'info');
    try {
      const url = await api.startCheckout(tier);
      // Stripe hosts the payment page; nothing about the tier changes until it
      // tells the backend so over a signed webhook.
      redirect(url);
    } catch (error) {
      // Deliberately not read as a pause, even though a paused checkout answers
      // 503: so does a deployment with no Stripe configured, and the two are
      // not the same news. What the page knows about the pause it knows from
      // /api/config above; the rest is reported as the server worded it.
      setMessage(t('checkout.failed', { message: error.message }), 'error');
    }
  }

  async function manage() {
    setMessage(t('portal.opening'), 'info');
    try {
      redirect(await api.openBillingPortal());
    } catch (error) {
      setMessage(t('portal.failed', { message: error.message }), 'error');
    }
  }

  function redirect(url) {
    globalThis.location.assign(url);
  }

  el.form.addEventListener('submit', (event) => {
    // Handled here rather than by navigating, but still a submission — which
    // is what a password manager is watching for.
    event.preventDefault();
    return submit();
  });
  el.switchMode.addEventListener('click', () => setMode(mode === 'signin' ? 'signup' : 'signin'));
  el.forgot.addEventListener('click', resetPassword);
  el.signOut.addEventListener('click', () => auth.signOut());
  el.manage.addEventListener('click', manage);
  el.verifyDone.addEventListener('click', () => confirmVerified());
  el.verifyResend.addEventListener('click', resendVerification);

  // The link is opened in a mail client or another tab, so coming back to this
  // one is the moment to find out whether it has been.
  globalThis.addEventListener?.('focus', () =>
    account?.emailVerified === false ? confirmVerified({ quiet: true }) : undefined
  );

  // The session outlives a reload, so the panel follows the session rather
  // than the other way round.
  auth.onChange((user) => {
    setFieldErrors({});
    // Whoever sees the form next has most likely come to sign in.
    if (!user) setMode('signin');
    refresh();
  });

  setMode('signin');

  return {
    account: () => account,
    refresh,

    /** Show the panel and load the account, if there is a session to load. */
    async start() {
      if (!config.authRequired) {
        // Gating is off: there is nothing to sign in to, so nothing to show.
        show(el.panel, false);
        await refresh();
        return;
      }
      if (!auth.enabled) {
        show(el.panel, true);
        show(el.signedOut, false);
        show(el.signedIn, false);
        setMessage(t('login.notConfigured'), 'warn');
        return;
      }
      show(el.panel, true);
      await refresh();
      render();
    },
  };
}
