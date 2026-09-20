/**
 * Composition root: picks the election source and wires the UI together.
 *
 * Order matters here. The bundled election renders first, so the page is
 * useful before any network call resolves; then the public config says whether
 * this deployment takes requests and imports; then the import panel follows the
 * owner's admin mode, if this tab holds the secret.
 */

import { mountAdmin, readSecret } from './admin.js';
import { createApiClient } from './api.js';
import { mountCoalitionCalculator } from './app.js';
import { mountImportUi } from './import-ui.js';
import {
  language, languageName, languages, parseStrings, setLanguage, t, useStrings,
} from './i18n.js';
import { Folketing2026Provider } from './providers/folketing-2026.js';
import { buildPath, parseLocation, shareId } from './share.js';

const apiBase = document.querySelector('meta[name="api-base"]')?.content ?? '';

const byId = (id) => document.getElementById(id);

// Every text comes from the sheet, so without a sound one there is no page to
// wire up: a failed fetch or a parse error throws here and nothing below runs.
const strings = await fetch(new URL('./strings.csv', import.meta.url));
if (!strings.ok) throw new Error(`Could not load strings.csv: HTTP ${strings.status}`);
useStrings(parseStrings(await strings.text()));

// The markup is worded in Danish. It is reworded before anything renders, so a
// visitor in another language sees it for a moment at most.
document.documentElement.lang = language();
for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
for (const node of document.querySelectorAll('[data-i18n-placeholder]')) {
  node.placeholder = t(node.dataset.i18nPlaceholder);
}
for (const node of document.querySelectorAll('[data-i18n-title]')) node.title = t(node.dataset.i18nTitle);

// The privacy section is linked to as #privacy, from the page and from outside
// it; arriving there opens it rather than scrolling to a closed heading.
const privacy = byId('privacy');
const openPrivacy = () => {
  if (globalThis.location.hash === '#privacy') privacy.open = true;
};
globalThis.addEventListener('hashchange', openPrivacy);
openPrivacy();

// One option per column of the sheet, each language named in itself.
const languagePicker = byId('language');
for (const locale of languages()) {
  const option = document.createElement('option');
  option.value = locale;
  option.textContent = languageName(locale);
  languagePicker.append(option);
}
languagePicker.value = language();
// Everything on screen was worded in the old language, so a reload rewords it
// all at once rather than message by message.
languagePicker.addEventListener('change', () => {
  setLanguage(languagePicker.value);
  globalThis.location.reload();
});

// The secret is read per request rather than captured, because it can be
// saved or forgotten at any point while the page is open.
const api = createApiClient({ baseUrl: apiBase, getAdminSecret: () => readSecret() });

let calculator = null;

// The share button links to the election on screen; `null` while none of it is
// known to be stored, which is also when the button stays hidden — nothing to
// link to yet.
let currentId = null;
const shareButton = byId('share');
const shareNote = byId('share-note');

function setShareNote(text, kind = 'warn') {
  shareNote.textContent = text ?? '';
  shareNote.className = 'msg' + (text ? ' msg-' + kind : '');
  shareNote.hidden = !text;
}

/** Keep the address bar in step with the selection, for this election's id. */
function syncUrl(indices, total) {
  if (currentId) history.replaceState(null, '', buildPath({ id: currentId, indices, total }));
}

/** `electionHash` is this election's stored hash, or null when it has none (the bundled one). */
function setShareTarget(electionHash) {
  currentId = electionHash ? shareId(electionHash) : null;
  shareButton.hidden = !currentId;
  // Nothing to share means nothing to link to: an old `/e/<id>?...` left in
  // the address bar would otherwise outlive the button that pointed at it.
  if (!currentId) history.replaceState(null, '', buildPath({ id: null }));
}

// Filled in once the config and the picker's list have arrived. The bundled
// election renders before either exists, and it is one a person put in the
// repository, so the defaults are the truth until then.
let issuesUrl = '';
let summaries = () => [];

/**
 * How this election got into the store, so the calculator knows whether to say
 * that nobody checked it. The picker's list is where provenance lives; a hash
 * nothing in the list matches — the bundled election above all — is one a
 * person put there.
 */
function provenanceOf(electionHash) {
  if (!electionHash) return 'manual';
  return summaries().find((summary) => summary.electionHash === electionHash)
    ?.provenance ?? 'manual';
}

