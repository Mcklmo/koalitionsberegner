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

export const TIER_LABELS = {
  free: 'Gratis',
  basic: 'Basis',
  premium: 'Premium',
};

const MIN_PASSWORD_LENGTH = 6;

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** What the form says in each mode. `autocomplete` is the part a password manager reads. */
const MODES = {
  signin: {
    submit: 'Log ind',
    autocomplete: 'current-password',
    switchText: 'Ny her?',
    switchLabel: 'Opret en konto',
  },
  signup: {
    submit: 'Opret konto',
    autocomplete: 'new-password',
    switchText: 'Har du allerede en konto?',
    switchLabel: 'Log ind',
  },
};

export function tierLabel(tier) {
  return TIER_LABELS[tier] ?? tier;
}

/** What the quota line says, given what the server reported. */
export function quotaSummary(account) {
  if (!account) return '';
  if (account.unlimited) {
    return account.admin
      ? 'Ubegrænsede importer som administrator.'
      : 'Ubegrænsede importer (adgangskontrol er slået fra).';
  }
  if (account.limit <= 0) {
    return 'Din plan giver ikke adgang til at importere nye valg.';
  }
  const noun = account.remaining === 1 ? 'import' : 'importer';
  return `${account.remaining} af ${account.limit} ${noun} tilbage denne måned.`;
}

/** One address, checked locally. */
export function validateEmail(email) {
  const value = (email ?? '').trim();
  if (!value) return { value, error: 'Angiv din e-mailadresse.' };
  if (!EMAIL_PATTERN.test(value)) {
    return { value, error: 'Adressen ser ikke ud til at være en e-mailadresse.' };
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
    errors.password = 'Angiv en adgangskode.';
  } else if (values.password.length < MIN_PASSWORD_LENGTH) {
    errors.password = `Adgangskoden skal være mindst ${MIN_PASSWORD_LENGTH} tegn.`;
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
    el.submit.textContent = copy.submit;
    el.passwordInput.autocomplete = copy.autocomplete;
    el.switchText.textContent = copy.switchText;
    el.switchMode.textContent = copy.switchLabel;
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
    const sellable = account?.unlimited ? [] : (config.tiers ?? []).filter(
      (row) => row.purchasable && row.tier !== account?.tier
    );
    for (const row of sellable) {
      const button = document.createElement('button');
      button.className = 'secondary';
      button.type = 'button';
      button.textContent = `${tierLabel(row.tier)} — ${row.monthlyImports} importer/md.`;
      button.addEventListener('click', () => checkout(row.tier));
      el.upgrades.appendChild(button);
    }
    show(el.upgradeRow, sellable.length > 0);
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
      setMessage(`Kunne ikke hente kontoen: ${error.message}`, 'error');
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
      setMessage('Kontoen er oprettet.', 'ok');
      return;
    }
    try {
      await auth.sendVerification();
      setMessage(
        `Kontoen er oprettet. Vi har sendt et link til ${values.email} — åbn det for at bekræfte adressen.`,
        'ok'
      );
    } catch (error) {
      // The account exists either way; the panel is already offering to send
      // the link again.
      setMessage(`Kontoen er oprettet, men mailen med linket kunne ikke sendes: ${error.message}`, 'error');
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
      setMessage(
        `Hvis der findes en konto for ${value}, har vi sendt et link til at vælge en ny adgangskode.`,
        'ok'
      );
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
      setMessage('Tak, din e-mailadresse er bekræftet.', 'ok');
    } else if (!quiet) {
      setMessage('Adressen er ikke bekræftet endnu. Åbn linket i mailen, og prøv igen.', 'warn');
    }
  }

  async function resendVerification() {
    setMessage('');
    try {
      busy(true);
      await auth.sendVerification();
      setMessage(`Vi har sendt et nyt link til ${account?.email ?? 'din e-mailadresse'}.`, 'ok');
    } catch (error) {
      setMessage(error.message, 'error');
    } finally {
      busy(false);
    }
  }

  async function checkout(tier) {
    setMessage('Åbner betaling…', 'info');
    try {
      const url = await api.startCheckout(tier);
      // Stripe hosts the payment page; nothing about the tier changes until it
      // tells the backend so over a signed webhook.
      redirect(url);
    } catch (error) {
      setMessage(`Kunne ikke starte betalingen: ${error.message}`, 'error');
    }
  }

  async function manage() {
    setMessage('Åbner abonnementet…', 'info');
    try {
      redirect(await api.openBillingPortal());
    } catch (error) {
      setMessage(`Kunne ikke åbne abonnementet: ${error.message}`, 'error');
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
        setMessage('Login er ikke konfigureret på denne server.', 'warn');
        return;
      }
      show(el.panel, true);
      await refresh();
      render();
    },
  };
}
