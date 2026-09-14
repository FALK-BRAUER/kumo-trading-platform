"""Would this order REDUCE the lane's position, or open an opposite one? (#748, second shape)

The protective-stop path was fixed by refusing to submit a stop it cannot stamp. That guards ONE
route. `fable`'s reading of the live book found a second, with the same root cause and no guard:

    a NON-reduce-only LANE EXIT stamped with a lane that does not hold the shares books as an
    OPPOSITE-SIDE position on that lane

Measured on paper 2026-08-31: WHD (BCTROT-004 +28 / MOMENTUM-002 −28), CGAU (MOMENTUM-002 −88).

WHY IT LOOKS HEALTHY AND IS NOT. The account NET matches the venue, so no reconciliation diff is
generated and no EXTERNAL mirror appears — the mint's usual fingerprint is absent. What is corrupted
is the SPLIT: one lane shows a long it does not have and another a short it never opened, and both
lanes' budgets size off those numbers. It is the same defect as the phantom, minus the tell.

REDUCE-ONLY IS NOT THE TEST, and that is the whole point. Nautilus enforces reduce-only itself
(`_reject_reduce_only_netting_position_open`) — which is why the protective-stop shape at least
FAILS LOUDLY at the venue boundary. An exit submitted without that flag has no such backstop: it is
accepted, it fills, and it silently opens the opposite side.

SO THIS ASKS THE OWNERSHIP QUESTION DIRECTLY: does the lane named on the order hold enough, on the
right side, for this order to be a reduction? Anything else is refused and said out loud.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Share-count tolerance. Venue-rounded quantities must not make a full exit look like an overshoot.
_QTY_EPS = 1e-9

OK = "reduces"
NO_POSITION = "the lane holds nothing"
WRONG_SIDE = "the lane holds the other side"
OVERSIZE = "larger than the lane holds"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def classify_exit(*, side: str, quantity: float, lane: str, instrument_id: str,
                  lane_signed_qty: float | None) -> Verdict:
    """Is this SELL/BUY a reduction of `lane`'s position, or would it open the opposite side?

    `lane_signed_qty` is the lane's own signed holding — POSITIVE long, NEGATIVE short, `0.0` flat,
    and `None` meaning THE BOOK COULD NOT BE READ. None is not zero: refusing every order because a
    cache read failed would stop a lane trading for a reason that has nothing to do with ownership,
    and this guard must never become the thing that halts a healthy book. Unreadable is ALLOWED and
    says so, exactly as the budget gate fails open.
    """
    if lane_signed_qty is None:
        return Verdict(True, "unreadable", f"{lane} holding for {instrument_id} could not be read")

    qty = abs(float(quantity))
    held = float(lane_signed_qty)
    # THE RULE IS THE RESULTING POSITION, NOT THE SIDE, and the first version got this wrong in a way
    # that stopped trading. It asked "does this reduce what the lane holds", which describes a normal
    # opening BUY exactly as badly as a broken exit — so it refused every entry on paper within
    # minutes of shipping: eight orders, two lanes, all declined by the guard meant to protect them.
    #
    # `budget_gate.is_entry` is not the discriminator either. A SELL from a flat lane GROWS exposure,
    # so that predicate calls it an entry — and it is precisely the WHD shape this guard exists for.
    #
    # In a LONG-ONLY book the honest question is simply: would this leave the lane SHORT? A buy that
    # opens or adds is fine; a sell that reduces or flattens is fine; anything that ends below zero is
    # a lane holding a short it never opened, which is the corrupted split with no venue-side tell.
    #
    # AND THE BOOK IS NOT LONG-ONLY ANY MORE (#1030). The question is the same on both sides — would
    # this leave the lane holding the side it is NOT declared to hold — and the side comes from ONE
    # place, `ownership.lane_side`, read here rather than passed in: a kwarg with a default is a kwarg
    # one call site forgets, and forgetting it is the original defect (a permitted short's every
    # entry refused as "the lane holds nothing"). For a SHORT lane every sign flips: a SELL from flat
    # is its entry, a BUY reduces, and anything that ends ABOVE zero is the lane holding a long it
    # never opened — the WHD shape with the sign reversed, which the long-only rule called "reduces".
    from api.ownership import LONG, lane_side

    declared = lane_side(lane)
    other = "SHORT" if declared == LONG else "LONG"
    # Measured in the lane's OWN direction, so one comparison serves both sides: positive means
    # "on the declared side", negative means "on the other side".
    sign = 1.0 if declared == LONG else -1.0
    resulting = held + (qty if side == "BUY" else -qty)
    if sign * resulting >= -_QTY_EPS:
        return Verdict(True, OK, f"{lane} holds {held:+g}, {side} {qty:g} leaves {resulting:+g}")

    if abs(held) <= _QTY_EPS:
        return Verdict(False, NO_POSITION,
                       f"{lane} holds no open {instrument_id}; a {side} {qty:g} would OPEN a {other} "
                       f"on that lane, which is declared {declared}, not close a position")
    if sign * held < 0:
        return Verdict(False, WRONG_SIDE,
                       f"{lane} already holds {held:+g} {instrument_id}; a {side} {qty:g} adds to a "
                       f"{other} it should not have")
    return Verdict(False, OVERSIZE,
                   f"{lane} holds {held:+g} {instrument_id} and this {side} is {qty:g} — the excess "
                   f"{qty - abs(held):g} would flip the lane {other}")
