"""What the book looked like, per LANE, at a capture (#734 step 2).

WHY THIS DOES NOT EXIST ALREADY. `_standing_unrealized_total` (engine_node.py:613) sums unrealized
across the whole ACCOUNT — it asks `portfolio.unrealized_pnl(instrument_id)`, which is per
INSTRUMENT and therefore blind to which lane holds what. Two lanes in one symbol are one number
there. Per-lane P&L history needs the split, and Nautilus already carries it: every `Position` has a
`strategy_id`, so the grouping is native rather than a parallel attribution.

THE THREE OBSERVATIONS PER ROW. `qty x (mark − avg_px_open)` computed here, Nautilus's own
`Position.unrealized_pnl(price)`, and — where the venue publishes one — the broker's figure. Two of
them disagreeing is the detector this table exists to provide; #370's WHD divergence (+$263.84
against the broker's +$9.52) would have shown up daily instead of forensically.

UNKNOWN NEVER BECOMES ZERO. A position with no mark yields an observation with `mark_px=None` and no
derived unrealized, and its LANE is reported as partially unknown. `_standing_unrealized_total` makes
the same choice at account level and says why: "one unpriced position makes the whole total unknown".
A row that silently dropped an unpriced leg would report part of a lane's book as all of it.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from api.eod_observer import LaneObservation, observe_lanes


class _Px:
    """A Nautilus `Price` is not a float; it answers `as_double()`. A double that accepted a bare
    float would let a caller pass one and never find out until production."""

    def __init__(self, v: float) -> None:
        self._v = float(v)

    def as_double(self) -> float:
        return self._v


class _Money(_Px):
    """`Position.unrealized_pnl` returns Money, which answers `as_double()` the same way."""


class _Position:
    """Shaped from the real `nautilus_trader.model.position.Position` surface, read off the class:
    `strategy_id`, `instrument_id`, `signed_qty`, `avg_px_open`, `unrealized_pnl(price)`.

    `unrealized_pnl` REQUIRES a price and returns Money — verified against the installed package's
    signature, not assumed. A double that took no argument would accept a caller that never looked up
    a mark."""

    def __init__(self, lane, instrument, qty, avg_px):
        self.strategy_id = lane
        self.instrument_id = instrument
        self.signed_qty = qty
        self.avg_px_open = _Px(avg_px)

    def unrealized_pnl(self, price):
        if not hasattr(price, "as_double"):
            raise TypeError("Position.unrealized_pnl takes a Price, not a float")
        return _Money(self.signed_qty * (price.as_double() - self.avg_px_open.as_double()))


class _Cache:
    def __init__(self, positions, prices):
        self._positions = positions
        self._prices = prices

    def positions_open(self):
        return list(self._positions)

    def price(self, instrument_id, price_type):
        px = self._prices.get(str(instrument_id))
        return None if px is None else _Px(px)


def _cache(positions, prices):
    return _Cache(positions, prices)


# ==================================================================================================
# THE DOUBLE MUST REJECT WHAT PRODUCTION REJECTS
# ==================================================================================================
def test_the_double_refuses_a_bare_float_where_Nautilus_demands_a_Price():
    """Six doubles in one prior session each hid a live defect by accepting what production rejects.
    `Position.unrealized_pnl(Price)` is Cython-adjacent and takes a Price; a double that accepted a
    float would let the observer pass one and pass its tests."""
    p = _Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0)
    with pytest.raises(TypeError):
        p.unrealized_pnl(105.0)


# ==================================================================================================
# THE GROUPING
# ==================================================================================================
def test_two_lanes_in_ONE_symbol_are_two_observations_not_one():
    """The reason this function exists. `portfolio.unrealized_pnl(instrument_id)` is per INSTRUMENT
    and would return a single number for both lanes — which is exactly what per-lane history cannot
    use. Nautilus tags every Position with its strategy, so the split is native."""
    cache = _cache(
        [_Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0),
         _Position("BCTROT-004", "AEM.XNYS", 5, 120.0)],
        {"AEM.XNYS": 110.0},
    )
    lanes = {o.strategy_id: o for o in observe_lanes(cache)}
    assert set(lanes) == {"MOMENTUM-002", "BCTROT-004"}
    assert lanes["MOMENTUM-002"].positions[0].qty == 9
    assert lanes["BCTROT-004"].positions[0].qty == 5
    # And they share ONE mark — the split is in the holdings, never in the price.
    assert lanes["MOMENTUM-002"].positions[0].mark_px == 110.0
    assert lanes["BCTROT-004"].positions[0].mark_px == 110.0


def test_the_derived_unrealized_and_NAUTILUS_own_figure_are_both_recorded():
    """TWO DERIVATIONS OF ONE FACT, which is the whole point of storing components. They agree here;
    the table exists so that the day they DISAGREE is visible without anyone going looking."""
    cache = _cache([_Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0)], {"AEM.XNYS": 110.0})
    pos = observe_lanes(cache)[0].positions[0]
    assert pos.unrealized_derived == pytest.approx(9 * (110.0 - 100.0))
    assert pos.unrealized_engine == pytest.approx(9 * (110.0 - 100.0))


def test_a_SHORT_position_keeps_its_sign_through_the_whole_row():
    """`signed_qty` is negative for a short and the derived unrealized must follow it — a short that
    rose in price has LOST money. Getting this wrong does not look wrong: it reports a losing short as
    a winner of the same size, which is the sign defect `_match` carries a comment about."""
    cache = _cache([_Position("MANUAL-001", "PENG.XNAS", -23, 53.70)], {"PENG.XNAS": 60.0})
    pos = observe_lanes(cache)[0].positions[0]
    assert pos.qty == -23
    assert pos.unrealized_derived == pytest.approx(-23 * (60.0 - 53.70))
    assert pos.unrealized_derived < 0


# ==================================================================================================
# UNKNOWN IS NOT ZERO
# ==================================================================================================
def test_an_UNPRICED_position_is_recorded_as_UNKNOWN_and_never_as_zero():
    """`_standing_unrealized_total` refuses the whole account total for one unpriced position and
    says why: reporting part of the book as all of it is the quiet wrong answer this panel keeps
    producing. Here the row survives — the holding is a fact — but its mark and its unrealized are
    None, and the LANE says it is incomplete."""
    cache = _cache([_Position("QC345-003", "AMAT.XNAS", 6, 400.0)], {})     # no mark
    lane = observe_lanes(cache)[0]
    pos = lane.positions[0]
    assert pos.qty == 6                       # the holding is known
    assert pos.mark_px is None                # the mark is not
    assert pos.unrealized_derived is None     # and the product of a known and an unknown is unknown
    assert lane.unrealized_total is None, "a lane with an unpriced leg has an UNKNOWN total, not a partial one"
    assert lane.unpriced == 1


def test_a_lane_whose_marks_are_ALL_known_reports_a_total():
    """The other half of the three states: known-complete is a number, and must not be dragged to
    None by a sibling lane's missing mark."""
    cache = _cache(
        [_Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0),
         _Position("QC345-003", "AMAT.XNAS", 6, 400.0)],
        {"AEM.XNYS": 110.0},                                            # AMAT unpriced
    )
    lanes = {o.strategy_id: o for o in observe_lanes(cache)}
    assert lanes["MOMENTUM-002"].unrealized_total == pytest.approx(90.0)
    assert lanes["MOMENTUM-002"].unpriced == 0
    assert lanes["QC345-003"].unrealized_total is None