/** Re-render the calculator for a different election. */
function render(election, { initialSelection = [], electionHash = null } = {}) {
  calculator = mountCoalitionCalculator(election, {}, {
    initialSelection,
    onChange: syncUrl,
    provenance: provenanceOf(electionHash),
    issuesUrl,
  });
}

// The bundled election renders immediately, so the page works with no backend.
const bundled = await Folketing2026Provider.getElection();
render(bundled);

byId('reset').addEventListener('click', () => calculator?.clearAll());

async function share() {
  if (!currentId) return;
  const url = new URL(
    buildPath({ id: currentId, indices: calculator.selection(), total: calculator.total() }),
    globalThis.location.origin,
  ).toString();
  try {
    if (globalThis.navigator?.share) {
      await globalThis.navigator.share({ url });
      return;
    }
    if (!globalThis.navigator?.clipboard) throw new Error('no share mechanism available');
    await globalThis.navigator.clipboard.writeText(url);
    const original = t('share.button');
    shareButton.textContent = t('share.copied');
    setTimeout(() => { shareButton.textContent = original; }, 2000);
  } catch (error) {
    if (error?.name === 'AbortError') return; // the visitor closed the native share sheet
    setShareNote(t('share.unavailable'), 'error');
  }
}
shareButton.addEventListener('click', share);

const datePart = (iso) => iso.split('T')[0];

/** The stored election that is this bundled one, if the archive already holds it. */
function bundledTwin() {
  return importUi.summaries().find((summary) => summary.nation === bundled.nation
    && datePart(summary.electionDate) === datePart(bundled.electionDate) && !summary.forecast);
}

/** Enable sharing the election on screen once it turns out to be the bundled one's twin. */
function enableShareForBundled() {
  const twin = bundledTwin();
  if (!twin) return;
  setShareTarget(twin.electionHash);
  syncUrl(calculator.selection(), calculator.total());
}

/** The picker (and a resolved shared link) both select an election this way. */
function selectElection(election, electionHash) {
  setShareNote('');
  setShareTarget(electionHash);
  render(election, { electionHash });
  // The bundled election has no hash of its own; look for its stored twin so
  // the share button still works when the archive already holds it.
  if (!electionHash) enableShareForBundled();
}

// Without a reachable backend there is nothing to ask for and nothing to
// import; the bundled election still works, which is the point of the fallback.
const config = await api.getConfig().catch(() => ({
  requestsEnabled: false,
  importsEnabled: false,
  importsOpen: false,
}));
issuesUrl = config.issuesUrl ?? '';

// The donate/sponsor footer link and the "Supported by …" line (plan 3, C4).
// Both stay hidden until the owner sets them; api.js has already kept the
// link to only an https address, and the sponsor's name reaches the DOM as
// text, never as markup.
const supportLink = byId('support-link');
if (config.supportLink) {
  supportLink.href = config.supportLink;
  supportLink.hidden = false;
}
const sponsorNote = byId('sponsor-note');
if (config.supportSponsor) {
  sponsorNote.textContent = t('footer.sponsoredBy', { sponsor: config.supportSponsor });
  sponsorNote.hidden = false;
}

const importUi = mountImportUi({
  api,
  bundled,
  config,
  onSelect: selectElection,
  // The server refused the secret; the panel has already stopped importing.
  onWrongSecret: () => adminUi.forget(),
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
  },
});
summaries = () => importUi.summaries();

const adminUi = mountAdmin({
  onChange: (isAdmin) => importUi.setAdmin(isAdmin),
  elements: {
    panel: byId('import'),
    form: byId('admin-form'),
    input: byId('f-admin-secret'),
    forget: byId('admin-forget'),
    message: byId('admin-message'),
  },
});

await importUi.start();
importUi.setAdmin(Boolean(readSecret()));

// A shared link names an election by a prefix of its hash. Resolved after the
// picker's list is loaded, so picking it there afterwards shows the right
// option selected.
const link = parseLocation(globalThis.location);
if (link) {
  try {
    const result = await api.getElection(link.id);
    if (!result.election) throw new Error('no election in the response');
    byId('picker').value = result.electionHash;
    setShareTarget(result.electionHash);
    render(result.election, { initialSelection: link.indices, electionHash: result.electionHash });
    const total = calculator.total();
    setShareNote(link.seats !== null && link.seats !== total ? t('share.stale') : '');
  } catch {
    // An id nobody holds, or ambiguous, or unreachable: the bundled election
    // already on screen stays, said as plainly as the reason is irrelevant.
    setShareNote(t('share.unknown'));
    enableShareForBundled();
  }
} else {
  enableShareForBundled();
}
