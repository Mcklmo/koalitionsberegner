---
description: Scan the configured subreddits for posts and comments about current elections, parties, coalitions or politicians; verify the hits with Claude; draft a reply linking to koalitionsberegner; queue each draft for the owner's emailed one-click approval. Run once a day on the dev machine.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(curl *), Bash(cat *), Bash(ls *), Read
---

# Reddit scan

You are running the daily outreach scan for koalitionsberegner. The script does
the work; your job is to check the prerequisites, run it, and report. You never
post to Reddit yourself, never call the Reddit API directly, and never edit the
disclosure footer the script appends to every draft.

Arguments given after the command: `$ARGUMENTS` (for example `--dry-run`).

## 1. Prerequisites

Check each and stop with a plain explanation if one fails:

1. `OUTREACH_SUBREDDITS` is set (comma-separated subreddit names, no `r/`).
   Read it from the environment or from `plugins/outreach/.env` if present
   (`cat plugins/outreach/.env` masks nothing, so only print variable names).
2. The local model answers: `curl -s http://localhost:11434/api/tags` lists the
   model named by `OUTREACH_LOCAL_MODEL` (default `qwen3:32b`). With
   `OUTREACH_LOCAL_API=openai` check `OUTREACH_LOCAL_URL/v1/models` instead.
3. Anthropic credentials resolve: `ANTHROPIC_API_KEY` is set, or `ant auth status`
   reports an active profile.
4. `OUTREACH_ADMIN_SECRET` is set when `--submit server` is used (the default).
   Without it, run with `--submit stdout` and say that drafts were only printed.

## 2. Run

```sh
uv run plugins/outreach/scripts/scan.py $ARGUMENTS
```

The script prints progress on stderr and a JSON summary on stdout. It keeps its
own memory of what it has seen in `~/.koalitionsberegner-outreach/state.json`,
so running it twice in a day is harmless.

## 3. Report

Relay the summary in prose: posts and comments scanned, how many the local
model flagged, how many Claude confirmed as election talk worth a reply, how
many drafts were queued for approval (with the subreddit and the post title of
each), how many were skipped because no matching election is stored on the
site, and any errors. If drafts were queued, remind the owner that approval
links arrive by email and expire.

If the summary lists elections that Reddit users discussed but the site does not
hold (`missing_elections`), say so: those are import candidates.