def test_a_NaN_mark_is_UNKNOWN_not_a_number():
    """NaN survives every comparison written for numbers — the family that disarmed a daily-loss halt
    in kumo-trading-strategies and reached this repo again in #588. A NaN mark must not become a NaN P&L that
    propagates into a stored row and then into a total."""
    cache = _cache([_Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0)], {"AEM.XNYS": math.nan})
    pos = observe_lanes(cache)[0].positions[0]
    assert pos.mark_px is None
    assert pos.unrealized_derived is None


# ==================================================================================================
# ABSENCE
# ==================================================================================================
def test_an_EMPTY_book_yields_NOTHING_and_the_caller_must_say_so_itself():
    """A lane holding nothing produces no observation here, deliberately — this function reports what
    IS held. Turning that into "observed, 0 positions" is the MANIFEST's job, and keeping the two
    separate is what stops a flat lane and a capture that never ran being the same absence."""
    obs = observe_lanes(_cache([], {}))
    assert list(obs) == []
    # And nothing was SKIPPED — an empty book and a book whose positions could not be described are
    # different facts, and the second must never hide inside the first.
    assert obs.skipped == ()


def test_an_UNREADABLE_book_RAISES_rather_than_reporting_an_empty_one():
    """Absence must not be readable as flatness. A cache that cannot be enumerated is a failure the
    manifest must record as `failed`, and returning [] here would make it indistinguishable from a
    genuinely flat account."""
    class _Broken:
        def positions_open(self):
            raise RuntimeError("cache unreadable")

    with pytest.raises(RuntimeError):
        observe_lanes(_Broken())


