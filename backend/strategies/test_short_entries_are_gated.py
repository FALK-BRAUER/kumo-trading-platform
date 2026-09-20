"""A SHORT lane's ENTRY is a SELL, and the submit seam used to assume otherwise (platform issue 858).

`execute_plan` was written for long lanes and hardcoded that reading:

    if side == "BUY":   ... may_submit(is_entry=True) ...     <- the ONLY budget gate
    if side == "SELL":  deployed -= qty * px                  <- "exits fund entries"

On CRSISHORT-006 that is exactly inverted: the SELL opens the position. Wired to the seam
unchanged, every short entry would have gone out UNGATED and been credited as FREEING capital —
the budget gate is the only thing between a lane and its sleeve, and a short lane would have had
none of it while every surface read normal.

That is why the lane shipped SHADOW-only rather than wired up. This file is the guard that lets it
be wired up: `position_side` decides which venue side is an entry, and every gate keys on the
MEANING rather than on the venue word.

THE DOUBLES ARE THE LONG LANE'S, deliberately — `_Broker` refuses what the installed broker
refuses and `_Order` is pinned to the installed dataclass by the sibling file. A short-specific
double could not have caught this, because the defect is that the two lanes share one seam.
"""
from __future__ import annotations

import asyncio

import pytest

from strategies.delta_execution import execute_plan
from strategies.test_smhgld_delta_execution import _Broker, _Claims, _Order

#: This lane's own names and prices — the long fixture's PX has neither.
PX = {"TSLQ": 11.50, "AAOI": 27.25, "GLD": 219.00}

SID = "CRSISHORT-006"
SESSION, SLOT = "2026-09-14", "open+175m"


class _Rows:
    def __init__(self):
        self.rows = []

    async def __call__(self, kind, code, message, **kw):
        self.rows.append((kind, code, message, kw))

    def codes(self):
        return [c for _k, c, _m, _kw in self.rows]


def _Sleeve(target=10_000.0, actual=10_000.0):
    """THE REAL SLEEVE TYPE, not a stand-in. A hand-rolled double here lacked `deployable()` and
    `may_submit` raised AttributeError instead of gating — a double that cannot represent
    production would have hidden whether the gate ran at all."""
    from api.budget import Sleeve

    return Sleeve(strategy_id=SID, target=target, actual=actual)


def _run(orders, *, position_side, sleeve=None, deployed=0.0, limits=None, broker=None, rows=None):
    broker = broker or _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    rows = rows if rows is not None else _Rows()
    sent, refused, entered, exited = asyncio.run(execute_plan(
        orders, broker=broker, write_claim=_Claims(), row=rows, sleeve=sleeve or _Sleeve(),
        deployed=deployed, budget_code=None, prices=dict(PX), session=SESSION, slot=SLOT,
        strategy_id=SID, position_side=position_side, limits=limits))
    return broker, rows, sent, refused, entered, exited


# ------------------------------------------------------------------ fixture properties ---------
def test_the_fixture_sleeve_is_small_enough_that_a_real_gate_MUST_refuse():
    """If the sleeve could fund the order, 'not refused' would prove nothing about the gate."""
    from api.budget_gate import may_submit
    gate = may_submit(_Sleeve(), is_entry=True, notional=9_999_999.0, currently_deployed=0.0)
    assert not gate.allowed, "fixture: the gate must refuse an oversized entry, or nothing below binds"


# ------------------------------------------------------------------ the defect ------------------
def test_a_SHORT_entry_is_BUDGET_GATED_even_though_it_is_a_SELL():
    """THE BUG. A short entry is a SELL; the seam gated only BUYs, so this went out unchecked."""
    huge = _Order("TSLQ", -100_000.0, -100_000.0, 0.0, "short entry beyond the sleeve")
    broker, rows, sent, refused, entered, _exited = _run([huge], position_side="SHORT")
    assert refused.get("TSLQ") == "budget_refused", (
        f"a short ENTRY escaped the budget gate: refused={refused} sent={[s['symbol'] for s in sent]}")
    assert broker.submitted == [], "nothing may reach the venue when the gate refuses"
    assert "budget_refused" in rows.codes()
    assert entered == []


def test_the_SAME_order_on_a_LONG_lane_is_an_EXIT_and_is_NOT_gated():
    """The other half of the same predicate: a SELL on a long lane closes, and closing is never
    budget-refused. If both readings gated, `position_side` would be doing nothing."""
    same = _Order("TSLQ", -100_000.0, -100_000.0, 0.0, "a long lane's exit")
    broker, _rows, _sent, refused, _entered, exited = _run([same], position_side="LONG")
    assert refused == {}, f"a LONG lane's SELL is an exit and must not be budget-gated: {refused}"
    assert exited == ["TSLQ"] and [r.side for r in broker.submitted] == ["SELL"]


def test_a_SHORT_COVER_frees_room_and_a_refused_COVER_WITHHOLDS_the_entries_after_it():
    """`exits fund entries` keyed on the MEANING. On a short lane the BUY is the exit, so a refused
    BUY must withhold the SELLs that follow it — the mirror of the long lane's rule."""
    cover = _Order("AAOI", 50.0, 0.0, -50.0, "cover")
    entry = _Order("TSLQ", -10.0, -10.0, 0.0, "short entry")
    broker = _Broker(held={}, prices=dict(PX), fail={"AAOI": "venue refused"},
                     buys_need_sells_first=False)
    _b, rows, _sent, refused, entered, _exited = _run(
        [cover, entry], position_side="SHORT", broker=broker)
    assert refused.get("AAOI") == "submit_refused"
    assert refused.get("TSLQ") == "buy_withheld", (
        f"the entry after a failed COVER must be withheld on a short lane: {refused}")
    assert entered == []


def test_a_LIMIT_price_reaches_the_venue_and_absence_keeps_MARKET():
    """CRSISHORT decides a limit per name. SMHGLD passes none and must stay MARKET, byte for byte."""
    o = _Order("TSLQ", -5.0, -5.0, 0.0, "short entry")
    broker, _rows, _sent, refused, _entered, _exited = _run(
        [o], position_side="SHORT", sleeve=_Sleeve(1_000_000.0, 1_000_000.0),
        limits={"TSLQ": 12.34})
    assert refused == {}, refused
    assert [(r.side, r.limit_px, r.time_in_force) for r in broker.submitted] == [("SELL", 12.34, "DAY")]

    broker2, _r2, _s2, refused2, _e2, _x2 = _run(
        [_Order("GLD", 5.0, 219.0, 214.0, "add")], position_side="LONG",
        sleeve=_Sleeve(1_000_000.0, 1_000_000.0))
    assert refused2 == {}, refused2
    assert [(r.side, r.limit_px) for r in broker2.submitted] == [("BUY", None)], "MARKET unchanged"


def test_position_side_REFUSES_a_value_it_does_not_understand():
    """Never defaulted by accident on a lane that shorts: an unknown value raises rather than
    falling back to LONG, which is the reading that sends short entries ungated."""
    with pytest.raises(ValueError, match="position_side"):
        _run([_Order("TSLQ", -1.0, -1.0, 0.0, "x")], position_side="SHORTT")
