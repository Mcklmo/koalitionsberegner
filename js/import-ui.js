/**
 * Import flow and election picker.
 *
 * Drives four steps: check the year and the country are there, ask whether that
 * election has been imported before, show what the server found — which
 * election it decided was meant, and the seats it read — and save only when the
 * user confirms.
 *
 * The user types a year and a place, not an address, and need not spell it
 * correctly. So the preview carries two things worth checking rather than one:
 * the numbers, and the *identity* — if "Sachen-Anhalt 2026" was read as some
 * other election, this is where that shows, and the page it was read from is
 * named so it can be opened. That confirmation is the only thing standing
 * between a misread request and the store.
 *
 * An election that has not been held has no seats to confirm, only polls. For
 * one of those the server answers with a list — the newest polls it could read,
 * each already turned into seats — and the user picks one to preview and save.
 * A poll that gave only vote shares had its seats computed by the server, and
 * both the list and the preview say so: those numbers are ours, not the
 * pollster's.
 *
 * An administrator also decides which stored elections a signed-out visitor
 * sees: a checkbox next to the picker says whether the chosen one is public and
 * changes it. It is shown only to an account the server calls an administrator,
 * and the server checks again.
 *
 * Importing is also the part that is sold. This module shows what the account
 * allows but never decides it: the form is disabled as a courtesy, and the
 * server refuses regardless — the two can disagree only in the safe direction.
 *
 * Anyone who may not import — signed out included — reaches the same form and
 * is not turned away at it. What it cannot do is make the server go and read pages,
 * which is the part that costs money; the election it wanted is still worth
 * knowing about, so the same button writes it down as an issue in the tracker
 * to be imported by hand (`POST /api/elections/requests`). The user types the
 * same three fields either way and the button keeps its name — from where they
 * stand they asked for an election, and the app did the most it could.
 */

import { ApiError, ImportStatus } from './api.js';
import { validateImportForm } from './import-form.js';
import { t } from './i18n.js';

const LOCAL_VALUE = 'local';

/** Why the server turned an import down, as the texts that say so in the page's own words. */
const REFUSALS = {
  401: 'import.refusal.signIn',
  402: 'import.refusal.subscription',
  429: 'import.refusal.usedUp',
};

/**
 * The same, for the request path, whose refusals are not the import's. Asking
 * reads no credential at all, so the tracker being away is the only refusal
 * left to word.
 */
const REQUEST_REFUSALS = {
  503: 'request.refusal.unavailable',
};

/** "Voxmeter · 2026-09-07" */
const forecastLabel = (forecast) => `${forecast.publisher} · ${forecast.publishedOn}`;

const placeOf = (election) =>
  election.state ? `${election.nation} — ${election.state}` : election.nation;

