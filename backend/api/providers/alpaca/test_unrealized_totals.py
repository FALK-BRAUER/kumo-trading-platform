"""The Δ half of NET, summed from the broker's own per-position fields (#596).

`NET(period) = realized(period) + Δunrealized(period)`. Δunrealized used to be BACK-SOLVED as
`net - realized`, which made the panel's stated identity true by construction — it could not
disagree, so it could not detect anything. Measured on an Alpaca paper instance 2026-08-27, all five periods rendered
Δ as exactly minus REALIZED with NET $0.00.

UNKNOWN MUST PROPAGATE. A total summed from only the positions that HAPPEN to carry the field would
report a partial book as the whole one. IBKR publishes no intraday field at all, so its total must
be None by construction rather than zero by accident.
"""

from __future__ import annotations

import pytest

from api.providers.alpaca.exec_client import _unrealized_totals

#: Measured off the live paper account, 2026-08-27.
_ALPACA = [
    {"symbol": "AEM", "unrealized_pl": "-31.05", "unrealized_intraday_pl": "-1.26"},
    {"symbol": "DELL", "unrealized_pl": "412.11", "unrealized_intraday_pl": "18.40"},
    {"symbol": "MRVL", "unrealized_pl": "1870.00", "unrealized_intraday_pl": "-50.14"},
]

#: What an IBKR-shaped payload looks like here: a standing figure and NO intraday key at all.
_IBKR = [
    {"symbol": "AEM.XNYS", "unrealized_pl": "-31.05"},
    {"symbol": "AMGN.XNAS", "unrealized_pl": "412.11"},
]


def test_the_fixture_is_shaped_like_the_BROKER_not_like_us():
    # Alpaca sends these as STRINGS. A fixture using floats would let a `sum()` over raw values pass
    # while production raised or concatenated — the double-cannot-represent-production trap.
    assert all(isinstance(p["unrealized_pl"], str) for p in _ALPACA)
    # And the two quantities must DIFFER, or no test below can tell standing from intraday.
    assert _ALPACA[0]["unrealized_pl"] != _ALPACA[0]["unrealized_intraday_pl"]


def test_standing_and_intraday_are_summed_separately():
    got = _unrealized_totals(_ALPACA)
    # -31.05 + 412.11 + 1870.00 = 2,251.06 — exactly the "standing" figure the panel printed.
    assert got["unrealized_standing_total"] == pytest.approx(2251.06, abs=0.01)
    # -1.26 + 18.40 - 50.14 = -33.00. The live panel read NET - 1D = -$33.24 on this same book.
    assert got["unrealized_intraday_total"] == pytest.approx(-33.00, abs=0.01)


def test_a_venue_WITHOUT_the_intraday_field_reports_None_not_zero():
    """THE IBKR CASE. Zero asserts "the mark did not move"; None says "not reported". Rendering the
    first would put a confident wrong number in the hero slot on every IBKR tenant."""
    got = _unrealized_totals(_IBKR)
    assert got["unrealized_intraday_total"] is None
    # ...and the standing total still works, or the venue-neutral half is broken too.
    assert got["unrealized_standing_total"] == pytest.approx(381.06, abs=0.01)


def test_ONE_missing_position_makes_the_WHOLE_total_unknown():
    """A partial sum is the quiet wrong answer: it reports part of the book as all of it."""
    mixed = [*_ALPACA, {"symbol": "NEW", "unrealized_pl": "10.00"}]  # no intraday on this one
    got = _unrealized_totals(mixed)
    assert got["unrealized_intraday_total"] is None, "a partial book was summed as if whole"
    assert got["unrealized_standing_total"] == pytest.approx(2261.06, abs=0.01)


@pytest.mark.parametrize("poison", ["NaN", "Infinity", "-Infinity"])
def test_a_NON_FINITE_value_makes_the_total_unknown(poison):
    """`float("nan")` survives every comparison written for numbers — the family that disarmed the
    daily-loss halt in kumo-trading-strategies 43c6d3e, and that reached this repo again in #588."""
    got = _unrealized_totals([{"symbol": "X", "unrealized_pl": poison, "unrealized_intraday_pl": "1"}])
    assert got["unrealized_standing_total"] is None


def test_unparseable_values_do_not_raise_into_the_drift_cycle():
    got = _unrealized_totals([{"symbol": "X", "unrealized_pl": "abc", "unrealized_intraday_pl": None}])
    assert got == {"unrealized_standing_total": None, "unrealized_intraday_total": None}


def test_an_EMPTY_book_is_zero_because_nothing_held_is_a_fact():
    assert _unrealized_totals([]) == {
        "unrealized_standing_total": 0.0,
        "unrealized_intraday_total": 0.0,
    }


def test_a_FAILED_position_fetch_clears_the_totals_rather_than_leaving_them_stale():
    """THE SEAM, not the summer. `_report_reconcile_drift` is retried every 3s by a caller that
    swallows its exception, so an attribute left untouched on failure keeps the LAST good unrealized
    figure on the account frame indefinitely — a number that looks live while the book moves under
    it. Unknown is the honest state; stale is the silent wrong answer.

    Drives the real coroutine and asserts the attribute, because a test on `_unrealized_totals`
    alone cannot see which value survives an error path.
    """
    import asyncio
    import inspect

    from api.providers.alpaca import exec_client as mod

    src = inspect.getsource(mod.AlpacaExecutionClient._report_reconcile_drift)
    fetch = src.index("list_positions()")
    assert "try:" in src[:fetch], "the position fetch is not guarded, so a failure leaves a stale total"

    class _Boom:
        async def list_positions(self):
            raise RuntimeError("alpaca 503")

    class _Stub:
        _http = _Boom()

        def __init__(self):
            self._unrealized_totals = {"unrealized_standing_total": 999.0,
                                       "unrealized_intraday_total": 42.0}

    stub = _Stub()
    with pytest.raises(RuntimeError):
        asyncio.run(mod.AlpacaExecutionClient._report_reconcile_drift(stub))
    assert stub._unrealized_totals == {"unrealized_standing_total": None,
                                       "unrealized_intraday_total": None}, (
        "a failed fetch left the previous totals in place; the panel would show them as current"
    )
