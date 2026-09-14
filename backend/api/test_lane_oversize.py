"""Which lane's stop is too big? (#748, lane-aware oversize.)

An oversize stop does not merely over-protect: when it triggers it sells shares that are not there,
so the position over-liquidates and FLIPS into opposite exposure nobody asked for. The existing
correction measures coverage against holdings per INSTRUMENT and then shrinks the largest orders
first. With one aggregate stop per leg that is right. With one stop per LANE it is wrong in two
directions, and the second is invisible.

Both scenarios below are the review's, with its numbers.
"""

from __future__ import annotations

from api.lane_oversize import oversize_by_lane


def _stop(coid, qty, symbol="AEM", side="sell", otype="trailing_stop", status="new"):
    return {"symbol": symbol, "side": side, "qty": qty, "order_type": otype,
            "status": status, "filled_qty": 0.0, "client_order_id": coid}


def _owners(m):
    return lambda coid: m.get(coid)


def test_the_WRONG_LANES_CORRECT_STOP_is_not_shrunk():
    """Scenario one. MANUAL sold its 2 AEM and holds 0; MOMENTUM still holds 54. Per-lane stops rest
    at 54 and 2.

    Instrument-level: held 54, covered 56, excess 2 — and largest-first shrinks MOMENTUM's CORRECT
    54-stop down to 52, leaving MANUAL's orphan 2-stop resting against shares MOMENTUM owns. Two
    naked MOMENTUM shares plus a foreign-owned stop whose fill books to a flat lane: the 2026-08-12
    cancel-and-replace shape, reintroduced by arithmetic.

    Lane-level: MOMENTUM is covered exactly and is left alone; MANUAL holds nothing, so its stop is
    cancelled outright — with nothing held, a stop is independently able to open a naked short out
    of nothing.
    """
    orders = [_stop("MOM-1", 54.0), _stop("MAN-1", 2.0)]
    result = oversize_by_lane(
        orders, "AEM.XNYS", "sell",
        held_by_lane={"MOMENTUM-002": 54.0, "MANUAL-001": 0.0},
        owner_of=_owners({"MOM-1": "MOMENTUM-002", "MAN-1": "MANUAL-001"}),
    )
    assert result.incomplete_reason is None
    targets = {o.order["client_order_id"]: o.target_quantity for o in result.oversize}
    assert targets == {"MAN-1": 0.0}, targets


def test_the_INVISIBLE_INVERSE_where_the_instrument_nets_out_and_a_lane_still_over_covers():
    """Scenario two, and the dangerous one, because every instrument-level detector reads green.

    MOMENTUM shrank to 44 while MANUAL grew to 12. Stops still rest at 54 and 2. Instrument: covered
    56 == held 56, so NO oversize fires at all. But MOMENTUM's 54-stop now over-covers its own 44-share
    sleeve by 10, and on trigger it sells 54 against 44 held — flipping MOMENTUM short 10. A mirrored
    short, minted by protection, with nothing anywhere reporting a problem.
    """
    orders = [_stop("MOM-1", 54.0), _stop("MAN-1", 2.0)]
    result = oversize_by_lane(
        orders, "AEM.XNYS", "sell",
        held_by_lane={"MOMENTUM-002": 44.0, "MANUAL-001": 12.0},
        owner_of=_owners({"MOM-1": "MOMENTUM-002", "MAN-1": "MANUAL-001"}),
    )
    # FIXTURE PROPERTY FIRST: the instrument really must net out, or this tests nothing new — the
    # whole point is that the instrument-level view sees a healthy leg.
    assert sum(o["qty"] for o in orders) == 56.0
    assert 44.0 + 12.0 == 56.0

    targets = {o.order["client_order_id"]: o.target_quantity for o in result.oversize}
    assert targets == {"MOM-1": 44.0}, targets


def test_a_lane_that_is_UNDER_covered_produces_no_correction():
    """Under-coverage is the planner's job, not this one's. Emitting a correction for it would shrink
    a stop that is already too small."""
    orders = [_stop("MOM-1", 20.0)]
    result = oversize_by_lane(orders, "AEM.XNYS", "sell",
                              held_by_lane={"MOMENTUM-002": 54.0},
                              owner_of=_owners({"MOM-1": "MOMENTUM-002"}))
    assert result.oversize == ()


def test_MANY_STOPS_IN_ONE_LANE_are_corrected_largest_first_until_the_excess_is_gone():
    """The existing reasoning, kept and scoped to the lane: shrinking only the largest is not enough
    — 50 held against 40+40+40 has an excess of 70, so the largest clamps at 0 and 80 still covers
    50, still able to over-liquidate and flip. Largest-first so the fewest orders are touched.

    Also the live shape: the planner sizes only the shortfall, so one lane routinely holds several
    stops (SSRM rests 53 + 51 today)."""
    orders = [_stop("A", 40.0), _stop("B", 40.0), _stop("C", 40.0)]
    result = oversize_by_lane(
        orders, "AEM.XNYS", "sell",
        held_by_lane={"MOMENTUM-002": 50.0},
        owner_of=_owners({"A": "MOMENTUM-002", "B": "MOMENTUM-002", "C": "MOMENTUM-002"}),
    )
    remaining = sum(o.target_quantity for o in result.oversize) + sum(
        40.0 for o in orders if o["client_order_id"] not in {x.order["client_order_id"] for x in result.oversize})
    assert remaining == 50.0, f"what stays resting must equal what is held: {remaining}"


