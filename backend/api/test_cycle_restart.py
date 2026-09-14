"""Wiring test for the restart seed (#74b.2) — proves seed_projection restores legs + cycle from durable state
with fakes (no engine, no services). The end-to-end reconstruction is proven separately against a real Redis in
test_restart_equality.py; this covers the glue the engine calls."""

from __future__ import annotations

import asyncio

import msgspec.msgpack as _msgpack
from nautilus_trader.model.identifiers import ClientId

from api.cycle_restart import seed_projection
from api.cycle_store import EnvelopeRow
from api.strategy_ids import MANUAL
from api.trade_cycle import TradeCycleProjection

_POS_ID = f"AAPL.XNAS-{MANUAL}"


class _FakeRedis:
    """Stands in for the snapshot-key Redis: one position with a persisted CLOSED leg (realized 1000)."""

    def __init__(self, trader_id: str):
        key = f"trader-{trader_id}:snapshots:positions:{_POS_ID}"
        closed = {"ts_opened": 1, "ts_closed": 2, "realized_pnl": "1000.00 USD"}
        opened = {"ts_opened": 3, "ts_closed": None, "realized_pnl": "0.00 USD"}  # a non-closed state → ignored
        self._data = {key: [_msgpack.encode(closed), _msgpack.encode(opened)]}

    def scan_iter(self, match=None):
        import fnmatch

        return (k for k in self._data if fnmatch.fnmatch(k, match))

    def lrange(self, key, start, end):
        return self._data[key]


class _FakeStore:
    """Records the load_active filter + returns one active cycle for the matching client/strategy only."""

    def __init__(self):
        self.filter_seen = None

    async def load_active(self, client_id=None, strategy_id=None):
        self.filter_seen = (client_id, strategy_id)
        if client_id == "ALPACA" and strategy_id == str(MANUAL):
            return [
                EnvelopeRow(
                    cycle_id="ALPACA:ALPACA:AAPL.XNAS:MANUAL-001:1",
                    account_id="ACC",
                    client_id="ALPACA",
                    instrument_id="AAPL.XNAS",
                    strategy_id=str(MANUAL),
                    opened_ts=1,
                    closed_ts=None,
                    state="HELD",
                    last_event_ts=9,
                )
            ]
        return []  # another node's rows must never seed us


def test_seed_projection_restores_legs_and_cycle_scoped():
    proj = TradeCycleProjection(ClientId("ALPACA"), MANUAL)
    store = _FakeStore()
    redis = _FakeRedis("COCKPIT-001")

    asyncio.run(
        seed_projection(proj, store, redis, "COCKPIT-001", "ALPACA", str(MANUAL))
    )

    # Load was scoped to this node's client + strategy (HIGH: no cross-node poisoning).
    assert store.filter_seen == ("ALPACA", str(MANUAL))
    # The CLOSED leg (realized 1000) was restored; the non-closed state was ignored.
    assert _POS_ID in proj._restored
    assert len(proj._restored[_POS_ID]) == 1
    assert str(proj._restored[_POS_ID][0].realized_pnl) == "1000.00 USD"
    # The active cycle boundary was seeded (same cycle_id + monotonic clock).
    key = ("ACC", "ALPACA", "AAPL.XNAS", str(MANUAL))
    assert key in proj._active
    assert proj._active[key].cycle_id == "ALPACA:ALPACA:AAPL.XNAS:MANUAL-001:1"
    assert proj._active[key].opened_ts == 1
    assert proj._active[key].last_event_ts == 9


def test_seed_projection_ignores_other_nodes_cycles():
    proj = TradeCycleProjection(ClientId("IBKR"), MANUAL)  # different client
    store = _FakeStore()
    asyncio.run(seed_projection(proj, store, _FakeRedis("OTHER"), "OTHER", "IBKR", str(MANUAL)))
    assert store.filter_seen == ("IBKR", str(MANUAL))
    assert not proj._active  # _FakeStore returns nothing for IBKR → no foreign cycle seeded
