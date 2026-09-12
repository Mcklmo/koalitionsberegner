"""Seats from vote shares, for a poll that publishes only percentages.

Most opinion polls say how many percent each party would get, and a coalition
calculator needs seats. Turning one into the other is arithmetic, so it is done
here, in code, rather than by the extraction agent — which is told never to
infer seats from vote shares, and still is not.

What this module computes is a *proportional approximation*: one nationwide
highest-averages allocation over the assembly's size, with its threshold. The
method, the size and the threshold come from :mod:`app.resolver`, which looked
the election up; the shares come from the poll. Real electoral systems add
wrinkles this does not model — constituency seats, overhang, regional
thresholds, seats reserved for a territory — so a computed forecast can be a
seat or two away from the one the publisher would have drawn. Every election
produced this way is marked ``computed`` (:class:`app.schema.Forecast`), and
the page says so wherever it is shown.
"""

from __future__ import annotations

from typing import Literal

#: The two highest-averages methods proportional systems use. D'Hondt divides by
#: 1, 2, 3, …; Sainte-Laguë by 1, 3, 5, …, which is kinder to small parties.
AllocationMethod = Literal["dhondt", "sainte_lague"]


def allocate(
    shares: list[float],
    seats: int,
    *,
    method: AllocationMethod,
    threshold: float = 0.0,
) -> list[int]:
    """Seats for each share, in the order given.

    ``shares`` are percentages (or any non-negative weights). A share below
    ``threshold`` wins nothing; the rest divide every seat between them. Ties for
    the last seat go to the larger share, then to the party listed first, so the
    same poll always yields the same allocation.
    """
    if seats < 1:
        raise ValueError("an assembly needs at least one seat")
    if any(share < 0 for share in shares):
        raise ValueError("a vote share cannot be negative")
    eligible = [i for i, share in enumerate(shares) if share > 0 and share >= threshold]
    if not eligible:
        raise ValueError("no party reaches the threshold")

    step = 1 if method == "dhondt" else 2
    won = [0] * len(shares)
    for _ in range(seats):
        # Highest quotient share / divisor, where the divisor for a party that
        # holds n seats is n + 1 (D'Hondt) or 2n + 1 (Sainte-Laguë).
        best = max(
            eligible,
            key=lambda i: (shares[i] / (step * won[i] + 1), shares[i], -i),
        )
        won[best] += 1
    return won
