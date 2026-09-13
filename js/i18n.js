/**
 * The page's words, in every language `strings.csv` has a column for.
 *
 * The sheet's first column is the key a text is asked for by; every other
 * column is headed by a locale code and holds that language's texts. Adding a
 * language is adding a column. The sheet is read once at startup, and anything
 * wrong with it — a stray quote, a missing translation, a broken placeholder —
 * throws there, so a bad sheet never reaches a visitor half-translated.
 *
 * Which language is decided once per visit: the one the visitor chose before,
 * or else the browser's own language where the sheet has it, or else English.
 * Changing it is remembered and reloads the page, so nothing that is already on
 * screen has to be translated in place.
 *
 * Nothing is looked up when a module is imported. Every string is read when it
 * is shown, which is what lets a test pin the language before it renders.
 *
 * A text is a template. `{name}` inserts a value; `{name, plural, one {…}
 * other {…}}` picks a wording by the language's own plural rules, so plurals
 * and word order stay each language's own business.
 */

/** Where the visitor's choice of language is remembered. */
const LANGUAGE_KEY = 'koalitionsberegner.language';

/** The language for anybody whose own the sheet doesn't have. */
const FALLBACK = 'en';

const PLURAL_CATEGORIES = ['zero', 'one', 'two', 'few', 'many', 'other'];

/**
 * RFC 4180 rows, each with the line it starts on. Blank lines are skipped;
 * anything else that isn't well-formed CSV throws.
 */
function parseCsv(text) {
  const rows = [];
  let cells = [];
  let field = '';
  let line = 1;
  let rowLine = 1;
  let quoteLine = 0;
  let quoted = false;
  let i = 0;
  const fail = (at, message) => { throw new Error(`strings.csv line ${at}: ${message}`); };
  const endRow = () => {
    cells.push(field);
    if (cells.length > 1 || cells[0] !== '') rows.push({ line: rowLine, cells });
    cells = [];
    field = '';
  };

  while (i < text.length) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i += 2;
      } else if (c === '"') {
        quoted = false;
        i += 1;
        if (i < text.length && !',\r\n'.includes(text[i])) fail(line, 'text after a closing quote');
      } else {
        if (c === '\n') line += 1;
        field += c;
        i += 1;
      }
    } else if (c === '"') {
      if (field !== '') fail(line, 'a quote inside an unquoted cell');
      quoted = true;
      quoteLine = line;
      i += 1;
    } else if (c === ',') {
      cells.push(field);
      field = '';
      i += 1;
    } else if (c === '\r' || c === '\n') {
      if (c === '\r' && text[i + 1] !== '\n') fail(line, 'a carriage return without a line feed');
      endRow();
      i += c === '\r' ? 2 : 1;
      line += 1;
      rowLine = line;
    } else {
      field += c;
      i += 1;
    }
  }
  if (quoted) fail(quoteLine, 'a quote that is never closed');
  endRow();
  return rows;
}

/** A function of the values a template names; throws on a malformed template. */
function compileTemplate(source, locale) {
  const parts = [];
  let literal = '';
  let i = 0;
  const fail = (at, message) => { throw new Error(`column ${at + 1}: ${message}`); };
  const match = (pattern) => {
    pattern.lastIndex = i;
    const m = pattern.exec(source);
    if (m) i = pattern.lastIndex;
    return m;
  };

  while (i < source.length) {
    const c = source[i];
    if (c === '}') fail(i, 'a "}" with no "{" before it');
    if (c !== '{') {
      literal += c;
      i += 1;
      continue;
    }
    if (literal) parts.push(literal);
    literal = '';
    const start = i;

    const variable = match(/\{\s*(\w+)\s*\}/y);
    if (variable) {
      const [, name] = variable;
      parts.push((vars) => String(vars[name]));
      continue;
    }

    const plural = match(/\{\s*(\w+)\s*,\s*plural\s*,/y);
    if (!plural) fail(start, 'a "{" that is neither {name} nor {name, plural, …}');
    const [, name] = plural;
    const branches = {};
    for (let branch; (branch = match(/\s*(\w+)\s*\{([^{}]*)\}/y));) {
      const [, category, text] = branch;
      if (!PLURAL_CATEGORIES.includes(category)) fail(start, `"${category}" is not a plural category`);
      if (Object.hasOwn(branches, category)) fail(start, `"${category}" is given twice`);
      branches[category] = text;
    }
    if (!match(/\s*\}/y)) fail(start, `the plural for "${name}" is not closed properly`);
    if (!Object.hasOwn(branches, 'other')) fail(start, `the plural for "${name}" has no "other"`);
    const rules = new Intl.PluralRules(locale);
    parts.push((vars) => branches[rules.select(Number(vars[name]))] ?? branches.other);
  }
  if (literal) parts.push(literal);

  return (vars = {}) => parts.map((part) => (typeof part === 'string' ? part : part(vars))).join('');
}

