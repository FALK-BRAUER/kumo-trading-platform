"""Bar granularity spec shared by the engine (produces bars) and the UI consumer (reads them).

Maps the UI's chart granularities (1m/5m/15m/30m/1h/1d/1w, #58) to a Databento schema + the Nautilus
bar-type suffix, and back. Only the Nautilus `BarType` string is used downstream. The suffix also encodes
the aggregation SOURCE: 1m/1d are EXTERNAL (Alpaca streams them live); 5m/15m/30m/1h/1w are INTERNAL —
Nautilus's DataEngine aggregates them live from the trade tape (Alpaca has no live channel above 1-min).
The Databento schema column is best-effort and only consulted on the (currently inactive) databento path.
"""

from __future__ import annotations

import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

# granularity → (databento schema, Nautilus bar-type suffix)
#
# The Nautilus suffix encodes step+unit+price+aggregation-source. The aggregation source is the crux of
# live-ticking (#58): Alpaca's live WebSocket only streams 1-minute bars (`bars` channel) and daily bars
# (`dailyBars`). Every other granularity has NO live venue channel, so an EXTERNAL bar type would load
# history via REST and then FREEZE ("aggregation unsupported" warning in the data client).
#
# Fix = let Nautilus aggregate them itself. A bar type whose source is INTERNAL is NOT routed to the data
# client's `_subscribe_bars`; instead `DataEngine._handle_subscribe_bars` sees `is_internally_aggregated()`
# and builds a `TimeBarAggregator` (nautilus_trader/data/engine.pyx `_handle_subscribe_bars` →
# `_start_bar_aggregator`). For a plain (non-composite) LAST bar type that aggregator subscribes to the
# instrument's TRADE TICKS (`_subscribe_bar_aggregator` issues `SubscribeTradeTicks`) and builds the bar
# live from them — and we already `subscribe_trade_ticks` per symbol in engine_node. So 5m/15m/30m/1h/1w
# tick live with ZERO new data-client code: the client only ever receives EXTERNAL subscribe commands.
#
# Chosen INTERNAL-from-trades over the composite alternative (`5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL`,
# which aggregates from the live 1-min bar) because: (a) trades are the SAME source as the cockpit's live
# last-price plane (on_trade_tick), so the forming candle stays consistent with the live price and updates
# sub-second rather than once a minute; (b) it needs one canonical bar type — a composite tags history bars
# `…@1-MINUTE-EXTERNAL` but live bars `.standard()` (no `@`), splitting the cache/label round-trip.
#
# HISTORY is unaffected by the source: a plain `request_bars` forwards the full bar type straight to the
# client (engine.pyx `_handle_request_bars` → `_date_range_client_request` → `client.request_bars`, no
# auto-aggregation), and `_alpaca_timeframe` reads only step+unit → Alpaca REST serves 5Min/15Min/30Min/
# 1Hour/1Week natively. 1m + 1d stay EXTERNAL — Alpaca streams both live, so the venue bar is authoritative.
#
# The databento schema (tuple[0]) is used ONLY on the databento provider path (`_window` availability
# check); on Alpaca it is unused, so 5m/15m/30m map to the finest real schema (ohlcv-1m) as best-effort.
GRANULARITIES: dict[str, tuple[str, str]] = {
    "1m": ("ohlcv-1m", "1-MINUTE-LAST-EXTERNAL"),  # native live (Alpaca `bars`)
    "5m": ("ohlcv-1m", "5-MINUTE-LAST-INTERNAL"),  # aggregated live from trades
    "15m": ("ohlcv-1m", "15-MINUTE-LAST-INTERNAL"),
    "30m": ("ohlcv-1m", "30-MINUTE-LAST-INTERNAL"),
    "1h": ("ohlcv-1h", "1-HOUR-LAST-INTERNAL"),
    "1d": ("ohlcv-1d", "1-DAY-LAST-EXTERNAL"),  # native live (Alpaca `dailyBars`)
    "1w": ("ohlcv-1d", "1-WEEK-LAST-INTERNAL"),
}
DEFAULT_GRANULARITY = "1d"
DEFN_LOOKBACK = pd.Timedelta(days=5)  # window for the instrument-definition request

