# Stage 4 — The first instrumented launch: Berlin and MV, September 2026

Written 2026-09-22, **before** anything is posted. The bars below are
pre-registered: they are not to be edited after the numbers come in.

## Why this election and not a future one

Berlin and Mecklenburg-Vorpommern voted on 2026-09-20. Sweden voted 09-13,
Saxony-Anhalt 09-06. The site already holds all four
(`GET /api/elections`), so the import work — the thing that usually makes a
launch miss its window — is done.

`elections-12-months.md` measured what happens next: in Germany the election
article stays above a tenth of its peak for **72–74 days** after the vote, and
the month after beats the month before by two to three times. MV drew 166,879
views and Berlin 81,900 (de) plus 109,285 (en) on election day alone. Coalition
talks in both are starting this week. There is no cheaper moment available, and
waiting for Latvia on 10-03 or Israel on 10-27 means doing import work first.

## The two channels, and nothing else

Two so the funnel stays readable.

**1. One hand-written community answer.** `r/berlin` or `r/de` for Berlin;
`r/Mecklenburg` for MV. Pick the live coalition thread — the one arguing about
which coalition is possible, not the results thread.

- Draft with `./plugins/outreach/scripts/reply.py next --dry-run`, then
  **rewrite it in the owner's own words**. The draft is a starting point, not
  the comment.
- Post from the owner's own account, disclosed, one subreddit at a time.
- Read the subreddit's rules first; modmail where they are unclear. The whole
  of `doc/plans/04-reddit-outreach.md`'s "Before you switch this on" applies,
  with nothing automated. The r/Denmark removal (2026-09-13) is the precedent:
  general subs remove promotion, and the account survives only if the comment
  answers the thread.
- Link to the **coalition**, not the front page: `/e/<id>?c=…&s=…&src=reddit`.

**2. One email to a named journalist.** A Berlin or MV outlet's politics desk,
or a local data journalist. Send a link to *their* election's coalition, the
sentence about coalition talks lasting 70-odd days, and the fact that
`GET /api/elections/<hash>` is open, CORS-ready and cacheable if they want it
on their own page. One email, one named person, no press release.
`?src=press`.

## Bars

Measured over the seven days from the first post, from the usage report
(`GET /api/admin/usage?period=weekly`) and the GitHub tracker:

| Bar | Target |
| --- | --- |
| Page loads in the launch week | ≥ 300 |
| Share-card previews per page load | ≥ 1 per 25 |
| `request_filed` from strangers | ≥ 3 |
| Unsolicited inbound (issue, comment, email reply) | ≥ 1 |

Attribution comes from the `?src=` counters. **A passed bar with unknown
attribution does not count**, because it teaches nothing about which channel to
repeat.

## Outcome

To be filled in after the week, including if it fails. A failed bar with a
known channel is a result worth more than a vague success.

| Bar | Target | Actual | Pass |
| --- | --- | --- | --- |
| Page loads | ≥ 300 | | |
| Previews per page load | ≥ 1/25 | | |
| Requests filed | ≥ 3 | | |
| Unsolicited inbound | ≥ 1 | | |

By channel: `src=reddit` …, `src=press` …, untagged …
