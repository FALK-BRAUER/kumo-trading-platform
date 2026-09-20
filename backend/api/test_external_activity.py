"""Tests for the external-activity classifier (#79). Pure/offline: fake cache objects exercising the
owned-vs-external split + origin classification. The classifier reads only native attributes, so light fakes
suffice; the two-strategy integration path is covered where a real engine is available."""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.identifiers import StrategyId

from api.external_activity import classify_external

_OWNED = {"MANUAL-001"}


def _pos(strategy: str, *, instrument="AAPL.XNAS", side="LONG", qty=100, opening_order_id=None):
    return SimpleNamespace(
        strategy_id=StrategyId(strategy),
        account_id="ACC",
        instrument_id=instrument,
        side=SimpleNamespace(name=side),
        quantity=qty,
        realized_pnl="0.00 USD",
        ts_last=5,
        opening_order_id=opening_order_id,
    )


def _order(strategy: str, *, tags=None, instrument="MSFT.XNAS", side="BUY"):
    return SimpleNamespace(
        strategy_id=StrategyId(strategy),
        account_id="ACC",
        instrument_id=instrument,
        side=SimpleNamespace(name=side),
        quantity=10,
        client_order_id=SimpleNamespace(value="O-EXT-1"),
        status=SimpleNamespace(name="ACCEPTED"),
        tags=tags,
        ts_last=7,
    )


class _Cache:
    def __init__(self, positions, orders, orders_by_id=None):
        self._p = positions
        self._o = orders
        self._by_id = orders_by_id or {}

    def positions_open(self):
        return self._p

    def orders_open(self):
        return self._o

    def order(self, order_id):
        return self._by_id.get(order_id)


def test_owned_strategy_is_not_external():
    cache = _Cache([_pos("MANUAL-001")], [_order("MANUAL-001")])
    assert classify_external(cache, _OWNED, "ALPACA") == []


def _oid(v: str):
    return SimpleNamespace(value=v)


def test_external_venue_position_and_order_quarantined():
    # VENUE position: opening order exists, no reconciliation tag.
    opening = _order("EXTERNAL", tags=None)
    cache = _Cache([_pos("EXTERNAL", opening_order_id="OP-1")], [_order("EXTERNAL")], {"OP-1": opening})
    ext = classify_external(cache, _OWNED, "ALPACA")
    assert len(ext) == 2
    pos = next(e for e in ext if e.source == "POSITION")
    order = next(e for e in ext if e.source == "ORDER")
    assert pos.origin == "VENUE" and pos.status == "QUARANTINED" and pos.strategy_id == "EXTERNAL"
    assert pos.client_id == "ALPACA" and pos.instrument_id == "AAPL.XNAS"
    assert order.origin == "VENUE" and order.client_order_id == "O-EXT-1"


def test_reconciliation_origin_from_order_tags():
    # ORDER: reconciliation tag.
    cache = _Cache([], [_order("EXTERNAL", tags=["RECONCILIATION"])])
    assert classify_external(cache, _OWNED, "ALPACA")[0].origin == "RECONCILIATION"


def test_reconciliation_position_origin_from_opening_order():
    opening = _order("EXTERNAL", tags=["RECONCILIATION"])
    cache = _Cache([_pos("EXTERNAL", opening_order_id="OP-R")], [], {"OP-R": opening})
    assert classify_external(cache, _OWNED, "ALPACA")[0].origin == "RECONCILIATION"


def test_external_position_unknown_when_opening_order_absent():
    # Can't distinguish VENUE vs RECONCILIATION without the opening order → UNKNOWN, not a guessed VENUE.
    cache = _Cache([_pos("EXTERNAL", opening_order_id=None)], [])
    assert classify_external(cache, _OWNED, "ALPACA")[0].origin == "UNKNOWN"


def test_foreign_strategy_is_external_but_not_reserved():
    # A non-owned, non-EXTERNAL strategy (e.g. another cockpit strategy) → FOREIGN, still quarantined.
    cache = _Cache([_pos("MOMENTUM-002")], [])
    ext = classify_external(cache, _OWNED, "ALPACA")
    assert len(ext) == 1
    assert ext[0].origin == "FOREIGN" and ext[0].strategy_id == "MOMENTUM-002"


def test_mixed_owned_and_external_only_surfaces_external():
    cache = _Cache([_pos("MANUAL-001"), _pos("EXTERNAL", instrument="NVDA.XNAS")], [])
    ext = classify_external(cache, _OWNED, "ALPACA")
    assert [e.instrument_id for e in ext] == ["NVDA.XNAS"]  # MANUAL excluded
