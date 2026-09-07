/**
 * Composition root: picks the election source and wires the UI together.
 */

import { createApiClient } from './api.js';
import { mountCoalitionCalculator } from './app.js';
import { mountImportUi } from './import-ui.js';
import { Folketing2026Provider } from './providers/folketing-2026.js';

const apiBase = document.querySelector('meta[name="api-base"]')?.content ?? '';
const api = createApiClient({ baseUrl: apiBase });

const byId = (id) => document.getElementById(id);

let calculator = null;

/** Re-render the calculator for a different election. */
function render(election) {
  calculator = mountCoalitionCalculator(election);
}

// The bundled election renders immediately, so the page works with no backend.
const bundled = await Folketing2026Provider.getElection();
render(bundled);

byId('reset').addEventListener('click', () => calculator?.clearAll());

const importUi = mountImportUi({
  api,
  bundled,
  onSelect: render,
  elements: {
    form: byId('import-form'),
    sourceUrl: byId('f-url'),
    submit: byId('f-submit'),
    message: byId('import-message'),
    fieldErrors: { sourceUrl: byId('e-url') },
    preview: byId('preview'),
    previewTitle: byId('preview-title'),
    previewMeta: byId('preview-meta'),
    previewList: byId('preview-list'),
    confirm: byId('preview-confirm'),
    discard: byId('preview-discard'),
    picker: byId('picker'),
    pickerRow: byId('picker-row'),
  },
});

await importUi.start();
