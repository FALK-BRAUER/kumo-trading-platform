"""Phase 0 proof harness for internal position transfers (#80 spin-off).

Pins the mechanism BEFORE any production code: moving quantity between two strategies must be doable with
purely INTERNAL orders + fills, so the broker net never changes and no order reaches a venue.

Why orders and not bare fills: Nautilus rejects an `OrderFilled` whose `client_order_id` isn't in the cache
(execution/engine.pyx — "Cannot apply event to any order"). So each leg is a real internal order that is
cached, accepted, then filled. It is never submitted through an exec client, so nothing is routed anywhere.

CARRY_OVER pricing: both legs fill at the SOURCE position's `avg_px_open`, so no P&L is realized by the
transfer and the destination inherits the source's basis. Nautilus derives basis from fills
(model/position.pyx), so filling at the source average IS the carry-over.

RUN SEPARATELY, AND ALONE — marked `engine`, same as test_projection_replay. A second BacktestEngine in one
interpreter segfaults, so the two engine-marked files cannot share a process either:

    pytest api/test_transfer_harness.py -m engine
    pytest api/test_projection_replay.py -m engine

`pytest api/ -m engine` runs both in one process and WILL abort.
"""

from __future__ import annotations

import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderType,
    PositionSide,
)
from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderSubmitted
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.test_kit.providers import TestInstrumentProvider

_VENUE = Venue("XNAS")
_IID = InstrumentId.from_str("AAPL.XNAS")
_SOURCE = StrategyId("EXTERNAL")
_TARGET = StrategyId("MANUAL-001")
_ACCOUNT_CASH = 1_000_000

# Tag every internal leg so blotters, turnover and execution analytics can filter them out — a transfer is
# bookkeeping, not trading, and counting it as a fill would double every turnover number.
_TAG = "INTERNAL_TRANSFER"


@pytest.fixture(scope="module")
def engine() -> BacktestEngine:
    eng = BacktestEngine(
        config=BacktestEngineConfig(trader_id="TRANSFER-001", logging=LoggingConfig(log_level="ERROR"))
    )
    eng.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=USD,
        starting_balances=[Money(_ACCOUNT_CASH, USD)],
    )
    eng.add_instrument(TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS"))
    eng.run()  # materialises the venue account; no data, so nothing trades
    yield eng
    eng.dispose()


def _leg(engine, strategy_id, side, qty, px, transfer_id, leg, seq):
    """One internal leg: build the order, cache it, accept it, fill it. Never submitted to a venue.

    IDs are deterministic and derived from the transfer id, so replaying the same transfer is idempotent —
    Nautilus rejects a duplicate `trade_id`, which is the durable guard against double application.
    """
    instrument = engine.cache.instrument(_IID)
    trader_id = engine.kernel.trader_id
    # The backtest account is only created once the engine runs; we never run it here (running would route
    # orders to the simulated venue, which is precisely what a transfer must not do), so name it directly.
    account_id = AccountId(f"{_VENUE}-001")
    coid = ClientOrderId(f"{transfer_id}-{leg}")
    now = 1_785_000_000_000_000_000 + seq

    order = MarketOrder(
        trader_id=trader_id,
        strategy_id=strategy_id,
        instrument_id=_IID,
        client_order_id=coid,
        order_side=side,
        quantity=Quantity.from_int(qty),
        init_id=UUID4(),
        ts_init=now,
        tags=[_TAG, transfer_id, leg],
    )
    engine.cache.add_order(order, position_id=PositionId(f"{_IID}-{strategy_id}"))

    exec_engine = engine.kernel.exec_engine
    for event in (
        OrderSubmitted(
            trader_id=trader_id, strategy_id=strategy_id, instrument_id=_IID, client_order_id=coid,
            account_id=account_id, event_id=UUID4(), ts_event=now, ts_init=now,
        ),
        OrderAccepted(
            trader_id=trader_id, strategy_id=strategy_id, instrument_id=_IID, client_order_id=coid,
            venue_order_id=VenueOrderId(f"{transfer_id}-{leg}-V"), account_id=account_id,
            event_id=UUID4(), ts_event=now, ts_init=now,
        ),
        OrderFilled(
            trader_id=trader_id, strategy_id=strategy_id, instrument_id=_IID, client_order_id=coid,
            venue_order_id=VenueOrderId(f"{transfer_id}-{leg}-V"), account_id=account_id,
            trade_id=TradeId(f"{transfer_id}-{leg}-T"),
            position_id=PositionId(f"{_IID}-{strategy_id}"),
            order_side=side, order_type=OrderType.MARKET,
            last_qty=Quantity.from_int(qty), last_px=Price.from_str(f"{px:.2f}"),
            currency=instrument.quote_currency,
            commission=Money(0, USD),  # a book transfer costs nothing — never accrue commission
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
            event_id=UUID4(), ts_event=now, ts_init=now,
        ),
    ):
        exec_engine.process(event)
    return order


def _fresh(engine) -> None:
    """Clean slate that still has an account: reset() drops the venue account too, and the ExecEngine refuses
    a fill without one ("no account found"), so re-run the empty engine to re-materialise it."""
    engine.reset()
    engine.run()


def _seed(engine, strategy_id, qty, px):
    """Give a strategy an opening position, the same way — this stands in for whatever created it."""
    return _leg(engine, strategy_id, OrderSide.BUY, qty, px, f"SEED-{strategy_id}", "OPEN", 0)


def _pos_qty(engine, strategy_id) -> float:
    pos = engine.cache.position(PositionId(f"{_IID}-{strategy_id}"))
    return float(pos.quantity) if pos is not None and pos.is_open else 0.0


def _aggregate(engine) -> float:
    """Signed net across every strategy — this is what the broker sees, and it must not move."""
    total = 0.0
    for pos in engine.cache.positions_open():
        if pos.instrument_id != _IID:
            continue
        total += float(pos.quantity) * (1 if pos.side == PositionSide.LONG else -1)
    return total


@pytest.mark.engine
def test_partial_transfer_moves_quantity_without_changing_broker_net(engine):
    """The core claim: 100 of 190 moves EXTERNAL → MANUAL and the net the broker sees is untouched."""
    _fresh(engine)
    _seed(engine, _SOURCE, 190, 23.33)
    assert _aggregate(engine) == 190.0

    basis = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}")).avg_px_open

    _leg(engine, _SOURCE, OrderSide.SELL, 100, basis, "T1", "SOURCE", 1)
    _leg(engine, _TARGET, OrderSide.BUY, 100, basis, "T1", "DEST", 2)

    assert _pos_qty(engine, _SOURCE) == 90.0
    assert _pos_qty(engine, _TARGET) == 100.0
    assert _aggregate(engine) == 190.0  # unchanged — nothing reached a venue


