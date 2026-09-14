"""Whose stop is that? (#748, lane-aware coverage.)

`unprotected_positions` credits coverage PRO-RATA: it measures protection per INSTRUMENT and then
multiplies by each lane's share of the quantity. That is the correct answer to the question it was
written for — its docstring says so plainly, "is the book covered", not "is the attribution tidy" —
and the wrong one for the question a per-lane split asks.

WHAT PRO-RATA PRODUCES. MOMENTUM holds 54 with its own 54-share stop resting; MANUAL holds 2 with
none. Instrument coverage is 54 against 56 held, so MOMENTUM is told it is 1.93 uncovered when it is
covered exactly, and MANUAL 0.07 when it is naked for all 2. A planner building per-lane stops from
those numbers rests a second ~1.93 MOMENTUM stop — lane-level OVER-coverage, which flips that lane
short on trigger — and a 0.07 MANUAL stub protecting nothing. The producer disclaims per-lane meaning
and the new consumer would read it as a per-lane fact anyway.

THE JOIN IS THE SAME SHAPE AS THE POSITION SPLIT, for the same reason. The BROKER says which orders
are resting and is the only trustworthy source for it (#285: the cache once held three orders as
REJECTED that Alpaca reported `new`, and a cache-based coverage check answers "nothing resting", so
the next pass places a DUPLICATE stop). The CACHE says whose they are, because a broker row carries a
client order id and nothing else that identifies a lane — the id's hash covers the lane but does not
reveal it, deliberately, since ids are capped at 36 characters.

AN ORDER THE CACHE CANNOT NAME MAKES THE SPLIT INCOMPLETE, NOT ZERO. Dropping it under-counts
coverage and rests a duplicate stop; crediting it to a guessed lane is the defect this ticket exists
to end. It is counted in the total, kept out of the per-lane map, and reported — so a consumer can
refuse to split that instrument instead of splitting on a number it cannot justify.

WHAT COUNTS AS PROTECTION IS NOT RE-DECIDED HERE. `protective_orders` already encodes it (a
take-profit limit above the market does nothing on the way down), and two derivations of that
judgement would drift — which is the subject of this entire area.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.protection import protective_orders


@dataclass(frozen=True)
class LaneCoverage:
    """Resting protective quantity, per lane, plus what could not be attributed."""

    by_lane: dict[str, float] = field(default_factory=dict)
    #: Resting protective quantity whose owner the cache could not name. Counted, never dropped:
    #: dropping it under-counts coverage, and the next pass rests a duplicate stop over shares that
    #: are already reserved — which is `403 insufficient qty available`, measured.
    unattributed: float = 0.0

    @property
    def is_complete(self) -> bool:
        """Whether every resting protective order was attributed to a lane.

        NOTHING RESTING IS COMPLETE, not unknown. Every lane is uncovered, which is a known state;
        reporting it as unknown would send every clean instrument down the caller's fallback and
        disable the per-lane split everywhere it is needed most.

        AND IT IS ONLY AS TRUTHFUL AS THE ORDERS READ, which is a PRECONDITION THIS REPO HAS ALREADY
        MEASURED BEING VIOLATED. `orders=None` raises, so that much is fail-closed. But an EMPTY
        ARRAY from a paced or failing read is indistinguishable from a genuinely empty book, and IB
        answers an over-paced request with exactly that: 1,516 empty responses, no error — a rate
        limiter that refuses by returning success. On that venue a false "nothing resting" becomes
        `is_complete=True` plus every-lane-uncovered, and the planner rests per-lane stops over
        shares already reserved by stops the read did not show. IB has no share reservation to bounce
        them, so both fire on trigger.
        
        The distinction cannot be made here — this function cannot tell a healthy empty list from a
        broken one. It must be a READ-HEALTH GATE AT THE CALLER, and the wiring must not inherit this
        property without that precondition.
        """
        return self.unattributed == 0.0

    @property
    def total(self) -> float:
        return sum(self.by_lane.values()) + self.unattributed


def coverage_by_lane(orders, instrument_id: str, reducing_side: str, owner_of) -> LaneCoverage:
    """Per-lane resting protective quantity on one leg.

    `orders` are BROKER rows. `owner_of(client_order_id)` returns the owning `strategy_id`, or None
    when the cache cannot name it — typically an order the cache never saw, or a bracket leg carrying
    a client order id the VENUE generated rather than ours.
    """
    by_lane: dict[str, float] = {}
    unattributed = 0.0

    for order in protective_orders(orders, instrument_id, reducing_side):
        # `_remaining` IS THE NUMBER `protective_orders` EXISTS TO PRODUCE — already net of fills,
        # and already bilingual: it is built from `qty or quantity`, because the broker's frames say
        # `qty` and ours say `quantity`. Re-deriving it here from `order["qty"]` alone read only one
        # of those languages, so a `quantity`-keyed row came back as ZERO coverage with
        # `is_complete=True` and the shares resting unseen. The vacuity sat in the quantity parse
        # rather than in attribution, so the refuse-to-split channel stayed silent while the answer
        # was confidently wrong. Second derivations of one fact drift; this module's docstring says
        # so about "what counts as protection", and it is just as true one field over.
        try:
            qty = float(order["_remaining"])
        except (TypeError, ValueError, KeyError):
            qty = 0.0
        if qty <= 0:
            continue
        coid = str(order.get("client_order_id") or "")
        lane = owner_of(coid) if coid else None
        if not lane:
            unattributed += qty
            continue
        by_lane[str(lane)] = by_lane.get(str(lane), 0.0) + qty

    return LaneCoverage(by_lane=by_lane, unattributed=unattributed)