# ==================================================================================================
# TWO DERIVATIONS OF ONE FACT — the check the live acceptance gate will make, made here first.
#
# `_standing_unrealized_total` is what the ACCOUNT panel already publishes, and it is computed a
# different way: per INSTRUMENT, via `portfolio.unrealized_pnl(instrument_id)`, with no notion of
# lanes at all. Summing this module's per-lane totals must reproduce it exactly.
#
# If the two disagree, the per-lane split is wrong and nothing built on it can be trusted — and the
# disagreement would otherwise surface as a panel whose cells do not add up to its own header, which
# is #596 all over again. This is the same assertion #734's acceptance gate makes against the LIVE
# engine; making it here means the gate is verifying a property that was already true in principle,
# rather than discovering it for the first time on production data.
# ==================================================================================================
class _Portfolio:
    """`_standing_unrealized_total` asks per INSTRUMENT and sums across every lane holding it."""

    def __init__(self, positions, prices):
        self._positions = positions
        self._prices = prices

    def unrealized_pnl(self, instrument_id):
        px = self._prices.get(str(instrument_id))
        if px is None:
            return None
        total = sum(
            p.signed_qty * (px - p.avg_px_open.as_double())
            for p in self._positions
            if str(p.instrument_id) == str(instrument_id)
        )
        return _Money(total)


def test_the_lanes_SUM_to_the_account_total_the_panel_already_publishes():
    """Two lanes in one symbol, one lane in another — the case where a per-instrument view and a
    per-lane view could most easily disagree."""
    positions = [
        _Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0),
        _Position("BCTROT-004", "AEM.XNYS", 5, 120.0),
        _Position("QC345-003", "AMAT.XNAS", 6, 400.0),
    ]
    prices = {"AEM.XNYS": 110.0, "AMAT.XNAS": 420.0}
    cache = _cache(positions, prices)

    from api.engine_node import _standing_unrealized_total

    account = _standing_unrealized_total(cache, _Portfolio(positions, prices))
    lanes = observe_lanes(cache)

    # FIXTURE PROPERTY: the account figure must be a real number, or the equality below is vacuous —
    # `_standing_unrealized_total` returns None on any unknown and None == None would "pass".
    assert account is not None and account != 0
    assert sum(lane.unrealized_total for lane in lanes) == pytest.approx(account)


