# Stage 1 — Desk audit

Measured 2026-09-22. Free sources only: Wikipedia's election lists, the
Wikimedia pageviews API, and the live site's own `GET /api/elections`.

## 1c. The attention spike, measured

Wikipedia daily pageviews of each election's own article, seven past elections,
one per market shape. Peak is the highest single day; `pre30` and `post30` are
total views in the 30 days before and the 30 days from election day; the last
two columns are how many days after the vote the article still drew a quarter,
and a tenth, of its peak.

| Election | Peak day | pre30 | post30 | post/pre | d ≥ 25% peak | d ≥ 10% peak |
| --- | --- | --- | --- | --- | --- | --- |
| DK Folketing 2022 | 6,946 | 26,361 | 25,811 | 1.0 | 2 | 43 |
| DE Bundestag 2025 | 227,540 | 354,910 | 708,462 | 2.0 | 1 | 72 |
| DE Berlin 2023 | 58,404 | 59,707 | 165,533 | 2.8 | 1 | 74 |
| NL Tweede Kamer 2023 | 20,453 | 26,017 | 77,913 | 3.0 | 2 | 9 |
| IE general 2024 | 118,652 | 192,695 | 567,705 | 2.9 | 4 | 9 |
| IL Knesset 2022 | 118,410 | 98,082 | 506,225 | 5.2 | 4 | 7 |
| AT Nationalrat 2024 | 114,396 | 156,806 | 280,139 | 1.8 | 1 | 99 |

Reproduce with `doc/research/pageviews.py` (needs nothing but Python 3 and an
honest `User-Agent`).

**What it says.**

1. **The window is after the vote.** post30 beats pre30 everywhere except
   Denmark 2022, whose article did not exist 90 days out, so its "before" is
   understated rather than large. Building for the campaign is building for the
   smaller half of the audience.
2. **The peak is one to four days wide,** and it lands on election day or the
   day after. Being live *before* the polls close is therefore the whole game
   for the peak; there is no catching up on day three.
3. **The tail is the coalition question.** Germany and Austria stay above a
   tenth of peak for 72–99 days; Denmark 43. That is the government-formation
   period, and it is the only period in which "does this coalition have a
   majority" is a live question rather than a historical one. NL/IE/IL decay in
   7–9 days on the *article*, which is a fact about Wikipedia articles, not
   about their coalition talks — for those markets the tail lives in the news
   cycle and needs a different instrument to measure.
4. **Order of magnitude.** A German Land election is a 50–170k-views-a-day
   event in one language; a national election in a mid-size European country is
   100–230k. The addressable spike is large enough that a page with no
   marketing budget can still be found in it — if it is already indexed and
   already holds the election.

## 1a/1b. Elections in the next 12 months

From Wikipedia's "List of elections in 2026" and "…2027", filtered to
proportional or mixed systems where a coalition question is plausible.
`existing_calculator` and `coalition_doubt` are the owner's to fill — each needs
a local-language search, and a claim of "none" must say what was searched.

### Live right now: elections already held, coalitions still unformed

The highest-value rows in the table, because the import work is already done.

| Election | Voted | In the store? | Why it matters |
| --- | --- | --- | --- |
| Berlin state election | 2026-09-20 | yes | 81.9k de + 109.3k en views on election day. Berlin 2023's tail ran 74 days |
| Mecklenburg-Vorpommern | 2026-09-20 | yes (two records) | 166.9k views on election day — the biggest single-day number of the four |
| Swedish general election | 2026-09-13 | yes, plus a Novus forecast | Peaked 230.5k (en) the day after; still 12.4k/day on day 7 |
| Saxony-Anhalt | 2026-09-06 | yes | 11.9k on 09-20, i.e. still alive a fortnight on |

Pageviews above are the last day the API had on 2026-09-22 (it lags a day or
two), so the post-election curve for Berlin and MV is not yet visible.

### Coming, in date order

| Date | Election | System | Notes |
| --- | --- | --- | --- |
| 2026-10-03 | Latvia parliamentary | PR | Fragmented parliament; coalition arithmetic is the story every cycle |
| 2026-10-04 | Bosnia general (+ FBiH, Republika Srpska) | PR | Three elections one day; complex allocation |
| 2026-10-25 | Serbia parliamentary | PR | |
| 2026-10-27 | Israel legislative | PR, nationwide | The coalition-arithmetic election. Large English-reading audience; 2022's post30 was 5.2× pre30 |
| 2026-11-07 | New Zealand general | MMP | Coalition arithmetic is the national conversation, in English, with overhang and threshold rules worth getting right |
| 2026-11-28 | Palestinian legislative | PR | |
| 2027-01-16 | Nigeria general | — | Majoritarian; out of scope |
| 2027-02-28 | El Salvador legislative | PR | |
| 2027-03-05 | Micronesia general | — | |
| 2027-04-18 | Finland parliamentary | PR | Finland 2023 already in the store — a second cycle of the same country |
| 2027-04-18 | Saarland, Schleswig-Holstein state | PR | German Länder, same week |
| 2027-04-25 | North Rhine-Westphalia state | PR | Largest Land |
| 2027-05-30 | Bremen state | PR | |
| 2027-06-06 | Mexico legislative | mixed | |
| 2027-09-26 | Lower Saxony state | PR | |
| 2027-10-17 | Åland legislative | PR | Tiny, and nobody else will ever build it a calculator |
| 2027-10-24 | Switzerland federal | PR | No coalition in the usual sense; Bundesrat is a formula |
| 2027-10-24 | Argentina general | mixed | |
| 2027 (TBD) | Estonia, Greece, Italy, Montenegro, Poland, Slovakia, Spain | PR | Dates unset; all coalition-heavy |

### How to fill the two remaining columns

For each row: search the local-language terms for a coalition/seat calculator
(`koalitionsrechner`, `mandatberegner`, `regeringsbildare`, `zetelverdeling`,
`coalition calculator`, `מחשבון קואליציה`, `koalīcijas kalkulators`), and check
the two or three biggest outlets' election pages. Record the URL or "none, and
this is what I searched". Then note whether current polls leave two or more
plausible majorities — an election with one obvious coalition has no arithmetic
worth arguing about.

`gap_score` = coalition doubt **and** no free local-language calculator **and** a
community that would notice. Fewer than three such rows is the kill criterion in
`README.md`.

## 1e. Positioning, from the competitor read-through

To be written after the audit. The expected shape, worth testing rather than
assuming: every incumbent calculator is built for one election in one country
by one outlet, and is switched off or left to rot afterwards — which is exactly
the period this measurement says the audience is largest. One tool that holds
every election, keeps the old polls, and still answers the question 60 days
after the vote is the claim to make.
