/**
 * The front door: search, grouping by election, keyboard navigation, and the
 * "ask for it"/"import it" fallback when nothing matches (plan 3, Section B).
 *
 * Split out of js/import-ui.js, which keeps the import form itself and now
 * only files a request or runs an import when asked to (`requestOrImport`).
 * This module owns the list of stored elections and renders it as a
 * combobox: a search box filters a dropdown grouped by `electionKey` — one
 * row per election, its result once it has one, otherwise its newest poll;
 * older polls stay reachable one click deeper rather than disappearing once
 * a result is final (plan 3, D9), because "what did the polls say" is part
 * of the story.
 *
 * The pure functions here (`groupSummaries`, `matchesQuery`,
 * `pickDefaultElection`, `parseQueryAsRequest`) are exported and tested on
 * their own in test/picker.test.mjs, without a DOM.
 */

import { t } from './i18n.js';
import { parseYear } from './import-form.js';

/** The bundled election's value in the list; it has no hash of its own. */
export const LOCAL_VALUE = 'local';

export const placeOf = (summary) => (summary.state ? `${summary.nation} — ${summary.state}` : summary.nation);
export const forecastLabel = (forecast) => `${forecast.publisher} · ${forecast.publishedOn}`;

const stripAccents = (value) => value.normalize('NFKD').replace(/\p{Mn}/gu, '');
const norm = (value) => stripAccents(String(value ?? '')).toLowerCase();

/**
 * Every summary sharing an `electionKey` becomes one group: `primary` is the
 * election's result once it has one, otherwise its newest poll; `older` is
 * the rest, newest first. Groups keep the order their first entry had in
 * `summaries` (the server already sorts by `election_date` descending).
 */
export function groupSummaries(summaries) {
  const order = [];
  const byKey = new Map();
  for (const summary of summaries) {
    const key = summary.electionKey ?? summary.electionHash;
    if (!byKey.has(key)) {
      order.push(key);
      byKey.set(key, []);
    }
    byKey.get(key).push(summary);
  }
  return order.map((key) => {
    const entries = byKey.get(key).slice().sort((a, b) => {
      // A result (no forecast) always leads; among polls, the newest first.
      if (Boolean(a.forecast) !== Boolean(b.forecast)) return a.forecast ? 1 : -1;
      if (a.forecast && b.forecast) return a.forecast.publishedOn < b.forecast.publishedOn ? 1 : -1;
      return 0;
    });
    const [primary, ...older] = entries;
    return { key, primary, older };
  });
}

/** Every string a group can be found by: place, title, year, any poll's publisher. */
function haystack(group) {
  const year = String(group.primary.electionDate).slice(0, 4);
  return [
    group.primary.nation, group.primary.state, group.primary.title, year,
    ...[group.primary, ...group.older].map((entry) => entry.forecast?.publisher),
  ].filter(Boolean).map(norm);
}

/** Whether every word of a search query matches somewhere in a group. */
export function matchesQuery(group, query) {
  const words = norm(query).trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const hay = haystack(group);
  return words.every((word) => hay.some((field) => field.includes(word)));
}

/** Groups whose search fields match every word of `query`, in the given order. */
export function filterGroups(groups, query) {
  return groups.filter((group) => matchesQuery(group, query));
}

/**
 * The election a plain visit lands on (plan 3, B4): the soonest upcoming
 * election that already has a stored poll, or — failing that — the most
 * recent result. `null` means neither exists, and the bundled election (the
 * pre-arrival render) stays on screen. A shared link bypasses this entirely;
 * `js/main.js` only calls it when there is none.
 */
export function pickDefaultElection(summaries, now = new Date()) {
  const today = now.toISOString().slice(0, 10);
  const upcomingWithAPoll = summaries.filter((s) => s.forecast && s.electionDate >= today);
  if (upcomingWithAPoll.length > 0) {
    return upcomingWithAPoll.reduce((soonest, s) => (s.electionDate < soonest.electionDate ? s : soonest));
  }
  const results = summaries.filter((s) => !s.forecast && s.electionDate <= today);
  if (results.length > 0) {
    return results.reduce((latest, s) => (s.electionDate > latest.electionDate ? s : latest));
  }
  return null;
}

