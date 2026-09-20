"""Tests for the order-action registry (#50). Each descriptor builds a real Nautilus order from a real
OrderFactory — asserting the constructed object, never submitting. Behaviour parity with the old hardcoded
_build_order is also guarded by the existing #33/#34 command tests."""

from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.objects import Price, Quantity

from api.order_actions import OrderBuildContext, action_ids, get_action, register_action
from api.order_actions.registry import OrderActionDescriptor

_IID = InstrumentId(Symbol("AAPL"), Venue("XNAS"))


def _ctx(coid="O-1", tags=None):
    factory = OrderFactory(TraderId("T-1"), StrategyId("MANUAL-001"), LiveClock())
    return OrderBuildContext(
        factory, _IID, OrderSide.BUY, Quantity.from_int(10), TimeInForce.DAY,
        ClientOrderId(coid) if coid else None, tags,
    )


def test_registry_has_the_standard_actions():
    assert set(action_ids()) >= {"market", "limit", "stop_market", "stop_limit", "bracket"}


def test_unknown_action_raises():
    with pytest.raises(ValueError, match="unknown order action"):
        get_action("no_such_action")


def test_register_duplicate_raises():
    with pytest.raises(ValueError, match="already registered"):
        register_action(OrderActionDescriptor("market", "both", {"type": "object"}, lambda c, p: None))


def test_market_builds_market_order():
    o = get_action("market").build(_ctx(), {})
    assert o.order_type == OrderType.MARKET
    assert o.side == OrderSide.BUY and o.quantity == Quantity.from_int(10)
    assert o.time_in_force == TimeInForce.DAY and o.client_order_id == ClientOrderId("O-1")


def test_limit_builds_limit_with_price():
    o = get_action("limit").build(_ctx(), {"price": "190.00"})
    assert o.order_type == OrderType.LIMIT and o.price == Price.from_str("190.00")


def test_stop_market_builds_with_trigger():
    o = get_action("stop_market").build(_ctx(), {"trigger_price": "185.50"})
    assert o.order_type == OrderType.STOP_MARKET and o.trigger_price == Price.from_str("185.50")


def test_stop_market_defaults_to_NOT_reduce_only():
    """The default is what every pre-#872 caller gets, and it must be the one they already had — a
    silent flip to reduce-only would make an operator's opening stop unfillable at the venue."""
    o = get_action("stop_market").build(_ctx(), {"trigger_price": "185.50"})
    assert o.is_reduce_only is False


def test_stop_market_carries_reduce_only_when_asked(monkeypatch):
    """#872's entry floors are STOP_MARKETs, and a protective stop must never OPEN a position: if it
    rests while the position closes by another route — a flatten, a PEAK exit, a reconciliation —
    firing does not close anything, it opens the opposite side. That is FIG in #252 and the phantom
    HSBC short. The flag had no way through this descriptor at all before."""
    o = get_action("stop_market").build(_ctx(), {"trigger_price": "185.50", "reduce_only": True})
    assert o.is_reduce_only is True
    assert o.order_type == OrderType.STOP_MARKET


def test_stop_limit_builds_with_price_and_trigger():
    o = get_action("stop_limit").build(_ctx(), {"price": "184.00", "trigger_price": "185.00"})
    assert o.order_type == OrderType.STOP_LIMIT
    assert o.price == Price.from_str("184.00") and o.trigger_price == Price.from_str("185.00")


def test_bracket_builds_order_list_of_three():
    ol = get_action("bracket").build(
        _ctx(),
        {
            "entry_order_type": OrderType.MARKET,
            "price": None,
            "stop_trigger": "180.00",
            "target_price": "200.00",
            "sl_client_order_id": ClientOrderId("O-SL"),
            "tp_client_order_id": ClientOrderId("O-TP"),
            "group": "G1",
        },
    )
    assert len(ol.orders) == 3
    types = {o.order_type for o in ol.orders}
    assert OrderType.MARKET in types and OrderType.STOP_MARKET in types and OrderType.LIMIT in types
    assert all("bracket:G1" in list(o.tags) for o in ol.orders)


def test_bracket_builds_with_limit_entry_not_just_market():
    """All other bracket tests use a MARKET entry — #47 (STOP-AND-REENTER's re-entry leg) is the first
    caller using a LIMIT entry, and a prior attempt to build a STOP-ONLY variant of this action (tp_price=
    None) looked fine by `inspect.signature` but actually raised when CALLED against the real installed
    Nautilus build (codex review, round 3) — this locks in the LIMIT-entry combination actually works, not
    just that the signature allows it."""
    ol = get_action("bracket").build(
        _ctx(),
        {
            "entry_order_type": OrderType.LIMIT,
            "price": "190.00",
            "stop_trigger": "180.00",
            "target_price": "220.00",
            "sl_client_order_id": ClientOrderId("O-SL"),
            "tp_client_order_id": ClientOrderId("O-TP"),
            "group": "G1",
        },
    )
    assert len(ol.orders) == 3
    entry = next(o for o in ol.orders if o.order_type == OrderType.LIMIT and o.price == Price.from_str("190.00"))
    assert entry.client_order_id == ClientOrderId("O-1")


def test_validate_rejects_missing_required_param():
    with pytest.raises(ValidationError):
        get_action("limit").validate({})  # limit requires price
    with pytest.raises(ValidationError):
        get_action("stop_market").validate({})  # requires trigger_price
    # numeric-string prices are accepted (parity with the old shapes)
    get_action("limit").validate({"price": "190.00"})
    get_action("limit").validate({"price": 190.0})
