/**
 * The Folketing 2026 election — currently the only implementation of the
 * ElectionProvider seam described in ../election.js.
 */

const TOTAL_SEATS = 179;
const MAJORITY_SEATS = 90;
// Two thirds of 179 rounded down; above this the coalition is called a large majority.
const SUPERMAJORITY_SEATS = 119;

/** @type {import('../election.js').Election} */
const folketing2026 = {
  title: 'Koalitionsberegner — Folketing 2026',
  subtitle: `Vælg partier og se om de tilsammen opnår flertal (${MAJORITY_SEATS}+ ud af ${TOTAL_SEATS} mandater)`,
  footerNote: `Flertal kræver ${MAJORITY_SEATS} mandater · Endelig resultat, 08:26 25. marts 2026`,
  totalSeatsLabel: `af ${TOTAL_SEATS} mandater`,
  totalSeats: TOTAL_SEATS,
  majoritySeats: MAJORITY_SEATS,
  supermajoritySeats: SUPERMAJORITY_SEATS,
  blocks: [
    {
      name: 'Rød blok', parties: [
        { abbr: 'Ø', name: 'Enhedslisten', seats: 11, color: '#C0392B' },
        { abbr: 'F', name: 'SF', seats: 20, color: '#E74C3C' },
        { abbr: 'A', name: 'Socialdemokratiet', seats: 38, color: '#C0392B' },
        { abbr: 'B', name: 'Radikale Venstre', seats: 10, color: '#9B59B6' },
        { abbr: 'Å', name: 'Alternativet', seats: 5, color: '#27AE60' },
        { abbr: 'SIU', name: 'Siumut (Grønland)', seats: 1, color: '#E67E22' },
      ]
    },
    {
      name: 'Midten', parties: [
        { abbr: 'M', name: 'Moderaterne', seats: 14, color: '#6A89CC' },
      ]
    },
    {
      name: 'Blå blok', parties: [
        { abbr: 'V', name: 'Venstre', seats: 18, color: '#2980B9' },
        { abbr: 'I', name: 'Liberal Alliance', seats: 16, color: '#3498DB' },
        { abbr: 'O', name: 'Dansk Folkeparti', seats: 16, color: '#F39C12' },
        { abbr: 'C', name: 'De Konservative', seats: 13, color: '#1ABC9C' },
        { abbr: 'Æ', name: 'Danmarksdemokraterne', seats: 10, color: '#D35400' },
        { abbr: 'Q', name: 'Borgernes Parti', seats: 4, color: '#85929E' },
      ]
    },
    {
      name: 'Nordatlantisk', parties: [
        { abbr: 'IA', name: 'Inuit Ataqatigiit', seats: 1, color: '#7F8C8D' },
        { abbr: 'Samb.', name: 'Sambandsflokkurin', seats: 1, color: '#7F8C8D' },
        { abbr: 'Jav.', name: 'Javnaðarflokkurin', seats: 1, color: '#7F8C8D' },
      ]
    },
  ],
};

/** @type {import('../election.js').ElectionProvider} */
export const Folketing2026Provider = {
  getElection() {
    return folketing2026;
  },
};
