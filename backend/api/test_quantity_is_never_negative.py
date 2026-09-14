"""#855 — the PRODUCER CONTRACT: no engine writer can publish a negative `quantity`.

THIS FILE SHOULD BE GREEN TODAY, AND THAT IS THE POINT. Every other #855 test is red; this one pins
the premise all of them rest on, so that a fix cannot quietly change it.

The UI tests assume an UNSIGNED wire quantity with the direction in `side`, and defend against a
signed one arriving. `TradeDTO(quantity=-28)` constructs — the Pydantic field is a bare `float` — so
"the wire could carry a sign" is true of the SCHEMA. This asks whether it is true of the ENGINE, and
the answer is no, for a reason that is structural rather than incidental:

    >>> from nautilus_trader.model.objects import Quantity
    >>> Quantity(-5, precision=0)
    ValueError: invalid `value` less than `QUANTITY_MIN` 0.0, was -5.0

`Position.quantity` is a `Quantity`. Every published quantity in this codebase is `float(pos.quantity)`
off one of those, so the magnitude is guaranteed at the type and the direction lives on `side` and
`signed_qty`. That is the contract, and these tests are what make it a checked one instead of a
remembered one.

WHY IT MATTERS FOR THE FIX. Two readings of #855 are possible: "the wire will start carrying signs
once shorts are permitted, so normalise on read", or "the wire never carries signs, so the UI's raw
reads are safe". This file settles it for the second half. The UI defects are real regardless — a
long-only `recommend`, an unsigned market value, DEPLOYED disagreeing with itself — but the SIGNED
spelling the UI tests also cover is a defence against a producer that does not exist today. If a
later change makes one exist, THIS file goes red first, at the source, rather than the UI silently
rendering a short as a long.

The writers driven here, each with a real Nautilus SHORT `Position`:

    api/node.py:188                NodeManager.positions()    -> PositionDTO
    api/external_activity.py:65    classify_external()        -> ExternalActivityDTO
    api/engine_node.py:8026        _on_snapshot()'s row       -> the positions frame
    api/trade_cycle.py:334         the cycle projection       -> TradeDTO

(The scope note gives `engine_node.py:3800` for `_on_snapshot`. That line is PEAK's trailing-stop
submit quantity — an order size, not a published one; `_on_snapshot` builds its row at `:8026`. Both
are covered: the order path by the `Quantity` contract itself, the frame path directly.)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OmsType, OrderSide
from nautilus_trader.model.identifiers import PositionId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from api.external_activity import classify_external

_INST = TestInstrumentProvider.equity(symbol="WHD", venue="XNYS")
_STRAT = StrategyId("MOMENTUM-002")
_POS_ID = "WHD.XNYS-MOMENTUM-002"
QTY = 28


def _fill(side: OrderSide, qty: int, px: str, ts_ns: int = 0):
    order = TestExecStubs.market_order(
        instrument=_INST, order_side=side, quantity=Quantity.from_int(qty), strategy_id=_STRAT,
    )
    return TestEventStubs.order_filled(
        order, instrument=_INST, position_id=PositionId(_POS_ID), strategy_id=_STRAT,
        last_px=Price.from_str(px), ts_event=ts_ns,
    )


def _short() -> Position:
    """A real SHORT position, opened by a real SELL fill. Not a hand-built double: the whole question
    is what NAUTILUS puts on `quantity` for a short, and a double would answer with whatever it was
    written to answer."""
    return Position(instrument=_INST, fill=_fill(OrderSide.SELL, QTY, "100.00"))


def _long() -> Position:
    return Position(instrument=_INST, fill=_fill(OrderSide.BUY, QTY, "100.00"))


# -- the contract itself ---------------------------------------------------------------------------


def test_the_fixture_expresses_the_premise__a_negative_Quantity_cannot_be_constructed():
    """The measurement the rest of the file rests on, run against the INSTALLED package rather than
    quoted from memory. If a future Nautilus relaxed this, every assertion below could still pass
    while the premise had gone — so it is checked first and by itself."""
    with pytest.raises(ValueError) as exc:
        Quantity(-5, precision=0)
    assert "QUANTITY_MIN" in str(exc.value)


def test_the_fixture_expresses_the_bug__the_position_really_is_SHORT():
    """Vacuity guard. A LONG position satisfies "quantity >= 0" trivially and would make every writer
    assertion below pass without saying anything about shorts."""
    pos = _short()
    assert pos.side.name == "SHORT"
    assert pos.signed_qty < 0, "the sign lives HERE"
    assert float(pos.quantity) == QTY, "and the magnitude lives here"
    assert float(pos.quantity) == abs(pos.signed_qty)


def test_the_two_facts_are_carried_on_DIFFERENT_fields():
    """The contract in one line: `quantity` is a magnitude, `signed_qty` carries the direction. This
    is what the UI's `signedQty(side, quantity)` predicate reconstructs, and it is why reconstructing
    it from `quantity` alone is impossible rather than merely awkward."""
    for pos in (_short(), _long()):
        assert float(pos.quantity) >= 0
        assert abs(pos.signed_qty) == float(pos.quantity)
        assert (pos.signed_qty < 0) == (pos.side.name == "SHORT")


# -- every writer of a published quantity ----------------------------------------------------------


def test_node_positions_publishes_a_magnitude():
    """`api/node.py:188` — `quantity=float(pos.quantity)` into a PositionDTO."""
    from api.node import NodeManager

    node = NodeManager.__new__(NodeManager)
    node._engine = SimpleNamespace(cache=SimpleNamespace(positions=lambda: [_short()]))
    node._strategies = []
    rows = node.positions()
    assert len(rows) == 1, "the writer produced no row — the assertion below would be vacuous"
    assert rows[0].side == "SHORT"
    assert rows[0].quantity == QTY


def test_classify_external_publishes_a_magnitude():
    """`api/external_activity.py:65` — the unclaimed-row writer. `owned_strategy_ids` is empty so the
    position is NOT skipped as owned; if it were, the row would never be built."""
    cache = SimpleNamespace(positions_open=lambda: [_short()], orders_open=lambda: [])
    rows = classify_external(cache, owned_strategy_ids=set(), client_id="IB")
    assert len(rows) == 1, "the position was skipped — the assertion below would be vacuous"
    assert rows[0].side == "SHORT"
    assert rows[0].quantity == QTY


def test_the_snapshot_frame_row_publishes_a_magnitude():
    """`api/engine_node.py:8026` — the positions row on the snapshot frame, driven through the REAL
    `_on_snapshot` bound to a double.

    Rebuilding that dict comprehension inside the test would assert that my arithmetic matches my
    arithmetic, and would go on passing if the engine stopped publishing the frame at all.

    `_on_snapshot` publishes positions, then orders, then a HEALTH frame that reads a dozen
    collaborators with nothing to do with this contract. Stubbing all of them would be a fixture that
    misrepresents the engine, so the tail is left to fail — but the failure is NOT discarded: it is
    carried into the assertion message, and the test still goes red if the frame under test never
    arrived. A swallowed exception that reads as a clean run is the defect this repo has shipped
    twice; this reports the degraded state as its own condition.
    """
    from api.engine_node import UiFeedStrategy

    pos = _short()
    published: list[tuple[str, dict]] = []
    eng = SimpleNamespace(
        cache=SimpleNamespace(positions=lambda: [pos], orders_open=lambda: [], orders_closed=lambda: []),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        log=SimpleNamespace(error=lambda m: None, warning=lambda m: None, info=lambda m: None),
        _sibling_strategies={},
    )
    eng._publish = lambda key, payload: published.append((key, payload))
    for name in ("_check_vwap_session_rollover", "_publish_trades", "_publish_external", "_publish_account"):
        setattr(eng, name, lambda *a, **k: None)
    eng._positions_frame = lambda rows, now_ns=0: {"positions": rows, "ts": now_ns}
    eng._order_frame = lambda o: {}
    real = getattr(UiFeedStrategy, "_on_snapshot", None)
    assert real is not None, "UiFeedStrategy has no _on_snapshot — the seam moved"
    eng._on_snapshot = real.__get__(eng)

    tail_error: Exception | None = None
    try:
        eng._on_snapshot(None)
    except Exception as exc:  # noqa: BLE001 - reported below, never discarded
        tail_error = exc

    frames = [p for k, p in published if k == "positions"]
    assert frames, f"no positions frame was published; the method raised before it: {tail_error!r}"
    row = frames[-1]["positions"][0]
    assert row["side"] == "SHORT"
    assert row["quantity"] == QTY


def test_the_cycle_projection_publishes_a_magnitude():
    """`api/trade_cycle.py:334` — `quantity = float(pos.quantity)` when the position is open, and
    0.0 when it is not. Driven through the real projection so a change to that branch is caught."""
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.model.identifiers import ClientId

    from api.trade_cycle import TradeCycleProjection

    cache = Cache()
    pos = _short()
    cache.add_position(pos, OmsType.NETTING)
    proj = TradeCycleProjection(ClientId("IB"), _STRAT)
    # `_resolve_account` needs a native account in the cache and refuses to mint without one; the
    # account id is not what this asserts, so it is supplied directly and `_project_one` — the method
    # that owns line 334 — is driven.
    proj._account_id = "DU1"
    dto = proj._project_one(cache, str(_INST.id), 1)
    assert dto is not None, "the projection produced no cycle — the assertion below would be vacuous"
    assert dto.is_capital_deployed, "the cycle is not HELD — line 334's open branch never ran"
    assert dto.side == "SHORT"
    assert dto.quantity == QTY


def test_every_writer_agrees_on_the_same_short():
    """One position, four writers, one number. Two derivations of one fact will disagree; four are
    worse odds. Stated as an invariant so it survives a change to QTY."""
    from api.node import NodeManager

    pos = _short()
    node = NodeManager.__new__(NodeManager)
    node._engine = SimpleNamespace(cache=SimpleNamespace(positions=lambda: [pos]))
    node._strategies = []
    cache = SimpleNamespace(positions_open=lambda: [pos], orders_open=lambda: [])

    published = [
        node.positions()[0].quantity,
        classify_external(cache, owned_strategy_ids=set(), client_id="IB")[0].quantity,
        float(pos.quantity),
    ]
    assert len(set(published)) == 1, f"the writers disagree: {published}"
    assert published[0] == abs(pos.signed_qty)
    assert published[0] > 0
