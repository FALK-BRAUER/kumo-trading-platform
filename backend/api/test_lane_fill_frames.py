"""A lane's fills must publish `fill` frames, not only MANUAL's (#651 item 5).

`on_order_filled` is Nautilus's PER-STRATEGY callback: `Strategy.on_start` subscribes
`events.order.{self.id}`, so it fires for MANUAL's own orders only — the operator's clicks. Every
sibling lane's events arrive through the `events.order.*` wildcard into `_handle_order_event`, which
published an `order` frame but no `fill` frame. Chart fill markers therefore showed only operator
clicks: a lane's whole rotation looked like it never traded.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.engine_node import UiFeedStrategy


def _real_filled(strategy_id: str, side: str = "BUY", qty: int = 7, px: float = 101.5):
    """A REAL `OrderFilled` via Nautilus's own stubs — production gates on `isinstance`, and a
    hand-rolled double is exactly the drifted-double shape this repo keeps paying for."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    instrument = TestInstrumentProvider.equity(symbol="PLTR")
    order = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
        quantity=Quantity.from_int(qty),
    )
    return TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        strategy_id=StrategyId(strategy_id),
        last_qty=Quantity.from_int(qty),
        last_px=Price.from_str(str(px)),
    )


class _Probe:
    """The REAL wildcard entry point with only the bus replaced."""

    id = "MANUAL-001"

    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.handled: list[str] = []
        # What `_publish_fill` reaches for on EVERY fill since #807 item 4: the cache (is the filled
        # order terminal?), the observation registry, and the terminal-fill registry. A probe without
        # them is a double that cannot represent production.
        import types

        from api.book_truth import InferredFills, TerminalFills
        from api.observation import Observations

        # `position()` like the real cache: typed, and None for an id it does not hold (#901).
        def _position(pid):
            from nautilus_trader.model.identifiers import PositionId
            if not isinstance(pid, PositionId):
                raise TypeError("expected PositionId")

        self.cache = types.SimpleNamespace(order=lambda coid: None, positions_open=list,
                                           position=_position)
        self._observations = Observations()
        self._observations.declare("inferred_fills", "fills_on_terminal_orders")
        self._inferred_fills = InferredFills()
        self._terminal_fills = TerminalFills()
        self._safe_now = lambda: 0

    _on_any_order_event = UiFeedStrategy._on_any_order_event
    _publish_fill = UiFeedStrategy._publish_fill

    def _publish(self, topic, frame):
        self.published.append((topic, frame))

    def _handle_order_event(self, event):
        self.handled.append(str(getattr(event, "strategy_id", "")))


def test_a_sibling_lanes_fill_publishes_a_fill_frame():
    """Drive `_on_any_order_event` — the seam every automated lane's events actually cross — with a
    real OrderFilled from MOMENTUM-002. On main it published an `order` frame and no `fill` frame,
    so the chart marked only operator clicks."""
    p = _Probe()
    event = _real_filled("MOMENTUM-002", side="BUY", qty=7, px=101.5)
    p._on_any_order_event(event)

    assert p.handled == ["MOMENTUM-002"], "fixture property: the event reached the wildcard handler"
    fills = [f for t, f in p.published if t == "fill"]
    assert fills, (
        "a MOMENTUM-002 fill crossed the wildcard seam and published NO fill frame — the lane's "
        "rotation is invisible on the chart"
    )
    frame = fills[0]
    assert frame["strategy_id"] == "MOMENTUM-002"
    assert frame["side"] == "BUY" and frame["quantity"] == 7.0 and frame["price"] == 101.5
    assert frame["instrument_id"].startswith("PLTR")


def test_a_sibling_lanes_non_fill_event_publishes_no_fill_frame():
    """Accepted/canceled/rejected still cross the same seam — only a FILL is a fill marker."""
    p = _Probe()

    class NotAFill(SimpleNamespace):
        pass

    p._on_any_order_event(NotAFill(strategy_id="MOMENTUM-002", client_order_id="x"))
    assert [t for t, _ in p.published if t == "fill"] == []
    assert p.handled == ["MOMENTUM-002"], "the order-frame path must still run"


def test_manuals_own_fill_is_not_published_twice():
    """MANUAL's fills arrive on BOTH subscriptions (per-strategy + wildcard). The native
    `on_order_filled` publishes the frame; the wildcard must stay out of it."""
    p = _Probe()
    p._on_any_order_event(_real_filled("MANUAL-001"))
    assert p.published == [] and p.handled == [], (
        "the wildcard handled MANUAL's own fill — every operator click would chart two markers"
    )


def test_both_fill_paths_are_one_function():
    """`on_order_filled` (MANUAL, native) and the wildcard path must build the frame through ONE
    `_publish_fill` — two derivations of one frame will drift (CLAUDE.md)."""
    import inspect

    native = inspect.getsource(UiFeedStrategy.on_order_filled)
    wildcard = inspect.getsource(UiFeedStrategy._on_any_order_event)
    assert "_publish_fill" in native, "on_order_filled no longer routes through _publish_fill"
    assert "_publish_fill" in wildcard, "the wildcard path no longer routes through _publish_fill"
