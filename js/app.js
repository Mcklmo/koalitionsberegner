import { isValidatedElection } from './election.js';
import { formatDate, language, t } from './i18n.js';

/**
 * Coalition calculator renderer.
 *
 * Election-agnostic: every number and name it displays comes from the injected,
 * schema-validated election (see election.js). Nothing here knows about any
 * particular country, assembly size or majority threshold.
 */

/** Formats a schema-valid ISO date/date-time as e.g. "08:26 25. marts 2026", in the page's language. */
function formatElectionDate(iso) {
  const [date, time] = iso.split('T');
  const [year, month, day] = date.split('-');
  const dayMonthYear = formatDate({ day: Number(day), month: Number(month), year });
  return time ? `${time.slice(0, 5)} ${dayMonthYear}` : dayMonthYear;
}

/** Where the choice between a party's two names is remembered. */
const PARTY_NAMES_KEY = 'koalitionsberegner.partyNames';

/** `local` (the default) or `english`; read once, kept for the visit even where storage fails. */
let partyNames = null;

function readPartyNames() {
  try {
    return globalThis.localStorage?.getItem(PARTY_NAMES_KEY) === 'english' ? 'english' : 'local';
  } catch {
    return 'local';
  }
}

function savePartyNames(value) {
  try {
    globalThis.localStorage?.setItem(PARTY_NAMES_KEY, value);
  } catch {
    // Private windows and blocked storage: the choice lasts until the page is left.
  }
}

/** Two thirds of the assembly, above which a coalition counts as a large majority. */
const supermajorityOf = (totalSeats) => Math.floor(totalSeats * 2 / 3);

/**
 * A party's share of the assembly, to one decimal — "28.6%" in English, "28,6 %"
 * in Danish and German. Which separator the decimal takes, and whether a space
 * belongs before the sign, are each language's own business, so the number is
 * left to Intl rather than spelled out in the sheet.
 *
 * Seats, not votes: a party's vote share never reaches the renderer, and for a
 * forecast the seats are themselves computed from one. Every validated
 * election's parties add up to `totalSeats` (election.js refuses one that
 * doesn't), so these shares are a whole assembly divided up.
 */
const seatShareOf = (seats, totalSeats) => new Intl.NumberFormat(language(), {
  style: 'percent',
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
}).format(seats / totalSeats);

/**
 * @param {import('./election.js').Election} election A validated election.
 * @param {Object} [elements] DOM nodes to render into; defaults to the ids used by index.html.
 * @param {Object} [options]
 * @param {number[]} [options.initialSelection] Flattened positions to preselect, as a shared
 *   link's `c` decodes to (js/share.js). A position with no matching party — past the last one,
 *   or left over from a different election — selects nothing on its own.
 * @param {(indices: number[], total: number) => void} [options.onChange] Called once on mount
 *   and again after every change, with the selection as flattened positions (ascending) and the
 *   seat total, so a caller can keep a shared link's URL in sync.
 * @param {string} [options.provenance] `'auto'` for an election the scheduled refresh stored
 *   with nobody reading it first; anything else is treated as confirmed.
 * @param {string} [options.issuesUrl] Where a problem with the figures is reported, as
 *   `https://github.com/<owner>/<name>/issues`. Empty means no link is offered.
 * @returns {{ clearAll: () => void, selection: () => number[], total: () => number }}
 */
