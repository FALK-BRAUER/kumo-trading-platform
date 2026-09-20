"""#76 — projection replay harness. Golden event-sequence replays that PIN the Nautilus 1.229.0 invariants the
trade-cycle projection (#73) leans on, so a version bump can't silently break the trade spine. Every assertion
reads NATIVE Nautilus cache/portfolio state — never a parallel ledger.

Invariants pinned here (backtest path: market data → simulated venue → native OrderFilled → position/snapshot):
  1. NETTING position_id == PositionId(f"{instrument_id}-{strategy_id}")
  2. snapshot-on-reopen: close-to-flat then reopen the same id archives the prior leg + resets new-leg realized

RUN SEPARATELY — marked `engine`. A BacktestEngine uses native globals that segfault if a second engine is
built in the same interpreter (see api/test_app.py), and the default suite already boots one. The default
`pytest api/` skips this file (addopts `-m 'not engine'`); the harness runs in its own process via
`pytest api/ -m engine`. One module-scoped engine is reused across sequences via reset()+clear_data().

Restart / external-order / corrections replays are NOT here — a faithful restart needs a real process boundary
(Redis cache or mocked broker reports), so they live in the live-reconciliation harness with #74. See
docs/plan-76-replay-harness.md.
"""

from __future__ import annotations

