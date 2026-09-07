/**
 * The election data contract consumed by the renderer.
 *
 * Everything election-specific — party names, seat counts, the size of the
 * assembly, the majority threshold and the user-facing copy that mentions any
 * of them — lives in this object. The render logic knows nothing else.
 *
 * @typedef {Object} Party
 * @property {string} abbr    Short label shown in the list (e.g. "A").
 * @property {string} name    Full party name.
 * @property {number} seats   Seats won.
 * @property {string} color   CSS color used for the party dot.
 *
 * @typedef {Object} Block
 * @property {string} name    Heading for the group (e.g. "Rød blok").
 * @property {Party[]} parties
 *
 * @typedef {Object} Election
 * @property {string} title              Page title and heading.
 * @property {string} subtitle           Line under the heading.
 * @property {string} footerNote         Small print in the sticky bar.
 * @property {string} totalSeatsLabel    Label next to the running total.
 * @property {number} totalSeats         Size of the assembly.
 * @property {number} majoritySeats      Seats needed for a majority.
 * @property {number} supermajoritySeats Seats above which the result counts as a large majority.
 * @property {Block[]} blocks
 */

/**
 * The injection seam. A provider is anything with a `getElection()` returning
 * an {@link Election} (or a promise of one), so a future implementation can
 * load an election over the network without the renderer noticing.
 *
 * @typedef {Object} ElectionProvider
 * @property {() => Election | Promise<Election>} getElection
 */

export {};
