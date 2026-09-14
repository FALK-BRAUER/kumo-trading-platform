"""Offline tests for the Alpaca REST client's request shaping. No network: `_get` is captured.

Follows the repo's sync-test + `asyncio.run` pattern (no pytest-asyncio).

The `adjustment` assertions are a DATA-POLICY guard, not a wire-format nicety. Backtests run on raw
executable prices (kumo-strategies `docs/momentum-rotation-literature.md`): a split/dividend-adjusted
series silently rewrites history so a backtest fills at prices that were never tradable, and the error is
invisible in the output. Alpaca's own default is `raw`, but a default we don't assert is a default that
can be changed by accident — so the request is pinned, and pinned here.
"""

from __future__ import annotations

import asyncio
import inspect

from api.providers.alpaca.http import AlpacaHttpClient


def _client() -> AlpacaHttpClient:
    return AlpacaHttpClient(
        key="test-key",
        secret="test-secret",
        trading_base="https://paper-api.example",
        data_base="https://data.example",
    )


class _Capture:
    """Stands in for `AlpacaHttpClient._get` — records the call, returns an empty bar page."""

    def __init__(self) -> None:
        self.base: str | None = None
        self.path: str | None = None
        self.params: dict | None = None

    async def __call__(self, base: str, path: str, params: dict | None = None):
        self.base, self.path, self.params = base, path, params
        return {"bars": [], "next_page_token": None}


def _call_get_bars(**kwargs) -> _Capture:
    client, capture = _client(), _Capture()
    client._get = capture  # type: ignore[method-assign]
    asyncio.run(client.get_bars(**kwargs))
    return capture


def test_get_bars_always_requests_raw_prices() -> None:
    capture = _call_get_bars(symbol="SPY", timeframe="1Day", start="2005-01-01", end="2026-01-01")
    assert capture.params is not None
    # The whole raw-data policy reduces to this one parameter.
    assert capture.params["adjustment"] == "raw"


def test_get_bars_has_no_caller_route_to_adjusted_prices() -> None:
    """`get_bars` takes no `adjustment` argument — a caller cannot request `split`/`dividend`/`all` even by
    mistake. If this signature ever gains one, its default must stay `raw` and this test must be replaced
    with an equivalent guard, never deleted."""
    assert "adjustment" not in inspect.signature(AlpacaHttpClient.get_bars).parameters


def test_get_bars_passes_window_feed_and_paging_through() -> None:
    capture = _call_get_bars(
        symbol="EFA",
        timeframe="1Day",
        start="2005-01-01",
        end="2026-01-01",
        feed="sip",
        page_token="tok-2",
    )
    assert capture.base == "https://data.example"
    assert capture.path == "/v2/stocks/EFA/bars"
    assert capture.params == {
        "timeframe": "1Day",
        "start": "2005-01-01",
        "limit": 10000,
        "feed": "sip",
        "adjustment": "raw",
        "end": "2026-01-01",
        "page_token": "tok-2",
    }


def test_get_bars_omits_optional_params_when_unset() -> None:
    """An `end=None` must be ABSENT, not sent as null — Alpaca reads a present-but-empty bound as an
    error rather than 'now', which would fail the export instead of running to the latest session."""
    capture = _call_get_bars(symbol="SPY", timeframe="1Day", start="2005-01-01")
    assert capture.params is not None
    assert "end" not in capture.params
    assert "page_token" not in capture.params
