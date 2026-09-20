"""Tests for the instrument-search catalog (#25) — ranking, catalog filters, TTL/refresh, failure modes.

Follows the repo's sync-test + `asyncio.run` pattern (no pytest-asyncio). Each test drives a real
`AlpacaInstrumentProvider`/`parse_equity` over a FAKE http client returning fixture Alpaca assets — so the
whole chain (tradable/venue filters, `Equity.info` name-carry, ranking) is exercised, not just the ranker.
"""

from __future__ import annotations

import asyncio

import pytest

from api.instrument_search import InstrumentSearchIndex

# Fixture universe. AAPL vs APP separates "app" symbol-exact (APP) from name-prefix (Apple) — the ranking
# case the plan's AC hinges on. NOPE has a blank name (→ symbol fallback); DEAD is untradable; CRYP is
# non-equity — both must be filtered out of the catalog.
ASSETS = [
    {"symbol": "AAPL", "name": "Apple Inc.", "class": "us_equity", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "APP", "name": "AppLovin Corporation", "class": "us_equity", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "MSFT", "name": "Microsoft Corporation", "class": "us_equity", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "F", "name": "Ford Motor Company", "class": "us_equity", "exchange": "NYSE", "tradable": True},
    {"symbol": "NOPE", "name": "", "class": "us_equity", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "DEAD", "name": "Delisted Co", "class": "us_equity", "exchange": "NASDAQ", "tradable": False},
    {"symbol": "CRYP", "name": "Crypto Thing", "class": "crypto", "exchange": "NASDAQ", "tradable": True},
]


class FakeClient:
    """Stands in for AlpacaHttpClient — records call count so refresh/backoff behaviour is observable."""

    def __init__(self, assets: list[dict], fail: bool = False) -> None:
        self.assets = assets
        self.fail = fail
        self.calls = 0

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def list_assets(self, status: str = "active", asset_class: str = "us_equity") -> list[dict]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("alpaca boom")
        return self.assets


class Clock:
    """Injectable monotonic clock for TTL tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_exact_symbol_ranks_first() -> None:
    idx = InstrumentSearchIndex(FakeClient(ASSETS))
    results = asyncio.run(idx.search("aapl", 20))
    assert results[0].instrument_id == "AAPL.XNAS"
    assert results[0].symbol == "AAPL"
    assert results[0].name == "Apple Inc."
    assert results[0].venue == "XNAS"


def test_name_prefix_hit() -> None:
    # "apple" matches only Apple's name (prefix) → Apple first.
    results = asyncio.run(InstrumentSearchIndex(FakeClient(ASSETS)).search("apple", 20))
    assert results[0].symbol == "AAPL"


def test_symbol_exact_outranks_name_prefix() -> None:
    # "app": APP is a symbol-exact hit (tier 1); Apple matches only by name prefix (tier 5) → APP first,
    # Apple present but below. This is the plan's AC nuance (`q=app` returns Apple in-list, not #1).
    results = asyncio.run(InstrumentSearchIndex(FakeClient(ASSETS)).search("app", 20))
    symbols = [r.symbol for r in results]
    assert symbols[0] == "APP"
    assert "AAPL" in symbols
    assert symbols.index("APP") < symbols.index("AAPL")


def test_blank_name_falls_back_to_symbol() -> None:
    results = asyncio.run(InstrumentSearchIndex(FakeClient(ASSETS)).search("nope", 20))
    assert results[0].symbol == "NOPE"
    assert results[0].name == "NOPE"


def test_untradable_and_non_equity_filtered() -> None:
    idx = InstrumentSearchIndex(FakeClient(ASSETS))
    assert asyncio.run(idx.search("dead", 20)) == []  # untradable
    assert asyncio.run(idx.search("cryp", 20)) == []  # non-equity


def test_empty_and_whitespace_query() -> None:
    idx = InstrumentSearchIndex(FakeClient(ASSETS))
    assert asyncio.run(idx.search("", 20)) == []
    assert asyncio.run(idx.search("   ", 20)) == []


def test_limit_clamped() -> None:
    idx = InstrumentSearchIndex(FakeClient(ASSETS))
    # substring "o" hits several names; limit respected and clamped into 1..50 without error.
    assert len(asyncio.run(idx.search("o", 1))) == 1
    assert len(asyncio.run(idx.search("o", 0))) == 1  # clamped up to 1
    asyncio.run(idx.search("o", 999))  # clamped down to 50, no raise


def test_ttl_refresh_reloads() -> None:
    async def body() -> None:
        clk = Clock()
        client = FakeClient(ASSETS)
        idx = InstrumentSearchIndex(client, ttl_seconds=10, clock=clk)
        await idx.search("aapl", 20)
        assert client.calls == 1
        clk.t += 5  # still fresh
        await idx.search("aapl", 20)
        assert client.calls == 1
        clk.t += 10  # now stale → reload
        await idx.search("aapl", 20)
        assert client.calls == 2

    asyncio.run(body())


def test_concurrent_searches_single_reload() -> None:
    async def body() -> None:
        client = FakeClient(ASSETS)
        idx = InstrumentSearchIndex(client)
        await asyncio.gather(*(idx.search("aapl", 20) for _ in range(5)))
        assert client.calls == 1  # lock double-check: waiters don't each reload

    asyncio.run(body())


def test_first_load_failure_raises() -> None:
    # No catalog ever loaded → surface the failure so the endpoint returns 503.
    with pytest.raises(Exception):
        asyncio.run(InstrumentSearchIndex(FakeClient([], fail=True)).search("aapl", 20))


def test_stale_served_when_reload_fails() -> None:
    async def body() -> None:
        clk = Clock()
        client = FakeClient(ASSETS)
        idx = InstrumentSearchIndex(client, ttl_seconds=10, clock=clk)
        await idx.search("aapl", 20)  # good load
        client.fail = True
        clk.t += 20  # stale → attempts reload → fails → keep stale rather than error
        results = await idx.search("aapl", 20)
        assert results and results[0].symbol == "AAPL"

    asyncio.run(body())
