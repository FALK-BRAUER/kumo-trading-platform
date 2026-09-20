"""NodeManager — owns the in-process Nautilus engine for the API layer.

#4 vertical slice: stands up a `BacktestEngine` (same setup as the passing paper spike), runs a small
set of tickers through `BridgeStrategy` lanes, and exposes two things to FastAPI:

  * `positions()` — live read of the Nautilus cache → typed DTOs (REST `GET /positions`)
  * `event_tape()` — the ordered bar/fill events Nautilus produced → paced out over WS

Backtest is batch (run-to-completion), so the WS layer replays the captured tape with pacing to
animate the charts. The events themselves are genuine Nautilus events — nothing synthetic past the
synthetic input bars. A live streaming feed (Sandbox exec client) lands in #6 and will replace the
replay while keeping this same DTO contract.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from api.bridge_strategy import BridgeStrategy, BridgeStrategyConfig
from api.models import BarDTO, ExternalActivityDTO, FillDTO, PositionDTO, PriceDTO, TradeDTO

VENUE = "XNAS"
STARTING_CASH = 1_000_000


@dataclass(frozen=True)
class SymbolSpec:
    """One demo ticker: symbol, trade size, and the synthetic price-path parameters."""

    symbol: str
    trade_size: int
    base: float  # starting price
    drift: float  # per-bar upward drift
    amp: float  # sine-wave amplitude (gives the cloud something to chew on)
    period: float  # sine wavelength in bars


# Two-stock demo universe (distinct price scales so the charts read differently).
# ~260 daily bars each → enough history for the full Ichimoku (Span B 52, MA200 200) to form.
N_BARS = 260
SYMBOLS: tuple[SymbolSpec, ...] = (
    SymbolSpec("AAPL", 100, base=88.0, drift=0.07, amp=6.0, period=34.0),
    SymbolSpec("MSFT", 30, base=360.0, drift=0.22, amp=20.0, period=27.0),
)


def _close_path(spec: SymbolSpec) -> list[float]:
    """Deterministic wandering price: drift + two sine harmonics (reproducible → stable tests)."""
    out: list[float] = []
    for i in range(N_BARS):
        wave = spec.amp * math.sin(i / spec.period) + spec.amp * 0.4 * math.sin(i / (spec.period * 0.37))
        out.append(round(spec.base + spec.drift * i + wave, 2))
    return out


def _make_bars(bar_type: BarType, closes: list[float]) -> list[Bar]:
    """Synthetic daily bars — stand-in input feed until the #11 market-data provider lands."""
    start = pd.Timestamp("2024-01-02", tz="UTC")
    bars: list[Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + abs(c - o) * 0.5 + 0.2
        lo = min(o, c) - abs(c - o) * 0.5 - 0.2
        ts = (start + pd.Timedelta(days=i)).value  # ns
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{o:.2f}"),
                high=Price.from_str(f"{hi:.2f}"),
                low=Price.from_str(f"{lo:.2f}"),
                close=Price.from_str(f"{c:.2f}"),
                volume=Quantity.from_int(1_000_000),
                ts_event=ts,
                ts_init=ts,
            )
        )
        prev = c
    return bars


