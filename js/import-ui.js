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
 * An account with no subscription behind it reaches the same form and is not
 * turned away at it. What it cannot do is make the server go and read pages,
 * which is the part that costs money; the election it wanted is still worth
 * knowing about, so the same button writes it down as an issue in the tracker
 * to be imported by hand (`POST /api/elections/requests`). The user types the
 * same three fields either way and the button keeps its name — from where they
 * stand they asked for an election, and the app did the most it could.
 */

import { ApiError, ImportStatus } from './api.js';
import { validateImportForm } from './import-form.js';

const LOCAL_VALUE = 'local';

/** Why the server turned an import down, in the page's own words. */
const REFUSALS = {
  401: 'Log ind for at importere eller ønske et valg.',
  402: 'Import kræver et abonnement. Vælg en plan ovenfor.',
  429: 'Denne måneds importer er brugt op. Kvoten fornys ved månedsskiftet.',
};

/** The same, for the request path, whose refusals are not the import's. */
const REQUEST_REFUSALS = {
  401: 'Log ind for at ønske et valg.',
  403: 'Bekræft din e-mailadresse, før du kan ønske et valg.',
  503: 'Ønskelisten er ikke tilgængelig lige nu. Prøv igen senere.',
};

/** What the button will do when there is no subscription behind the account. */
const REQUEST_NOTE = 'Uden abonnement henter vi ikke valget automatisk. '
  + 'Vi skriver det op som et ønske, og det bliver importeret manuelt.';

