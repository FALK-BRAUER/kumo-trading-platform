"""A fresh instance must be able to create its FIRST pool source, deliberately.

`api.pool.refresh_source` hardcodes `create=False` — correct, and the whole reason the wrapper exists:
a refresh naming `typo_watchlist` must not invent a source that is fresh, real and feeds nothing while
the source it was meant to refresh ages into a hard block on deciding.

But the surface offered NO way to say "yes, create it". ibkr-paper-retired's pool was empty, seeding it
returned 404 with upstream's own advice — "Pass create=True to add it deliberately" — and nothing
could. An enabled rotation with an empty pool raises at build and takes the whole node down, MANUAL
included, so "cannot seed" means "cannot run this instance at all" (2026-08-24).

Same class as the three migrations that altered tables a fresh database did not have: a path that
works everywhere it has already worked, and nowhere new.
"""
from __future__ import annotations

import asyncio

import pytest

import api.pool


class UnknownSource(KeyError):
    """Upstream's exception, by shape: raised when the source is absent and create was not asked for."""


class _ShrinkRejected(Exception):
    """Only so the endpoint's second `except` resolves; this file never triggers it."""


class FakePool:
    """A double that REFUSES what production refuses.

    `check_source_known(source, known=..., create=...)` in kumo_strategies raises unless the source
    exists OR create was passed. A double that accepted every call would let the endpoint pass this
    file while still being unable to seed a fresh instance — which is exactly how four drifted doubles
    shipped green defects this week.
    """

    def __init__(self, known: set[str]):
        self.known = known
        self.calls: list[dict] = []

    async def refresh_source(self, source, symbols, *, detail=None, create=True, allow_shrink=None):
        self.calls.append({"source": source, "create": create, "n": len(symbols)})
        if source not in self.known and not create:
            raise UnknownSource(f"{source!r} is not a known pool source. Pass create=True")
        self.known.add(source)
        return len(symbols)


@pytest.fixture
def fake(monkeypatch):
    p = FakePool(known={"ledger_book"})
    monkeypatch.setattr(api.pool, "_pool", lambda: p)
    return p


def test_the_double_refuses_an_unknown_source_when_create_is_not_asked_for(fake):
    """The fixture's own property first — otherwise every assertion below passes vacuously."""
    import asyncio
    with pytest.raises(UnknownSource):
        asyncio.run(fake.refresh_source("brand_new", ["AAPL"], create=False))
    assert asyncio.run(fake.refresh_source("ledger_book", ["AAPL"], create=False)) == 1


def test_a_known_source_is_still_refreshed_without_create(fake):
    import asyncio
    asyncio.run(api.pool.refresh_source("ledger_book", ["AAPL", "MSFT"]))
    assert fake.calls[-1]["create"] is False, "the strict default must survive this change"


def test_a_new_source_can_be_created_when_asked_explicitly(fake):
    """The bug: there was no way to express this, so a fresh instance could not seed its first source."""
    import asyncio
    asyncio.run(api.pool.refresh_source("my_watchlist", ["NVDA"], create=True))
    assert fake.calls[-1]["create"] is True
    assert "my_watchlist" in fake.known


def test_create_defaults_to_false_so_a_typo_still_cannot_invent_a_source(fake):
    """The discriminating half. Making creation POSSIBLE must not make it the default."""
    import asyncio
    with pytest.raises(UnknownSource):
        asyncio.run(api.pool.refresh_source("typo_watchlist", ["AAPL"]))
    assert fake.calls[-1]["create"] is False


def test_the_ENDPOINT_carries_create_through_to_the_pool(fake, monkeypatch):
    """THE SEAM. A field the model accepts and the endpoint drops passes every test above.

    That is the exact shape that shipped five production breaks on 2026-08-14: the unit correct, the
    wiring absent. Drive the real route, assert on what the pool was actually called with.
    """
    body, route = _route(monkeypatch)
    asyncio.run(route("brand_new_source", body(symbols=["NVDA"], create=True)))
    assert fake.calls[-1] == {"source": "brand_new_source", "create": True, "n": 1}


def test_the_endpoint_still_404s_a_typo_when_create_is_absent(fake, monkeypatch):
    """The discriminating half at the seam: the default must survive the new field."""
    from fastapi import HTTPException

    body, route = _route(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route("typo_source", body(symbols=["NVDA"])))
    assert exc.value.status_code == 404
    assert fake.calls[-1]["create"] is False


def _route(monkeypatch):
    """The real endpoint function and the real request model, with only the DB read stubbed.

    `refresh_pool_source` ends by returning `get_pool()`, which queries. Everything the test is about
    happens before that line.
    """
    import api.app as app_mod
    from api.models import PoolSourceRefreshRequest

    monkeypatch.setattr(app_mod, "pool", api.pool)

    # THE ENDPOINT IMPORTS UnknownSource FROM kumo_strategies INSIDE THE FUNCTION, and its `except`
    # matches by identity. This venv's kumo_strategies is OLDER than the deployed one and has no such
    # name — so the double has to BE the class the endpoint will resolve, not merely resemble it.
    # A double raising a look-alike would sail past the except clause and report no 404 at all.
    from kumo_strategies.runtime.executor import pgpool as real_pgpool

    monkeypatch.setattr(real_pgpool, "UnknownSource", UnknownSource, raising=False)
    monkeypatch.setattr(real_pgpool, "ShrinkRejected", _ShrinkRejected, raising=False)

    async def _no_db():
        return None

    monkeypatch.setattr(app_mod, "get_pool", _no_db)
    return PoolSourceRefreshRequest, app_mod.refresh_pool_source
