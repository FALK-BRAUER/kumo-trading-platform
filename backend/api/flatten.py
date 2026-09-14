"""Flatten a position (#170, first slice) — close what a strategy holds, long or short.

Deliberately NOT a plain order from the UI. An order sized when the screen rendered can be wrong by the time
the human finishes the confirm gesture: the position may have been partly closed, a stop may have fired, or a
resting exit may already be working. Sending the stale quantity then doesn't flatten — it **reverses**, which
is the one outcome an exit control must never produce.

So the size is decided by the ENGINE against its live cache, and the request carries what the human SAW
(`expected_side`, `expected_qty`). If reality has moved, the command is rejected and they re-read rather than
being silently given a different trade from the one they confirmed.

Symmetric by construction: a LONG is closed by SELLing its quantity, a SHORT by BUYing it. Nothing here
assumes a direction.

Session: two outcomes only, deliberately not three. An earlier version tried to price a marketable limit
through the current bid/ask outside regular hours — but outside RTH there often isn't a live quote at all
(the feed goes idle), and a thin extended-hours book routinely shows a 10%+ spread, so pricing off it either
never fills (safe side of the spread) or dumps the position several percent through the last print (unsafe
side). Fighting that spread was the wrong problem to solve. Queuing until the open sidesteps it entirely:
regular hours has real two-sided liquidity, so the close just becomes the same market order it always is,
merely deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

FlattenAction = Literal["EXECUTE", "QUEUE", "REJECT"]


@dataclass(frozen=True)
class FlattenPlan:
    """The order to send — market only. A limit has no role here: in regular hours a market order is exactly
    right, and outside them the request is queued rather than priced into an unreliable book."""

    side: str  # BUY | SELL — the CLOSING side, opposite the position
    quantity: Decimal


@dataclass(frozen=True)
class FlattenDecision:
    """Exactly one of `order`/`reason` is set, matching `action`."""

    action: FlattenAction
    order: FlattenPlan | None = None
    reason: str | None = None
    #: Resting reducing orders must be cancelled AND CONFIRMED GONE before the close is sent. Left
    #: resting they would fire against a position the close has already removed, opening the opposite
    #: side — the risk the old REJECT branch named but made the operator handle by hand.
    cancel_resting_first: bool = False


def closing_side(position_side: str) -> str:
    """The side that reduces a position. A LONG is closed by selling, a SHORT by buying."""
    side = position_side.upper()
    if side == "LONG":
        return "SELL"
    if side == "SHORT":
        return "BUY"
    raise ValueError(f"cannot flatten side {position_side!r} — expected LONG or SHORT")


def decide(
    *,
    position_side: str,
    live_qty: Decimal,
    expected_side: str,
    expected_qty: Decimal | None,
    resting_reducing_qty: Decimal,
    market_open: bool,
) -> FlattenDecision:
    """Validate against live state, then EXECUTE now, QUEUE for the open, or REJECT.

    Validation runs identically whether this is the operator's first request or a replay at the open — the
    replay calls this again with the SAME payload against fresh live state, so a position that changed while
    queued is caught exactly like one that changed between render and confirm.

    `resting_reducing_qty` blocks rather than being netted off. A protective stop resting on the position is
    the common case, and flattening beneath it would leave that stop live against a position that no longer
    exists — it fires later and opens the opposite side. Cancelling it automatically is the right end state
    but a cancel is not instant, and racing it against the close is worse than saying no. Cancel-then-close
    belongs with the full manage ticket.
    """
    if live_qty <= 0:
        return FlattenDecision("REJECT", reason="nothing to flatten — the position is already closed")
    if position_side.upper() != expected_side.upper():
        return FlattenDecision(
            "REJECT",
            reason=f"the position is {position_side} but you confirmed a {expected_side} — it flipped; re-read and retry",
        )
    if expected_qty is not None and expected_qty != live_qty:
        return FlattenDecision(
            "REJECT",
            reason=f"the position is {live_qty} now, not the {expected_qty} you confirmed — re-read and retry",
        )
    # Resting exits are CANCELLED as part of the close, not a reason to refuse it.
    #
    # This used to reject, telling the operator to go and cancel them by hand. The danger it named is
    # real — an exit left resting after the position closes fires against nothing and opens the opposite
    # side — but refusing does not avoid that danger, it just makes the human perform the same sequence
    # manually, and leaves them unable to exit their own position in the meantime. Operator, 2026-08-13, on
    # WDAY: "I cannot even flatten it."
    #
    # The engine cancels them, waits for the venue to CONFIRM (a cancel is asynchronous — an unconfirmed
    # one still reserves the shares), and only then sends the close.
    order = FlattenPlan(side=closing_side(position_side), quantity=live_qty)
    if resting_reducing_qty > 0:
        return FlattenDecision(
            "EXECUTE" if market_open else "QUEUE", order=order, cancel_resting_first=True
        )

    if market_open:
        return FlattenDecision("EXECUTE", order=order)
    return FlattenDecision("QUEUE", order=order)
