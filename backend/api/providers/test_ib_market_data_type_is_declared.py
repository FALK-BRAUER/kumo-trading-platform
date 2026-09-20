"""The IB market-data type is a `[data.ibkr]` key, and a bad one is refused rather than defaulted (#834).

MEASURED on ibkr-paper across the whole 2026-09-09 open. The adapter issues
`reqHistoricalData(keepUpToDate=True)` for every 1-MINUTE subscription — 112 of them, all accepted,
no error from IB — and delivered ZERO live bars in 40 open-market minutes. The same call for 1-DAY
pushed exactly one bar per symbol, yesterday's, stamped 00:00 UTC. That stamp is what the UI showed
as "feed 14h".

IB serves REAL-TIME market data to one session per user, and the operator's live TWS session holds
it; the paper gateway's request is refused SILENTLY. `market_data_type` was hardcoded REALTIME in
`build_data`, so no instance could ask for the DELAYED type that IB serves without the entitlement.

THE DEFAULT IS UNCHANGED. Every instance that does not set the key gets exactly what it got. Whether
delayed data is acceptable is an operator decision per instance — fine for a chart, wrong for an
entry price — so this file pins only that the key travels and that garbage is refused.
"""

from __future__ import annotations

import pytest
from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum

from api.providers.ibkr import build_data


def test_the_adapter_still_offers_DELAYED_so_this_key_has_a_reason_to_exist():
    """Fixture property: if the installed adapter dropped DELAYED, the escape this key provides is
    gone and the docstring above is a story about a different package."""
    names = {a for a in dir(IBMarketDataTypeEnum) if a.isupper()}
    assert {"REALTIME", "DELAYED"} <= names, names


def test_absent_means_REALTIME_exactly_as_before():
    """Inert unless set. A default that drifted would change every IBKR instance on the next deploy."""
    assert build_data({}).config.market_data_type == IBMarketDataTypeEnum.REALTIME


def test_the_key_TRAVELS_to_the_client_config():
    """Not merely parsed: the value has to reach the Nautilus config the client is built from, or the
    instance file says DELAYED while the gateway is asked for REALTIME — agreement without connection."""
    assert build_data({"market_data_type": "DELAYED"}).config.market_data_type == IBMarketDataTypeEnum.DELAYED


@pytest.mark.parametrize("bad", ["delayed", "Delayed", "DELAY", "3", ""])
def test_a_name_the_adapter_does_not_know_is_REFUSED_not_defaulted(bad):
    """A typo silently becoming REALTIME reproduces the silent-stream failure this key exists to
    escape — while the instance file reads as if delayed data had been chosen."""
    with pytest.raises(ValueError, match="market_data_type"):
        build_data({"market_data_type": bad})
