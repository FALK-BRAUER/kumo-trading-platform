"""Rebuilding what each lane held on a past day, from fills (#734 backfill).

WHY IT IS POSSIBLE AT ALL. The engine can only observe TODAY, so a forward-only table would leave
1W/1M/3M dark for weeks. But the broker's fill history reaches account inception (607 activities,
earliest 2026-07-13, zero corporate actions), and this repo already owns a FIFO lot matcher that is
production-hardened — shorts, partial fills, flips through flat, per-lot opening tags. Truncate the
fills at a past instant and its residue IS that day's book.

SO THIS FUNCTION MATCHES NOTHING ITSELF. It truncates, delegates to `open_lots_detail`, and shapes
the result. A second matcher would drift from the one whose leftovers are reconciled against the
broker's real positions in production — the check that found the PENG phantom lot.

WHAT IT REFUSES TO DO. It never guesses an opener. A lot whose tag is None is money whose owner is
unknowable from the fills (the venue-order-cache join is 100% only from 2026-08-17 and ~1% before),
and #292 is explicit that it must not be credited to whoever closed the position. Those lots go to
UNCLAIMED as a first-class row, which is also what keeps the per-lane cells summing to the account.
"""

from __future__ import annotations

import pytest

from api.eod_reconstruct import UNCLAIMED, reconstruct_lanes


def _fill(sym, side, qty, price, t):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price), "transaction_time": t}


TAGS = {
    "2026-08-01T13:00:00Z": "MOMENTUM-002",
    "2026-08-02T13:00:00Z": "BCTROT-004",
}
_tag = lambda f: TAGS.get(f["transaction_time"])          # noqa: E731


# ==================================================================================================
# THE FIXTURE MUST BE ABLE TO EXPRESS WHAT IS BEING TESTED
# ==================================================================================================
def test_the_fixture_spans_the_cutoff_it_is_used_to_test():
    """A fill list entirely before the cutoff would make truncation a no-op, and every assertion
    about "as of" would pass without exercising anything."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-20T13:00:00Z"),
    ]
    before = [f for f in fills if f["transaction_time"] <= "2026-08-10"]
    assert len(before) == 1 and len(fills) == 2


# ==================================================================================================
# AS OF A PAST INSTANT
# ==================================================================================================
def test_it_reconstructs_the_book_AS_OF_a_past_day_ignoring_later_fills():
    """The whole point: what did this lane hold THEN, not now."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-20T13:00:00Z"),      # after the cutoff
    ]
    obs = reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0}, strategy_of=_tag)
    lanes = {l.strategy_id: l for l in obs}
    assert list(lanes) == ["MOMENTUM-002"]
    assert lanes["MOMENTUM-002"].positions[0].qty == 10.0
    assert lanes["MOMENTUM-002"].positions[0].avg_px_engine == 100.0


def test_a_position_CLOSED_before_the_cutoff_leaves_no_row():
    """Flat then is flat. A zero-qty row would make every consumer decide whether zero means flat or
    unknown, and that distinction is what the manifest exists to keep."""
    fills = [
        _fill("FIG", "buy", 278, 24.10, "2026-08-01T13:00:00Z"),
        _fill("FIG", "sell", 278, 25.96, "2026-08-02T13:00:00Z"),
    ]
    assert list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"FIG": 26.0}, strategy_of=_tag)) == []


def test_the_unrealized_uses_THAT_DAY_mark_and_the_lot_basis():
    """`qty x (mark(T) − basis)`. The mark is the day's close, supplied by the caller from historical
    bars — not today's price, which would report months of accrued movement as that day's state."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z")]
    pos = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 130.0}, strategy_of=_tag))[0].positions[0]
    assert pos.mark_px == 130.0
    assert pos.unrealized_derived == pytest.approx(10 * (130.0 - 100.0))


# ==================================================================================================
# ATTRIBUTION — measured, never guessed
# ==================================================================================================
def test_two_lanes_in_one_symbol_split_by_the_lot_that_OPENED_each():
    """#292: P&L follows the OPENING strategy, tagged at LOT level, disposal FIFO. The matcher
    already carries the tag; this only groups by it."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-02T13:00:00Z"),
    ]
    obs = reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0}, strategy_of=_tag)
    lanes = {l.strategy_id: l.positions[0].qty for l in obs}
    assert lanes == {"MOMENTUM-002": 10.0, "BCTROT-004": 5.0}


