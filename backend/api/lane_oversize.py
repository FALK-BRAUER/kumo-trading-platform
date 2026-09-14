"""Which lane's stop is too big? (#748, lane-aware oversize.)

An oversize stop does not merely over-protect: when it triggers it sells shares that are not there,
so the position over-liquidates and FLIPS into opposite exposure nobody asked for. Flat-with-a-stop-
resting is the worst case — the trigger opens a naked short out of nothing.

The existing correction measures coverage against holdings per INSTRUMENT and shrinks the largest
orders first. With one aggregate stop per leg that is right. With one stop per LANE it is wrong in
two directions, and the second direction is invisible:

  WRONG VICTIM. MANUAL sold its 2 AEM and holds 0; MOMENTUM still holds 54; stops rest at 54 and 2.
  Instrument-level sees held 54, covered 56, excess 2 — and largest-first shrinks MOMENTUM's CORRECT
  54-stop to 52, leaving MANUAL's orphan 2-stop resting against shares MOMENTUM owns. Two naked
  MOMENTUM shares plus a foreign-owned stop whose fill books to a flat lane. That is the
  cancel-and-replace shape that stripped protection off five live positions on 2026-08-12, arrived at
  by arithmetic instead.

  NO VICTIM AT ALL, WHICH IS WORSE. MOMENTUM shrinks to 44 while MANUAL grows to 12; the stops still
  rest at 54 and 2. Instrument-level sees covered 56 == held 56 and fires NOTHING — while MOMENTUM's
  54-stop over-covers its own 44-share sleeve by 10 and, on trigger, flips MOMENTUM short 10. A
  mirrored short minted by protection itself, with every instrument-level detector green.

SO THE COMPARISON MOVES TO THE LANE and the existing shrink reasoning is kept, scoped: largest-first
until what remains resting equals what that lane holds, because shrinking only the largest is not
enough (50 held against 40+40+40 has an excess of 70, so the largest clamps at 0 and 80 still covers
50). One lane routinely holds several stops — the planner sizes only the shortfall, so SSRM rests
53 + 51 today.

AND IT REFUSES RATHER THAN GUESSING. If any resting protective order cannot be attributed to a lane,
no lane's coverage is known, and shrinking on unknown numbers is exactly how a correctly-sized stop
gets cut. The whole leg is reported as undecidable and left to the instrument-level path — loudly,
because a silent hand-back is indistinguishable from a leg with nothing to correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.lane_coverage import coverage_by_lane
from api.protection import Oversize, protective_orders, protective_quantity

#: Same epsilon the instrument-level correction uses. Two thresholds for one question drift apart.
_QTY_EPS = 1e-9


@dataclass(frozen=True)
class LaneOversize:
    oversize: tuple[Oversize, ...] = field(default_factory=tuple)
    #: Why this leg could not be decided per lane, or None. Never silently empty: an undecidable leg
    #: and a healthy one must not read the same.
    incomplete_reason: str | None = None


def oversize_by_lane(orders, instrument_id: str, reducing_side: str,
                     held_by_lane: dict, owner_of) -> LaneOversize:
    """Per-lane oversize corrections on one leg.

    `orders` are BROKER rows; `held_by_lane` maps `strategy_id` to the quantity that lane holds on
    this leg (unsigned); `owner_of(client_order_id)` names the owning lane or returns None.
    """
    coverage = coverage_by_lane(orders, instrument_id, reducing_side, owner_of)

    # TWO DERIVATIONS OF ONE FACT, AND THE DISAGREEMENT IS THE DETECTOR. Per-lane coverage and the
    # instrument-level total answer "how much protection is resting on this leg" by different routes,
    # and they must agree exactly. If they do not, one of them is wrong about which orders are
    # resting — and this path is about to SHRINK LIVE PROTECTIVE ORDERS on the strength of that
    # number. Raising rather than returning is the same call made elsewhere in this codebase when a
    # mechanism cannot be trusted: stop, do not degrade.
    #
    # THE CALLER MUST CATCH THIS PER LEG, AND MUST NOT SWALLOW IT. Raising is right AT THIS FUNCTION
    # and wrong as a TICK OUTCOME: it already earned its keep by catching a real drift — a
    # `quantity`-keyed protective row parsed as zero coverage — and that single bilingual row would
    # have raised on EVERY tick. An unwrapped caller turns a vocabulary defect into a permanent
    # protection outage on that path, which is a worse failure than the one being detected.
    #
    # So: catch per leg, place nothing for THAT leg, and report the broken check as its OWN standing
    # condition. Never let it kill the tick, and never let the catch be silent — a swallowed
    # exception on every poll is indistinguishable from a clean poll, which is the failure two
    # detectors here already shipped with.
    instrument_total = protective_quantity(orders, instrument_id, reducing_side)
    if abs(coverage.total - instrument_total) > _QTY_EPS:
        raise ValueError(
            f"per-lane and instrument-level coverage disagree on {instrument_id} "
            f"({coverage.total:g} vs {instrument_total:g}); refusing to shrink live protective "
            f"orders on a quantity two derivations cannot agree on"
        )

    if not coverage.is_complete:
        return LaneOversize(incomplete_reason=(
            f"{coverage.unattributed:g} share(s) of resting protection on {instrument_id} could not "
            f"be attributed to a lane, so no lane's coverage is known and shrinking would cut a stop "
            f"on a guess"
        ))

    # The orders themselves, grouped by owner, so a correction names a REAL order rather than a
    # quantity. `protective_orders` decides what counts as protection — not re-decided here, because
    # two derivations of that judgement drift, which is this area's whole subject.
    by_lane: dict[str, list] = {}
    for order in protective_orders(orders, instrument_id, reducing_side):
        lane = owner_of(str(order.get("client_order_id") or ""))
        if lane:
            by_lane.setdefault(str(lane), []).append(order)

    out: list[Oversize] = []
    for lane, lane_orders in sorted(by_lane.items()):
        covered = coverage.by_lane.get(lane, 0.0)
        # A LANE ABSENT FROM THE HOLDINGS MAP HOLDS NOTHING, and that is the most dangerous state
        # rather than a missing key to skip: a stop resting against no position opens a naked short
        # when it triggers. Absence must not read as permission to leave it alone.
        held = float(held_by_lane.get(lane, 0.0) or 0.0)
        excess = covered - held
        # An early exit, not a guard: the inner loop breaks on the same condition, so a mutation
        # deleting this line changes nothing. Said here so the next reader does not mistake a
        # surviving mutant on it for a missing test.
        if excess <= _QTY_EPS:
            continue
        for order in sorted(lane_orders, key=lambda x: -float(x["_remaining"])):
            if excess <= _QTY_EPS:
                break
            remaining = float(order["_remaining"])
            take = min(remaining, excess)
            out.append(Oversize(
                instrument_id=instrument_id,
                side="SELL" if reducing_side.lower() == "sell" else "BUY",
                held=held,
                covered=covered,
                order=order,
                target_quantity=max(0.0, remaining - take),
            ))
            excess -= take

    return LaneOversize(oversize=tuple(out))
