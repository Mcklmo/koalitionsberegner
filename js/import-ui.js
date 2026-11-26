/**
 * The import form: check the country is there, ask whether that
 * election has been imported before, show what the server found — which
 * election it decided was meant, and the seats it read — and save only when the
 * user confirms.
 *
 * The user types a place and, if they like, a year — not an address — and need
 * not spell it correctly. So the preview carries two things worth checking rather than one:
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
 * Importing spends money, so it is the owner's alone: the page imports only in
 * admin mode (js/admin.js), or on a local run where the server says importing
 * is open. This module never decides that — the server refuses regardless, and
 * the two can disagree only in the safe direction.
 *
 * Everyone else reaches the same form and is not turned away at it. The
 * election they wanted is worth knowing about, so the same form writes it down
 * as an issue in the tracker, to be imported later
 * (`POST /api/elections/requests`). The fields are the same either way; the
 * button says which of the two it will do.
 *
 * Without a year the user means "the latest election there". The server
 * answers with the elections that may mean — the previous one, and the next
 * one when it is less than a year away — and the user picks between the next
 * election's polls and the previous election's result. Picking fills in that
 * election's year and runs the ordinary import; with only one to offer, the
 * page picks it by itself.
 *
 * The election *picker* — search, grouping, keyboard nav — lives in
 * `js/picker.js` (plan 3, Section B). This module no longer renders it, but
 * still owns the one thing that must happen after a change here: telling the
 * picker to re-fetch, via the `refreshList` callback, and `requestOrImport`
 * lets the picker's own "ask for it"/"import it" fallback drive this same
 * form without a submit event to reuse.
 */

import { ApiError, ImportStatus } from './api.js';
import { validateImportForm } from './import-form.js';
import { forecastLabel, placeOf } from './picker.js';
import { formatDate, t } from './i18n.js';

/**
 * Why the server turned an import down, as the texts that say so in the page's
 * own words. Only the owner imports, so a refusal means the secret was wrong.
 */
const REFUSALS = {
  403: 'admin.wrongSecret',
};

/**
 * The same, for the request path, whose refusals are not the import's. Asking
 * reads no credential at all, so the tracker being away is the only refusal
 * left to word.
 */
const REQUEST_REFUSALS = {
  503: 'request.refusal.unavailable',
};

/** `YYYY-MM-DD` in the page's own words for a date. */
function formatIsoDate(iso) {
  const [year, month, day] = iso.split('-');
  return formatDate({ day: Number(day), month: Number(month), year });
}

