"""BEDTIME TRIM (#425) — bank half of a green position before the operator is asleep.

THE PROBLEM IS A TIMEZONE, NOT A DISCIPLINE. Fill distribution on the operator's real IBKR book, 90 fills over
30 days, by hour in ET and his local SGT:

    ET     SGT    BUYS            SELLS
    09:xx  21:xx    4 / $17,835    16 / $33,849
    10:xx  22:xx   28 / $57,059     8 / $24,018
    11:xx  23:xx    6 / $12,981    10 / $18,897
    12:xx  00:xx    1 / $ 2,290     8 / $12,711
    14:xx  02:xx    4 / $ 6,982     1 / $ 2,297
    15:xx  03:xx    0               2 / $ 1,987
    16:xx+ 04:xx+   0               0

ZERO FILLS AFTER 16:00 ET IN NINETY DAYS. The US open is 21:30 SGT and the close is 04:00 SGT, so the
last four hours of every session — including the closing hour where the day's move is settled — and
every overnight gap run unattended.

MEASURED: +$1,082 across 282 long round trips, 17 flips negative->positive and ZERO flips
positive->negative. It is the only variant tested that had no downside flips, and that is structural
rather than luck: banking part of a gain cannot convert a winner into a loser.

WHAT THIS MODULE IS NOT. It decides a QUANTITY. It places nothing, and it does not know whether today's
trim already happened — the caller owns that, because durability belongs with the caller's journal and
not in a pure function's memory.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

#: Half. Measured at half; a different fraction is a different experiment with no number behind it.
TRIM_FRACTION = 0.5

#: 15:45 ET — late enough that the session's move is largely in, early enough to be a regular-hours
#: market order rather than something queued into the gap this exists to de-risk.
TRIM_AT = dt.time(15, 45)

#: Regular-hours close. Past this a market order queues to the NEXT open, which is the gap itself.
SESSION_CLOSE = dt.time(16, 0)


@dataclass(frozen=True)
class TrimDecision:
    quantity: int
    reason: str


def bedtime_trim(
    *,
    avg_px: float,
    last_px: float,
    quantity: float,
    now_et: dt.datetime,
    trimmed_today: bool,
    fraction: float = TRIM_FRACTION,
    trim_at: dt.time = TRIM_AT,
) -> TrimDecision | None:
    """Shares to sell now, or None when there is nothing to do.

    None is used for every "no" so the caller has one branch. A zero-quantity decision would be a
    rejected order at the venue and would read as a failure in the journal.
    """
    if trimmed_today:
        return None
    # A WINDOW, NOT AN INSTANT. The caller polls, and a single missed tick — a restart, a slow
    # reconciliation, a GC pause — must not silently cost the whole session. That is the shape that
    # lost MOMENTUM its 08-17 and 08-18 sessions.
    if not (trim_at <= now_et.time() < SESSION_CLOSE):
        return None
    # FLAT IS NOT GREEN. Trimming at cost pays the spread for nothing.
    if last_px <= avg_px:
        return None
    shares = int(quantity * fraction)      # rounds DOWN, so a residual position always remains
    if shares <= 0:
        return None
    return TrimDecision(
        quantity=shares,
        reason=(f"bedtime trim {fraction:.0%} of {quantity:g} at {now_et:%H:%M} ET — "
                f"marked {last_px:g} against {avg_px:g}"),
    )