export function mountCoalitionCalculator(
  election,
  elements = {},
  { initialSelection = [], onChange, provenance = 'manual', issuesUrl = '' } = {},
) {
  if (!isValidatedElection(election)) {
    throw new TypeError('mountCoalitionCalculator requires an election from validateElection()');
  }

  const el = {
    title: document.getElementById('title'),
    subtitle: document.getElementById('subtitle'),
    list: document.getElementById('party-list'),
    bar: document.getElementById('bar'),
    total: document.getElementById('total'),
    totalOf: document.getElementById('total-of'),
    verdict: document.getElementById('verdict'),
    footerNote: document.getElementById('footer-note'),
    autoNote: document.getElementById('auto-note'),
    attributionNote: document.getElementById('attribution-note'),
    names: document.getElementById('names'),
    namesRow: document.getElementById('names-row'),
    ...elements,
  };

  partyNames ??= readPartyNames();
  // An election stored before parties had a local name has nothing to switch to.
  const hasLocalNames = election.blocks.some((b) => b.parties.some((p) => p.localName && p.localName !== p.name));
  if (el.namesRow) el.namesRow.hidden = !hasLocalNames;
  if (el.names) {
    el.names.value = partyNames;
    el.names.onchange = () => {
      partyNames = el.names.value === 'english' ? 'english' : 'local';
      savePartyNames(partyNames);
      render();
    };
  }
  const nameOf = (party) => (partyNames === 'local' && party.localName) || party.name;

  const supermajoritySeats = supermajorityOf(election.totalSeats);

  /** Selection is keyed by position so parties with a repeated abbreviation stay distinct. */
  const selected = new Set();
  const keyOf = (blockIndex, partyIndex) => blockIndex + ':' + partyIndex;

  /** Every `block:party` key, in the same flattened order a shared link's `c` counts in. */
  const flattenedKeys = election.blocks.flatMap((block, blockIndex) =>
    block.parties.map((_, partyIndex) => keyOf(blockIndex, partyIndex)));
  const wanted = new Set(initialSelection);
  flattenedKeys.forEach((key, position) => {
    if (wanted.has(position)) selected.add(key);
  });
  /** Current selection as flattened positions, ascending — what a link's `c` would encode. */
  const currentSelection = () => flattenedKeys
    .map((key, position) => (selected.has(key) ? position : -1))
    .filter((position) => position !== -1);

  document.title = election.title;
  el.title.textContent = election.title;
  el.subtitle.textContent = t('calc.subtitle', { majority: election.majoritySeats, total: election.totalSeats });
  el.totalOf.textContent = t('calc.totalOf', { total: election.totalSeats });
  // A forecast is not a result, and one whose seats we computed says so.
  const { forecast } = election;
  el.footerNote.textContent = `${t('calc.majorityNeeds', { majority: election.majoritySeats })} · `
    + (forecast
      ? t('calc.forecastFrom', { publisher: forecast.publisher, date: formatElectionDate(forecast.publishedOn) })
        + (forecast.computed ? ` (${t('seats.computed')})` : '')
      : t('calc.finalResult', { date: formatElectionDate(election.electionDate) }));
  renderAutoNote();
  renderAttributionNote();

  /**
   * Names where the figures came from, every election, regardless of provenance
   * (plan 3, C2). Wikipedia's CC BY-SA licence requires attribution to stay
   * visible wherever its data is shown; a non-Wikipedia source is named the
   * same way, without a licence claim that would not be true of it.
   */
  function renderAttributionNote() {
    if (!el.attributionNote) return;
    el.attributionNote.textContent = '';
    const { hostname } = new URL(election.sourceUrl);
    el.attributionNote.append(t('data.attribution', { source: hostname }));
  }

  /**
   * The one line that says nobody checked these figures before they went up.
   *
   * Only for `provenance === 'auto'`: the scheduled refresh stores an election
   * without anyone confirming it (doc/plans/03-remaining-work.md, A1), and the
   * honest answer to that is to say so and make reporting it one click. The
   * source is the election's own `sourceUrl`, which `election.js` has already
   * validated as an absolute http(s) address, and everything reaches the DOM
   * as text or as a URL-encoded query parameter — never as markup.
   */
  function renderAutoNote() {
    if (!el.autoNote) return;
    el.autoNote.textContent = '';
    el.autoNote.hidden = provenance !== 'auto';
    if (el.autoNote.hidden) return;

    const { hostname } = new URL(election.sourceUrl);
    el.autoNote.append(t('calc.autoImported', { source: hostname }));
    if (!issuesUrl) return;
    const report = document.createElement('a');
    const url = new URL(`${issuesUrl.replace(/\/+$/, '')}/new`);
    url.searchParams.set('labels', 'data-problem');
    url.searchParams.set('title', election.title);
    // The address is the only thing a page reader could act on, so it names
    // the election and nothing that was read off the source page.
    url.searchParams.set('body', election.sourceUrl);
    report.href = url.toString();
    report.rel = 'noopener noreferrer';
    report.target = '_blank';
    report.textContent = t('calc.reportProblem');
    el.autoNote.append(' ', report);
  }

  function render() {
    el.list.innerHTML = '';
    election.blocks.forEach((block, blockIndex) => {
      const lbl = document.createElement('div');
      lbl.className = 'blok-label';
      lbl.textContent = block.name;
      el.list.appendChild(lbl);
      block.parties.forEach((p, partyIndex) => {
        const id = keyOf(blockIndex, partyIndex);
        const row = document.createElement('div');
        row.className = 'party-row' + (selected.has(id) ? ' selected' : '');
        row.onclick = () => {
          if (selected.has(id)) selected.delete(id);
          else selected.add(id);
          update();
        };
        const chk = document.createElement('div');
        chk.className = 'check' + (selected.has(id) ? ' on' : '');
        const dot = document.createElement('div');
        dot.className = 'dot';
        dot.style.background = p.color;
        const abbr = document.createElement('span');
        abbr.className = 'abbr';
        abbr.textContent = p.abbr;
        const name = document.createElement('span');
        name.className = 'party-name';
        name.textContent = nameOf(p);
        const seats = document.createElement('span');
        seats.className = 'seats';
        seats.textContent = p.seats;
        const share = document.createElement('span');
        share.className = 'share';
        share.textContent = seatShareOf(p.seats, election.totalSeats);
        // A bare percentage could be read as the vote share it is not.
        share.title = t('calc.seatShare');
        row.append(chk, dot, abbr, name, seats, share);
        el.list.appendChild(row);
      });
    });
  }

  let lastTotal = 0;

  function update() {
    let total = 0;
    election.blocks.forEach((block, blockIndex) => block.parties.forEach((p, partyIndex) => {
      if (selected.has(keyOf(blockIndex, partyIndex))) total += p.seats;
    }));
    lastTotal = total;
    el.total.textContent = total;
    el.bar.style.width = Math.min(total / election.totalSeats * 100, 100) + '%';
    if (total >= election.majoritySeats) {
      const large = total > supermajoritySeats;
      el.bar.style.background = large ? '#1D9E75' : '#378ADD';
      el.verdict.className = 'verdict ' + (large ? 'v-over' : 'v-yes');
      el.verdict.textContent = large
        ? t('calc.largeMajority')
        : t('calc.majority', { over: total - election.majoritySeats });
    } else {
      el.bar.style.background = '#888780';
      el.verdict.className = 'verdict v-no';
      el.verdict.textContent = t('calc.short', { missing: election.majoritySeats - total });
    }
    render();
    onChange?.(currentSelection(), total);
  }

  update();

  return {
    clearAll() {
      selected.clear();
      update();
    },
    selection: currentSelection,
    total: () => lastTotal,
  };
}