export function mountImportUi({
  api,
  elements,
  onSelect,
  config = {},
  onWrongSecret = () => {},
  /** Told to re-fetch (and, given a hash, select it) after anything here
   *  changes the store: a confirm, or a lookup that found an existing
   *  election. `js/main.js` wires this to `js/picker.js`'s `refresh`. */
  refreshList = async () => {},
}) {
  const el = elements;
  /**
   * What is held for confirmation, if anything. `option` is set when it is one
   * of the forecasts in `offered`, and is what confirming sends.
   */
  let pending = null;
  /** An upcoming election's forecasts, while the user chooses between them. */
  let offered = null;
  /** Whether the page holds the owner's secret. The server still checks it. */
  let admin = false;
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
    if (isBusy) el.submit.textContent = label;
  }

  /**
   * Whether this page imports: the owner, or a local run with no secret. Never
   * when the backend has no parser, where every import would fail.
   */
  function allowed() {
    return (admin && Boolean(config.importsEnabled)) || Boolean(config.importsOpen);
  }

  /**
   * Whether the button should write the election down instead of importing it.
   *
   * Everyone who cannot import can: asking needs nothing, because nothing about
   * it searches, fetches or spends.
   */
  function canRequest() {
    return Boolean(config.requestsEnabled) && !allowed();
  }

  /**
   * The one place the button's state is decided: whether it works, and — when
   * nothing is under way — whether it imports or asks. The role can change in
   * the middle of a submit, and that must not hand the user a second click
   * racing the first over the preview.
   */
  function renderSubmit() {
    el.submit.disabled = submitting || !(allowed() || canRequest());
    if (!submitting) el.submit.textContent = t(allowed() ? 'import.submit' : 'request.submit');
  }

  /** The standing note under the form: what the button will do, if anything. */
  function renderAvailability() {
    if (allowed()) {
      el.availability.textContent = '';
    } else if (canRequest()) {
      // The button still works; it does something else. Saying which is the
      // difference between an offer and a dead end.
      el.availability.textContent = t('import.requestNote');
    } else {
      el.availability.textContent = t('import.unavailable');
    }
    renderSubmit();
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

  /**
   * The elections a yearless request may mean. Two buttons, or — when only the
   * previous election is worth offering — no question at all: its year is
   * filled in and the import carries on.
   */
  async function renderElectionPick(result) {
    const candidates = result.candidates ?? [];
    if (candidates.length === 0) {
      setMessage(t('import.failed', { error: t('pick.none') }), 'error');
      return;
    }
    if (candidates.length === 1) {
      await pickElection(candidates[0]);
      return;
    }
    // Discarding goes through the same button as a list of polls does.
    offered = { requestKey: result.requestKey, forecasts: [] };
    el.choicesTitle.textContent = t('pick.title', { where: placeOf(candidates[0]) });
    if (el.choicesHint) show(el.choicesHint, false);
    el.choicesList.innerHTML = '';
    for (const candidate of candidates) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'choice';
      const which = document.createElement('span');
      const date = formatIsoDate(candidate.electionDate);
      which.textContent = candidate.which === 'upcoming'
        ? t('pick.next', { date })
        : t('pick.previous', { date });
      const what = document.createElement('span');
      what.className = 'choice-note';
      // The resolver's name for it, as text: model output never becomes markup.
      what.textContent = candidate.title;
      button.append(which, what);
      button.addEventListener('click', () => pickElection(candidate));
      el.choicesList.appendChild(button);
    }
    show(el.choices, true);
    setMessage(t('pick.message'), 'info');
  }

  /** Ask again for the picked election, this time with its year. */
  async function pickElection(candidate) {
    // A second click while the first is importing would clear its preview.
    if (submitting) return;
    el.year.value = String(candidate.year);
    el.nation.value = candidate.nation;
    el.subnation.value = candidate.state ?? '';
    await requestOrImport({
      year: candidate.year,
      nation: candidate.nation,
      subnation: candidate.state ?? '',
    });
  }

  function renderChoices(result) {
    offered = { requestKey: result.requestKey, forecasts: result.forecasts };
    if (el.choicesHint) show(el.choicesHint, true);
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
    await requestOrImport({
      year: el.year.value,
      nation: el.nation.value,
      subnation: el.subnation.value,
    });
  }

  /**
   * The form's submit handler minus the DOM event and the fields: validates
   * `input`, then either files a request or runs the import, exactly as a
   * submit would. Callable directly, so `js/picker.js`'s "ask for it"/"import
   * it" fallback can drive this form from a parsed search query without a
   * click of its own (plan 3, B3).
   *
   * @returns {Promise<{valid: boolean, errors?: Record<string, string>}>}
   */
  async function requestOrImport(input) {
    clearPreview();
    clearChoices();
    setMessage('');

    const { valid, values, errors } = validateImportForm(input);
    setFieldErrors(errors);
    if (!valid) return { valid: false, errors };

    // Not the owner: the election is written down rather than fetched. Not
    // preceded by a lookup — the server answers 409 if it already holds the
    // election, which is the same round trip and one fewer.
    if (!allowed()) {
      if (canRequest()) await fileRequest(values);
      return { valid: true };
    }

    try {
      // Ask first: an election somebody already imported must never be looked
      // up and read again, whoever asked for it.
      busy(true, t('busy.checking'));
      const existing = await api.lookup(values);
      if (existing.status === ImportStatus.READY) {
        setMessage(t('import.alreadyImportedPick'), 'warn');
        await refreshList(existing.electionHash).catch(() => {});
        return { valid: true };
      }
      if (existing.status === ImportStatus.CHOOSE) {
        // Somebody read these polls a moment ago; choosing from them is free.
        renderChoices(existing);
        return { valid: true };
      }
      if (existing.status === ImportStatus.PICK) {
        // The latest there was looked up a moment ago; picking is free too.
        busy(false);
        await renderElectionPick(existing);
        return { valid: true };
      }

      // An import already running, or already read and waiting to be checked —
      // from an earlier click, a reload, another tab — is picked up where it
      // is rather than asked for again.
      let result = existing;
      if (existing.status === ImportStatus.PENDING) {
        busy(true, t('busy.fetching'));
        result = await api.getImport(existing.requestKey);
      } else if (existing.status !== ImportStatus.PREVIEW) {
        busy(true, t('busy.fetching'));
        result = await api.importElection(values);
      }
      await handleResult(result);
      return { valid: true };
    } catch (error) {
      setMessage(refusal(error), 'error');
      forgetOnRefusal(error);
      return { valid: true };
    } finally {
      busy(false);
    }
  }

  /** A 403 means the secret was wrong: drop it, so the page stops acting as the owner. */
  function forgetOnRefusal(error) {
    if (!(error instanceof ApiError) || error.status !== 403 || !admin) return;
    admin = false;
    renderAvailability();
    onWrongSecret();
  }

  /** Write the election down instead of importing it. Nothing is fetched or read. */
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
        await refreshList().catch(() => {});
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
      await refreshList(result.electionHash).catch(() => {});
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
      return;
    }
    if (result.status === ImportStatus.PICK) {
      busy(false);
      await renderElectionPick(result);
      return;
    }
    pending = result;
    renderPreview(result.election);
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
      await refreshList(saved.electionHash).catch(() => {});
      const kind = isForecast ? 'forecast' : 'election';
      setMessage(t(saved.duplicate ? `saved.${kind}Already` : `saved.${kind}`), 'ok');
      el.form.reset();
      setFieldErrors({});
      onSelect(saved.election, saved.electionHash);
    } catch (error) {
      setMessage(t('saved.failed', { message: error.message }), 'error');
      forgetOnRefusal(error);
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

  el.form.addEventListener('submit', submit);
  el.confirm.addEventListener('click', confirm);
  el.discard.addEventListener('click', discard);
  el.choicesDiscard.addEventListener('click', discardChoices);

  return {
    /** Follow the admin mode: whether the form imports or asks. */
    setAdmin(next) {
      admin = Boolean(next);
      renderAvailability();
    },

    /** Populate the picker (via `refreshList`); failures leave the bundled
     *  election in place. */
    async start() {
      renderAvailability();
      try {
        await refreshList();
      } catch {
        setMessage(t('archive.unreachable'), 'warn');
      }
    },

    /** Whether this page imports right now — `js/picker.js`'s fallback row
     *  says "import it" rather than "ask for it" when this is true. */
    isImportAllowed: () => allowed(),

    /** Whether anyone may file a request right now. */
    isRequestAllowed: () => canRequest(),

    /** Validate `input` and either file a request or run the import, exactly
     *  as this form's own submit does — for `js/picker.js`'s "ask for
     *  it"/"import it" fallback (plan 3, B3). */
    requestOrImport,
  };
}
