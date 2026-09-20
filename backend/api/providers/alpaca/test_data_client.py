"""Offline unit tests for the Alpaca data client's pure logic — timeframe mapping + bar parsing.

No network: exercises the wire-shape translation that the live smoke test can't assert deterministically.
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from api.providers.alpaca.data_client import AlpacaDataClient, _alpaca_timeframe

_AAPL = Equity(
    instrument_id=InstrumentId(Symbol("AAPL"), Venue("XNAS")),
    raw_symbol=Symbol("AAPL"),
    currency=USD,
    price_precision=2,
    price_increment=Price.from_str("0.01"),
    lot_size=Quantity.from_int(1),
    ts_event=0,
    ts_init=0,
)


@pytest.mark.parametrize(
    ("bar_type_str", "expected"),
    [
        ("AAPL.XNAS-1-MINUTE-LAST-EXTERNAL", "1Min"),
        ("AAPL.XNAS-1-HOUR-LAST-EXTERNAL", "1Hour"),
        ("AAPL.XNAS-1-DAY-LAST-EXTERNAL", "1Day"),
        ("AAPL.XNAS-5-MINUTE-LAST-EXTERNAL", "5Min"),
        # Live-aggregated granularities (#58): the timeframe is source-agnostic — INTERNAL bar types
        # still resolve to the native Alpaca REST timeframe for their history request.
        ("AAPL.XNAS-5-MINUTE-LAST-INTERNAL", "5Min"),
        ("AAPL.XNAS-15-MINUTE-LAST-INTERNAL", "15Min"),
        ("AAPL.XNAS-30-MINUTE-LAST-INTERNAL", "30Min"),
        ("AAPL.XNAS-1-HOUR-LAST-INTERNAL", "1Hour"),
        ("AAPL.XNAS-1-WEEK-LAST-INTERNAL", "1Week"),
    ],
)
def test_alpaca_timeframe_maps_supported_aggregations(bar_type_str, expected):
    assert _alpaca_timeframe(BarType.from_str(bar_type_str)) == expected


def test_alpaca_timeframe_rejects_unsupported_aggregation():
    # SECOND has no Alpaca timeframe → must raise, not silently mis-map.
    with pytest.raises(ValueError):
        _alpaca_timeframe(BarType.from_str("AAPL.XNAS-1-SECOND-LAST-EXTERNAL"))


def test_parse_bar_maps_ohlcv_and_timestamp():
    bt = BarType.from_str("AAPL.XNAS-1-DAY-LAST-EXTERNAL")
    raw = {"t": "2024-01-02T14:30:00Z", "o": 187.15, "h": 188.44, "l": 183.89, "c": 185.64, "v": 82488200}
    bar = AlpacaDataClient._parse_bar(bt, _AAPL, raw)

    assert bar.bar_type == bt
    assert bar.open == Price.from_str("187.15")
    assert bar.high == Price.from_str("188.44")
    assert bar.low == Price.from_str("183.89")
    assert bar.close == Price.from_str("185.64")
    assert bar.volume == Quantity.from_int(82488200)
    assert bar.ts_event == 1704205800000000000  # 2024-01-02T14:30:00Z in epoch ns
    assert bar.ts_event == bar.ts_init


def test_parse_bar_rounds_to_instrument_precision():
    # Alpaca can return more decimals than the equity's 2dp tick → parse must round to precision.
    bt = BarType.from_str("AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    raw = {"t": "2024-01-02T14:30:00Z", "o": 187.157, "h": 187.157, "l": 187.157, "c": 187.157, "v": 10}
    bar = AlpacaDataClient._parse_bar(bt, _AAPL, raw)
    assert bar.close == Price.from_str("187.16")


def test_parse_bar_refuses_a_bar_with_no_volume():
    """It used to substitute a real ZERO for an absent volume. Nautilus `Quantity` cannot express
    "unknown", so the choice is between inventing a number and refusing the record — and the
    invented one flows into the Vol KPI, into sorting, and into anything reading volume, where a
    genuine zero-volume bar and a bar whose volume never arrived are indistinguishable.

    A dropped bar leaves a visible hole and is logged. A fabricated zero looks like data."""
    bt = BarType.from_str("AAPL.XNAS-1-DAY-LAST-EXTERNAL")
    raw = {"t": "2024-01-02T14:30:00Z", "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0}
    assert AlpacaDataClient._parse_bar(bt, _AAPL, raw) is None


def test_parse_bar_refuses_a_bar_missing_any_ohlc_field():
    bt = BarType.from_str("AAPL.XNAS-1-DAY-LAST-EXTERNAL")
    base = {"t": "2024-01-02T14:30:00Z", "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 5}
    for k in ("o", "h", "l", "c", "v"):
        raw = {kk: vv for kk, vv in base.items() if kk != k}
        assert AlpacaDataClient._parse_bar(bt, _AAPL, raw) is None, f"accepted a bar missing {k}"
    assert AlpacaDataClient._parse_bar(bt, _AAPL, base) is not None
