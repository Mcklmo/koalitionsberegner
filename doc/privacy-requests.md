# Answering a privacy request

The page's privacy section tells people to write to info@moritzmarcus.com to
see, correct or delete their data. This is what to do when someone does. Answer
within a month (GDPR Art. 12(3)); keep a short note of what was asked, when, and
what was done, and nothing more.

There is nothing to look up. Nobody signs in, there are no accounts, and
nothing this app stores is tied to a person:

- **Elections and imports** name no one — an import is not linked to whoever
  started it.
- **Usage counts** (`backend/app/usage.py`) are numbers per day: how many
  elections were picked, how many imports started. Nothing says who, which
  election, or from where.
- **Election requests** are filed as public GitHub issues that name only the
  election, never the person who asked.
- **Technical logs** at Cloudflare and Google Cloud, which may include IP
  addresses, expire on their own within 30 days. This app's own logs record
  no personal data (see `backend/app/observability.py`).

So the answer to almost every request is: there is nothing held about you.
Say so, and point at the privacy section for what is processed and why. The
one thing to actually check is whether the person has filed an election
request under a name or detail they now want changed or removed — in that
case, edit or close the GitHub issue as they ask.