@pytest.mark.engine
def test_carry_over_realizes_no_pnl_and_carries_the_basis(engine):
    """CARRY_OVER: filling both legs at the source average means no P&L is realized and the destination
    inherits the basis — the whole point of the mode."""
    _fresh(engine)
    _seed(engine, _SOURCE, 190, 23.33)
    basis = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}")).avg_px_open

    _leg(engine, _SOURCE, OrderSide.SELL, 100, basis, "T2", "SOURCE", 1)
    _leg(engine, _TARGET, OrderSide.BUY, 100, basis, "T2", "DEST", 2)

    source = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}"))
    target = engine.cache.position(PositionId(f"{_IID}-{_TARGET}"))

    assert float(source.realized_pnl) == 0.0      # nothing crystallized by the transfer
    assert target.avg_px_open == basis            # basis carried, not re-marked


@pytest.mark.engine
def test_full_transfer_closes_the_source_position(engine):
    _fresh(engine)
    _seed(engine, _SOURCE, 168, 106.63)
    basis = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}")).avg_px_open

    _leg(engine, _SOURCE, OrderSide.SELL, 168, basis, "T3", "SOURCE", 1)
    _leg(engine, _TARGET, OrderSide.BUY, 168, basis, "T3", "DEST", 2)

    assert _pos_qty(engine, _SOURCE) == 0.0       # source flat — it genuinely moved
    assert _pos_qty(engine, _TARGET) == 168.0
    assert _aggregate(engine) == 168.0


@pytest.mark.engine
def test_replaying_a_transfer_is_rejected_by_duplicate_trade_id(engine):
    """Deterministic ids make replay safe: the second application must not double the position."""
    _fresh(engine)
    _seed(engine, _SOURCE, 190, 23.33)
    basis = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}")).avg_px_open

    _leg(engine, _SOURCE, OrderSide.SELL, 100, basis, "T4", "SOURCE", 1)
    _leg(engine, _TARGET, OrderSide.BUY, 100, basis, "T4", "DEST", 2)
    before = (_pos_qty(engine, _SOURCE), _pos_qty(engine, _TARGET))

    with pytest.raises(Exception):  # duplicate ClientOrderId — the cache refuses to re-add the same leg
        _leg(engine, _SOURCE, OrderSide.SELL, 100, basis, "T4", "SOURCE", 1)

    assert (_pos_qty(engine, _SOURCE), _pos_qty(engine, _TARGET)) == before


@pytest.mark.engine
def test_transfer_legs_are_tagged_so_analytics_can_exclude_them(engine):
    """Untagged, these fills would inflate turnover and pollute every execution-quality metric."""
    _fresh(engine)
    _seed(engine, _SOURCE, 190, 23.33)
    basis = engine.cache.position(PositionId(f"{_IID}-{_SOURCE}")).avg_px_open

    order = _leg(engine, _SOURCE, OrderSide.SELL, 100, basis, "T5", "SOURCE", 1)

    assert _TAG in order.tags
    assert "T5" in order.tags