import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import (
    AccountType,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from api.strategy_ids import MANUAL, MANUAL_NAME, MANUAL_TAG

pytestmark = pytest.mark.engine

_VENUE = Venue("XNAS")
_IID = InstrumentId.from_str("AAPL.XNAS")
_POS_ID = PositionId(f"{_IID}-{MANUAL}")  # the NETTING invariant, spelled out
_STARTING_CASH = 1_000_000

# One scripted action per fed quote. ("buy"/"sell", qty) → market; ("limit_buy", qty, px) → resting limit;
# ("noop",) → advance a tick without acting (lets a prior order fill / market move).
Action = tuple


class _ReplayDriverConfig(StrategyConfig, frozen=True):
    pass


class ReplayDriver(Strategy):
    """Minimal MANUAL-strategy stand-in: drains a scripted action queue, one action per quote tick, submitting
    real orders so the simulated venue produces native fills/positions/snapshots. The projection (#73) will read
    the same native state this harness asserts on."""

    def __init__(self, config: _ReplayDriverConfig) -> None:
        super().__init__(config)
        self._script: list[Action] = []
        self.projection = None  # attach a TradeCycleProjection to record its fold over this run's events
        self.trace: list[tuple[str, list]] = []  # (event label, projection.project() output) per event

    def set_sequence(self, actions: list[Action]) -> None:
        """Load the next golden sequence's actions (call before engine.run())."""
        self._script = list(actions)

    def _record(self, label: str) -> None:
        # Run the projection exactly where UiFeedStrategy will — on each native position/order event, post-cache
        # update — so the harness exercises the real event fold, not a single end-of-run scan.
        if self.projection is not None:
            self.trace.append((label, self.projection.project(self.cache, self.clock.timestamp_ns())))

    def on_position_opened(self, event) -> None:
        self._record("pos_opened")

    def on_position_changed(self, event) -> None:
        self._record("pos_changed")

    def on_position_closed(self, event) -> None:
        self._record("pos_closed")

    def on_order_event(self, event) -> None:
        self._record("order_event")

    def on_start(self) -> None:
        self.subscribe_quote_ticks(_IID)

    def on_reset(self) -> None:
        self._script = []
        self.trace = []

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if not self._script:
            return
        kind, *rest = self._script.pop(0)
        if kind == "buy":
            self.submit_order(self.order_factory.market(_IID, OrderSide.BUY, Quantity.from_int(rest[0])))
        elif kind == "sell":
            self.submit_order(self.order_factory.market(_IID, OrderSide.SELL, Quantity.from_int(rest[0])))
        elif kind == "limit_buy":
            self.submit_order(
                self.order_factory.limit(
                    _IID, OrderSide.BUY, Quantity.from_int(rest[0]), Price.from_str(str(rest[1]))
                )
            )
        elif kind == "cancel":
            for order in self.cache.orders_open(instrument_id=_IID, strategy_id=self.id):
                self.cancel_order(order)
        elif kind == "noop":
            pass  # advance a tick without acting (let a resting order fill / market move)
        else:
            # An unrecognized action must fail loudly — a silent no-op would let a mistyped golden
            # sequence green-pass while testing nothing.
            raise AssertionError(f"unknown replay action: {kind!r}")

    @property
    def script_drained(self) -> bool:
        return not self._script


def _quote(px: float, ts: int) -> QuoteTick:
    """A tight two-sided quote at `px` — a market order fills here; `ts` orders events (ns)."""
    p = Price.from_str(f"{px:.2f}")
    size = Quantity.from_int(1_000)
    return QuoteTick(
        instrument_id=_IID, bid_price=p, ask_price=p, bid_size=size, ask_size=size, ts_event=ts, ts_init=ts
    )


@pytest.fixture(scope="module")
def engine() -> BacktestEngine:
    """One BacktestEngine for the whole module (second-engine-per-interpreter segfaults). NETTING venue so
    position ids carry the strategy natively."""
    eng = BacktestEngine(
        config=BacktestEngineConfig(trader_id="REPLAY-001", logging=LoggingConfig(log_level="ERROR"))
    )
    eng.add_venue(
        venue=_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=USD,
        starting_balances=[Money(_STARTING_CASH, USD)],
    )
    eng.add_instrument(TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS"))
    driver = ReplayDriver(_ReplayDriverConfig(strategy_id=MANUAL_NAME, order_id_tag=MANUAL_TAG))
    eng.add_strategy(driver)
    yield eng
    eng.dispose()


def _run_sequence(
    engine: BacktestEngine, actions: list[Action], quotes: list[QuoteTick], projection=None
) -> ReplayDriver:
    """Reset to a clean slate, load fresh data + actions, run to completion. reset() clears cache
    (positions/snapshots/orders/portfolio) but keeps the venue/instrument/strategy + loaded data, so we also
    clear_data() before adding the sequence's quotes."""
    engine.reset()
    engine.clear_data()
    # Post-reset smoke: no state bleeds between sequences (positions, orders, AND archived snapshots).
    assert not engine.cache.positions()
    assert not engine.cache.orders()
    assert not engine.cache.position_snapshot_ids()
    driver = engine.trader.strategies()[0]
    assert isinstance(driver, ReplayDriver)
    driver.projection = projection  # None for the pure-native invariant tests
    driver.set_sequence(actions)
    engine.add_data(quotes)
    engine.run()
    # Every scripted action must have been consumed (one per quote) — a leftover action means the sequence
    # and the fed quotes are mismatched and the test asserted less than it claims.
    assert driver.script_drained, "replay script not fully drained — actions/quotes count mismatch"
    return driver


def test_netting_position_id_carries_strategy(engine: BacktestEngine) -> None:
    """Invariant 1: a MANUAL fill opens a position whose id is exactly {instrument}-{strategy_id}."""
    _run_sequence(engine, actions=[("buy", 100)], quotes=[_quote(100.0, 1)])
    positions = engine.cache.positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.id == _POS_ID
    assert pos.strategy_id == MANUAL
    assert pos.instrument_id == _IID
    assert pos.quantity == Quantity.from_int(100)


def test_snapshot_on_reopen_archives_prior_leg_and_resets_realized(engine: BacktestEngine) -> None:
    """Invariant 2: open → partial → flat (leg 1 realized) → ARMED limit → refill reopens the SAME id, with
    leg 1 archived to position_snapshots and the new leg's realized reset. This is the native mechanism the
    cycle projection stitches; if a Nautilus bump changes it, this fails loudly."""
    actions: list[Action] = [
        ("buy", 100),            # open @100
        ("sell", 40),            # partial close @110
        ("sell", 60),            # flat/close @110 → leg 1 realized (+$1000 over the round trip)
        ("limit_buy", 100, 90),  # rests below market (110) → ARMED (flat + open entry order)
        ("noop",),               # market drops to 90 → the resting limit fills → reopen SAME id, new leg
    ]
    quotes = [
        _quote(100.0, 1),
        _quote(110.0, 2),
        _quote(110.0, 3),
        _quote(110.0, 4),  # limit rests here (90 < 110 ask → not marketable) = ARMED
        _quote(90.0, 5),   # market hits 90 → limit becomes marketable → fills → reopen
    ]
    _run_sequence(engine, actions, quotes)

    # ARMED interval is real, not assumed: the entry limit was ACCEPTED at ts=4 (resting, flat, while market
    # was 110) and only FILLED at ts=5 when the market reached 90. Asserting the transition — not just the
    # final long — catches a regression where the limit fills immediately yet the end state still looks right.
    limits = [o for o in engine.cache.orders() if o.order_type == OrderType.LIMIT]
    assert len(limits) == 1
    entry = limits[0]
    assert entry.status == OrderStatus.FILLED
    assert entry.ts_accepted == 4  # armed here (before the crossing quote)
    assert entry.ts_last == 5      # filled only once market crossed → reopen
    assert entry.filled_qty == Quantity.from_int(100)
    assert entry.avg_px == Price.from_str("90.00")

    # EXACTLY ONE snapshot: leg 1 archived when the id was reused on reopen. A precise assert (not `>= 1`) so a
    # stale/duplicate/mis-signed snapshot can't false-green the invariant #73's cycle P&L depends on.
    snapshots = engine.cache.position_snapshots(_POS_ID)
    assert len(snapshots) == 1
    leg1 = snapshots[0]
    assert leg1.is_closed
    assert leg1.side == PositionSide.FLAT
    assert leg1.quantity == Quantity.zero()
    assert leg1.instrument_id == _IID
    assert leg1.strategy_id == MANUAL
    assert str(leg1.id).startswith(str(_POS_ID))  # snapshot id = {pos_id}-{uuid}
    assert float(leg1.avg_px_open) == 100.0
    assert float(leg1.avg_px_close) == 110.0
    assert str(leg1.realized_pnl) == "1000.00 USD"  # +$1000 round trip lives on the SNAPSHOT
    assert leg1.ts_closed == 3

    # Reopened leg under the SAME id: fresh long 100 @ 90 with realized reset to EXACTLY zero — leg 1's +$1000
    # is NOT carried forward. (Cycle P&L = Σ snapshot legs' realized + current leg's realized — #73.)
    pos = engine.cache.position(_POS_ID)
    assert pos is not None
    assert pos.id == _POS_ID
    assert pos.strategy_id == MANUAL
    assert pos.is_open
    assert pos.side == PositionSide.LONG
    assert pos.quantity == Quantity.from_int(100)
    assert float(pos.avg_px_open) == 90.0
    assert str(pos.realized_pnl) == "0.00 USD"  # fresh leg — no profit carried forward


def test_projection_folds_two_cycles_over_close_then_rearm(engine: BacktestEngine) -> None:
    """#73 fold, run the SAME way UiFeedStrategy will (project() on each native position/order event): the
    close-then-rearm golden sequence yields TWO cycles — the round trip CLOSES (realized 1000), and because the
    entry limit was placed AFTER a flat/CLOSED moment (no order bridged the flat), the reopen mints a NEW
    cycle_id with realized reset. Pins cycle identity + per-cycle native P&L partitioning (leg_count excludes the
    prior cycle's snapshot leg)."""
    from nautilus_trader.model.identifiers import ClientId

    from api.trade_cycle import TradeCycleProjection

    proj = TradeCycleProjection(ClientId("SIM"), MANUAL)
    actions: list[Action] = [("buy", 100), ("sell", 40), ("sell", 60), ("limit_buy", 100, 90), ("noop",)]
    quotes = [_quote(100.0, 1), _quote(110.0, 2), _quote(110.0, 3), _quote(110.0, 4), _quote(90.0, 5)]
    driver = _run_sequence(engine, actions, quotes, projection=proj)

    frames = [dto for _label, dtos in driver.trace for dto in dtos]
    assert frames, "projection emitted nothing"

    # Exactly two distinct cycles were seen across the run.
    cycle_ids = {f.cycle_id for f in frames}
    assert len(cycle_ids) == 2

    # Cycle 1 — the 100-share round trip — reached CLOSED with the full native realized on the closed leg.
    closed = [f for f in frames if f.state == "CLOSED"]
    assert len(closed) >= 1
    c1 = closed[-1]
    assert c1.side == "FLAT"
    assert c1.quantity == 0.0
    assert c1.realized_pnl == "1000.00 USD"
    assert c1.leg_count == 1
    assert c1.avg_px_open is None
    assert c1.closed_ts is not None
    assert c1.is_capital_deployed is False and c1.is_engaged is False  # dual-lens: CLOSED = neither

    # An ARMED frame appeared for cycle 2 (flat + a live entry limit, before it filled).
    armed = [f for f in frames if f.state == "ARMED"]
    assert armed, "expected an ARMED frame while the entry limit rested flat"
    c2_id = armed[-1].cycle_id
    assert c2_id != c1.cycle_id  # close-then-rearm → NEW cycle
    # dual-lens: an ARMED qty-0 name is ENGAGED but NOT capital-deployed (must not inflate %-deployed).
    assert armed[-1].is_engaged is True and armed[-1].is_capital_deployed is False

    # Final frame — cycle 2 reopened HELD: fresh long 100 @ 90, realized reset, and leg_count == 1 (cycle 1's
    # archived snapshot leg is partitioned OUT by ts_opened, not double-counted).
    final = frames[-1]
    assert final.cycle_id == c2_id
    assert final.state == "HELD"
    assert final.side == "LONG"
    assert final.quantity == 100.0
    assert final.avg_px_open == 90.0
    assert final.realized_pnl == "0.00 USD"
    assert final.leg_count == 1
    assert final.manager_id is None
    assert final.is_capital_deployed is True and final.is_engaged is True  # HELD = deployed + engaged
    assert final.client_id == "SIM"  # the exec client, threaded through — not the data client


class _RestartCache:
    """A stub cache simulating post-restart NATIVE state: Nautilus `load_cache` restored the current position
    and account, but the in-memory close-reopen snapshot archive is GONE (it never rehydrates). This is exactly
    the state the #74 gap-fill must recover cycle P&L from."""

    def __init__(self, position, account):
        self._pos = position
        self._account = account

    def accounts(self):
        return [self._account]

    def positions_open(self):
        return [self._pos] if self._pos.is_open else []

    def orders_open(self):
        return []

    def position(self, pos_id):
        return self._pos if self._pos.id == pos_id else None

    def position_snapshots(self, pos_id):
        return []  # the loss: snapshots do not survive restart


def test_gap_fill_recovers_cycle_pnl_across_restart(engine: BacktestEngine) -> None:
    """#74 gap-fill: after a restart the in-memory snapshot archive is lost, so cycle P&L would drop every
    pre-restart leg. Seeding the cycle (envelope) + the restored leg (from Nautilus's persisted position state)
    must reproduce the SAME cycle_id and the FULL cycle realized (prior leg + current), via the SAME reducer."""
    from nautilus_trader.model.identifiers import ClientId

    from api.trade_cycle import CycleLeg, TradeCycleProjection

    # --- LIVE (pre-restart): open 100 @100 -> flat @110 (leg 1, +1000) -> reopen 100 @90 (current leg) ---
    live = TradeCycleProjection(ClientId("SIM"), MANUAL)
    _run_sequence(
        engine,
        actions=[("buy", 100), ("sell", 100), ("limit_buy", 100, 90), ("noop",)],
        quotes=[_quote(100.0, 1), _quote(110.0, 2), _quote(110.0, 3), _quote(90.0, 4)],
        projection=live,
    )
    pre = [d for _l, dtos in engine.trader.strategies()[0].trace for d in dtos][-1]
    assert pre.state == "HELD" and pre.leg_count == 1  # reopened leg only; leg 1 is a separate closed cycle here

    # Capture what would be DURABLE across restart: the reopened position (Nautilus load_cache restores it) and
    # leg 1's persisted state-dict (Nautilus wrote it to snapshots:positions on close).
    reopened = engine.cache.position(_POS_ID)
    account = engine.cache.accounts()[0]
    leg1_state = engine.cache.position_snapshots(_POS_ID)[0].to_dict()  # the persisted-format source
    leg1 = CycleLeg.from_state_dict(leg1_state)
    assert str(leg1.realized_pnl) == "1000.00 USD"  # recovered natively from the persisted dict

    # --- RESTART: fresh projection, stub cache (position restored, snapshots gone), seed from durable state ---
    restarted = TradeCycleProjection(ClientId("SIM"), MANUAL)
    cycle_id = f"{account.id}:SIM:{_IID}:{MANUAL}:{leg1.ts_opened}"  # what the envelope stored
    restarted.seed_cycle(str(account.id), str(_IID), cycle_id, opened_ts=leg1.ts_opened)
    restarted.seed_restored_legs(f"{_IID}-{MANUAL}", [leg1])

    dtos = restarted.project(_RestartCache(reopened, account))
    assert len(dtos) == 1
    dto = dtos[0]
    assert dto.cycle_id == cycle_id            # SAME cycle_id, not a fresh mint
    assert dto.state == "HELD"
    assert dto.leg_count == 2                  # leg 1 (restored) + current leg
    assert dto.realized_pnl == "1000.00 USD"   # cycle P&L SURVIVED restart (would be 0.00 without the gap-fill)
    assert dto.quantity == 100.0


def test_gap_fill_no_double_count_when_current_pos_is_a_restored_leg(engine: BacktestEngine) -> None:
    """codex-flagged: on restart while CLOSED, cache.position(pos_id) IS the last closed leg, which the restore
    also recovers under the same ts_opened. It must be counted ONCE (current pos wins the dedup), not doubled."""
    from nautilus_trader.model.identifiers import ClientId

    from api.trade_cycle import CycleLeg, TradeCycleProjection

    # Produce a real CLOSED leg (open 100 @100 -> sell 100 @110 -> flat, realized 1000).
    live = TradeCycleProjection(ClientId("SIM"), MANUAL)
    _run_sequence(
        engine,
        actions=[("buy", 100), ("sell", 100)],
        quotes=[_quote(100.0, 1), _quote(110.0, 2)],
        projection=live,
    )
    closed_pos = engine.cache.position(_POS_ID)  # the flat/closed leg
    account = engine.cache.accounts()[0]
    assert str(closed_pos.realized_pnl) == "1000.00 USD"

    # Restart: the SAME leg is both the current closed position AND a restored leg (same ts_opened).
    restarted = TradeCycleProjection(ClientId("SIM"), MANUAL)
    cycle_id = f"{account.id}:SIM:{_IID}:{MANUAL}:{closed_pos.ts_opened}"
    restarted.seed_cycle(str(account.id), str(_IID), cycle_id, opened_ts=closed_pos.ts_opened)
    restarted.seed_restored_legs(f"{_IID}-{MANUAL}", [CycleLeg.from_state_dict(closed_pos.to_dict())])

    dtos = restarted.project(_RestartCache(closed_pos, account))
    assert len(dtos) == 1
    dto = dtos[0]
    assert dto.state == "CLOSED"
    assert dto.leg_count == 1              # NOT 2 — deduped by ts_opened
    assert dto.realized_pnl == "1000.00 USD"  # NOT 2000.00 — counted once


def test_no_fill_armed_cancel_closes_with_ts_and_monotonic_clock(engine: BacktestEngine) -> None:
    """codex HIGH: an ARMED entry limit that is CANCELED (never filled) closes with NO native position, so
    closed_ts would be None and last_event_ts 0 — the envelope would reload it as active and reject the close.
    The fold clock must give the terminal CLOSED a closed_ts and a last_event_ts strictly above the ARMED one."""
    from nautilus_trader.model.identifiers import ClientId

    from api.trade_cycle import TradeCycleProjection

    proj = TradeCycleProjection(ClientId("SIM"), MANUAL)
    # limit rests below market (ARMED), then is canceled (CLOSED, no fill, no position).
    driver = _run_sequence(
        engine,
        actions=[("limit_buy", 100, 90), ("cancel",)],
        quotes=[_quote(110.0, 1), _quote(110.0, 2)],
        projection=proj,
    )
    frames = [d for _l, dtos in driver.trace for d in dtos]
    armed = [f for f in frames if f.state == "ARMED"]
    closed = [f for f in frames if f.state == "CLOSED"]
    assert armed and closed, [f.state for f in frames]
    # Same cycle armed then closed with no fill.
    assert closed[-1].cycle_id == armed[-1].cycle_id
    assert closed[-1].side == "FLAT" and closed[-1].quantity == 0.0
    assert closed[-1].realized_pnl == "0.00 USD"
    # The terminal CLOSED is durably distinguishable + out-orders the ARMED write.
    assert closed[-1].closed_ts is not None
    assert closed[-1].last_event_ts > armed[-1].last_event_ts
