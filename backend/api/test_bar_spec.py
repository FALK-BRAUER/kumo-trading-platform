"""Unit tests for the bar granularity spec (#58).

Guards the two invariants the engine and consumer rely on:
  1. `bar_type` ↔ `granularity_of` round-trips for every granularity (no substring collision — the
     15m suffix trailing-contains the 5m suffix).
  2. The aggregation source is EXTERNAL only where Alpaca streams a live venue channel (1m, 1d), and
     INTERNAL everywhere else so Nautilus aggregates them live from trades (they'd otherwise freeze).
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AggregationSource
from nautilus_trader.model.identifiers import InstrumentId

from api.bar_spec import DEFAULT_GRANULARITY, GRANULARITIES, bar_type, granularity_of

_IID = InstrumentId.from_str("AAPL.XNAS")

# Alpaca streams only these two live; everything else must aggregate internally to tick live.
_EXTERNAL_GRANULARITIES = {"1m", "1d"}


def test_expected_granularities_present():
    assert set(GRANULARITIES) == {"1m", "5m", "15m", "30m", "1h", "1d", "1w"}


@pytest.mark.parametrize("granularity", list(GRANULARITIES))
def test_bar_type_granularity_round_trips(granularity):
    bt = bar_type(_IID, granularity)
    # The suffix must be a parseable BarType and label back to the same granularity.
    assert granularity_of(str(bt)) == granularity


def test_granularity_of_no_substring_collision():
    # Regression: '15-MINUTE-LAST-INTERNAL' must NOT resolve to 5m (its suffix trailing-contains the
    # 5m suffix). Anchoring on the '-' step boundary is what prevents the mis-match.
    assert granularity_of("AAPL.XNAS-15-MINUTE-LAST-INTERNAL") == "15m"
    assert granularity_of("AAPL.XNAS-5-MINUTE-LAST-INTERNAL") == "5m"


def test_granularity_of_unknown_falls_back_to_default():
    assert granularity_of("AAPL.XNAS-3-SECOND-LAST-EXTERNAL") == DEFAULT_GRANULARITY


@pytest.mark.parametrize("granularity", list(GRANULARITIES))
def test_aggregation_source_matches_alpaca_live_channels(granularity):
    bt = bar_type(_IID, granularity)
    if granularity in _EXTERNAL_GRANULARITIES:
        assert bt.aggregation_source == AggregationSource.EXTERNAL
        assert not bt.is_internally_aggregated()
    else:
        # INTERNAL → DataEngine builds a TimeBarAggregator from trades; never hits the data client.
        assert bt.aggregation_source == AggregationSource.INTERNAL
        assert bt.is_internally_aggregated()


@pytest.mark.parametrize("granularity", list(GRANULARITIES))
def test_bar_type_is_parseable(granularity):
    # `bar_type` must always yield a BarType that BarType.from_str round-trips (no malformed suffix).
    bt = bar_type(_IID, granularity)
    assert BarType.from_str(str(bt)) == bt
