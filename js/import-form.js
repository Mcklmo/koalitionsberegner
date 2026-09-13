/**
 * Client-side validation of the import form.
 *
 * Three fields: the year, the nation, and — for a regional election — the
 * region within it. Spelling is deliberately *not* checked. The server's
 * resolver is there to read "Sachen-Anhalt" as Saxony-Anhalt and to say so in
 * the preview, and a form that rejected it first would be refusing the one
 * thing that makes this worth typing instead of hunting for a results page.
 *
 * So this only catches what no amount of interpretation can fix: a missing
 * country, and a year that is not a year. The year is read the way the backend
 * reads it (app/identity.py), including the digits a hand misses on the number
 * row, so the two cannot disagree about what is acceptable.
 */

import { t } from './i18n.js';

/** The years an election may be asked for. Mirrors MIN_YEAR/MAX_YEAR. */
export const MIN_YEAR = 1800;
export const MAX_YEAR = 2100;

/** Letters that are really digits, on a number row typed in a hurry. */
const YEAR_TYPOS = { o: '0', O: '0', l: '1', I: '1', i: '1' };

/**
 * The year as a number, or null when the text cannot be one.
 * @param {unknown} value
 */
export function parseYear(value) {
  if (typeof value === 'number') return Number.isInteger(value) ? inRange(value) : null;
  if (typeof value !== 'string') return null;
  const digits = value.trim().replace(/[oOlIi]/g, (ch) => YEAR_TYPOS[ch]);
  if (!/^[0-9]+$/.test(digits)) return null;
  return inRange(Number(digits));
}

function inRange(year) {
  return year >= MIN_YEAR && year <= MAX_YEAR ? year : null;
}

/**
 * @param {{year?: string|number, nation?: string, subnation?: string}} input
 * @returns {{valid: boolean, values: {year: number|null, nation: string, subnation: string|null},
 *            errors: Record<string, string>}}
 */
export function validateImportForm(input = {}) {
  const year = parseYear(input.year ?? '');
  const nation = (input.nation ?? '').trim();
  const subnation = (input.subnation ?? '').trim();
  const values = { year, nation, subnation: subnation || null };
  const errors = {};

  if (!String(input.year ?? '').trim()) {
    errors.year = t('form.yearMissing');
  } else if (year === null) {
    errors.year = t('form.yearInvalid', { min: MIN_YEAR, max: MAX_YEAR });
  }
  if (!nation) {
    errors.nation = t('form.nationMissing');
  }

  return { valid: Object.keys(errors).length === 0, values, errors };
}
