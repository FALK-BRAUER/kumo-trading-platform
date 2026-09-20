"""Alpaca provider package (data client + instrument provider).

Increment A (done): config, REST http client, instrument provider.
Increment B (done): live data client + REST historical bars (`_request_bars`) + factory + `build_data`.
Increment C (market-open): WS live client (`_subscribe_bars`/trades/quotes) — replaces the subscribe stubs.
"""

from api.providers.alpaca.config import AlpacaDataClientConfig, AlpacaExecClientConfig
from api.providers.alpaca.data_client import (
    AlpacaDataClient,
    AlpacaLiveDataClientFactory,
    build_data,
)
from api.providers.alpaca.exec_client import (
    AlpacaExecutionClient,
    AlpacaLiveExecClientFactory,
)
from api.providers.alpaca.exec_client import (
    build as build_exec,
)
from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.alpaca.providers import AlpacaInstrumentProvider, parse_equity

__all__ = [
    "AlpacaDataClient",
    "AlpacaDataClientConfig",
    "AlpacaExecClientConfig",
    "AlpacaExecutionClient",
    "AlpacaHttpClient",
    "AlpacaInstrumentProvider",
    "AlpacaLiveDataClientFactory",
    "AlpacaLiveExecClientFactory",
    "build_data",
    "build_exec",
    "parse_equity",
]
