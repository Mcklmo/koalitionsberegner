/**
 * Import flow and election picker.
 *
 * Drives four steps: validate the form locally, check the store for a duplicate
 * before any extraction runs, show what was extracted, and save only when the
 * user confirms. The picker feeds the selected election back to the renderer.
 */

import { ApiError, ImportStatus } from './api.js';
import { validateImportForm } from './import-form.js';

const LOCAL_VALUE = 'local';

export function mountImportUi({ api, elements, onSelect, bundled }) {
  const el = elements;
  /** The election currently held for confirmation, if any. */
  let pending = null;
  let summaries = [];

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
    el.submit.disabled = isBusy;
    el.submit.textContent = isBusy ? label : 'Hent valgresultat';
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
    el.previewMeta.textContent =
      `${election.state ? `${election.nation} — ${election.state}` : election.nation}`
      + ` · ${election.electionDate} · ${election.totalSeats} mandater`
      + ` · flertal ved ${election.majoritySeats}`;
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
    show(el.preview, false);
  }

  async function submit(event) {
    event.preventDefault();
    clearPreview();
    setMessage('');

    const { valid, values, errors } = validateImportForm({
      sourceUrl: el.sourceUrl.value,
      nation: el.nation.value,
      state: el.state.value,
      electionDate: el.electionDate.value,
    });
    setFieldErrors(errors);
    if (!valid) return;

    try {
      // Duplicate check first: a known election must never trigger extraction.
      busy(true, 'Tjekker…');
      const existing = await api.lookup(values);
      if (existing.status === ImportStatus.READY) {
        setMessage(
          'Dette valg er allerede importeret. Vælg det i listen ovenfor.', 'warn'
        );
        await refreshPicker(existing.electionHash).catch(() => {});
        return;
      }

      busy(true, 'Henter…');
      const result = await api.importElection(values);
      if (result.status === ImportStatus.READY) {
        setMessage('Dette valg er allerede importeret.', 'warn');
        await refreshPicker(result.electionHash).catch(() => {});
        return;
      }
      if (result.status === ImportStatus.FAILED) {
        setMessage(`Kunne ikke læse valgresultatet: ${result.error}`, 'error');
        return;
      }
      if (result.status === ImportStatus.PENDING) {
        setMessage('Behandling er stadig i gang. Prøv igen om lidt.', 'info');
        return;
      }
      pending = result;
      renderPreview(result.election);
      setMessage('Kontrollér de udtrukne tal, og gem hvis de er korrekte.', 'info');
    } catch (error) {
      setMessage(
        error instanceof ApiError ? error.message : `Uventet fejl: ${error.message}`, 'error'
      );
    } finally {
      busy(false);
    }
  }

  async function confirm() {
    if (!pending) return;
    const { electionHash } = pending;
    try {
      el.confirm.disabled = true;
      const saved = await api.confirm(electionHash);
      clearPreview();
      await refreshPicker(electionHash).catch(() => {});
      setMessage('Valget er gemt.', 'ok');
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
    const { electionHash } = pending;
    clearPreview();
    setMessage('Forkastet. Intet blev gemt.', 'info');
    await api.discardPreview(electionHash).catch(() => {});
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
    /** Populate the picker; failures leave the bundled election in place. */
    async start() {
      try {
        await refreshPicker();
      } catch {
        setMessage('Valgarkivet kan ikke nås — viser kun det indbyggede valg.', 'warn');
      }
    },
  };
}
