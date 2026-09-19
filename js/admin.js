/**
 * The owner's admin mode: the one secret that lets this page import.
 *
 * Nobody signs in to this site. Importing is the one thing that spends money,
 * so it is the owner's alone, and the owner proves it by presenting
 * `ADMIN_SECRET` in the `x-admin-secret` header (js/api.js adds it). The secret
 * is pasted in once, into a form that only opens when the address ends in
 * `#admin`, and kept in `sessionStorage` — so it is gone when the tab closes,
 * and never leaves this browser except in that header.
 *
 * Nothing here decides anything. A wrong secret is simply refused by the
 * server; the import panel then forgets it (see `onWrongSecret` there).
 */

import { t } from './i18n.js';

/** Where the secret is kept for the life of the tab. */
export const ADMIN_SECRET_KEY = 'koalitionsberegner.adminSecret';

/** The address fragment that opens the form. */
export const ADMIN_HASH = '#admin';

/** The session's storage, or null where the browser will not give us one. */
function sessionStore() {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    // Reading the property itself throws where storage is blocked.
    return null;
  }
}

/** The saved secret, or null. Storage that is unavailable reads as none. */
export function readSecret(storage = sessionStore()) {
  try {
    return storage?.getItem(ADMIN_SECRET_KEY) || null;
  } catch {
    return null;
  }
}

/** Keep the secret for this tab. False when storage would not take it. */
export function saveSecret(secret, storage = sessionStore()) {
  try {
    if (!storage) return false;
    storage.setItem(ADMIN_SECRET_KEY, secret);
    return true;
  } catch {
    return false;
  }
}

export function forgetSecret(storage = sessionStore()) {
  try {
    storage?.removeItem(ADMIN_SECRET_KEY);
  } catch {
    // Nothing was kept, so there is nothing to forget.
  }
}

/**
 * Wire the form. `onChange(isAdmin)` is called whenever the secret is saved or
 * forgotten here, so the import panel can follow.
 */
export function mountAdmin({
  elements,
  storage = sessionStore(),
  location = globalThis.location,
  onChange = () => {},
}) {
  const el = elements;

  function setMessage(text, kind = 'info') {
    el.message.textContent = text ?? '';
    el.message.className = 'msg' + (text ? ' msg-' + kind : '');
  }

  /** The form shows only at #admin; forgetting is offered once there is something to forget. */
  function render() {
    const asked = location?.hash === ADMIN_HASH;
    el.form.hidden = !asked;
    el.forget.hidden = !readSecret(storage);
    // The form sits inside the import panel, which is closed by default.
    if (asked) el.panel.open = true;
  }

  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    const secret = el.input.value.trim();
    if (!secret) return;
    el.input.value = '';
    if (!saveSecret(secret, storage)) {
      setMessage(t('admin.notSaved'), 'error');
      return;
    }
    setMessage(t('admin.saved'), 'ok');
    render();
    onChange(true);
  });

  el.forget.addEventListener('click', () => {
    forgetSecret(storage);
    setMessage(t('admin.forgotten'), 'info');
    render();
    onChange(false);
  });

  globalThis.addEventListener?.('hashchange', render);
  render();

  return {
    render,
    /** The server refused the secret: drop it, so the next request goes without. */
    forget() {
      forgetSecret(storage);
      render();
    },
  };
}
