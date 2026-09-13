"""Give already-stored elections their parties' local names.

    cd backend && uv run python tools/backfill_local_names.py            # dry run
    cd backend && uv run python tools/backfill_local_names.py --apply    # write

Uses the store the app would use — ``ELECTION_STORE`` and the rest, from the
environment or ``.env`` — and needs ``ANTHROPIC_API_KEY``. Only elections in
which no party has a local name are asked about, so a second run asks nothing.

Read the dry run before ``--apply``: a local name may come from the model's
knowledge of the party rather than from any page, and nobody previews these.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_store  # noqa: E402
from app.local_names import AnthropicLocalNames, backfill  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Give stored elections their parties' local names.")
    parser.add_argument("--apply", action="store_true", help="write the names; without it nothing is written")
    apply = parser.parse_args().apply

    store = get_store()
    print(f"store: {type(store).__name__}{'' if apply else ' (dry run: nothing is written)'}")
    changed = asyncio.run(backfill(store, AnthropicLocalNames(), apply=apply))
    print(
        f"\n{changed} election(s) updated" if apply
        else f"\n{changed} election(s) would be updated; run again with --apply to write"
    )


if __name__ == "__main__":
    main()