const COMPUTED_NOTE = 'mandater beregnet ud fra stemmeandele';

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
    el.submit.textContent = isBusy ? label : 'Hent valgresultat';
  }

  /** Whether importing is worth offering. The server still decides. */
  function allowed() {
    return account === null ? false : account.mayImport;
  }

  /**
   * Whether this account is one the request path is for: signed in, confirmed,
   * and on a tier that buys no imports at all.
   *
   * A subscriber who has merely run out for the month is deliberately *not*
   * one of them. They bought the imports, the allowance comes back at the
   * month's end, and the message under the form says so — turning that into a
   * hand-written issue would be a worse answer than waiting.
   *
   * Signed out is not one either: the request is filed against an account, and
   * the endpoint says so. The note under the form asks them to sign in, which
   * is free.
   */
  function canRequest() {
    if (!config.requestsEnabled || account === null) return false;
    if (account.emailVerified === false) return false;
    return !account.mayImport && !account.unlimited && account.limit <= 0;
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
    if (account === null) {
      el.availability.textContent = REFUSALS[401];
    } else if (account.unlimited) {
      el.availability.textContent = '';
    } else if (canRequest()) {
      // The button still works; it does something else. Saying which is the
      // difference between an offer and a dead end.
      el.availability.textContent = REQUEST_NOTE;
    } else if (!account.mayImport) {
      el.availability.textContent = account.limit > 0 ? REFUSALS[429] : REFUSALS[402];
    } else {
      const noun = account.remaining === 1 ? 'import' : 'importer';
      el.availability.textContent = `${account.remaining} ${noun} tilbage denne måned.`;
    }
    renderSubmit();
  }

  function optionLabel(summary) {
    const where = placeOf(summary);
    const label = summary.forecast
      ? `${where} · prognose ${forecastLabel(summary.forecast)}`
      : `${where} · ${summary.electionDate}`;
    // Only an administrator is told, since only they can change it.
    return account?.admin && summary.selected ? `${label} · offentlig` : label;
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
    if (bundled) {
      const option = document.createElement('option');
      option.value = LOCAL_VALUE;
      option.textContent = bundled.title;
      el.picker.appendChild(option);
    }
    for (const summary of summaries) {
      const option = document.createElement('option');
      option.value = summary.electionHash;
      option.textContent = optionLabel(summary);
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
      (forecast ? `Prognose fra ${forecastLabel(forecast)} for valget ${identity}` : identity)
      + ` · ${election.totalSeats} mandater`
      + ` · flertal ved ${election.majoritySeats}`;
    // Nobody chose this address: the server searched for it. Naming it is how
    // the numbers can be checked against their source.
    el.previewSource.textContent = forecast?.computed
      ? `Stemmeandelene er læst fra ${election.sourceUrl}; mandaterne er beregnet ud fra dem og er et skøn.`
      : `Tallene er læst fra ${election.sourceUrl}`;
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
    el.discard.textContent = pending?.option === undefined ? 'Forkast' : 'Tilbage til listen';
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
    el.choicesTitle.textContent = `${placeOf(first)} · valget afholdes senest ${first.electionDate}`;
    el.choicesList.innerHTML = '';
    result.forecasts.forEach((election, option) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'choice';
      const who = document.createElement('span');
      who.textContent = forecastLabel(election.forecast);
      const what = document.createElement('span');
      what.className = 'choice-note';
      what.textContent = `${election.totalSeats} mandater`
        + (election.forecast.computed ? ` · ${COMPUTED_NOTE}` : '');
      button.append(who, what);
      button.addEventListener('click', () => choose(option));
      el.choicesList.appendChild(button);
    });
    show(el.choices, true);
    setMessage('Valget er ikke afholdt endnu. Vælg en meningsmåling at regne på.', 'info');
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
    setMessage('Kontrollér tallene, før du gemmer prognosen.', 'info');
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
      busy(true, 'Tjekker…');
      const existing = await api.lookup(values);
      if (existing.status === ImportStatus.READY) {
        setMessage('Dette valg er allerede importeret. Vælg det i listen ovenfor.', 'warn');
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
        busy(true, 'Henter…');
        result = await api.getImport(existing.requestKey);
      } else if (existing.status !== ImportStatus.PREVIEW) {
        busy(true, 'Henter…');
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
      busy(true, 'Sender ønske…');
      const filed = await api.requestElection(values);
      const opening = filed.duplicate
        ? `Det valg er der allerede et ønske om (#${filed.number}).`
        : `Ønsket er noteret som #${filed.number}.`;
      setMessage(`${opening} Valget bliver importeret manuelt.`, 'ok');
      // The issue is public, so the number is worth something to follow.
      const link = document.createElement('a');
      link.href = filed.url;
      link.target = '_blank';
      link.rel = 'noreferrer noopener';
      link.textContent = 'Se ønsket';
      el.message.append(' ', link);
      el.form.reset();
      setFieldErrors({});
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Already imported. Showing it beats writing down a wish for it.
        setMessage('Dette valg er allerede importeret. Vælg det i listen ovenfor.', 'warn');
        await refreshPicker().catch(() => {});
        return;
      }
      setMessage(requestRefusal(error), 'error');
    } finally {
      busy(false);
    }
  }

  function requestRefusal(error) {
    if (!(error instanceof ApiError)) return `Uventet fejl: ${error.message}`;
    return REQUEST_REFUSALS[error.status] ?? `Kunne ikke sende ønsket: ${error.message}`;
  }

  /** Show what an import came back with, whether started now or picked up. */
  async function handleResult(result) {
    if (result.status === ImportStatus.READY) {
      // It turned out to be an election we already hold, asked for another way.
      setMessage('Dette valg er allerede importeret.', 'warn');
      await refreshPicker(result.electionHash).catch(() => {});
      return;
    }
    if (result.status === ImportStatus.FAILED) {
      setMessage(`Kunne ikke finde valgresultatet: ${result.error}`, 'error');
      return;
    }
    if (result.status === ImportStatus.PENDING) {
      setMessage('Behandling er stadig i gang. Prøv igen om lidt.', 'info');
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
    setMessage(
      'Kontrollér at det er det rigtige valg, og at tallene passer, før du gemmer.',
      'info'
    );
  }

  function refusal(error) {
    if (!(error instanceof ApiError)) return `Uventet fejl: ${error.message}`;
    return REFUSALS[error.status] ?? error.message;
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
      const noun = isForecast ? 'Prognosen' : 'Valget';
      setMessage(saved.duplicate ? `${noun} var allerede gemt.` : `${noun} er gemt.`, 'ok');
      el.form.reset();
      setFieldErrors({});
      onSelect(saved.election);
    } catch (error) {
      setMessage(`Kunne ikke gemme valget: ${error.message}`, 'error');
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
    setMessage('Forkastet. Intet blev gemt.', 'info');
    await api.discardPreview(requestKey).catch(() => {});
  }

  async function discardChoices() {
    if (!offered) return;
    const { requestKey } = offered;
    clearPreview();
    clearChoices();
    setMessage('Forkastet. Intet blev gemt.', 'info');
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
          ? 'Valget er nu synligt for alle, også uden login.'
          : 'Valget vises nu kun for brugere, der er logget ind.',
        'ok'
      );
    } catch (error) {
      el.curate.checked = !wanted;
      setMessage(`Kunne ikke ændre synligheden: ${refusal(error)}`, 'error');
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
      setMessage(`Kunne ikke hente valget: ${error.message}`, 'error');
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
        setMessage('Valgarkivet kan ikke nås — viser kun det indbyggede valg.', 'warn');
      }
    },
  };
}
