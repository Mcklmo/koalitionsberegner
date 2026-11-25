#!/usr/bin/env python3
"""The attention curve around an election, from Wikipedia's own pageviews.

Free, no key, no account: the Wikimedia REST API serves daily per-article views
to anyone who identifies themselves in a User-Agent. What the numbers are for
is in `elections-12-months.md`, Stage 1c — in one line, the month after an
election beats the month before it, and in German-speaking countries the tail
runs for two to three months, which is the coalition-formation period this
project exists to serve.

Add rows to CASES to measure another election. The article title must be the
one the local Wikipedia actually uses; `pageviews.py` returns a 404 for a
redirect, so resolve it first (the search API does this: `action=query&
list=search`).

    python3 doc/research/pageviews.py
"""
import json, urllib.request, urllib.parse, datetime as dt
UA = {"User-Agent": "koalitionsberegner-research (+https://github.com/mcklmo/koalitionsberegner)"}
CASES = [
    ("DK Folketing 2022",     "da", "Folketingsvalget_2022",                  "2022-11-01"),
    ("DE Bundestag 2025",     "de", "Bundestagswahl_2025",                    "2025-02-23"),
    ("DE Berlin 2023",        "de", "Wahl_zum_Abgeordnetenhaus_von_Berlin_2023","2023-02-12"),
    ("NL Tweede Kamer 2023",  "nl", "Tweede_Kamerverkiezingen_2023",          "2023-11-22"),
    ("IE general 2024",       "en", "2024_Irish_general_election",            "2024-11-29"),
    ("IL Knesset 2022",       "en", "2022_Israeli_legislative_election",      "2022-11-01"),
    ("AT Nationalrat 2024",   "de", "Nationalratswahl_in_Österreich_2024",    "2024-09-29"),
]
def views(lang, art, s, e):
    url=(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia/"
         f"all-access/user/{urllib.parse.quote(art,safe='')}/daily/{s:%Y%m%d}/{e:%Y%m%d}")
    with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=30) as r:
        return {dt.datetime.strptime(i["timestamp"][:8],"%Y%m%d").date(): i["views"] for i in json.load(r)["items"]}
print(f"{'election':22} {'peak':>8} {'pre30':>8} {'post30':>8} {'post/pre':>8} {'d>25% peak':>11} {'d>10% peak':>11}")
for name, lang, art, date in CASES:
    d=dt.date.fromisoformat(date)
    v=views(lang,art,d-dt.timedelta(days=45),d+dt.timedelta(days=120))
    peak=max(v.values())
    tot=lambda a,b: sum(n for k,n in v.items() if d+dt.timedelta(days=a)<=k<=d+dt.timedelta(days=b))
    pre,post=tot(-30,-1),tot(0,29)
    def decay(frac):
        last=0
        for off in range(0,121):
            if v.get(d+dt.timedelta(days=off),0) >= frac*peak: last=off
        return last
    print(f"{name:22} {peak:>8} {pre:>8} {post:>8} {post/max(pre,1):>8.1f} {decay(.25):>11} {decay(.10):>11}")
