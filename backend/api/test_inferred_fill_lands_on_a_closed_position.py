"""`inferred_fills` named every FULL EXIT as unlanded (#901).

`_publish_fill` computed `landed` as "the lane holds an OPEN position on the instrument" — AFTER the
fill was applied. A fill that fully closes the position removes it from `positions_open()`, so every
full exit that arrives by inference was recorded `unlanded`, stamped with the lane that had just
correctly exited. Measured on paper 2026-09-10 19:40:13Z: BCTROT-004's DINO exit — `ExecEngine:
Generated inferred OrderFilled` → `BCTROT: <--[EVT] OrderFilled` 2 ms later → `PositionClosed
(DINO.XNYS-BCTROT-004)` — and `/health.inferred_fills` read `unlanded_strategy: BCTROT-004`.

On this adapter EVERY fill is inferred (`exec_client.py` has no fill-event path), so the "inferred"
half of the surface is constant-true and the whole signal rests on `unlanded` — which fired on every
routine correct exit. The #807 detector had NO signal on Alpaca: clean exits and the real mint were
indistinguishable, and a surface that fires on every exit is one nobody reads.

LANDED means the fill's position RESOLVES in the cache — open or CLOSED — under the lane the fill is
stamped with. Nautilus keeps closed positions (`Cache.position(position_id)` returns them;
`positions_closed()` exists), and the ExecEngine creates/updates the position BEFORE it publishes the
fill to strategies. The mint — a reduce-only fill stamped with a lane holding nothing on the instrument,
whose position id resolves to no position at all — stays `unlanded`.
"""
from __future__ import annotations

import types

from api.engine_node import UiFeedStrategy


def _reconciliation_fill(strategy_id: str, side: str, qty: int, position_id: str | None):
    """A REAL `OrderFilled` with `reconciliation=True` and the position id the ExecEngine would have
    assigned — via Nautilus's own event round-trip, not a hand-rolled double."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.events import OrderFilled
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    instrument = TestInstrumentProvider.equity(symbol="DINO")
    order = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
        quantity=Quantity.from_int(qty),
    )
    stub = TestEventStubs.order_filled(
        order=order, instrument=instrument, strategy_id=StrategyId(strategy_id),
        last_qty=Quantity.from_int(qty), last_px=Price.from_str("40.10"),
    )
    d = OrderFilled.to_dict(stub)
    d["reconciliation"] = True
    d["position_id"] = position_id
    return OrderFilled.from_dict(d)


class _Probe:
    """The real `_publish_fill` with a cache that can hold a CLOSED position for a lane — the state the
    old double could not represent (it had `positions_open` only).

    THE DOUBLE REJECTS WHAT PRODUCTION REJECTS (review, HIGH): `Cache.position` takes a `PositionId`
    and raises `TypeError` on a `str` or `None`. A double keyed by string would have passed a fix
    written `position(str(event.position_id))` that raises on the real cache — inside the observation
    wrapper, which absorbs it, so zero inferred-fill rows forever and the #807 surface goes dark.
    """

    id = "MANUAL-001"

    def __init__(self, positions: dict[str, tuple[str, bool, list]]):
        """`positions`: position_id -> (strategy_id, is_open, trade_ids the position carries)."""
        from nautilus_trader.model.identifiers import PositionId, StrategyId, TradeId

        from api.book_truth import InferredFills, TerminalFills
        from api.observation import Observations

        self._positions = {
            PositionId(pid): types.SimpleNamespace(
                strategy_id=StrategyId(sid), is_open=is_open, instrument_id=pid.split("-")[0],
                trade_ids=[TradeId(t) for t in trade_ids])
            for pid, (sid, is_open, trade_ids) in positions.items()
        }

        def _position(pid):
            if not isinstance(pid, PositionId):
                raise TypeError(f"Argument 'position_id' has incorrect type (expected PositionId, got {type(pid).__name__})")
            return self._positions.get(pid)

        self.cache = types.SimpleNamespace(
            order=lambda coid: None,
            positions_open=lambda: [p for p in self._positions.values() if p.is_open],
            position=_position,
        )
        self._observations = Observations()
        self._observations.declare("inferred_fills", "fills_on_terminal_orders")
        self._inferred_fills = InferredFills()
        self._terminal_fills = TerminalFills()
        self._safe_now = lambda: 0
        self.published: list = []

    _publish_fill = UiFeedStrategy._publish_fill

    def _publish(self, topic, frame):
        self.published.append((topic, frame))

    def clean(self) -> bool:
        """The inferred-fill closure ran and raised nothing (review, HIGH): every raise inside it is
        absorbed by `Observations.run`, so a test that only reads the rows cannot tell a landed fill
        from a crashed closure that recorded nothing."""
        return self._observations.summary()["failing"] == 0 and "inferred_fills" not in self._observations.summary()["never_ran_names"]


def test_FIXTURE_the_event_is_a_reconciliation_fill_and_the_cache_holds_the_position_CLOSED():
    """Fixture property first: `reconciliation=True` on a real event with the ExecEngine's position id;
    a cache in which that position exists, is CLOSED, absent from the open list, and carries the fill's
    own trade id (Nautilus adds it to `position.trade_ids` before publishing, `position.pyx:574`) — the
    state two milliseconds after a full exit. And the double REJECTS a string id like the real cache."""
    import pytest

    ev = _reconciliation_fill("BCTROT-004", "SELL", 23, "DINO.XNAS-BCTROT-004")
    assert ev.reconciliation is True and str(ev.position_id) == "DINO.XNAS-BCTROT-004"
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", False, [str(ev.trade_id)])})
    assert probe.cache.positions_open() == []
    pos = probe.cache.position(ev.position_id)
    assert pos is not None and not pos.is_open and ev.trade_id in pos.trade_ids
    with pytest.raises(TypeError):
        probe.cache.position(str(ev.position_id))
    with pytest.raises(TypeError):
        probe.cache.position(None)


def test_a_full_exit_that_CLOSES_the_lanes_position_LANDED():
    """The DINO shape. Counted as an inferred fill; NOT named unlanded; the closure ran clean."""
    ev = _reconciliation_fill("BCTROT-004", "SELL", 23, "DINO.XNAS-BCTROT-004")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", False, [str(ev.trade_id)])})

    probe._publish_fill(ev)

    assert probe.clean(), probe._observations.summary()
    rows = probe._inferred_fills.as_rows()
    assert rows and rows[0]["instrument_id"] == "DINO.XNAS" and rows[0]["count"] == 1
    assert rows[0].get("unlanded_strategy") is None, rows


def test_a_fill_on_a_still_OPEN_position_lands_as_before():
    ev = _reconciliation_fill("BCTROT-004", "BUY", 23, "DINO.XNAS-BCTROT-004")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", True, [str(ev.trade_id)])})
    probe._publish_fill(ev)
    assert probe.clean() and probe._inferred_fills.as_rows()[0].get("unlanded_strategy") is None


def test_the_MINT_is_still_named_a_fill_whose_position_id_resolves_to_NOTHING():
    """A reduce-only fill stamped MANUAL-001 on an instrument MANUAL-001 holds nothing of: the
    ExecEngine derives `DINO.XNAS-MANUAL-001`, no such position exists open or closed, the fill is
    applied to nothing and the position poll fabricates the difference. Unchanged: named."""
    ev = _reconciliation_fill("MANUAL-001", "SELL", 23, "DINO.XNAS-MANUAL-001")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", True, [])})   # the real holder is another lane

    probe._publish_fill(ev)

    assert probe.clean() and probe._inferred_fills.as_rows()[0].get("unlanded_strategy") == "MANUAL-001"


def test_the_MINTS_SIBLING_a_reduce_only_fill_against_an_already_CLOSED_position_is_unlanded():
    """#807 one step over (review, HIGH): the position id RESOLVES — to a position that was already
    closed — and `_reject_reduce_only_netting_position_open` (`engine.pyx:1702`) refuses to reopen it,
    applies the fill to nothing, and publishes it anyway. Existence under the lane is not enough; the
    fill must be ON the position: `event.trade_id in position.trade_ids`."""
    ev = _reconciliation_fill("BCTROT-004", "SELL", 23, "DINO.XNAS-BCTROT-004")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", False, ["some-earlier-fill"])})

    probe._publish_fill(ev)

    assert probe.clean() and probe._inferred_fills.as_rows()[0].get("unlanded_strategy") == "BCTROT-004"


def test_a_position_that_exists_under_ANOTHER_lane_does_not_land_this_lanes_fill():
    """Belt and braces on the predicate: the id resolves, but to a position stamped with a different
    lane (a corrupt id, or a double that ignores the lane). Not this lane's — unlanded."""
    ev = _reconciliation_fill("MANUAL-001", "SELL", 23, "DINO.XNAS-BCTROT-004")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", False, [str(ev.trade_id)])})
    probe._publish_fill(ev)
    assert probe.clean() and probe._inferred_fills.as_rows()[0].get("unlanded_strategy") == "MANUAL-001"


