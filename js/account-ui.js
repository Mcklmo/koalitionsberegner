/**
 * The account panel: sign in, see your tier, see what is left, upgrade.
 *
 * Two states, never both: a sign-in form, or a summary of the account. The
 * summary is the only place the page says what a user may do, and it says it
 * from `/api/me` rather than from anything it worked out locally — the server
 * owns the tier and the allowance, and a page that guessed would eventually
 * offer a button that 402s.
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

export function tierLabel(tier) {
  return TIER_LABELS[tier] ?? tier;
}

/** What the quota line says, given what the server reported. */
export function quotaSummary(account) {
  if (!account) return '';
  if (account.unlimited) return 'Ubegrænsede importer (adgangskontrol er slået fra).';
  if (account.limit <= 0) {
    return 'Din plan giver ikke adgang til at importere nye valg.';
  }
  const noun = account.remaining === 1 ? 'import' : 'importer';
  return `${account.remaining} af ${account.limit} ${noun} tilbage denne måned.`;
}

/** Local checks only, so an obvious mistake costs no round trip. */
export function validateCredentials({ email, password } = {}) {
  const values = { email: (email ?? '').trim(), password: password ?? '' };
  const errors = {};
  if (!values.email) {
    errors.email = 'Angiv din e-mailadresse.';
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email)) {
    errors.email = 'Adressen ser ikke ud til at være en e-mailadresse.';
  }
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
    el.signIn.disabled = isBusy;
    el.signUp.disabled = isBusy;
  }

  /** One button per tier that is actually for sale and is not the current one. */
  function renderUpgrades() {
    el.upgrades.innerHTML = '';
    const sellable = (config.tiers ?? []).filter(
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
    show(el.signedOut, !signedIn);
    show(el.signedIn, signedIn);
    if (!signedIn) {
      el.upgrades.innerHTML = '';
      show(el.upgradeRow, false);
      show(el.manage, false);
      return;
    }
    el.email.textContent = account.email ?? '';
    el.tier.textContent = tierLabel(account.tier);
    el.quota.textContent = quotaSummary(account);
    renderUpgrades();
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

  async function submit(mode) {
    setMessage('');
    const { valid, values, errors } = validateCredentials({
      email: el.emailInput.value,
      password: el.passwordInput.value,
    });
    setFieldErrors(errors);
    if (!valid) return;

    try {
      busy(true);
      await (mode === 'signup'
        ? auth.signUp(values.email, values.password)
        : auth.signIn(values.email, values.password));
      el.passwordInput.value = '';
      setMessage(mode === 'signup' ? 'Kontoen er oprettet.' : '', 'ok');
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

  el.signIn.addEventListener('click', () => submit('signin'));
  el.signUp.addEventListener('click', () => submit('signup'));
  el.signOut.addEventListener('click', () => auth.signOut());
  el.manage.addEventListener('click', manage);
  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit('signin');
  });

  // The session outlives a reload, so the panel follows the session rather
  // than the other way round.
  auth.onChange(() => {
    setFieldErrors({});
    refresh();
  });

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