def test_an_UNTAGGED_lot_becomes_UNCLAIMED_and_is_never_given_to_a_lane():
    """A lot from before the cache's horizon has no knowable opener. Crediting it to whoever closed
    the position is the exact leak #292 exists to close — "MOMENTUM does the work, a human clicks
    sell, and the manual book gets the credit"."""
    fills = [_fill("WPM", "buy", 7, 50.0, "2026-07-01T13:00:00Z")]          # not in TAGS
    obs = reconstruct_lanes(fills, as_of="2026-08-10", marks={"WPM": 60.0}, strategy_of=_tag)
    lanes = {l.strategy_id: l for l in obs}
    assert list(lanes) == [UNCLAIMED]
    assert lanes[UNCLAIMED].positions[0].qty == 7.0


def test_attribution_coverage_is_MEASURED_from_the_tags_not_assumed():
    """The review's F11: a constant standing in for a measurement is the QC27-allocated-equity shape.
    Coverage is the fraction of THIS row's qty whose opener is known — 1.0 for a tagged row, 0.0 for
    the unclaimed one — and it falls out of the tagging rather than being declared."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),            # tagged
        _fill("WPM", "buy", 7, 50.0, "2026-07-01T13:00:00Z"),              # untagged
    ]
    obs = reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0, "WPM": 60.0}, strategy_of=_tag)
    cov = {l.strategy_id: l.attribution_coverage for l in obs}
    assert cov["MOMENTUM-002"] == 1.0
    assert cov[UNCLAIMED] == 0.0


# ==================================================================================================
# UNKNOWN IS NOT ZERO — the same rule as the live observer
# ==================================================================================================
def test_a_symbol_with_NO_MARK_for_that_day_is_UNKNOWN_and_keeps_its_quantity():
    """A day the bar feed cannot price is not a day the lane held nothing. The holding is a fact; the
    valuation is not, and the lane's total goes UNKNOWN rather than partial — the same choice
    `observe_lanes` and `_standing_unrealized_total` both make."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z")]
    lane = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={}, strategy_of=_tag))[0]
    assert lane.positions[0].qty == 10.0
    assert lane.positions[0].mark_px is None
    assert lane.positions[0].unrealized_derived is None
    assert lane.unrealized_total is None
    assert lane.unpriced == 1


def test_it_produces_the_SAME_SHAPE_the_live_observer_does():
    """So one writer serves both. If these drifted, the reconstructed rows and the engine rows would
    stop being comparable — and the overlap between them is the only thing that validates the
    backfill before anyone trusts it."""
    from api.eod_observer import LaneObservation, PositionObservation

    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z")]
    lane = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0}, strategy_of=_tag))[0]
    assert isinstance(lane, LaneObservation)
    assert isinstance(lane.positions[0], PositionObservation)