/**
 * The sheet as `{ locales, strings: { [locale]: { [key]: template } } }`.
 * Throws, naming the line, on anything that would leave a text missing or
 * wrong.
 */
export function parseStrings(text) {
  const rows = parseCsv(text);
  const fail = (line, message) => { throw new Error(`strings.csv line ${line}: ${message}`); };
  if (rows.length === 0) fail(1, 'the sheet is empty');

  const [{ line: headerLine, cells: header }, ...body] = rows;
  if (header[0] !== 'key') fail(headerLine, 'the first column must be headed "key"');
  const locales = header.slice(1);
  const strings = {};
  for (const locale of locales) {
    if (locale === '') fail(headerLine, 'a column with no locale code');
    if (Object.hasOwn(strings, locale)) fail(headerLine, `the locale "${locale}" appears twice`);
    try {
      Intl.getCanonicalLocales(locale);
    } catch {
      fail(headerLine, `"${locale}" is not a locale code`);
    }
    strings[locale] = Object.create(null);
  }
  if (!Object.hasOwn(strings, FALLBACK)) fail(headerLine, `the sheet needs a "${FALLBACK}" column`);

  for (const { line, cells } of body) {
    if (cells.length !== header.length) {
      fail(line, `${cells.length} cells where the header has ${header.length}`);
    }
    const [key, ...texts] = cells;
    if (key === '') fail(line, 'a row with no key');
    if (key in strings[FALLBACK]) fail(line, `the key "${key}" appears twice`);
    texts.forEach((source, column) => {
      const locale = locales[column];
      if (source === '') fail(line, `"${key}" has no ${locale} text`);
      try {
        strings[locale][key] = compileTemplate(source, locale);
      } catch (error) {
        fail(line, `"${key}" in ${locale}, ${error.message}`);
      }
    });
  }
  return { locales, strings };
}

let table = null;
let current = null;

/** Use a parsed sheet from now on. */
export function useStrings(parsed) {
  table = parsed;
  current = null;
}

function loaded() {
  if (!table) throw new Error('No strings loaded: call useStrings(parseStrings(…)) before showing anything.');
  return table;
}

/** The locale codes the sheet has columns for, in its order. */
export function languages() {
  return loaded().locales;
}

/** A language's name for itself, for the language picker. */
export function languageName(locale) {
  return loaded().strings[locale]['language.name']?.() ?? locale;
}

/** The language to use, given what was saved and what the browser says. */
export function detectLanguage({ saved, browser, languages: known = languages() } = {}) {
  if (known.includes(saved)) return saved;
  const wanted = String(browser ?? '').toLowerCase();
  const primary = (code) => code.toLowerCase().split('-')[0];
  return known.find((code) => code.toLowerCase() === wanted)
    ?? known.find((code) => primary(code) === primary(wanted))
    ?? FALLBACK;
}

function readSaved() {
  try {
    return globalThis.localStorage?.getItem(LANGUAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

/** One of `languages()`; read once, kept for the visit even where storage fails. */
export function language() {
  current ??= detectLanguage({ saved: readSaved(), browser: globalThis.navigator?.language });
  return current;
}

/**
 * Use `lang` from now on. `remember: false` is for tests, which have no
 * visitor whose choice is worth keeping.
 */
export function setLanguage(lang, { remember = true } = {}) {
  current = languages().includes(lang) ? lang : FALLBACK;
  if (remember) {
    try {
      globalThis.localStorage?.setItem(LANGUAGE_KEY, current);
    } catch {
      // Private windows and blocked storage: the choice lasts until the page is left.
    }
  }
  return current;
}

/**
 * The text for `key` in the current language. A key with no text is shown as
 * itself — a visible slip, not a broken page; the i18n tests keep it from
 * shipping.
 */
export function t(key, vars = {}) {
  const texts = loaded().strings[language()];
  return key in texts ? texts[key](vars) : key;
}

/** A calendar date in the current language, e.g. "25. marts 2026". */
export function formatDate({ day, month, year }) {
  return t('date', { day, year, monthName: t(`month.${month}`) });
}
