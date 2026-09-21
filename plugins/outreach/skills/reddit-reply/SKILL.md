---
description: Spend one Claude Opus call on one scanned Reddit post: read the whole thread, pick the comment worth answering, and queue a reply that links to a specific coalition on koalitionsberegner for the owner's emailed one-click approval. Run it deliberately, one post at a time — this is the half that costs money.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(curl *), Bash(cat *), Bash(ls *), Read
---

# Reddit reply

Stage 2 of the outreach pipeline. `/outreach:reddit-scan` has already triaged
threads for free; this takes **one** of them and answers it. Never run it in a
loop, and never run it to "clear the queue" — the whole point of the split is
that each call is a decision the owner made.

Arguments given after the command: `$ARGUMENTS` (for example `next`,
`--post <id>`, `--retry`, `--dry-run`, `list`, `show <id>`, `reset <id>`).

## 1. Prerequisites

Check each and stop with a plain explanation if one fails:

1. Something is waiting: `uv run plugins/outreach/scripts/reply.py list`. An
   empty `waiting` list means the scan found nothing to answer — say so instead
   of running anything.
2. Anthropic credentials resolve: `ANTHROPIC_API_KEY` is set, or `ant auth status`
   reports an active profile.
3. `OUTREACH_ADMIN_SECRET` is set. Without it the draft cannot be queued: run
   with `--dry-run`, which prints the draft instead, and say that it was only
   printed.

## 2. Run

```sh
uv run plugins/outreach/scripts/reply.py $ARGUMENTS
```

Two Claude calls per post at most: one to name the election and decide whether
the thread is worth answering at all (a no ends it there), one to write the
reply. The link, the seat totals and the disclosure footer are assembled in
code; no model writes a URL or a number into the text.

## 3. Report

Relay the JSON in prose:

- **queued** — which item was answered (and whether it was one the local pass
  had flagged, which `answered.was_flagged` says), which coalition the link
  names, how its seats compare to the majority, and the reply text in full.
  Remind the owner that the approval link arrives by email and expires, and
  that nothing reaches Reddit until they press send.
- **not_worth** — Claude read the thread and found nothing worth answering.
  Say why it said so. Nothing was queued and the post is done.
- **no_election** — the site holds no election for what the thread is about, so
  it was filed in the issue tracker. Give the issue link. The post is *kept*:
  once that election is imported, `reply.py next --retry` picks it up again.
- **failed** — say what failed. The post stays claimable with `--retry`.

## 4. Answering a post again

`reply.py reset <post_id>` clears the "done" mark and the post is next in line
again — that is how a changed prompt is tried on a thread that was already
answered. `reset --all` does it for every post. Mention this when a draft comes
out badly; do not rerun `next` and hope for a different one.