def test_the_ENGINE_figure_is_NOT_invented_for_a_reconstructed_row():
    """`unrealized_engine` is Nautilus's own reading of a live position. There is no such reading for
    a past day, and copying the derived number into that column would manufacture an agreement on the
    exact detector the table exists to provide — two derivations that are secretly one."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z")]
    pos = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0}, strategy_of=_tag))[0].positions[0]
    assert pos.unrealized_derived is not None
    assert pos.unrealized_engine is None, "there is no engine reading for a past day; None says so"


def test_ONE_lane_holding_TWO_lots_gets_a_QUANTITY_WEIGHTED_basis():
    """10@100 and 5@120 is 106.67, not 110. The unweighted mean is wrong in the direction that looks
    plausible, and every fixture above holds ONE lot per lane — so mutation showed the weighting was
    untested: dividing by something other than the total quantity left the suite green.
    """
    # REAL TIMESTAMPS: the cutoff is a STRING comparison, so "a"/"b"/"z" only worked by alphabetical
    # accident and would not have caught an ordering bug. The double must look like production.
    tags = {"2026-08-01T13:00:00Z": "MOMENTUM-002", "2026-08-02T13:00:00Z": "MOMENTUM-002"}
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-02T13:00:00Z"),
    ]
    lane = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0},
                                  strategy_of=lambda f: tags.get(f["transaction_time"])))[0]
    pos = lane.positions[0]
    # FIXTURE PROPERTY: one lane, two lots, different prices — or the weighting cannot be observed.
    assert pos.qty == 15.0
    assert round(pos.avg_px_engine, 4) == round((10 * 100.0 + 5 * 120.0) / 15, 4) == 106.6667
    # And the unweighted mean would be 110.0, which is what a plausible-looking bug produces.
    assert pos.avg_px_engine != 110.0


# ==================================================================================================
# THE CUTOFF CONTRACT — review found the boundary pinned by nothing
# ==================================================================================================
def test_a_fill_on_an_EARLIER_day_is_included_and_the_boundary_is_pinned():
    """MUTATION SURVIVOR, reported by review: changing `<=` to `<` left all 23 tests green, because
    every fixture used a date-only `as_of` where same-day fills arrive via the PREFIX match and the
    comparison boundary was never exercised. This drives the comparison directly."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-09T13:00:00Z")]     # strictly before the cutoff
    lane = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0},
                                  strategy_of=lambda f: "MOMENTUM-002"))[0]
    assert lane.positions[0].qty == 10.0


def test_a_fill_ON_the_cutoff_day_is_included_whatever_its_time_or_precision():
    """The prefix half. Fills on the cutoff day carry every shape the venue emits — whole seconds,
    fractional seconds of varying length — and all of them belong to that session."""
    for stamp in ("2026-08-10T09:30:00Z", "2026-08-10T13:35:53.9Z", "2026-08-10T19:59:59.999999Z"):
        fills = [_fill("AEM", "buy", 10, 100.0, stamp)]
        lanes = list(reconstruct_lanes(fills, as_of="2026-08-10", marks={"AEM": 110.0},
                                       strategy_of=lambda f: "MOMENTUM-002"))
        assert lanes and lanes[0].positions[0].qty == 10.0, f"{stamp} was excluded from its own day"


def test_a_MALFORMED_cutoff_is_REFUSED_rather_than_silently_selecting_the_wrong_fills():
    """`as_of="2026-08-1"` was silently catastrophic: it took everything through 08-19 via the prefix
    match AND everything before 08-10 via the comparison. Two wrong answers combined into one
    plausible-looking book."""
    fills = [_fill("AEM", "buy", 10, 100.0, "2026-08-19T13:00:00Z")]
    for bad in ("2026-08-1", "2026-08", "2026-08-10T16:00:00Z", "", "yesterday"):
        with pytest.raises(ValueError, match="whole trading date"):
            reconstruct_lanes(fills, as_of=bad, marks={}, strategy_of=None)


def test_a_DOTTED_symbol_survives_resolution_intact():
    """`BRK.B` is a real symbol in the live paper database, alongside `BRKB`. Any resolver written as
    `instrument_id.split(".")[0]` turns `BRK.B.XNYS` into `BRK` — a different company — and that
    parse is exactly what a hurried fix reaches for. The reconstruction never parses; it asks. This
    pins that the value it is given survives unmangled, so a future "simplification" that starts
    splitting has something to fail against."""
    fills = [_fill("BRK.B", "buy", 10, 100.0, "2026-08-20T13:00:00Z")]
    lanes = list(reconstruct_lanes(fills, as_of="2026-08-20", marks={"BRK.B": 110.0},
                                   strategy_of=lambda f: "MOMENTUM-002",
                                   instrument_of=lambda sym: f"{sym}.XNYS"))
    assert lanes[0].positions[0].instrument_id == "BRK.B.XNYS"