def test_a_lane_holding_NOTHING_has_its_stop_CANCELLED_not_shrunk():
    """Flat-with-a-stop-resting is the worst case: the trigger opens a naked short from nothing."""
    orders = [_stop("MOM-1", 54.0)]
    result = oversize_by_lane(orders, "AEM.XNYS", "sell",
                              held_by_lane={"MOMENTUM-002": 0.0},
                              owner_of=_owners({"MOM-1": "MOMENTUM-002"}))
    assert [o.target_quantity for o in result.oversize] == [0.0]


def test_a_lane_ABSENT_from_the_holdings_map_is_treated_as_holding_NOTHING_and_said_so():
    """A lane with a stop resting and no entry in the holdings map is flat as far as this read can
    tell — the most dangerous state there is. It must not be skipped for want of a key."""
    orders = [_stop("GHOST-1", 54.0)]
    result = oversize_by_lane(orders, "AEM.XNYS", "sell",
                              held_by_lane={"MOMENTUM-002": 54.0},
                              owner_of=_owners({"GHOST-1": "BCTROT-004"}))
    assert [o.target_quantity for o in result.oversize] == [0.0]


def test_UNATTRIBUTED_COVERAGE_REFUSES_TO_DECIDE_rather_than_shrinking_a_guess():
    """An order the cache cannot name cannot be assigned to a lane, so no lane's coverage is known —
    and shrinking on unknown numbers is how a correctly-sized stop gets cut. The whole leg is
    reported as undecidable and left to the instrument-level path, loudly."""
    orders = [_stop("MOM-1", 54.0), _stop("MYSTERY", 20.0)]
    result = oversize_by_lane(orders, "AEM.XNYS", "sell",
                              held_by_lane={"MOMENTUM-002": 54.0},
                              owner_of=_owners({"MOM-1": "MOMENTUM-002"}))
    assert result.oversize == ()
    assert result.incomplete_reason and "attribut" in result.incomplete_reason


def test_THE_TWO_DERIVATIONS_DISAGREEING_RAISES_rather_than_shrinking_on_one_of_them():
    """Two derivations of one fact will disagree, and here the disagreement is the DETECTOR.

    Per-lane coverage and the instrument-level total answer the same question by different routes:
    `sum(by_lane) + unattributed` must equal `protective_quantity`. If they ever differ, one of them
    is wrong about which orders are resting — and this path is about to SHRINK live protective orders
    on the strength of that number. Continuing would cut a stop on a figure two implementations
    cannot agree on.

    So it raises rather than returning, which is the same call kumo-strategies' ablation makes when
    two arms come back identical: a mechanism that cannot be trusted must stop, not degrade.
    """
    import pytest

    orders = [_stop("MOM-1", 54.0)]

    # A deliberately inconsistent coverage view: names the lane AND leaves it unattributed, so the
    # two derivations cannot both be right.
    import api.lane_oversize as mod

    real = mod.coverage_by_lane
    try:
        mod.coverage_by_lane = lambda *a, **k: type(real(*a, **k))(
            by_lane={"MOMENTUM-002": 54.0}, unattributed=99.0)
        with pytest.raises(ValueError, match="disagree"):
            oversize_by_lane(orders, "AEM.XNYS", "sell",
                             held_by_lane={"MOMENTUM-002": 0.0},
                             owner_of=_owners({"MOM-1": "MOMENTUM-002"}))
    finally:
        mod.coverage_by_lane = real


def test_the_LARGEST_stop_is_the_one_shrunk_when_one_of_several_will_do():
    """Largest-first is the fewest-orders-touched rule, and it was unpinned — mutating it to
    smallest-first passed the whole file, because the TOTAL shrink is order-independent.

    It is not correctness, but it is not free either: every order touched is a cancel-and-replace at
    the venue, and cancel-and-replace on protective orders is what stripped protection off five live
    positions on 2026-08-12. Touching two stops where one would do doubles that exposure.

    SSRM's live shape: 53 + 51 resting, and an excess of 10 must come entirely out of the 53.
    """
    orders = [_stop("BIG", 53.0, symbol="SSRM"), _stop("SMALL", 51.0, symbol="SSRM")]
    result = oversize_by_lane(
        orders, "SSRM.XNAS", "sell",
        held_by_lane={"MOMENTUM-002": 94.0},
        owner_of=_owners({"BIG": "MOMENTUM-002", "SMALL": "MOMENTUM-002"}),
    )
    touched = {o.order["client_order_id"]: o.target_quantity for o in result.oversize}
    assert touched == {"BIG": 43.0}, f"only the largest should be touched: {touched}"
