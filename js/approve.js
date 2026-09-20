/**
 * The outreach approval page: `/approve/<token>` (doc/plans/04-reddit-outreach.md).
 *
 * Reached from an emailed link, on the owner's phone or laptop. Viewing needs
 * only the token in the URL; sending or rejecting needs `ADMIN_SECRET` as well,
 * read the same way the import panel reads it (`js/admin.js`) — pasted once,
 * kept in `sessionStorage` for the tab.
 *
 * Everything from the server — the thread's title, its excerpt, the proposed
 * reply — is Reddit text, untrusted the same way an imported page's text is:
 * it is written with `textContent`, never `innerHTML`, so nothing in it can
 * become markup.
 */

import { readSecret, saveSecret } from './admin.js';
import { buttonStates } from './approve-state.js';
import { ADMIN_SECRET_HEADER } from './api.js';
import { language, parseStrings, t, useStrings } from './i18n.js';

const byId = (id) => document.getElementById(id);

const strings = await fetch(new URL('./strings.csv', import.meta.url));
if (!strings.ok) throw new Error(`Could not load strings.csv: HTTP ${strings.status}`);
useStrings(parseStrings(await strings.text()));

document.documentElement.lang = language();
for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);

const token = decodeURIComponent(location.pathname.replace(/^\/approve\//, '').replace(/\/$/, ''));

const el = {
  loading: byId('outreach-loading'),
  notFound: byId('outreach-not-found'),
  form: byId('outreach-form'),
  subreddit: byId('outreach-subreddit'),
  permalink: byId('outreach-permalink'),
  excerpt: byId('outreach-excerpt'),
  election: byId('outreach-election'),
  reply: byId('outreach-reply'),
  secret: byId('outreach-secret'),
  send: byId('outreach-send'),
  reject: byId('outreach-reject'),
  message: byId('outreach-message'),
};

function setMessage(text, kind = 'info') {
  el.message.textContent = text ?? '';
  el.message.className = 'msg' + (text ? ` msg-${kind}` : '');
}

/** A link's own text is its address — nothing from the draft becomes markup. */
function fillLink(node, url) {
  node.href = url;
  node.textContent = url;
}

async function callApi(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : response.statusText;
    throw new Error(String(detail));
  }
  return body;
}

let draft = null;

async function load() {
  try {
    draft = await callApi(`/api/outreach/approval/${encodeURIComponent(token)}`);
  } catch {
    el.loading.hidden = true;
    el.notFound.hidden = false;
    return;
  }
  el.loading.hidden = true;
  el.form.hidden = false;
  el.subreddit.textContent = `r/${draft.subreddit}`;
  fillLink(el.permalink, draft.permalink);
  el.excerpt.textContent = draft.excerpt;
  fillLink(el.election, draft.link);
  el.election.textContent = draft.election_title;
  el.reply.value = draft.reply_text;
  const saved = readSecret();
  if (saved) el.secret.value = saved;
  const offered = buttonStates(draft.status);
  el.send.disabled = !offered.send;
  el.reject.disabled = !offered.reject;
  if (draft.last_error) setMessage(draft.last_error, 'error');
}

function secretHeaders() {
  const secret = el.secret.value.trim();
  if (secret) saveSecret(secret);
  return secret ? { [ADMIN_SECRET_HEADER]: secret } : {};
}

async function send() {
  if (!el.secret.value.trim()) {
    setMessage(t('admin.label'), 'error');
    return;
  }
  el.send.disabled = true;
  el.reject.disabled = true;
  try {
    const edited = el.reply.value !== draft.reply_text;
    await callApi(`/api/outreach/approval/${encodeURIComponent(token)}/send`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...secretHeaders() },
      body: JSON.stringify(edited ? { reply_text: el.reply.value } : {}),
    });
    setMessage(t('outreach.sent'), 'ok');
  } catch (error) {
    setMessage(t('error.unexpected', { message: error.message }), 'error');
    el.send.disabled = false;
    el.reject.disabled = false;
  }
}

async function reject() {
  if (!el.secret.value.trim()) {
    setMessage(t('admin.label'), 'error');
    return;
  }
  el.send.disabled = true;
  el.reject.disabled = true;
  try {
    await callApi(`/api/outreach/approval/${encodeURIComponent(token)}/reject`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...secretHeaders() },
    });
    setMessage(t('outreach.rejected'), 'ok');
  } catch (error) {
    setMessage(t('error.unexpected', { message: error.message }), 'error');
    el.send.disabled = false;
    el.reject.disabled = false;
  }
}

el.send.addEventListener('click', send);
el.reject.addEventListener('click', reject);

await load();
