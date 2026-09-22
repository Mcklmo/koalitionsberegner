# outreach plugin

A Claude Code plugin with two commands. They are deliberately separate,
because one of them is free and the other one is not.

| Command | Script | Costs | What it does |
| --- | --- | --- | --- |
| `/outreach:reddit-scan` | `scripts/scan.py` | nothing | Triages a thread saved from Reddit with a local model and writes what it found to SQLite. |
| `/outreach:reddit-reply` | `scripts/reply.py` | two Claude Opus calls | Answers **one** scanned post and queues the draft for approval. |

Nothing in this plugin posts to Reddit. Every draft goes to the site's approval
queue, where the owner reads it, edits it if needed, and presses send from a
one-time link that arrived by email (`doc/plans/04-reddit-outreach.md`).

## Stage 1 — scan

Reddit answers a scripted fetch of a thread with `403` but serves the same JSON
to a logged-in browser. Open the post with `.json` on the end and save the
page:

```
https://www.reddit.com/r/de/comments/<id>/whatever/.json?limit=500&raw_json=1
```

`limit=500` asks for the whole comment page rather than the first handful, and
`raw_json=1` stops Reddit HTML-escaping `&`, `<` and `>` in the text. Save it
into the inbox, `plugins/outreach/inbox/` — it is beside the code, so the
editor's own file tree is where you drop it, and its contents are gitignored.
Then:

```sh
./plugins/outreach/scripts/scan.py -v            # everything in the inbox
./plugins/outreach/scripts/scan.py ~/Downloads/saved.json -v   # or one file
```

A file, a directory of them, `-` for stdin, or nothing at all — which reads the
inbox and creates it if it is not there yet. A thread already in the database
is skipped, so leaving the files in the inbox costs one lookup each.

`--slim` keeps the blob down to the fields the pipeline actually reads. A real
r/berlin thread saved from the browser is 2.7 MB, of which about 3 KB is the
argument: each comment carries seventy keys — awards, flair, `body_html`,
moderation fields — and four are read. Slimmed, that thread is 8 KB, in
Reddit's own shape, so `reply.py` cannot tell the difference. Nothing else in
the pipeline is affected either way: the clutter never reached a model, which
is handed the post and the comment bodies and nothing else.

The local model is asked three questions about each item, not one:

* does it mention an election that has been held,
* does it mention one still to come,
* does it make a statement involving two or more parties.

The post itself is asked first. If it says yes to anything the scan stops
there; otherwise the comments go **three at a time, concurrently**, and the
scan stops after the first batch containing a yes. One reason to open a thread
is enough, and every comment after that would be a local call spent on a
decision already made — on a 31-comment thread that is usually six calls
instead of thirty-two.

What comes out is the path of the thread's JSON blob and the flagged item(s),
on stdout and in the database:

```json
{"post_id": "1abc23", "blob": "~/.koalitionsberegner-outreach/posts/de/1abc23.json",
 "comments_total": 31, "comments_scanned": 6,
 "flagged": [{"thing_id": "t1_xyz", "position": 5, "upcoming_election": true,
              "multi_party": true, "reason": "…", "permalink": "https://…"}]}
```

`--classifier keyword` runs the whole pipeline with a word list instead of a
model — useful to check the plumbing on a machine without Ollama, useless as a
judgement. `--rescan` scans a thread that is already in the database again.

## Stage 2 — reply

```sh
./plugins/outreach/scripts/reply.py next          # the oldest flagged post
./plugins/outreach/scripts/reply.py list          # what is waiting
./plugins/outreach/scripts/reply.py show <id>     # one row, its flags, its blob
./plugins/outreach/scripts/reply.py reset <id>    # answer that post again
```

`next` takes exactly one post per invocation. It reads the **whole** thread
back from the blob — not only what the local pass flagged, because that pass
stops at its first yes and a comment further down is often the better opening —
and then:

1. asks Claude which election the thread is about and whether a reply is worth
   writing at all (no → the post is marked, and the second call never happens);
2. matches that election against what the site holds;
3. asks Claude for the reply: which item to answer, which parties make the
   coalition worth arguing about, and the text;
4. builds the link, the seat totals and the disclosure footer **in code**, and
   queues the draft.

The reply is written to provoke an argument about the arithmetic, because that
is what gets read: a contestable claim about who reaches a majority and who
does not. The prompt draws the line — never an insult, never a number the seat
list does not support — and the owner still reads every draft before it goes
anywhere.

The link is a coalition, not the front page: `/e/<id>?c=<parties>&s=<seats>`,
the same shape the site's own share button produces.

**When the site holds no election for the thread**, the election is filed in
the issue tracker through the same open endpoint the page's "ask for it" button
uses, and the post is *kept*: `reply.py next --retry` takes it again once the
election has been imported.

`--dry-run` prints the draft instead of queueing it. It still calls Claude, and
still marks the row — `reset` is how you get the post back.

## Load it

```sh
claude --plugin-dir ./plugins/outreach
/outreach:reddit-scan ~/Downloads/saved.json
/outreach:reddit-reply next
```

## Configure

Put these in the environment or in `plugins/outreach/.env` (gitignored):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OUTREACH_SITE` | `https://koalitionsberegner.moritzmarcus.com` | Where elections are read and drafts are queued. |
| `OUTREACH_ADMIN_SECRET` | required by `reply.py` | The site's `ADMIN_SECRET`. |
| `OUTREACH_LOCAL_API` | `ollama` | `ollama` or `openai` (LM Studio, llama.cpp server, vLLM). |
| `OUTREACH_LOCAL_URL` | `http://localhost:11434` | Base URL of the local model server. |
| `OUTREACH_LOCAL_MODEL` | `qwen3:32b` | Model name as the local server knows it. |
| `OUTREACH_VERIFY_MODEL` | `claude-opus-5` | The model that writes the reply. |
| `ANTHROPIC_API_KEY` | from `ant auth login` if unset | Anthropic credentials. |
| `OUTREACH_DB` | `~/.koalitionsberegner-outreach/outreach.db` | What the scan found, and what has been answered. |
| `OUTREACH_BLOBS` | `~/.koalitionsberegner-outreach/posts` | Where saved threads are kept. |
| `OUTREACH_INBOX` | `plugins/outreach/inbox` | Where the scan looks when no file is named. |

A default Ollama install serialises requests unless `OLLAMA_NUM_PARALLEL` is
set; the scan is correct either way, just not faster.

## Test offline

```sh
uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts
```

Nothing in the suite touches the network, Reddit, Ollama or Claude.

## Before you switch any of this on

Read `doc/plans/04-reddit-outreach.md`, "Before you switch this on". Reddit
removes and bans for undisclosed self-promotion; a reply that does not answer
the thread is spam even when it is honest, and a provocative one that misses is
worse. The footer the sanitiser appends discloses both who is behind the link
and that a model drafted the text. Do not remove it — the server checks for it
and refuses a draft without it.