def test_a_fill_WITHOUT_a_position_id_is_unlanded_not_a_crashed_closure():
    """Production never publishes a fill without a position id (`_determine_position_id`,
    `engine.pyx:1452`, assigns before both publishes) — so this pins the GUARD, not a reachable state:
    the real cache raises `TypeError` on None, the observation wrapper would absorb it, and the row
    would silently never be written. Named, with the lane, and the closure ran clean."""
    ev = _reconciliation_fill("BCTROT-004", "SELL", 23, None)
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", True, [])})
    probe._publish_fill(ev)
    assert probe.clean(), probe._observations.summary()
    assert probe._inferred_fills.as_rows()[0].get("unlanded_strategy") == "BCTROT-004"


def test_two_fills_on_one_instrument_a_landed_one_does_NOT_clear_an_earlier_unlanded():
    """Sequence (review): `InferredFills.unlanded` is STICKY — a later fill that lands does not erase
    the record of one that could not (`test_book_truth` pins it). BIL read `count 3` for one
    two-share buy plus its sell-back; this pins the count and the stickiness together so the prose in
    `book_truth.py` cannot describe a "most recent" that the code does not implement."""
    first = _reconciliation_fill("MANUAL-001", "SELL", 23, "DINO.XNAS-MANUAL-001")
    later = _reconciliation_fill("BCTROT-004", "SELL", 23, "DINO.XNAS-BCTROT-004")
    probe = _Probe({"DINO.XNAS-BCTROT-004": ("BCTROT-004", False, [str(later.trade_id)])})
    probe._publish_fill(first)
    probe._publish_fill(later)
    row = probe._inferred_fills.as_rows()[0]
    assert row["count"] == 2 and row["unlanded_strategy"] == "MANUAL-001"


def test_the_predicate_no_longer_reads_the_OPEN_list():
    """The class sweep on #901 found this the only site asking about an EVENT from state AFTER the
    event. Pinned so it cannot come back: the inferred-fill closure resolves the fill's position id
    and does not iterate `positions_open()`."""
    import inspect

    src = inspect.getsource(UiFeedStrategy._publish_fill)
    closure = src[src.index("def _record_inferred_fill_against_its_lane"):src.index("self._observations.run(\"inferred_fills\"")]
    assert "positions_open()" not in closure, "landed is still derived from the OPEN list after the fill"
    assert ".position(" in closure and "trade_ids" in closure, "landed does not resolve the fill's position and trade id"