class NodeManager:
    """Builds, runs, and exposes the Nautilus engine. One instance per process."""

    def __init__(self) -> None:
        self._engine: BacktestEngine | None = None
        self._strategies: list[BridgeStrategy] = []
        self._venue = Venue(VENUE)
        self._synthetic_commands: set[str] = set()  # #39: ids we minted, so command_status can ack them

    async def start(self) -> None:
        """Build the engine and run the slice to completion (off-loop — it's CPU-bound)."""
        await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> None:
        engine = BacktestEngine(
            config=BacktestEngineConfig(
                trader_id="PLATFORM-001",
                logging=LoggingConfig(log_level="WARNING"),
            )
        )
        engine.add_venue(
            venue=self._venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=USD,
            starting_balances=[Money(STARTING_CASH, USD)],
        )

        for spec in SYMBOLS:
            instrument = TestInstrumentProvider.equity(symbol=spec.symbol, venue=VENUE)
            engine.add_instrument(instrument)
            bar_type = BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL")
            engine.add_data(_make_bars(bar_type, _close_path(spec)))
            # Explicit paper-slice unlock: opt in to the single market BUY so a real fill flows
            # through the WS path. Strategy default stays buy_on_first_bar=False (no autonomous BUY).
            strategy = BridgeStrategy(
                BridgeStrategyConfig(
                    bar_type=bar_type,
                    trade_size=spec.trade_size,
                    buy_on_first_bar=True,
                )
            )
            engine.add_strategy(strategy)
            self._strategies.append(strategy)

        engine.run()
        self._engine = engine

    async def stop(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def trades_realized(self) -> dict | None:
        """No engine here, so nothing computed it (#233).

        None rather than an empty dict on purpose: None means "nobody answered" and the tile falls back
        to summing live cycles, while a zeroed dict would assert that nothing closed today — a claim
        this node is in no position to make.
        """
        return None

    def trades_periods(self) -> dict | None:
        """Same reasoning as `trades_realized` (#322, #846) — no engine, no legs, nothing computed it."""
        return None

    def trades_periods_swept(self) -> dict | None:
        """No broker sweep runs here (#322)."""
        return None

    def trades_legs(self) -> dict | None:
        """No engine, no closed-leg registry (#846). None = nobody answered, not "complete"."""
        return None

    def trades_flows(self) -> dict | None:
        """No engine, no cache orders (#699 a). None = nobody answered, never an empty map."""
        return None

    def trades_health(self) -> tuple[str | None, str | None]:
        """No engine, no projection — nothing to report (#298)."""
        return None, None

    def trades(self) -> list[TradeDTO]:
        """The synthetic node has no exec client → no trade cycles (#73). The live engine (engine_node) owns
        the TradeCycleProjection; this slice is data-only."""
        return []

    def strategy_position(self, instrument_id: str, strategy_id: str, side: str) -> dict | None:
        """No broker book on the synthetic node — nothing to transfer."""
        return None

    def external(self) -> list[ExternalActivityDTO]:
        """No exec client → no external/quarantine activity (#79)."""
        return []

    def positions(self) -> list[PositionDTO]:
        """Live read of the Nautilus cache → typed DTOs."""
        if self._engine is None:
            return []
        out: list[PositionDTO] = []
        for pos in self._engine.cache.positions():
            out.append(
                PositionDTO(
                    instrument_id=str(pos.instrument_id),
                    side=pos.side.name,
                    quantity=float(pos.quantity),
                    avg_px_open=float(pos.avg_px_open),
                    realized_pnl=str(pos.realized_pnl),
                    strategy_id=str(pos.strategy_id),
                )
            )
        out.sort(key=lambda p: p.instrument_id)
        return out

    def bars(self) -> list[BarDTO]:
        """All lanes' bars in event-time order — the WS snapshot's price history."""
        out = [bar for s in self._strategies for bar in s.bars]
        out.sort(key=lambda b: (b.ts_event, b.instrument_id))
        return out

    async def bars_for(self, symbol: str, granularity: str) -> list[BarDTO]:
        """One symbol's bars. Synthetic feed is daily-only — granularity is ignored."""
        return [b for b in self.bars() if b.instrument_id == symbol]

    def fills(self) -> list[FillDTO]:
        """All lanes' fills in event-time order — the WS snapshot's trade markers."""
        out = [fill for s in self._strategies for fill in s.fills]
        out.sort(key=lambda f: (f.ts_event, f.instrument_id))
        return out

    def orders(self) -> list:
        return []  # the synthetic backtest node has no live order blotter (#33)

    def prices(self) -> list[PriceDTO]:
        return []  # the synthetic backtest node has no real-time trade stream

    def quotes(self) -> list:
        return []  # the synthetic backtest node has no NBBO quote stream (#40)

    def vwaps(self) -> list:
        return []  # the synthetic backtest node has no session VWAP (#182 follow-up, KPI Phase 2)

    def today_ranges(self) -> list:
        return []  # the synthetic backtest node has no Alpaca snapshot feed (#182 follow-up, KPI Phase 3)

    def fundamentals(self) -> list:
        return []  # the synthetic backtest node has no FMP feed (#182 follow-up, KPI Phase 2)

    def account(self):
        return None  # the synthetic backtest node has no live account (#41)

    def equity_curve(self) -> dict:
        return {}  # no broker, so no account history (#243) — empty means "not known", never a zero line

    def health(self) -> dict:
        # Synthetic node is always self-consistent (offline backtest) — report healthy for tests/dev.
        return {"bridge_ok": True, "engine_ok": True, "last_tick_ts": 0}

    def session(self) -> dict:
        return {}  # the synthetic backtest node runs no strategy (#212)

    async def request_stream(self, instrument_id: str) -> None:
        return  # synthetic node has no live engine to stream on demand — no-op

    async def send_command(self, ctype: str, payload: dict) -> str:
        # Synthetic node simulates a working engine (offline/demo/tests): mint a real id and record it so the
        # ack flow completes. It never places a real order — there's no broker here.
        cid = uuid.uuid4().hex
        self._synthetic_commands.add(cid)
        return cid

    def command_status(self, command_id: str) -> dict | None:
        # Only ids WE minted resolve (accepted); unknown ids → None → pending (never a false ack). No real
        # broker → no rejection path in synthetic mode.
        return {"id": command_id, "status": "ok", "type": "synthetic"} if command_id in self._synthetic_commands else None

    def event_tape(self) -> list[BarDTO | FillDTO]:
        """Bars and fills from all lanes, merged in event-time order (fills after the bar at ts)."""
        tagged: list[tuple[int, int, BarDTO | FillDTO]] = []
        for strategy in self._strategies:
            for bar in strategy.bars:
                tagged.append((bar.ts_event, 0, bar))  # bars sort before fills at equal ts
            for fill in strategy.fills:
                tagged.append((fill.ts_event, 1, fill))
        tagged.sort(key=lambda t: (t[0], t[1]))
        return [evt for _, _, evt in tagged]
