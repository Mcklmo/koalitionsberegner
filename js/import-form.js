/**
 * Client-side validation of the import form.
 *
 * The form is one field: the results URL. Nation, region and date are inferred
 * from the page by the extraction agent, so there is nothing else to check
 * before submitting — but a malformed URL is still worth catching here rather
 * than after a round trip.
 */

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
 * @param {{sourceUrl?: string}} input
 * @returns {{valid: boolean, values: {sourceUrl: string}, errors: Record<string, string>}}
 */
export function validateImportForm(input = {}) {
  const values = { sourceUrl: (input.sourceUrl ?? '').trim() };
  const errors = {};

  if (!values.sourceUrl) {
    errors.sourceUrl = 'Angiv adressen på det officielle valgresultat.';
  } else if (!isHttpUrl(values.sourceUrl)) {
    errors.sourceUrl = 'Adressen skal være en http- eller https-adresse.';
  }

  return { valid: Object.keys(errors).length === 0, values, errors };
}