def test_an_UNPRICED_leg_makes_BOTH_views_refuse_together():
    """The agreement must hold in the unknown direction too. `_standing_unrealized_total` returns
    None for the whole account; the lane holding the unpriced leg returns None while its siblings
    still report — so the account-level refusal and the per-lane refusal must not contradict."""
    positions = [
        _Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0),
        _Position("QC345-003", "AMAT.XNAS", 6, 400.0),
    ]
    prices = {"AEM.XNYS": 110.0}                                   # AMAT unpriced
    cache = _cache(positions, prices)

    from api.engine_node import _standing_unrealized_total

    assert _standing_unrealized_total(cache, _Portfolio(positions, prices)) is None
    lanes = {o.strategy_id: o for o in observe_lanes(cache)}
    assert lanes["QC345-003"].unrealized_total is None
    # And the priced lane is NOT dragged to unknown by its neighbour: that is the whole reason to
    # keep the split, and reporting it as unknown would lose information the account view cannot hold.
    assert lanes["MOMENTUM-002"].unrealized_total == pytest.approx(90.0)


# ==================================================================================================
# WHAT COULD NOT BE DESCRIBED IS REPORTED, NOT DROPPED
# ==================================================================================================
def test_an_UNREADABLE_QUANTITY_is_skipped_and_COUNTED_never_stored_as_zero():
    """The module's headline is UNKNOWN NEVER BECOMES ZERO, and this line used to break it: an
    unreadable `signed_qty` was coerced to 0.0 — which reads as "this lane held nothing", the one
    thing the manifest exists to distinguish from "we could not tell".

    Mutation proved the wire untested before this test existed: replacing the 0.0 with 12345.0 left
    the whole suite green, so any value would have passed."""
    class _Unreadable:
        """`_Position` assigns `signed_qty` in __init__, so a property cannot shadow it — this stands
        alone rather than subclassing, and still answers everything the observer asks for."""

        strategy_id = "MOMENTUM-002"
        instrument_id = "AEM.XNYS"
        avg_px_open = _Px(100.0)

        @property
        def signed_qty(self):
            raise RuntimeError("quantity unreadable")

        def unrealized_pnl(self, price):
            return _Money(0.0)

    pos = _Unreadable()
    obs = observe_lanes(_cache([pos], {"AEM.XNYS": 110.0}))
    assert list(obs) == [], "a position whose quantity is unknown must not become a stored holding"
    assert len(obs.skipped) == 1 and "unreadable" in obs.skipped[0]


def test_an_UNATTRIBUTABLE_position_is_reported_rather_than_vanishing():
    """Guessing which lane owns a position is the attribution leak #292 exists to close. Skipping is
    right; skipping SILENTLY is not — the count is what makes the gap auditable."""
    pos = _Position("", "AEM.XNYS", 9, 100.0)
    obs = observe_lanes(_cache([pos], {"AEM.XNYS": 110.0}))
    assert list(obs) == []
    assert len(obs.skipped) == 1 and "unattributable" in obs.skipped[0]


def test_the_price_is_read_ONCE_so_the_two_derivations_cannot_TEAR():
    """Reading the price twice lets a tick land between them, and the row then records a mark that is
    not the price the engine figure was computed at — a manufactured disagreement on the exact
    derived-vs-engine detector this table exists to provide, at the close capture where the close row
    is the one that ALARMS.

    Demonstrated in review with a ticking cache: mark 110.0, derived 90.0, engine 99.0, no defect
    present anywhere."""
    class _Ticking(_Cache):
        def __init__(self, positions):
            super().__init__(positions, {})
            self._seq = [110.0, 111.0, 112.0]
            self.reads = 0

        def price(self, instrument_id, price_type):
            px = self._seq[min(self.reads, len(self._seq) - 1)]
            self.reads += 1
            return _Px(px)

    cache = _Ticking([_Position("MOMENTUM-002", "AEM.XNYS", 9, 100.0)])
    pos = list(observe_lanes(cache))[0].positions[0]
    assert cache.reads == 1, f"the price was read {cache.reads} times — the two derivations can tear"
    # And with one read they agree, which is what makes a real disagreement meaningful.
    assert pos.unrealized_derived == pytest.approx(pos.unrealized_engine)
