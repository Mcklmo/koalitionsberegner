# outreach plugin

A Claude Code plugin with one command, `/outreach:reddit-scan`. It runs the
daily low-budget marketing scan for koalitionsberegner on the owner's machine:

1. Reads the newest posts (and the top-level comments of the most-discussed
   posts) in the subreddits named by `OUTREACH_SUBREDDITS`.
2. Asks a local model (Qwen through Ollama, or any OpenAI-compatible local
   server) one structured question per post or comment: does this mention
   politics, meaning elections, parties, coalitions or politicians? True or
   false.
3. Hands every true to Claude Opus 5 for verification: which election, is it
   coalition talk, is a reply genuinely helpful, and a short reply draft.
4. Matches the election against what the site holds, appends the disclosure
   footer, and queues the draft for the owner's approval through the site's
   `POST /api/admin/outreach/drafts` endpoint (see
   `doc/plans/04-reddit-outreach.md`). Nothing is posted to Reddit by this
   plugin; the owner approves each draft from an emailed one-time link.

## Load it

```sh
claude --plugin-dir ./plugins/outreach
/outreach:reddit-scan --dry-run
```

`--dry-run` scans and classifies locally but calls neither Claude nor the
server.

## Configure

Put these in the environment or in `plugins/outreach/.env` (gitignored):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OUTREACH_SUBREDDITS` | required | Comma-separated subreddit names. Only subreddits whose rules you have read and that allow a disclosed, relevant link. |
| `OUTREACH_SITE` | `https://koalitionsberegner.moritzmarcus.com` | Where elections are listed and drafts are queued. |
| `OUTREACH_ADMIN_SECRET` | required for `--submit server` | The site's `ADMIN_SECRET`. |
| `OUTREACH_LOCAL_API` | `ollama` | `ollama` or `openai` (LM Studio, llama.cpp server, vLLM). |
| `OUTREACH_LOCAL_URL` | `http://localhost:11434` | Base URL of the local model server. |
| `OUTREACH_LOCAL_MODEL` | `qwen3:32b` | Model name as the local server knows it. |
| `OUTREACH_VERIFY_MODEL` | `claude-opus-5` | The verifying model. |
| `ANTHROPIC_API_KEY` | from `ant auth login` if unset | Anthropic credentials. |
| `OUTREACH_LINK_STYLE` | `root` | `root` links to the front page; `share` links to `/e/<id>`, once plan 2 has built that route. |
| `OUTREACH_LIMIT` | `50` | Newest posts read per subreddit. |
| `OUTREACH_COMMENT_POSTS` | `10` | Most-commented posts per subreddit whose top-level comments are read too. |
| `OUTREACH_MAX_DRAFTS` | `5` | Drafts queued per run, whatever was found. |
| `OUTREACH_REDDIT_USER_AGENT` | `koalitionsberegner-outreach/0.1` | Identify yourself to Reddit: add `(by u/yourname)`. |
| `OUTREACH_REDDIT_DELAY` | `2.0` | Seconds between Reddit requests. |
| `OUTREACH_STATE` | `~/.koalitionsberegner-outreach/state.json` | Memory of what has been processed. |

## Test offline

```sh
uv run --with pytest --with pydantic --with anthropic pytest plugins/outreach/scripts
```