/**
 * Best-effort year and place out of free text typed into the search box, for
 * the "ask for it" fallback. Never a substitute for the import form's own
 * fields: `js/import-form.js`'s `validateImportForm` still checks whatever
 * this returns before anything is sent, so a query this cannot make sense of
 * simply fails validation rather than filing something wrong.
 */
export function parseQueryAsRequest(query) {
  const words = String(query ?? '').trim().split(/\s+/).filter(Boolean);
  let year = null;
  const placeWords = [];
  for (const word of words) {
    // A trailing comma or full stop must not stop a year being recognised —
    // but it is kept in `placeWords` below, because it is the delimiter a
    // "nation, region" query uses.
    const bare = word.replace(/[,.]+$/, '');
    if (year === null) {
      const parsed = parseYear(bare);
      if (parsed !== null) {
        year = parsed;
        continue;
      }
    }
    placeWords.push(word);
  }
  const place = placeWords.join(' ').replace(/,+$/, '').trim();
  const [nation, subnation] = place.includes(',')
    ? place.split(',').map((part) => part.trim()).filter(Boolean)
    : [place, null];
  return { year, nation: nation || '', subnation: subnation || null };
}

/**
 * @param {Object} elements `{ row, input, results }` — the search input and
 *   the `<ul>` it opens, both from index.html.
 * @param {{title: string}} [bundled] The bundled election, listed as `LOCAL_VALUE`.
 * @param {(election: Object, electionHash: string|null) => void} onSelect
 *   Called with the full election (fetched by hash, unless it is the bundled
 *   one) and its hash — `null` for the bundled election, which has none.
 * @param {() => boolean} [canImport] Whether the owner may import right now —
 *   decides whether the fallback row says "ask for it" or "import it".
 * @param {() => boolean} [canRequestElection] Whether anyone may file a
 *   request right now.
 * @param {(values: {year: number|null, nation: string, subnation: string|null})
 *   => Promise<{valid: boolean}>} [onAskForIt] Files the request or runs the
 *   import — `js/import-ui.js`'s `requestOrImport`.
 */
