"""Offline tests for the custom Nautilus `Data` types (#182 follow-up) — FundamentalsData construction/
serialization and the DataType topic derivation that makes publish/subscribe actually match."""

from __future__ import annotations

import math

from nautilus_trader.model.identifiers import InstrumentId

from api.data_types import FundamentalsData, fundamentals_data_type, nan_if_none


def _fund(**overrides) -> FundamentalsData:
    defaults = dict(
        ts_event=1, ts_init=1,
        instrument_id=InstrumentId.from_str("AAPL.XNAS"),
        market_cap=1_000.0, beta=1.1, eps=8.2, pe=37.5, dividend_amount=1.05, as_of="2026-07-27",
    )
    defaults.update(overrides)
    return FundamentalsData(**defaults)


def test_nan_if_none():
    assert nan_if_none(1.0) == 1.0
    assert math.isnan(nan_if_none(None))


def test_fundamentals_data_constructs_and_round_trips_through_dict():
    d = _fund()
    assert d.instrument_id == InstrumentId.from_str("AAPL.XNAS")
    assert d.market_cap == 1_000.0
    payload = d.to_dict()
    assert payload["instrument_id"] == "AAPL.XNAS"
    assert payload["ts_event"] == 1
    restored = FundamentalsData.from_dict(payload)
    assert restored.instrument_id == d.instrument_id
    assert restored.market_cap == d.market_cap


def test_fundamentals_data_round_trips_through_bytes_with_nan_fields():
    # The Arrow-schema-safe missing-value convention (module docstring) — NaN must survive a real
    # serialize/deserialize round trip, not just construction.
    d = _fund(beta=math.nan, pe=math.nan)
    restored = FundamentalsData.from_bytes(d.to_bytes())
    assert math.isnan(restored.beta)
    assert math.isnan(restored.pe)
    assert restored.eps == 8.2  # non-NaN fields unaffected


def test_fundamentals_data_type_topic_matches_for_the_same_instrument():
    # Nautilus derives the msgbus topic from DataType.metadata — publisher and subscriber must build the
    # SAME DataType (same metadata) for publish_data/subscribe_data to actually connect (verified against
    # nautilus_trader's own data_topics.pyx: get_custom_data_topic uses data_type.topic when metadata is
    # present, ignoring the separately-passed instrument_id param entirely).
    dt1 = fundamentals_data_type("AAPL.XNAS")
    dt2 = fundamentals_data_type("AAPL.XNAS")
    assert dt1.topic == dt2.topic
    assert dt1 == dt2


def test_fundamentals_data_type_topic_differs_across_instruments():
    dt_aapl = fundamentals_data_type("AAPL.XNAS")
    dt_msft = fundamentals_data_type("MSFT.XNAS")
    assert dt_aapl.topic != dt_msft.topic
