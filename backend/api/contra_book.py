"""Verify the AIM, then book the repair — or refuse (#744, the executor's booking half).

`prepare_execution` answers "is the approved plan still true of the book as it is now". This answers
the narrower and more dangerous question: **is each leg pointed at a position that actually exists?**

WHY THAT IS THE WHOLE RISK. From `contra_execute`'s own docstring: a fill aimed at an id that is not
the real one does not close the short — it OPENS A NEW POSITION, re-minting the exact defect being
repaired, in a lane that may hold nothing at all. And a live cache read once found all sixteen legs
matching the reconstructed shape while only ONE was inspected deeply enough to confirm the id
embedded in it. Being right by construction is not the same as being right.

So every leg is checked against the LIVE cache before anything is booked, and a pair with even one
unverifiable leg is refused WHOLE. Half a repair is worse than none: closing the short without giving
back what it claimed leaves the book further from the broker than it started.

ARMING IS SEPARATE AND DEFAULTS OFF. This decides; the caller books. That split is not ceremony — it
is what lets the decision be tested against a real damaged book with nothing at stake, and it follows
the repo's rule that every new automation gate is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: A leg's target must match the position's own id exactly. Positions are keyed
#: `{instrument}-{strategy}` under NETTING, and a near-miss is not a near-hit — it is a new position.
UNKNOWN_POSITION = "no such position in the live book"
WRONG_LANE = "the position at that id belongs to another lane"
WRONG_SIDE = "the leg would move the position the wrong way"
NOT_ENOUGH = "the position no longer holds what the leg gives back"


@dataclass(frozen=True)
class Booking:
    fingerprint: str
    instrument_id: str
    legs: tuple


@dataclass(frozen=True)
class AimRefusal:
    fingerprint: str
    instrument_id: str
    reason: str
    detail: str


@dataclass(frozen=True)
class BookingPlan:
    book: tuple[Booking, ...] = field(default_factory=tuple)
    refused: tuple[AimRefusal, ...] = field(default_factory=tuple)
    #: Pairs skipped because the ledger says they are already repaired. Reported separately from
    #: refusals: "done" and "declined" are different facts, and collapsing them would make a
    #: completed repair look like a failing one on every subsequent tick.
    already_done: tuple[str, ...] = field(default_factory=tuple)


def _index(positions) -> dict:
    out = {}
    for p in positions or ():
        pid = str(getattr(p, "id", "") or getattr(p, "position_id", "") or "")
        if pid:
            out[pid] = p
    return out


def verify_and_book(execution_plan, positions, ledger) -> BookingPlan:
    """Decide which approved repairs may be booked against the book as it is RIGHT NOW.

    `positions` is the live position book — open AND closed, because a leg aimed at a position that
    has since closed must be refused rather than silently opening a new one at that id.
    """
    by_id = _index(positions)
    book, refused, done = [], [], []

    for order in getattr(execution_plan, "orders", ()) or ():
        fp = str(order.fingerprint)
        if ledger is not None and ledger.already_booked(fp):
            done.append(fp)
            continue

        problem = None
        for leg in order.legs:
            pos = by_id.get(str(leg.position_id))
            if pos is None:
                problem = AimRefusal(fp, order.instrument_id, UNKNOWN_POSITION,
                                     f"{leg.position_id} is not in the live book; booking against it "
                                     f"would OPEN a position rather than close one")
                break
            if str(getattr(pos, "strategy_id", "")) != str(leg.strategy_id):
                problem = AimRefusal(fp, order.instrument_id, WRONG_LANE,
                                     f"{leg.position_id} belongs to "
                                     f"{getattr(pos, 'strategy_id', '?')}, not {leg.strategy_id}")
                break
            signed = float(getattr(pos, "signed_qty", 0) or 0)
            # A BUY leg closes a phantom SHORT; a SELL leg gives back from a real LONG. Either way the
            # leg must REDUCE the magnitude, never add to it — the same rule as `exit_ownership`, for
            # the same reason, on the internal plane.
            if (leg.side == "BUY" and signed >= 0) or (leg.side == "SELL" and signed <= 0):
                problem = AimRefusal(fp, order.instrument_id, WRONG_SIDE,
                                     f"{leg.position_id} is {signed:+g}; a {leg.side} "
                                     f"{leg.quantity:g} would increase it, not close it")
                break
            if abs(signed) + 1e-9 < float(leg.quantity):
                problem = AimRefusal(fp, order.instrument_id, NOT_ENOUGH,
                                     f"{leg.position_id} holds {signed:+g} and the leg is "
                                     f"{leg.quantity:g} — it would overshoot and flip the position")
                break

        if problem is not None:
            # THE WHOLE PAIR, not the offending leg. Closing the short without giving back what it
            # claimed leaves the book further from the broker than it started.
            refused.append(problem)
            continue
        book.append(Booking(fp, order.instrument_id, tuple(order.legs)))

    return BookingPlan(tuple(book), tuple(refused), tuple(done))
