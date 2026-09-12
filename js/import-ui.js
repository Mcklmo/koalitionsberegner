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
 * Importing is also the part that is sold. This module shows what the account
 * allows but never decides it: the form is disabled as a courtesy, and the
 * server refuses regardless — the two can disagree only in the safe direction.
 */

import { ApiError, ImportStatus } from './api.js';
import { validateImportForm } from './import-form.js';

const LOCAL_VALUE = 'local';

/** Why the server turned an import down, in the page's own words. */
const REFUSALS = {
  401: 'Log ind for at importere valg.',
  402: 'Import kræver et abonnement. Vælg en plan ovenfor.',
  429: 'Denne måneds importer er brugt op. Kvoten fornys ved månedsskiftet.',
};

export function mountImportUi({ api, elements, onSelect, bundled, onImported = () => {} }) {
  const el = elements;
  /** The election currently held for confirmation, if any. */
  let pending = null;
  let summaries = [];
  /** What the server last said this account may do; null when signed out. */
  let account = null;

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
    el.submit.disabled = isBusy || !allowed();
    el.submit.textContent = isBusy ? label : 'Hent valgresultat';
  }

  /** Whether importing is worth offering. The server still decides. */
  function allowed() {
    return account === null ? false : account.mayImport;
  }

  /** The standing note under the form: why importing is or is not available. */
  function renderAvailability() {
    if (account === null) {
      el.availability.textContent = REFUSALS[401];
    } else if (account.unlimited) {
      el.availability.textContent = '';
    } else if (!account.mayImport) {
      el.availability.textContent = account.limit > 0 ? REFUSALS[429] : REFUSALS[402];
    } else {
      const noun = account.remaining === 1 ? 'import' : 'importer';
      el.availability.textContent = `${account.remaining} ${noun} tilbage denne måned.`;
    }
    el.submit.disabled = !allowed();
  }

  function optionLabel(summary) {
    const where = summary.state ? `${summary.nation} — ${summary.state}` : summary.nation;
    return `${where} · ${summary.electionDate}`;
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
  }

  function renderPreview(election) {
    const seats = election.blocks.flatMap((b) => b.parties);
    el.previewTitle.textContent = election.title;
    // The identity line is what the server decided the request meant, not what
    // was typed — it is shown first because confirming it is the point of this
    // step.
    el.previewMeta.textContent =
      `${election.state ? `${election.nation} — ${election.state}` : election.nation}`
      + ` · ${election.electionDate} · ${election.totalSeats} mandater`
      + ` · flertal ved ${election.majoritySeats}`;
    // Nobody chose this address: the server searched for it. Naming it is how
    // the numbers can be checked against their source.
    el.previewSource.textContent = `Tallene er læst fra ${election.sourceUrl}`;
    show(el.previewSource, true);
    el.previewList.innerHTML = '';
    for (const party of seats) {
      const row = document.createElement('div');
      row.className = 'preview-row';
      const name = document.createElement('span');
      name.textContent = `${party.abbr} · ${party.name}`;
      const count = document.createElement('span');
      count.textContent = party.seats;
      row.append(name, count);
      el.previewList.appendChild(row);
    }
    show(el.preview, true);
  }

  function clearPreview() {
    pending = null;
    show(el.previewSource, false);
    show(el.preview, false);
  }

  async function submit(event) {
    event.preventDefault();
    clearPreview();
    setMessage('');

    const { valid, values, errors } = validateImportForm({
      year: el.year.value,
      nation: el.nation.value,
      subnation: el.subnation.value,
    });
    setFieldErrors(errors);
    if (!valid) return;

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

      busy(true, 'Henter…');
      const result = await api.importElection(values);
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
      pending = result;
      renderPreview(result.election);
      // The import has now been charged for, so the allowance has moved.
      await onImported();
      setMessage(
        'Kontrollér at det er det rigtige valg, og at tallene passer, før du gemmer.',
        'info'
      );
    } catch (error) {
      setMessage(refusal(error), 'error');
      // A refusal usually means the allowance moved; take the server's word for it.
      if (error instanceof ApiError && REFUSALS[error.status]) await onImported();
    } finally {
      busy(false);
    }
  }

  function refusal(error) {
    if (!(error instanceof ApiError)) return `Uventet fejl: ${error.message}`;
    return REFUSALS[error.status] ?? error.message;
  }

  async function confirm() {
    if (!pending) return;
    const { requestKey } = pending;
    try {
      el.confirm.disabled = true;
      const saved = await api.confirm(requestKey);
      clearPreview();
      await refreshPicker(saved.electionHash).catch(() => {});
      setMessage(saved.duplicate ? 'Valget var allerede gemt.' : 'Valget er gemt.', 'ok');
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
    const { requestKey } = pending;
    clearPreview();
    setMessage('Forkastet. Intet blev gemt.', 'info');
    await api.discardPreview(requestKey).catch(() => {});
  }

  async function select() {
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
  el.picker.addEventListener('change', select);

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
