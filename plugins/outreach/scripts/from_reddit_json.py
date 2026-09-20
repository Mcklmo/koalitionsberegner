#!/usr/bin/env python3
"""Turn Reddit's own JSON for one thread into a `--posts-file` for `scan.py`.

Reddit answers a scripted fetch of a thread with 403, but it serves the same
JSON to a logged-in browser: open the post with `.json` appended, save the
page, and run this on the file. For trying the pipeline on a thread you picked
by hand, which is what the plugin's README calls the first week's work.

    python3 from_reddit_json.py saved.json > thread.json
    scan.py --posts-file thread.json --submit stdout --state none

Reads one thread's listing: `[{post}, {comments}]`, as `/comments/<id>.json`
returns it. Nothing here reaches the network, and nothing is posted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Top-level comments only, and never Reddit's "load more" placeholder: the
#: pipeline answers a thread, and a reply to a reply is a different argument.
COMMENT_KIND = "t1"


def _post(listing: list) -> dict:
    children = listing[0]["data"]["children"]
    if not children:
        raise SystemExit("no post in that file — is it a thread's .json?")
    return children[0]["data"]


def _comments(listing: list) -> list[dict]:
    if len(listing) < 2:
        return []
    return [
        child["data"]
        for child in listing[1]["data"]["children"]
        if child.get("kind") == COMMENT_KIND and child["data"].get("body")
    ]


def convert(raw: list) -> dict:
    post = _post(raw)
    post_id = post["id"]
    out = {
        "posts": [
            {
                "id": post_id,
                "subreddit": post["subreddit"],
                "title": post.get("title", ""),
                "text": post.get("selftext", ""),
                "permalink": "https://www.reddit.com" + post["permalink"],
                "created_utc": post.get("created_utc", 0),
                "num_comments": post.get("num_comments", 0),
            }
        ],
    }
    comments = [
        {
            "id": comment["id"],
            "text": comment["body"],
            "permalink": "https://www.reddit.com" + comment.get("permalink", ""),
            "created_utc": comment.get("created_utc", 0),
        }
        for comment in _comments(raw)
    ]
    if comments:
        out["comments"] = {post_id: comments}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="the saved <thread>.json, or - for stdin")
    parser.add_argument("-o", "--out", help="write here instead of stdout")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"that file is not JSON ({error}) — save the page, not a screenshot") from None
    if not isinstance(raw, list):
        raise SystemExit("expected a thread listing: append .json to the post's URL")

    converted = json.dumps(convert(raw), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(converted + "\n", encoding="utf-8")
        post = converted and json.loads(converted)["posts"][0]
        print(
            f"r/{post['subreddit']} {post['id']}: "
            f"{len(json.loads(converted).get('comments', {}).get(post['id'], []))} comments "
            f"-> {args.out}",
            file=sys.stderr,
        )
    else:
        print(converted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
