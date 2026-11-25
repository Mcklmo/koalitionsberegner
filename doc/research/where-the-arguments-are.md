# Stage 2 — Where the arguments are

Question: *where* do people argue about coalition arithmetic, and how often?
Answered with the classifier already in the tree, which posts nothing and costs
nothing.

## Why the outreach scanner is the right instrument

`plugins/outreach/scripts/scan.py` already asks the local model three questions
per item — does it mention an election that has been held, one still to come,
and does it make a statement involving two or more parties. Those are exactly
the questions a market-research pass would ask, so measuring needs no new code:
run the scan for its *flag rate* rather than for the reply it would enable.

It also stops at the first yes per thread, which is right for outreach and
wrong for measurement: a thread's flag rate is understated. So compare
**threads flagged / threads scanned** across communities, not flags per thread,
and record `comments_scanned` so a rate can be recomputed later.

## Protocol

1. Pick 8–10 communities from `elections-12-months.md`'s live and upcoming rows,
   spread across Danish, German, English-global and one under-served market.
   Candidates for the elections that are live right now: `r/berlin`, `r/de`,
   `r/Mecklenburg`, `r/svenskpolitik`, `r/sweden`, `r/europe`, plus `r/Denmark`
   for the March 2026 Folketing cycle and `r/newzealand` and `r/Israel` ahead of
   November and October.
2. From each, save **3 threads** — the top political threads of the last month,
   *not* ones already about coalitions, or the measurement is rigged. Open each
   with `.json` on the end and save into `plugins/outreach/inbox/`:
   `https://www.reddit.com/r/<sub>/comments/<id>/x/.json?limit=500&raw_json=1`
3. Scan: `./plugins/outreach/scripts/scan.py -v --slim`
4. Fill the table below.
5. Read the flagged comments themselves and copy the *wording*. How people
   phrase a majority claim in each language is what the page's strings and every
   later post should sound like.

Before trusting the rates, confirm the plumbing on one thread with
`--classifier keyword`, and keep the suite green:
`uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts`.

## Results

| Community | Subscribers | Threads scanned | Threads with a flag | Comments scanned | Flags: upcoming / multi-party | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

Already on disk and unscanned as of 2026-09-22: `plugins/outreach/inbox/berlin26.json`,
`mp26-1.json` … `mp26-4.json`.

## Sampling bias to state in any conclusion

Hand-picked threads, one month, one platform, no discovery, and the scanner
stops early. This ranks communities against each other; it does not measure how
many people anywhere argue about coalitions.
