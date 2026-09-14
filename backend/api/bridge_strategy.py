"""BridgeStrategy — the seam between the Nautilus engine and the API layer.

Out-of-tree `Strategy` subclass (Nautilus stays pinned/unforked — we extend, never modify).
It does the minimal trading needed for the #4 slice (one market BUY on the first bar, tagged to
the MANUAL lane) and records every `on_bar` / `on_order_filled` event as an API DTO onto a tape.
The NodeManager owns the tape; the WS layer paces it out to the UI.
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from api.models import BarDTO, FillDTO


class BridgeStrategyConfig(StrategyConfig, frozen=True):
    bar_type: BarType
    trade_size: int = 100
    # Default False: no autonomous BUY unless explicitly unlocked (carry-over live-safety rule).
    buy_on_first_bar: bool = False


class BridgeStrategy(Strategy):
    """Subscribe to one instrument's bars; record bars + fills as API DTOs."""

    def __init__(self, config: BridgeStrategyConfig) -> None:
        super().__init__(config)
        self.bars: list[BarDTO] = []
        self.fills: list[FillDTO] = []
        self._bought = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.bar_type.instrument_id)
        self.subscribe_bars(self.config.bar_type)
        self.log.info(f"BridgeStrategy START — watching {self.config.bar_type}")

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(
            BarDTO(
                instrument_id=str(self.instrument.id),
                ts_event=bar.ts_event,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
            )
        )
        if self.config.buy_on_first_bar and not self._bought:
            self._bought = True
            order = self.order_factory.market(
                instrument_id=self.instrument.id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(self.config.trade_size),
            )
            self.submit_order(order)
            self.log.info(f"SUBMITTED market BUY {self.config.trade_size} @ {bar.close}")

    def on_order_filled(self, event: OrderFilled) -> None:
        self.fills.append(
            FillDTO(
                instrument_id=str(event.instrument_id),
                side=event.order_side.name,
                quantity=float(event.last_qty),
                price=float(event.last_px),
                ts_event=event.ts_event,
                order_type=event.order_type.name,
                strategy_id=str(event.strategy_id),
            )
        )
        self.log.info(f"FILLED {event.order_side.name} {event.last_qty} @ {event.last_px}")
