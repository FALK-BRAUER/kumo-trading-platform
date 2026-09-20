"""How much of a window's Δ UNREALIZED is secured by a resting stop (#787).

Operator: "how much of the unrealised delta for instance for the day is secured by a stop".

THE BASELINE IS THE WINDOW'S OWN, NOT THE ENTRY. That is the whole point, and it is what makes this
a genuine subset of the number it sits beside:

    Δ unrealized(W) = Σ (mark_now − mark_at_window_start) × qty
    secured(W)      = Σ max(0, (stop − mark_at_window_start) × qty)

For a long the stop sits BELOW the mark, so a position's secured term cannot exceed its own delta
term; a loser contributes ZERO rather than netting against a winner. So "of today's +$409.63, $X
survives a gap to stops" is a true statement about the same number on the same screen.

WHY THE EXISTING `securedValue` COULD NOT ANSWER THIS. It measures against ENTRY — "how much of my
open gain since I bought cannot evaporate" — which is a different and also useful question, but a
category error against a window delta. Measured on 2026-09-02: $419.91 secured beside $14.57
standing unrealized, because one ignores losers and the other nets them. Secured exceeded the thing
it was supposedly a part of.

UNKNOWN IS NOT ZERO, and today it is the common case. Only 1D has an observed window start
(`/pnl/unrealized-base` returns base_date NONE for 1W, 1M, 3M and all — the book holds two days of
EOD observations). A window whose start was never observed has no baseline to measure against, and
saying 0.00 there would read as "nothing is protected" when the truth is "we cannot tell".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SecuredDelta:
    #: None when the window's start was never observed. NEVER 0.0 for that case.
    value: float | None
    state: str          # "ok" | "unknown"
    #: Held positions with no resting stop — none of their move is protected.
    unsecured: int = 0
    #: Positions the window start captured WITHOUT a mark: cannot be measured, not assumed flat.
    unmarked: int = 0
    reason: str = ""


def secured_delta(base_rows, stops: dict[str, float]) -> SecuredDelta:
    """Secured portion of the window's unrealized move.

    `base_rows` is the window start's per-position observation — `instrument_id`, `qty`, `mark_px` —
    or None when that day was never captured. `stops` maps instrument id to the resting stop's
    trigger price.

    SYMMETRIC BY CONSTRUCTION. A long is secured when the stop sits ABOVE the baseline, a short when
    it sits BELOW. Nothing here may assume a long-only book, even though this one currently is.
    """
    if base_rows is None:
        return SecuredDelta(
            value=None, state="unknown",
            reason="this window's start was never observed, so there is no baseline to measure the "
                   "move against — that is not the same as nothing being protected",
        )

    total = 0.0
    unsecured = 0
    unmarked = 0
    for row in base_rows:
        qty = float(row.get("qty") or 0)
        if qty == 0:
            continue
        base = row.get("mark_px")
        if base is None:
            # CAPTURED WITHOUT A MARK. Excluding it silently understates; counting it as zero
            # misstates. It is reported so the surface can qualify itself.
            unmarked += 1
            continue
        stop = stops.get(str(row.get("instrument_id")))
        if stop is None:
            unsecured += 1
            continue
        # A long is secured by a stop ABOVE the baseline; a short by one BELOW it.
        gain = (float(stop) - float(base)) if qty > 0 else (float(base) - float(stop))
        if gain > 0:
            total += gain * abs(qty)
    return SecuredDelta(value=total, state="ok", unsecured=unsecured, unmarked=unmarked)
