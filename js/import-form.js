/**
 * Client-side validation of the import form.
 *
 * Pure: no DOM, no network. Everything here runs before anything is submitted
 * for extraction, so an obviously malformed request never reaches the backend.
 */

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Checks the date is well formed *and* a real calendar day. */
function isRealDate(value) {
  if (!ISO_DATE.test(value)) return false;
  const [y, m, d] = value.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
}

function isHttpUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  return (url.protocol === 'http:' || url.protocol === 'https:') && Boolean(url.host);
}

/**
 * @param {{sourceUrl?: string, nation?: string, state?: string, electionDate?: string}} input
 * @returns {{valid: boolean, values: object, errors: Record<string, string>}}
 */
export function validateImportForm(input = {}) {
  const values = {
    sourceUrl: (input.sourceUrl ?? '').trim(),
    nation: (input.nation ?? '').trim(),
    state: (input.state ?? '').trim(),
    electionDate: (input.electionDate ?? '').trim(),
  };
  const errors = {};

  if (!values.sourceUrl) {
    errors.sourceUrl = 'Angiv adressen på det officielle valgresultat.';
  } else if (!isHttpUrl(values.sourceUrl)) {
    errors.sourceUrl = 'Adressen skal være en http- eller https-adresse.';
  }

  if (!values.nation) errors.nation = 'Angiv et land.';
  if (!values.electionDate) {
    errors.electionDate = 'Angiv en valgdato.';
  } else if (!isRealDate(values.electionDate)) {
    errors.electionDate = 'Valgdatoen skal være en gyldig dato (ÅÅÅÅ-MM-DD).';
  }

  return {
    valid: Object.keys(errors).length === 0,
    values: { ...values, state: values.state || null },
    errors,
  };
}
