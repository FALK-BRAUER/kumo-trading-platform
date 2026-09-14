"""WIDE-STOP-ON-OPEN (#425) — loosen the protective stop through the opening auction, restore it after.

WHY THIS EXISTS, measured on the operator's real IBKR book over 2026-05-27..2026-08-18 (301 FIFO round trips):

    exit order type   fills   realized P&L   hit rate
    STOP                 57      -3,259.85      17.5%
    LIMIT                30        +104.75      25.0%
    MARKET              174        +157.01      40.6%

Everything exited by hand nets +$262. The stop orders are -$3,260 — 109% of the loss. And 28 of the 64
stop-exited round trips fired in the FIRST FIFTEEN MINUTES of the session, of which 25 (89%) reclaimed
their own stop price the same day. They were not detecting danger; they were being swept by the opening
auction and then the price came back.

WHY WIDEN RATHER THAN SUSPEND. the operator's proposal, and it measured the same as suspending while keeping
real protection. Simulated on the 28 first-15-minute stop-outs, arm 10:00, hold 2 sessions, stacked with
PEAK:

    wide stop at      delta        wide stop actually hit
    1.0x the leash    +$2,934      10/11
    1.5x             +$2,903        9/11
    2.0x             +$2,874        7/11
    3.0x             +$2,894        4/11      <- chosen
    none (suspend)   +$2,901        0/11

FLAT ACROSS EVERY LEVEL. The level is not what matters; the opening print is. So take the variant that
still fires 4 times in 11 — at 3x the stop is real protection against a genuine collapse, and it scores
within $7 of deleting the stop outright. The same-day-only grid (no multi-day hold) gives +$2,845..2,877,
so the effect is inside the first thirty minutes rather than a holding effect in disguise.

WHAT THIS MODULE IS NOT. It decides a PRICE. It does not place, modify or cancel anything. The order
path must reach the venue with `PATCH /v2/orders/{leg_id}` and NEVER cancel-then-submit in either
order: Alpaca reserves shares against a resting sell and releases them only on a CONFIRMED cancel, so
cancel->submit gets `available: 0` and submit->cancel gets the replacement rejected and then cancels the
only protection there was. That is FIG on 2026-08-11 (#240) and the five positions stripped on
2026-08-12. `_report_reconcile_drift`'s own probe notes the venue swaps atomically on PATCH and releases
no reservation, which is the whole reason this is expressible at all.

SCOPE: PLAIN stop legs only. A trailing stop is out until `scripts/probe_trailing_replace.py` answers
whether the high-water mark survives a replace — if it RESETS, a restored tight trail sits below the
market and silently never elects, which is worse than never having widened.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

#: Multiple of the original leash the stop is widened to through the window. See the table above — the
#: curve is flat, so this is chosen for retained protection rather than for P&L.
WIDE_MULTIPLE = 3.0

#: The window closes here, ET. 09:45 measured +$2,873 against 10:00's +$2,901 with PEAK; both work, and
#: the later one is chosen because the opening imbalance is not reliably done at 09:45.
RESTORE_AT = dt.time(10, 0)

#: Session open, ET. Before this there is no auction to be swept by and the stop stays as placed.
SESSION_OPEN = dt.time(9, 30)


@dataclass(frozen=True)
class StopDecision:
    """`price` is what should REST right now. `widened` says which regime produced it, so a caller can
    log the transition rather than inferring it from a float comparison."""

    price: float
    widened: bool
    reason: str


def open_window_stop(
    *,
    entry_price: float,
    original_stop: float,
    now_et: dt.datetime,
    wide_multiple: float = WIDE_MULTIPLE,
    restore_at: dt.time = RESTORE_AT,
) -> StopDecision | None:
    """The stop price that should rest for a LONG at `now_et`, or None when it cannot be decided.

    None rather than a guess: a non-positive leash means the caller handed us a stop at or above entry,
    which is not a protective stop for a long and must not be silently widened into one.
    """
    leash = entry_price - original_stop
    if leash <= 0 or entry_price <= 0:
        return None
    if wide_multiple < 1.0:
        # A multiple below 1 would TIGHTEN the stop into the auction, which is the opposite of the point
        # and would manufacture the very sweep this exists to avoid.
        return None

    in_window = SESSION_OPEN <= now_et.time() < restore_at
    if not in_window:
        return StopDecision(price=original_stop, widened=False,
                            reason="outside the opening window — the original stop rests")

    widened = entry_price - (leash * wide_multiple)
    return StopDecision(price=widened, widened=True,
                        reason=f"opening window until {restore_at:%H:%M} ET — "
                               f"{wide_multiple:g}x the {leash:.4f} leash")