export function mountPicker({
  elements,
  bundled = null,
  onSelect = () => {},
  canImport = () => false,
  canRequestElection = () => false,
  onAskForIt = async () => ({ valid: false }),
} = {}) {
  const el = elements;
  let api = null;
  let summaries = [];
  let groups = [];
  let visible = [];
  /** `electionKey`s whose older polls are shown, expanded by a click. */
  const expanded = new Set();
  let activeIndex = -1;
  let note = '';

  const bundledGroup = () => (bundled
    ? { key: LOCAL_VALUE, primary: { electionHash: LOCAL_VALUE, title: bundled.title, nation: bundled.nation, state: bundled.state, electionDate: bundled.electionDate, forecast: null }, older: [] }
    : null);

  function computeVisible() {
    const query = el.input.value;
    const withBundled = bundledGroup() ? [...groups, bundledGroup()] : groups;
    visible = filterGroups(withBundled, query);
    activeIndex = visible.length > 0 ? 0 : -1;
  }

  function labelFor(entry) {
    // The bundled election is named by its title, as the old `<select>` did —
    // it is a fixed choice, not a summary to describe by place and date.
    if (entry.electionHash === LOCAL_VALUE) return entry.title;
    return entry.forecast
      ? t('picker.forecast', { where: placeOf(entry), label: forecastLabel(entry.forecast) })
      : `${placeOf(entry)} · ${entry.electionDate}`;
  }

  function showAskRow() {
    return visible.length === 0 && (canImport() || canRequestElection());
  }

  function render() {
    el.results.innerHTML = '';
    let rowIndex = 0;

    if (note) {
      const li = document.createElement('li');
      li.className = 'picker-result-ask';
      li.textContent = note;
      el.results.appendChild(li);
    } else if (visible.length === 0) {
      const li = document.createElement('li');
      li.className = 'picker-result-ask';
      if (showAskRow()) {
        li.textContent = t(canImport() ? 'picker.importIt' : 'picker.askForIt');
        li.addEventListener('click', askForIt);
      } else {
        li.textContent = t('picker.noMatches');
      }
      el.results.appendChild(li);
    } else {
      for (const group of visible) {
        const row = document.createElement('li');
        row.className = 'picker-result';
        if (rowIndex === activeIndex) row.classList.add('active');
        row.setAttribute('role', 'option');
        row.textContent = labelFor(group.primary);
        const thisIndex = rowIndex;
        row.addEventListener('mouseenter', () => setActive(thisIndex));
        row.addEventListener('click', () => choose(group.primary));
        el.results.appendChild(row);
        rowIndex += 1;

        if (group.older.length > 0) {
          if (expanded.has(group.key)) {
            for (const entry of group.older) {
              const older = document.createElement('li');
              older.className = 'picker-result picker-result-older';
              older.setAttribute('role', 'option');
              older.textContent = labelFor(entry);
              older.addEventListener('click', () => choose(entry));
              el.results.appendChild(older);
            }
          } else {
            const toggle = document.createElement('li');
            toggle.className = 'picker-result picker-result-older';
            toggle.textContent = t('picker.olderPolls', { count: group.older.length });
            toggle.addEventListener('click', () => {
              expanded.add(group.key);
              render();
            });
            el.results.appendChild(toggle);
          }
        }
      }
    }
    el.results.hidden = false;
    el.input.setAttribute('aria-expanded', 'true');
  }

  function setActive(index) {
    activeIndex = index;
    render();
  }

  function close() {
    el.results.hidden = true;
    el.input.setAttribute('aria-expanded', 'false');
  }

  /**
   * A group's row only carries the summary, not the full election — so
   * choosing one fetches it, exactly as the old `<select>`-based picker did.
   * `entry.electionHash === LOCAL_VALUE` is the bundled row, which needs no
   * fetch: it is already on screen.
   */
  async function choose(entry) {
    note = '';
    close();
    if (entry.electionHash === LOCAL_VALUE) {
      onSelect(bundled, null);
      return;
    }
    try {
      const result = await api.getElection(entry.electionHash);
      if (result.election) onSelect(result.election, result.electionHash);
    } catch (error) {
      note = t('select.failed', { message: error.message });
      render();
    }
  }

  async function askForIt() {
    const parsed = parseQueryAsRequest(el.input.value);
    const result = await onAskForIt(parsed);
    if (result?.valid) {
      note = t('picker.askedInline');
      render();
    }
  }

  function onInput() {
    note = '';
    computeVisible();
    render();
  }

  function onFocus() {
    computeVisible();
    render();
  }

  function onKeydown(event) {
    if (el.results.hidden && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      onFocus();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (visible.length > 0) setActive((activeIndex + 1) % visible.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (visible.length > 0) setActive((activeIndex - 1 + visible.length) % visible.length);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (activeIndex >= 0 && visible[activeIndex]) choose(visible[activeIndex].primary);
      else if (showAskRow()) askForIt();
    } else if (event.key === 'Escape') {
      close();
    }
  }

  el.input.addEventListener('input', onInput);
  el.input.addEventListener('focus', onFocus);
  el.input.addEventListener('keydown', onKeydown);

  return {
    /** Fetch the list from the API; failures leave the previous list in place. */
    async refresh(nextApi) {
      api = nextApi;
      summaries = await api.listElections();
      groups = groupSummaries(summaries);
      // Nothing worth searching when there is at most one election to show —
      // the bundled one alone, say. Mirrors the old `<select>` picker's rule.
      if (el.row) el.row.hidden = groups.length + (bundled ? 1 : 0) <= 1;
    },

    /** The stored elections currently listed — a shared link's bundled twin, for instance. */
    summaries: () => summaries,

    /** The election a plain visit should land on; `null` keeps the bundled one. */
    defaultElection: (now) => pickDefaultElection(summaries, now),
  };
}
