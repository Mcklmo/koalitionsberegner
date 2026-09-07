import { isValidatedElection } from './election.js';

/**
 * Coalition calculator renderer.
 *
 * Election-agnostic: every number and name it displays comes from the injected,
 * schema-validated election (see election.js). Nothing here knows about any
 * particular country, assembly size or majority threshold.
 */

const DANISH_MONTHS = ['januar', 'februar', 'marts', 'april', 'maj', 'juni',
  'juli', 'august', 'september', 'oktober', 'november', 'december'];

/** Formats a schema-valid ISO date/date-time as e.g. "08:26 25. marts 2026". */
function formatElectionDate(iso) {
  const [date, time] = iso.split('T');
  const [year, month, day] = date.split('-');
  const dayMonthYear = `${Number(day)}. ${DANISH_MONTHS[Number(month) - 1]} ${year}`;
  return time ? `${time.slice(0, 5)} ${dayMonthYear}` : dayMonthYear;
}

/** Two thirds of the assembly, above which a coalition counts as a large majority. */
const supermajorityOf = (totalSeats) => Math.floor(totalSeats * 2 / 3);

/**
 * @param {import('./election.js').Election} election A validated election.
 * @param {Object} [elements] DOM nodes to render into; defaults to the ids used by index.html.
 * @returns {{ clearAll: () => void }}
 */
export function mountCoalitionCalculator(election, elements = {}) {
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
    ...elements,
  };

  const supermajoritySeats = supermajorityOf(election.totalSeats);

  /** Selection is keyed by position so parties with a repeated abbreviation stay distinct. */
  const selected = new Set();
  const keyOf = (blockIndex, partyIndex) => blockIndex + ':' + partyIndex;

  document.title = election.title;
  el.title.textContent = election.title;
  el.subtitle.textContent = `Vælg partier og se om de tilsammen opnår flertal `
    + `(${election.majoritySeats}+ ud af ${election.totalSeats} mandater)`;
  el.totalOf.textContent = `af ${election.totalSeats} mandater`;
  el.footerNote.textContent = `Flertal kræver ${election.majoritySeats} mandater `
    + `· Endelig resultat, ${formatElectionDate(election.electionDate)}`;

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
        name.textContent = p.name;
        const seats = document.createElement('span');
        seats.className = 'seats';
        seats.textContent = p.seats;
        row.append(chk, dot, abbr, name, seats);
        el.list.appendChild(row);
      });
    });
  }

  function update() {
    let total = 0;
    election.blocks.forEach((block, blockIndex) => block.parties.forEach((p, partyIndex) => {
      if (selected.has(keyOf(blockIndex, partyIndex))) total += p.seats;
    }));
    el.total.textContent = total;
    el.bar.style.width = Math.min(total / election.totalSeats * 100, 100) + '%';
    if (total >= election.majoritySeats) {
      const large = total > supermajoritySeats;
      el.bar.style.background = large ? '#1D9E75' : '#378ADD';
      el.verdict.className = 'verdict ' + (large ? 'v-over' : 'v-yes');
      el.verdict.textContent = large
        ? 'Stort flertal ✓'
        : 'Flertal ✓ (+' + (total - election.majoritySeats) + ')';
    } else {
      el.bar.style.background = '#888780';
      el.verdict.className = 'verdict v-no';
      el.verdict.textContent = 'Mangler ' + (election.majoritySeats - total);
    }
    render();
  }

  update();

  return {
    clearAll() {
      selected.clear();
      update();
    },
  };
}
