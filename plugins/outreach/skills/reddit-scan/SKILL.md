---
description: Triage a Reddit thread saved from the browser with the local model: does anything in it mention a recent election, an upcoming one, or two or more parties? Writes the thread's JSON blob and the flagged comment(s) to the outreach database for `/outreach:reddit-reply` to answer later. Free — it calls no paid API.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(curl *), Bash(cat *), Bash(ls *), Read
---

# Reddit scan

Stage 1 of the outreach pipeline. It costs nothing, so it is the one to run on
everything worth a look; stage 2 (`/outreach:reddit-reply`) is the one that
spends, and it is run separately, one post at a time.

Arguments given after the command: `$ARGUMENTS` — the saved thread file(s), plus
any of `--rescan`, `--classifier`, `--db`, `--blobs`, `-v`.

## 1. Where the file comes from

Reddit answers a scripted fetch of a thread with `403`, but serves the same JSON
to a logged-in browser. The owner opens the post with `.json` appended
(`https://www.reddit.com/r/de/comments/<id>/.json`) and saves the page. That
saved file is what this command takes; nothing here reads Reddit.

If `$ARGUMENTS` names no file, say so and explain the step above rather than
guessing at a path.

## 2. Prerequisites

Check and stop with a plain explanation if one fails:

1. The file exists and is JSON (`ls -l`, and `head -c 200`).
2. The local model answers: `curl -s http://localhost:11434/api/tags` lists the
   model named by `OUTREACH_LOCAL_MODEL` (default `qwen3:32b`). With
   `OUTREACH_LOCAL_API=openai` check `OUTREACH_LOCAL_URL/v1/models` instead.
   With `--classifier keyword` no model is needed at all — say that the run was
   a plumbing check, not a judgement.

No Anthropic key and no admin secret are needed here. Do not ask for them.

## 3. Run

```sh
uv run plugins/outreach/scripts/scan.py $ARGUMENTS
```

Progress is on stderr, a JSON summary on stdout. The scan asks the local model
three questions per item — recent election, upcoming election, a statement
involving two or more parties — starting with the post itself and then going
through the comments three at a time, and it stops after the first batch that
answers yes to anything. `comments_scanned` says how far it got.

## 4. Report

In prose: which thread was scanned, where its blob was written, how many of its
comments the local pass had to look at before it stopped, and each flagged item
with the questions it answered yes to and its permalink. If `gave_up` is set,
the local model failed three times in a row — say that the scan found nothing
because it could not ask, which is not the same as a thread with nothing in it.

Finish by saying that nothing has been sent anywhere, and that
`/outreach:reddit-reply` is what turns a flagged post into a draft — and that it
reads the *whole* thread, so a comment the local pass never reached is still in
play.
