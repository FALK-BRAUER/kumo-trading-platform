"""An IBKR node must publish its own standing unrealized (#596).

`NET(period) = realized + Δunrealized`, and the Δ term reaches the panel from the ALPACA exec
client, which sums that venue's own per-position fields. Nothing publishes a broker snapshot on an
IBKR node, so staging served:

    GET :8010/account -> {"unrealized_standing_total": null, "unrealized_intraday_total": null, ...}

and every NET period rendered an em dash. Correct behaviour on missing input; the input should not
have been missing. `Portfolio.unrealized_pnl` is Nautilus's own figure and exists on every adapter.

INTRADAY STAYS NULL ON THAT PATH, deliberately. IBKR reports no day figure, and substituting the
standing number would report a position's whole life as today — the #573 mistake in another costume.
"""

from __future__ import annotations

import pytest

from api.engine_node import UiFeedStrategy, _standing_unrealized_total


class _Money:
    """Nautilus returns `Money`, not a float. A double handing back a bare number would let a
    missing `.as_double()` pass here and raise in production."""

    def __init__(self, value: float):
        self._value = value

    def as_double(self) -> float:
        return self._value


class _Pos:
    def __init__(self, instrument_id):
        self.instrument_id = instrument_id


class _Cache:
    def __init__(self, instrument_ids):
        self._ids = instrument_ids

    def positions_open(self):
        return [_Pos(i) for i in self._ids]


class _Portfolio:
    def __init__(self, pnls):
        self._pnls = pnls

    def unrealized_pnl(self, instrument_id):
        return self._pnls[instrument_id]


def _total(instrument_ids, pnls):
    return _standing_unrealized_total(_Cache(instrument_ids), _Portfolio(pnls))


def test_the_double_returns_MONEY_like_nautilus_does():
    """Fixture property. If it returned a float, `as_double()` would be untested and the production
    call would raise on the first real position."""
    assert hasattr(_Portfolio({"A": _Money(1.0)}).unrealized_pnl("A"), "as_double")


def test_it_sums_every_open_instrument():
    assert _total(["A", "B"], {"A": _Money(100.5), "B": _Money(-30.25)}) == pytest.approx(70.25, abs=0.01)


def test_an_EMPTY_book_is_zero_because_nothing_held_is_a_fact():
    """Distinct from unknown: a book holding nothing has an unrealized of exactly zero."""
    assert _standing_unrealized_total(_Cache([]), _Portfolio({})) == 0.0


def test_ONE_unpriced_instrument_makes_the_WHOLE_total_unknown():
    """A partial sum is the quiet wrong answer — it reports part of the book as all of it. `None`
    from `unrealized_pnl` means the instrument has no mark."""
    assert _total(["A", "B"], {"A": _Money(100.0), "B": None}) is None


@pytest.mark.parametrize("poison", [float("nan"), float("inf"), float("-inf")])
def test_a_NON_FINITE_pnl_makes_the_total_unknown(poison):
    """`nan` survives every comparison written for numbers — the family that disarmed a daily-loss
    halt in kumo-trading-strategies 43c6d3e and reached this repo again in #588."""
    assert _total(["A"], {"A": _Money(poison)}) is None


def test_absent_cache_or_portfolio_is_UNKNOWN_not_zero():
    """Thirteen existing tests drive `_publish_account` UNBOUND against a `types.SimpleNamespace`.
    Missing inputs must yield unknown rather than a confident zero — which is also why this is a
    module function and not a method: it must not demand anything of `self`."""
    assert _standing_unrealized_total(None, None) is None


def test_an_UNREADABLE_position_book_is_unknown_and_SAYS_SO(caplog):
    """Some doubles hold bare `object()` positions with no `instrument_id`. Unknown is the honest
    answer, but it must report itself — a silent 0.0 would pass as a healthy reading, and this panel
    has published enough confident wrong numbers."""

    class _Opaque:
        def positions_open(self):
            return [object()]

    with caplog.at_level("WARNING"):
        assert _standing_unrealized_total(_Opaque(), _Portfolio({})) is None
    assert any("position book unreadable" in r.getMessage() for r in caplog.records), (
        "the degraded path did not report itself"
    )


def test_the_DERIVED_frame_actually_carries_BOTH_keys():
    """THE SEAM. A correct helper proves nothing about the frame — staging's defect was that the
    derived frame simply had no such key. The unit was never the problem."""
    import inspect

    # The frame body moved into `derived_account_frame` (#591) — one builder for both frames — so
    # the pin follows the fact to its new home: the publish site computes standing and HANDS IT to
    # the builder, and the builder carries both keys with intraday explicitly None.
    from api.engine_node import derived_account_frame

    src = inspect.getsource(UiFeedStrategy._publish_account)
    assert "_standing_unrealized_total(" in src, "the derived frame never computes it"
    assert "standing=standing" in src, "the publish site does not hand standing to the builder"
    frame = derived_account_frame(equity=1.0, cash=1.0, cash_ccy=None, standing=42.0, ts=1)
    assert frame["unrealized_standing_total"] == 42.0, "the builder drops the standing key"
    assert frame["unrealized_intraday_total"] is None, (
        "intraday must be EXPLICITLY None here — IBKR reports no day figure, and borrowing Alpaca's "
        "semantics would report a position's whole life as today (#573's shape)"
    )