export function mountImportUi({
  api,
  elements,
  onSelect,
  bundled,
  config = {},
  onImported = () => {},
}) {
  const el = elements;
  /**
   * What is held for confirmation, if anything. `option` is set when it is one
   * of the forecasts in `offered`, and is what confirming sends.
   */
  let pending = null;
  /** An upcoming election's forecasts, while the user chooses between them. */
  let offered = null;
  let summaries = [];
  /** What the server last said this account may do; null when signed out. */
  let account = null;
  /** True while a submit is under way, from the lookup to the last message. */
  let submitting = false;

  const show = (node, visible) => {
    node.hidden = !visible;
  };

  function setMessage(text, kind = 'info') {
    el.message.textContent = text ?? '';
    el.message.className = 'msg' + (text ? ' msg-' + kind : '');
  }

  function setFieldErrors(errors) {
    for (const [field, node] of Object.entries(el.fieldErrors)) {
      node.textContent = errors[field] ?? '';
    }
  }

  function busy(isBusy, label) {
    submitting = isBusy;
    renderSubmit();
    el.submit.textContent = isBusy ? label : t('import.submit');
  }

  /** Whether importing is worth offering. The server still decides. */
  function allowed() {
    return account === null ? false : account.mayImport;
  }

  /**
   * Whether the button should write the election down instead of importing it.
   *
   * Everyone who cannot import can, signed out included — asking needs no
   * account, because nothing about it searches, fetches or spends. A visitor
   * who never signs in is exactly the person most likely to find the election
   * missing, and turning them away would lose the one signal this exists for.
   */
  function canRequest() {
    return Boolean(config.requestsEnabled) && !allowed();
  }

  /**
   * The one place the button's state is decided. The account is re-read in the
   * middle of a submit, and that must not hand the user a second click racing
   * the first over the preview.
   */
  function renderSubmit() {
    el.submit.disabled = submitting || !(allowed() || canRequest());
  }

  /** The standing note under the form: why importing is or is not available. */
  function renderAvailability() {
    if (canRequest()) {
      // The button still works; it does something else. Saying which is the
      // difference between an offer and a dead end. Checked before the rest
      // because it is true of a signed-out visitor too.
      el.availability.textContent = t('import.requestNote');
    } else if (account === null) {
      el.availability.textContent = t(REFUSALS[401]);
    } else if (account.unlimited) {
      el.availability.textContent = '';
    } else if (!account.mayImport) {
      el.availability.textContent = t(account.limit > 0 ? REFUSALS[429] : REFUSALS[402]);
    } else {
      el.availability.textContent = t('import.remaining', { remaining: account.remaining });
    }
    renderSubmit();
  }

  function optionLabel(summary) {
    const where = placeOf(summary);
    const label = summary.forecast
      ? t('picker.forecast', { where, label: forecastLabel(summary.forecast) })
      : `${where} · ${summary.electionDate}`;
    // Only an administrator is told, since only they can change it.
    return account?.admin && summary.selected ? `${label} · ${t('picker.public')}` : label;
  }

  /** The stored election the picker is on; null for the bundled one. */
  function chosen() {
    return summaries.find((summary) => summary.electionHash === el.picker.value) ?? null;
  }

  function renderCuration() {
    const summary = chosen();
    show(el.curateRow, Boolean(account?.admin && summary));
    el.curate.checked = Boolean(summary?.selected);
  }

  function renderPicker() {
    el.picker.innerHTML = '';
    const entries = summaries.map((summary) => [summary.electionHash, optionLabel(summary)]);
    if (bundled) entries.push([LOCAL_VALUE, bundled.title]);
    // Ignoring the separators puts a nation's own election before its regions'.
    entries.sort(([, a], [, b]) => a.localeCompare(b, undefined, { ignorePunctuation: true }));
    for (const [value, label] of entries) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      el.picker.appendChild(option);
    }
    show(el.pickerRow, el.picker.options.length > 1);
    renderCuration();
  }

  async function refreshPicker(selectHash) {
    try {
      summaries = await api.listElections();
    } catch (error) {
      // A missing backend is not fatal: the bundled election still renders.
      summaries = [];
      renderPicker();
      throw error;
    }
    renderPicker();
    if (selectHash) el.picker.value = selectHash;
    renderCuration();
  }

  function renderPreview(election) {
    const seats = election.blocks.flatMap((b) => b.parties);
    el.previewTitle.textContent = election.title;
    // The identity line is what the server decided the request meant, not what
    // was typed — it is shown first because confirming it is the point of this
    // step.
    const identity = `${placeOf(election)} · ${election.electionDate}`;
    const forecast = election.forecast;
    el.previewMeta.textContent =
      (forecast ? t('preview.forecastFor', { label: forecastLabel(forecast), identity }) : identity)
      + ` · ${t('seats', { count: election.totalSeats })}`
      + ` · ${t('preview.majorityAt', { majority: election.majoritySeats })}`;
    // Nobody chose this address: the server searched for it. Naming it is how
    // the numbers can be checked against their source.
    el.previewSource.textContent = forecast?.computed
      ? t('preview.sourceComputed', { url: election.sourceUrl })
      : t('preview.source', { url: election.sourceUrl });
    show(el.previewSource, true);
    el.previewList.innerHTML = '';
    for (const party of seats) {
      const row = document.createElement('div');
      row.className = 'preview-row';
      const name = document.createElement('span');
      // Both names are shown here, because this is where they are checked.
      name.textContent = party.localName && party.localName !== party.name
        ? `${party.abbr} · ${party.localName} (${party.name})`
        : `${party.abbr} · ${party.name}`;
      const count = document.createElement('span');
      count.textContent = party.seats;
      row.append(name, count);
      el.previewList.appendChild(row);
    }
    // From the list, stepping back is not throwing anything away.
    el.discard.textContent = t(pending?.option === undefined ? 'discard' : 'preview.backToList');
    show(el.preview, true);
  }

  function clearPreview() {
    pending = null;
    show(el.previewSource, false);
    show(el.preview, false);
  }

  function renderChoices(result) {
    offered = { requestKey: result.requestKey, forecasts: result.forecasts };
    const [first] = result.forecasts;
    el.choicesTitle.textContent = t('choices.title', { where: placeOf(first), date: first.electionDate });
    el.choicesList.innerHTML = '';
    result.forecasts.forEach((election, option) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'choice';
      const who = document.createElement('span');
      who.textContent = forecastLabel(election.forecast);
      const what = document.createElement('span');
      what.className = 'choice-note';
      what.textContent = t('seats', { count: election.totalSeats })
        + (election.forecast.computed ? ` · ${t('seats.computed')}` : '');
      button.append(who, what);
      button.addEventListener('click', () => choose(option));
      el.choicesList.appendChild(button);
    });
    show(el.choices, true);
    setMessage(t('choices.message'), 'info');
  }

  function clearChoices() {
    offered = null;
    show(el.choices, false);
  }

  function choose(option) {
    if (!offered) return;
    const election = offered.forecasts[option];
    pending = { requestKey: offered.requestKey, option, election };
    show(el.choices, false);
    renderPreview(election);
    setMessage(t('choices.check'), 'info');
  }

  async function submit(event) {
    event.preventDefault();
    // The button is disabled meanwhile, but a second submit must not slip past
    // it: it would clear the preview the first one is about to show.
    if (submitting) return;
    clearPreview();
    clearChoices();
    setMessage('');

    const { valid, values, errors } = validateImportForm({
      year: el.year.value,
      nation: el.nation.value,
      subnation: el.subnation.value,
    });
    setFieldErrors(errors);
    if (!valid) return;

    // No subscription behind this account: the election is written down rather
    // than fetched. Not preceded by a lookup — the server answers 409 if it
    // already holds the election, which is the same round trip and one fewer.
    if (!allowed() && canRequest()) {
      await fileRequest(values);
      return;
    }

    try {
      // Ask first: an election somebody already imported must never be looked
      // up and read again, whoever asked for it.
      busy(true, t('busy.checking'));
      const existing = await api.lookup(values);
      if (existing.status === ImportStatus.READY) {
        setMessage(t('import.alreadyImportedPick'), 'warn');
        await refreshPicker(existing.electionHash).catch(() => {});
        return;
      }
      if (existing.status === ImportStatus.CHOOSE) {
        // Somebody read these polls a moment ago; choosing from them is free.
        renderChoices(existing);
        return;
      }

      // An import already running, or already read and waiting to be checked —
      // from an earlier click, a reload, another tab — is picked up where it
      // is. Importing again would reserve quota just to join it, and be refused
      // outright if that import spent the last of the month.
      let result = existing;
      if (existing.status === ImportStatus.PENDING) {
        busy(true, t('busy.fetching'));
        result = await api.getImport(existing.requestKey);
      } else if (existing.status !== ImportStatus.PREVIEW) {
        busy(true, t('busy.fetching'));
        result = await api.importElection(values);
      }
      await handleResult(result);
    } catch (error) {
      setMessage(refusal(error), 'error');
      // A refusal usually means the allowance moved; take the server's word for it.
      if (error instanceof ApiError && REFUSALS[error.status]) await onImported();
    } finally {
      busy(false);
    }
  }

  /**
   * Write the election down instead of importing it. Costs no quota, so the
   * account is not re-read afterwards: nothing about it changed.
   */
  async function fileRequest(values) {
    try {
      busy(true, t('busy.requesting'));
      const filed = await api.requestElection(values);
      const opening = filed.duplicate
        ? t('request.duplicate', { number: filed.number })
        : t('request.filed', { number: filed.number });
      setMessage(`${opening} ${t('request.byHand')}`, 'ok');
      // The issue is public, so the number is worth something to follow.
      const link = document.createElement('a');
      link.href = filed.url;
      link.target = '_blank';
      link.rel = 'noreferrer noopener';
      link.textContent = t('request.link');
      el.message.append(' ', link);
      el.form.reset();
      setFieldErrors({});
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Already imported. Showing it beats writing down a wish for it.
        setMessage(t('import.alreadyImportedPick'), 'warn');
        await refreshPicker().catch(() => {});
        return;
      }
      setMessage(requestRefusal(error), 'error');
    } finally {
      busy(false);
    }
  }

  function requestRefusal(error) {
    if (!(error instanceof ApiError)) return t('error.unexpected', { message: error.message });
    const key = REQUEST_REFUSALS[error.status];
    return key ? t(key) : t('request.failed', { message: error.message });
  }

  /** Show what an import came back with, whether started now or picked up. */
  async function handleResult(result) {
    if (result.status === ImportStatus.READY) {
      // It turned out to be an election we already hold, asked for another way.
      setMessage(t('import.alreadyImported'), 'warn');
      await refreshPicker(result.electionHash).catch(() => {});
      return;
    }
    if (result.status === ImportStatus.FAILED) {
      setMessage(t('import.failed', { error: result.error }), 'error');
      return;
    }
    if (result.status === ImportStatus.PENDING) {
      setMessage(t('import.pending'), 'info');
      return;
    }
    if (result.status === ImportStatus.CHOOSE) {
      renderChoices(result);
      await onImported();
      return;
    }
    pending = result;
    renderPreview(result.election);
    // The import has been charged for, so the allowance has moved.
    await onImported();
    setMessage(t('preview.check'), 'info');
  }

  function refusal(error) {
    if (!(error instanceof ApiError)) return t('error.unexpected', { message: error.message });
    const key = REFUSALS[error.status];
    return key ? t(key) : error.message;
  }

  async function confirm() {
    if (!pending) return;
    const { requestKey, option } = pending;
    const isForecast = option !== undefined;
    try {
      el.confirm.disabled = true;
      const saved = await api.confirm(requestKey, isForecast ? { option } : {});
      clearPreview();
      clearChoices();
      await refreshPicker(saved.electionHash).catch(() => {});
      const kind = isForecast ? 'forecast' : 'election';
      setMessage(t(saved.duplicate ? `saved.${kind}Already` : `saved.${kind}`), 'ok');
      el.form.reset();
      setFieldErrors({});
      onSelect(saved.election);
    } catch (error) {
      setMessage(t('saved.failed', { message: error.message }), 'error');
    } finally {
      el.confirm.disabled = false;
    }
  }

  async function discard() {
    if (!pending) return;
    if (pending.option !== undefined && offered) {
      // One poll out of a list was never staged on its own; go back to the list.
      clearPreview();
      show(el.choices, true);
      setMessage('');
      return;
    }
    const { requestKey } = pending;
    clearPreview();
    setMessage(t('discarded'), 'info');
    await api.discardPreview(requestKey).catch(() => {});
  }

  async function discardChoices() {
    if (!offered) return;
    const { requestKey } = offered;
    clearPreview();
    clearChoices();
    setMessage(t('discarded'), 'info');
    await api.discardPreview(requestKey).catch(() => {});
  }

  async function curate() {
    const summary = chosen();
    if (!summary) return;
    const wanted = el.curate.checked;
    el.curate.disabled = true;
    try {
      const saved = await api.setSelected(summary.electionHash, wanted);
      summaries = summaries.map((s) => (s.electionHash === saved.electionHash ? saved : s));
      renderPicker();
      el.picker.value = saved.electionHash;
      renderCuration();
      setMessage(
        saved.selected
          ? t('curate.nowPublic')
          : t('curate.nowPrivate'),
        'ok'
      );
    } catch (error) {
      el.curate.checked = !wanted;
      setMessage(t('curate.failed', { message: refusal(error) }), 'error');
    } finally {
      el.curate.disabled = false;
    }
  }

  async function select() {
    renderCuration();
    const value = el.picker.value;
    if (value === LOCAL_VALUE) {
      onSelect(bundled);
      return;
    }
    try {
      const result = await api.getElection(value);
      if (result.election) onSelect(result.election);
    } catch (error) {
      setMessage(t('select.failed', { message: error.message }), 'error');
    }
  }

  el.form.addEventListener('submit', submit);
  el.confirm.addEventListener('click', confirm);
  el.discard.addEventListener('click', discard);
  el.choicesDiscard.addEventListener('click', discardChoices);
  el.picker.addEventListener('change', select);
  el.curate.addEventListener('change', curate);

  return {
    /**
     * Follow the signed-in account: what may be imported, and which elections
     * are visible, both change when somebody signs in or out.
     */
    async setAccount(next) {
      account = next;
      renderAvailability();
      await refreshPicker(el.picker.value || undefined).catch(() => {});
    },

    /** Populate the picker; failures leave the bundled election in place. */
    async start() {
      renderAvailability();
      try {
        await refreshPicker();
      } catch {
        setMessage(t('archive.unreachable'), 'warn');
      }
    },
  };
}
