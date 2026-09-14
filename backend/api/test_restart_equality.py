"""#74c — restart equality (needs_services: a live Redis).

The faithful restart proof, offline: a BacktestEngine persists to a Redis cache exactly as a live node does
(`flush_on_start=False`, `snapshot_positions=True`). Engine A runs a cycle and persists; Engine B reloads from
the SAME Redis (positions/orders come back, the in-memory snapshot archive does NOT), then the projection is
re-seeded from the durable envelope (`seed_cycle`) + the persisted snapshots (`read_restored_legs` →
`seed_restored_legs`), and must reproduce A's TradeDTO IDENTICALLY. This pins the #74 invariant: same reducer,
same cycle_id + P&L, live vs restart.

Redis via KUMO_REDIS_HOST/KUMO_REDIS_PORT (default localhost:6399). Marked `needs_services` — run explicitly.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import redis
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    ExecEngineConfig,
    LoggingConfig,
)
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import ClientId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from api.snapshot_reader import read_restored_legs
from api.strategy_ids import MANUAL, MANUAL_NAME, MANUAL_TAG
from api.test_projection_replay import _IID, _POS_ID, ReplayDriver, _quote, _ReplayDriverConfig
from api.trade_cycle import TradeCycleProjection

# BOTH marks: it needs live services (Redis) AND must run in its own interpreter — Nautilus's pyo3 Redis cache
# load breaks after a prior test's asyncio.run() (the postgres cycle_store tests), so it can't share the
# needs_services process. run-tests.sh gives `engine and needs_services` its own pass.
pytestmark = [pytest.mark.needs_services, pytest.mark.engine]

_TRADER_ID = "RESTART-001"
_HOST = os.environ.get("KUMO_REDIS_HOST", "localhost")
_PORT = int(os.environ.get("KUMO_REDIS_PORT", "6399"))


def _cache(flush: bool) -> CacheConfig:
    return CacheConfig(
        database=DatabaseConfig(type="redis", host=_HOST, port=_PORT),
        encoding="msgpack",
        flush_on_start=flush,
        use_trader_prefix=True,
        use_instance_id=False,
    )


def _engine(flush: bool) -> BacktestEngine:
    eng = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=_TRADER_ID,
            logging=LoggingConfig(log_level="ERROR"),
            cache=_cache(flush),
            exec_engine=ExecEngineConfig(snapshot_positions=True, snapshot_orders=True),
        )
    )
    eng.add_venue(
        venue=Venue("XNAS"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
    )
    eng.add_instrument(TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS"))
    return eng


def _purge_trader_keys() -> None:
    """Hermetic setup: drop only THIS trader's keys (never FLUSHALL — KUMO_REDIS_HOST could be the shared bus).
    `flush_on_start` clears the main cache but not the append-list snapshot keys, so a prior run's legs would
    otherwise leak into the reader."""
    r = redis.Redis(host=_HOST, port=_PORT)
    keys = list(r.scan_iter(match=f"trader-{_TRADER_ID}:*"))
    if keys:
        r.delete(*keys)


def test_restart_reproduces_identical_trade_dto():
    # A prior test's asyncio.run() closes the default loop; BacktestEngine's Redis cache load needs a live one.
    asyncio.set_event_loop(asyncio.new_event_loop())
    _purge_trader_keys()
    # --- Process A analogue: run a cycle, persist to Redis (flush first for a clean keyspace) ---
    eng_a = _engine(flush=True)
    driver_a = ReplayDriver(_ReplayDriverConfig(strategy_id=MANUAL_NAME, order_id_tag=MANUAL_TAG))
    eng_a.add_strategy(driver_a)
    proj_a = TradeCycleProjection(ClientId("SIM"), MANUAL)
    driver_a.projection = proj_a
    # A cycle that SPANS a flat via an ARMED re-entry: open 100 @100, rest a re-entry limit @90, sell 100 @110
    # (flat but the limit bridges → same cycle), drop to 90 → the limit fills → reopen 100. Leg 1 (the closed
    # round trip, realized +1000) gets snapshotted; the reopened leg is fresh. Cycle realized = 1000 across 2
    # legs — and leg 1's 1000 lives ONLY in the persisted snapshot, so restart must recover it via the reader.
    driver_a.set_sequence([("buy", 100), ("limit_buy", 100, 90), ("sell", 100), ("noop",)])
    eng_a.add_data([_quote(100.0, 1), _quote(100.0, 2), _quote(110.0, 3), _quote(90.0, 4)])
    eng_a.run()
    live = [d for _l, dtos in driver_a.trace for d in dtos][-1]
    account_id = str(eng_a.cache.accounts()[0].id)
    eng_a.dispose()

    assert live.state == "HELD"
    assert live.quantity == 100.0
    assert live.leg_count == 2
    assert live.realized_pnl == "1000.00 USD"  # leg 1 (+1000) + reopened leg (0)

    # --- Process B analogue: reload from the same Redis; the in-memory snapshot archive is GONE ---
    eng_b = _engine(flush=False)
    assert eng_b.cache.position(_POS_ID) is not None  # position reloaded
    assert not eng_b.cache.position_snapshots(_POS_ID)  # snapshots did NOT rehydrate (the gap)

    # Re-seed the SAME reducer: the cycle boundary from the (simulated) envelope + the legs from the persisted
    # snapshots. Then project over B's reloaded cache.
    proj_b = TradeCycleProjection(ClientId("SIM"), MANUAL)
    proj_b.seed_cycle(account_id, str(_IID), live.cycle_id, live.opened_ts, live.last_event_ts)
    r = redis.Redis(host=_HOST, port=_PORT)
    restored = read_restored_legs(r, _TRADER_ID)
    for pos_id, legs in restored.items():
        proj_b.seed_restored_legs(pos_id, legs)

    dtos = proj_b.project(eng_b.cache, now_ns=live.last_event_ts + 1)
    eng_b.dispose()

    assert len(dtos) == 1
    after = dtos[0]
    # IDENTICAL identity + state + P&L across the restart — the whole point of #74.
    assert after.cycle_id == live.cycle_id
    assert after.state == live.state == "HELD"
    assert after.side == live.side
    assert after.quantity == live.quantity == 100.0
    assert after.opened_ts == live.opened_ts
    assert after.leg_count == live.leg_count == 2  # leg 1 recovered from the snapshot + the reopened leg
    # The decisive assertion: leg 1's +1000 lived ONLY in the persisted snapshot (cache archive gone), so this
    # equals live ONLY because read_restored_legs recovered it. Without the reader it would be "0.00 USD".
    assert after.realized_pnl == live.realized_pnl == "1000.00 USD"
