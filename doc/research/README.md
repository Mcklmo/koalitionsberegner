# Market research: is this needed, and where do the first users come from

Started 2026-09-22. Strategy and stage definitions:
`~/.claude/plans/gentle-cooking-cascade.md`.

**The decision this research settles:** how to get the first real users — which
entrypoint is cheapest and actually works. Not whether to keep building; that is
settled. Free sources only, no ads.

## The one-paragraph answer so far

Attention on an election is **mostly after the vote, not before it**. In every
past election measured (Stage 1c below), the month after election day drew one
to five times the pageviews of the whole month before, and in Germany and
Austria the tail stayed above a tenth of peak for 72–99 days — which is exactly
the government-formation period a coalition calculator is for. So the product's
window is not the campaign; it is **election night plus the weeks of coalition
talks that follow**.

That reframes the entrypoint. The first launch does not have to wait for the
next election: **Berlin and Mecklenburg-Vorpommern voted on 2026-09-20, Sweden
on 2026-09-13, Saxony-Anhalt on 2026-09-06, and the site already holds all
four.** As of 2026-09-22 all of them are in coalition talks, with no import work
left to do.

## Pass bars, written before the launch

Set for the first instrumented launch, per `launch-<election>.md`:

- ≥ 300 page loads in the launch week (usage report, page loads by language)
- ≥ 1 share-card preview per 25 page loads — links actually get pasted
- ≥ 3 `request_filed` from strangers
- ≥ 1 unsolicited inbound: a GitHub issue, a comment, or a reply to the
  newsroom email

A failed bar with a known channel is a result. A passed bar with unknown
attribution is not — hence `?src=` (see the strategy's "one code change").

## What is here

| File | Stage | State |
| --- | --- | --- |
| `elections-12-months.md` | 1 — desk audit | Election list and the attention-spike measurements: done. Per-election `existing_calculator` and `coalition_doubt`: owner's to fill |
| `where-the-arguments-are.md` | 2 — listening | Protocol and the table to fill; needs threads saved by hand |
| `interviews.md` | 3 — interviews | Segments, script and write-up template; no interviews done |
| `launch-berlin-mv-2026.md` | 4 — launch | Pre-registered bars and the plan for the live window |

## What would change the answer

- Stage 1's gap count coming out under three → the "one tool for every
  election" pitch has no market, and the project is a Danish/German side tool.
- Interviews saying "I used the outlet's calculator" → the wedge is only the
  elections outlets do not cover, which is the sub-national and small-country
  list, not national elections in big markets.
- The launch week passing its bars on the newsroom email but not the community
  post → the product is infrastructure for publishers, not a consumer page, and
  the roadmap should be the embed, not the front door.
