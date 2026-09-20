"""ATR RATCHET (#425) — what replaces hand-set stop distances.

WHY. The real IBKR book, 2026-05-27..2026-08-18, split by HOW the position was exited:

    STOP     57 fills   -3,259.85   17.5% hit rate
    LIMIT    30 fills     +104.75   25.0%
    MARKET  174 fills     +157.01   40.6%

Everything exited by hand nets +$262. The stop orders are -$3,260 — more than the entire loss. The
distances were chosen by hand while the position was underwater, which is the one moment nobody can
choose one well. ARGX: bought 1001.53 at 15:19:52Z, stopped 1000.74 at 15:22:06Z — 134 seconds, -0.08%,
a distance inside the spread. PLTR: stopped $0.52 above the day's low, 25 minutes after entry.

FOUR PROPERTIES. Three of them exist so this cannot decay into a hand-set stop with extra steps:

  1. ARMS ONLY IN PROFIT — nothing rests until the position is +1.0 x ATR14 unrealized.
  2. NEVER BELOW ENTRY — an armed position cannot be taken out for a loss by this mechanism.
  3. RATCHETS — the level only rises. A stop that can retreat gives back protection already earned.
  4. IT DOES NOT REPLACE THE OPERATOR'S OWN EXIT. Measured as an ADDITION: +$310. Measured REPLACING
     his exit with a 5-session horizon: -$11,587. His 2.3-hour median hold is the thing he already does
     right, and every variant that overrode it lost five figures.

1.5 x ATR is deliberately the SAME multiple `protection.py` already uses for the resting protective
stop, so the ratchet and the backstop cannot disagree about how wide a stop on this symbol should be —
two derivations of one width have already differed by a factor of five here (#239: 11.91% on FIG while
PEAK rested 2.5%, same instrument, same moment).

Decision only. Places nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Unrealized profit, in ATRs, required before anything rests at all.
ARM_AT_ATR = 1.0

#: Trail width below the running high, in ATRs. Matches protection.py's protective-stop multiple.
TRAIL_ATR = 1.5


@dataclass(frozen=True)
class RatchetStop:
    price: float
    reason: str


def ratchet_stop(
    *,
    entry: float,
    running_high: float,
    atr: float,
    current_stop: float | None,
    arm_at_atr: float = ARM_AT_ATR,
    trail_atr: float = TRAIL_ATR,
) -> RatchetStop | None:
    """The stop that should rest for a LONG, or None when this mechanism should rest nothing.

    None is not "no protection" — the platform's own backstop is a separate mechanism and is
    unaffected. It means THIS operator has nothing to say yet.
    """
    if atr <= 0 or entry <= 0:
        # No usable ATR means no defensible width. Guessing one is how two mechanisms came to disagree
        # by a factor of five on the same symbol.
        return None
    if running_high < entry + arm_at_atr * atr:
        return None                                   # not yet a full ATR in profit — nothing rests

    trailed = running_high - trail_atr * atr
    # THE FLOOR. Without it an armed stop sits below entry whenever the high is under
    # entry + (arm + trail) x ATR, and the mechanism becomes the hand-set stop it replaces.
    price = max(trailed, entry)
    # THE RATCHET. Only ever upward; an existing tighter stop wins.
    if current_stop is not None:
        price = max(price, current_stop)
    return RatchetStop(
        price=price,
        reason=(f"armed at +{arm_at_atr:g} ATR, trailing {trail_atr:g} ATR below {running_high:g}"
                f"{' (floored at entry)' if price == entry else ''}"),
    )
