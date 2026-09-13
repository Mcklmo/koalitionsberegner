import { createElectionProvider } from '../election.js';

/**
 * The Denmark — 2026 election — currently the only election source.
 * Shaped as raw input; `createElectionProvider` validates it against the
 * canonical schema before it can reach the renderer.
 */
const folketing2026 = {
  nation: 'Danmark',
  electionDate: '2026-03-25T08:26',
  title: 'Denmark — 2026',
  sourceUrl: 'https://www.dst.dk/valg',
  totalSeats: 179,
  majoritySeats: 90,
  blocks: [
    {
      name: 'Rød blok', parties: [
        { abbr: 'Ø', name: 'Red–Green Alliance', localName: 'Enhedslisten', seats: 11, color: '#C0392B' },
        { abbr: 'F', name: 'Green Left', localName: 'SF', seats: 20, color: '#E74C3C' },
        { abbr: 'A', name: 'Social Democrats', localName: 'Socialdemokratiet', seats: 38, color: '#C0392B' },
        { abbr: 'B', name: 'Danish Social Liberal Party', localName: 'Radikale Venstre', seats: 10, color: '#9B59B6' },
        { abbr: 'Å', name: 'The Alternative', localName: 'Alternativet', seats: 5, color: '#27AE60' },
        { abbr: 'SIU', name: 'Siumut (Greenland)', localName: 'Siumut (Grønland)', seats: 1, color: '#E67E22' },
      ]
    },
    {
      name: 'Midten', parties: [
        { abbr: 'M', name: 'Moderates', localName: 'Moderaterne', seats: 14, color: '#6A89CC' },
      ]
    },
    {
      name: 'Blå blok', parties: [
        { abbr: 'V', name: 'Venstre', localName: 'Venstre', seats: 18, color: '#2980B9' },
        { abbr: 'I', name: 'Liberal Alliance', localName: 'Liberal Alliance', seats: 16, color: '#3498DB' },
        { abbr: 'O', name: "Danish People's Party", localName: 'Dansk Folkeparti', seats: 16, color: '#F39C12' },
        { abbr: 'C', name: "Conservative People's Party", localName: 'De Konservative', seats: 13, color: '#1ABC9C' },
        { abbr: 'Æ', name: 'Denmark Democrats', localName: 'Danmarksdemokraterne', seats: 10, color: '#D35400' },
        { abbr: 'Q', name: "Citizens' Party", localName: 'Borgernes Parti', seats: 4, color: '#85929E' },
      ]
    },
    {
      name: 'Nordatlantisk', parties: [
        { abbr: 'IA', name: 'Inuit Ataqatigiit', localName: 'Inuit Ataqatigiit', seats: 1, color: '#7F8C8D' },
        { abbr: 'Samb.', name: 'Union Party', localName: 'Sambandsflokkurin', seats: 1, color: '#7F8C8D' },
        { abbr: 'Jav.', name: 'Social Democratic Party', localName: 'Javnaðarflokkurin', seats: 1, color: '#7F8C8D' },
      ]
    },
  ],
};

/** @type {import('../election.js').ElectionProvider} */
export const Folketing2026Provider = createElectionProvider(() => folketing2026);