# reverse: Nautilus bar-type suffix → granularity, for the consumer to label incoming bars.
_SUFFIX_TO_GRANULARITY = {suffix: g for g, (_schema, suffix) in GRANULARITIES.items()}


def bar_type(instrument_id: InstrumentId, granularity: str = DEFAULT_GRANULARITY) -> BarType:
    _schema, suffix = GRANULARITIES[granularity]
    return BarType.from_str(f"{instrument_id}-{suffix}")


def granularity_of(bar_type_str: str) -> str:
    """'AAPL.XNAS-1-DAY-LAST-EXTERNAL' → '1d'. Falls back to the default if the suffix is unknown.

    Anchors on the '-' step boundary (`bar_type` always joins as f'{id}-{suffix}'). A naive endswith
    would mis-match '…-15-MINUTE-LAST-INTERNAL' to the '5-MINUTE-LAST-INTERNAL' (5m) suffix, since the
    5m suffix is a trailing substring of the 15m one.
    """
    for suffix, g in _SUFFIX_TO_GRANULARITY.items():
        if bar_type_str.endswith(f"-{suffix}"):
            return g
    return DEFAULT_GRANULARITY


# --------------------------------------------------------------------------------------------------
# WHICH GRANULARITIES A VENUE CAN ACTUALLY SERVE (#612)
# --------------------------------------------------------------------------------------------------
from dataclasses import dataclass, field  # noqa: E402


@dataclass(frozen=True)
class AggregationPlan:
    """Granularities this venue can serve, and the ones it cannot, with reasons."""

    usable: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    reasons: dict = field(default_factory=dict)

    def reason_for(self, granularity: str) -> str:
        return self.reasons.get(granularity, "")


def aggregation_plan(granularities: dict, *, streams_trade_ticks: bool | None) -> AggregationPlan:
    """Split a granularity table by what the venue can actually deliver.

    AN INTERNAL BAR IS BUILT FROM TRADE TICKS. Nautilus routes an internally-aggregated bar type to a
    `TimeBarAggregator` which subscribes to the instrument's TRADE TICKS
    (`_subscribe_bar_aggregator` issues `SubscribeTradeTicks`) — so a venue that streams none
    produces an empty series, forever, with no error. The reasoning in this module's header is
    written entirely about Alpaca's WebSocket, and that assumption was never declared.

    MEASURED 2026-08-31: paper subscribed `1-HOUR-LAST-INTERNAL` and `1-WEEK-LAST-INTERNAL`; staging
    subscribed neither, four of six sparklines were blank, and 11 of 22 held positions had no price
    because `_last_price_for` falls back to bars and there were none. Same code, one venue apart.

    EXTERNAL GRANULARITIES SURVIVE. m1 and d1 are venue-streamed and backfilled, so they work with no
    tape at all — and they are what the rotation strategies actually trade off. Refusing them would
    take a working stack dark to fix a display defect.

    UNKNOWN REFUSES. A provider that has not declared is exactly the state that produced this: the
    split assumed ticks because the venue it was written for had them.
    """
    usable, refused, reasons = [], [], {}
    for name, (_schema, suffix) in granularities.items():
        needs_ticks = "INTERNAL" in str(suffix)
        if needs_ticks and not streams_trade_ticks:
            refused.append(name)
            reasons[name] = (
                "internally aggregated from trade ticks, and this venue "
                + ("does not stream them" if streams_trade_ticks is False
                   else "has not declared whether it streams them")
            )
        else:
            usable.append(name)
    return AggregationPlan(tuple(usable), tuple(refused), reasons)
