"""Applying a distribution must be atomic, conserved, and land exactly once.

WHY THIS FILE EXISTS
--------------------
`plan_distribution` is pure and tested separately. This is the half that touches the book, and the two
failure modes it has are ones only a database can enforce:

  APPLIED TWICE   a retried or replayed request funds every sleeve a second time. The caps cannot catch
                  it, because each application looked individually correct — the same argument
                  `Transfer.fill_id` already makes for fills, and the reason uniqueness lives in the
                  table rather than in an in-memory set that forgets across the restart when
                  reconciliation replays hardest.

  APPLIED HALFWAY the source is debited and only some recipients credited, leaving the book unbalanced
                  against the account with no record of how far it got.

Marked `needs_services` like its siblings: the guarantee IS the unique index, so an in-memory double
would be testing a more forgiving model of the thing rather than the thing.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

_IDS = ["D-SHORT-A", "D-SHORT-B", "D-OVER", "UNALLOCATED"]


def _sf():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    return async_sessionmaker(engine, expire_on_commit=False)


async def _clean(sf) -> None:
    async with sf() as s:
        await s.execute(text("DELETE FROM sleeve_transfer WHERE fill_id LIKE 'dist-test-%'"))
        await s.execute(text("DELETE FROM strategy_sleeve WHERE strategy_id = ANY(:i)"), {"i": _IDS})
        await s.commit()


def _targets(monkeypatch, mapping: dict[str, float], registered: set[str] | None = None) -> None:
    """Targets come from SETTINGS, not from the sleeve table.

    `strategy_sleeve.target` is VESTIGIAL and `load_book` does not read it (`budget_store.py:48`). The
    first version of these tests seeded that column and every headroom came back zero, so nothing was
    ever distributed and two tests failed for a reason that had nothing to do with the code under test.
    A double that cannot supply a target cannot represent production.
    """
    import sys

    import api

    fake = type("m", (), {"resolve": staticmethod(lambda domain: dict(mapping))})

    # THE REGISTRY TOO. `_targets` now intersects the settings domain with the registered strategy ids,
    # because settings FLAGS (`QC345_ENABLED`) were arriving as zero-target strategies and could be
    # funded once switched on. That filter is production behaviour, so a double that supplies targets
    # without supplying the registry is not representing production — it hands over targets the real
    # code would drop, and every test using a synthetic strategy id silently distributes nothing.
    import api.strategy_registry as _reg

    entry = type("e", (), {})
    # `registered` defaults to every key, which is right for tests that only pass strategies. A test
    # asserting that settings FLAGS cannot be funded must pass it explicitly — otherwise the double
    # registers `QC345_ENABLED` as a strategy and defeats the very filter under test.
    fake_registry = tuple(
        type("E", (entry,), {"strategy_id": sid})()
        for sid in (mapping if registered is None else registered)
    )
    monkeypatch.setattr(_reg, "REGISTRY", fake_registry, raising=True)
    # BOTH bindings. `_targets` does `from api import settings`, which resolves the ATTRIBUTE on the
    # `api` package — so once any other test has imported the real module, patching `sys.modules` alone
    # is ignored and the stub silently does nothing. These tests passed in isolation and failed the
    # moment they ran after `test_budget_store.py`, which is the signature of a double that only works
    # when nothing else has loaded.
    monkeypatch.setitem(sys.modules, "api.settings", fake)
    monkeypatch.setattr(api, "settings", fake, raising=False)


async def _seed(sf, rows) -> None:
    """Only `actual` — the one field the table actually owns."""
    from decimal import Decimal

    from api.db.models import StrategySleeve

    async with sf() as s:
        for sid, _target, actual in rows:
            s.add(StrategySleeve(strategy_id=sid, target=Decimal(0), actual=Decimal(str(actual))))
        await s.commit()


async def _book(sf) -> dict[str, float]:
    from api.budget_store import load_book

    async with sf() as s:
        b = await load_book(s)
        return {sid: float(sl.actual) for sid, sl in b.sleeves.items() if sid in _IDS}


@pytest.mark.needs_services
def test_a_distribution_conserves_capital_and_lands_where_it_is_short(monkeypatch):
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        # A is 15k short, B is 5k short, OVER is above target and must receive nothing.
        _targets(monkeypatch, {"D-SHORT-A": 20000, "D-SHORT-B": 20000, "D-OVER": 20000})
        await _seed(sf, [("D-SHORT-A", 20000, 5000), ("D-SHORT-B", 20000, 15000),
                         ("D-OVER", 20000, 30000), ("UNALLOCATED", 0, 8000)])
        before = await _book(sf)

        async with sf() as s:
            allocs = await distribute_unallocated(s, actor="test", run_id="dist-test-1")
            await s.commit()

        after = await _book(sf)
        by = {a.to_strategy: a.amount for a in allocs}
        assert by == {"D-SHORT-A": pytest.approx(6000.0), "D-SHORT-B": pytest.approx(2000.0)}, by
        assert after["D-OVER"] == before["D-OVER"], "an over-target sleeve received capital"
        assert after["UNALLOCATED"] == pytest.approx(before["UNALLOCATED"] - 8000.0)
        assert sum(after.values()) == pytest.approx(sum(before.values())), (
            f"capital was created or destroyed: {before} -> {after}"
        )
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_a_replayed_distribution_applies_exactly_once(monkeypatch):
    """The guard only the database can enforce. Same run id twice must be a no-op, not a second funding."""
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        # UNALLOCATED must still hold capital on the SECOND call, or the overdraft cap refuses it before
        # the uniqueness guard is ever reached — and the replay would "pass" without testing idempotency
        # at all. 10,000 in the source, 5,000 handed out per call: the second call is affordable and must
        # be refused for being a REPLAY, not for being unaffordable.
        _targets(monkeypatch, {"D-SHORT-A": 20000})
        await _seed(sf, [("D-SHORT-A", 20000, 0), ("UNALLOCATED", 0, 10000)])

        # `available=` EXPLICITLY on both calls. The first draft let the second call read UNALLOCATED,
        # which the first call had just emptied — so it returned [] because there was no capital, never
        # reaching the uniqueness guard at all. The mutation that removes idempotency stayed green.
        # Assert the fixture can violate the invariant before asserting it does not.
        async with sf() as s:
            first = await distribute_unallocated(s, actor="test", run_id="dist-test-replay", available=5000)
            await s.commit()
        assert first, "the first distribution moved nothing — the replay proves nothing"
        once = await _book(sf)

        async with sf() as s:
            second = await distribute_unallocated(s, actor="test", run_id="dist-test-replay", available=5000)
            await s.commit()
        twice = await _book(sf)

        assert second == [], "a replayed distribution reported allocations"
        assert twice == once, f"the replay moved capital again: {once} -> {twice}"
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_a_sleeve_with_no_row_yet_is_created_rather_than_skipped(monkeypatch):
    """A strategy can be given a target before it has ever traded. Skipping it would silently starve the
    newest strategy — the one most likely to need funding."""
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        _targets(monkeypatch, {"D-SHORT-B": 10000})
        await _seed(sf, [("UNALLOCATED", 0, 4000)])
        # DELIBERATELY NO ROW for D-SHORT-B. The first draft created one, so `row is None` never
        # happened and skipping missing sleeves entirely stayed green. A strategy given a target before
        # it has ever traded has no sleeve row, and that is the one most likely to need funding.
        async with sf() as s:
            existing = (await s.execute(
                text("SELECT count(*) FROM strategy_sleeve WHERE strategy_id = 'D-SHORT-B'"))).scalar()
            assert existing == 0, "the fixture already has a row — it cannot exercise creation"

        async with sf() as s:
            allocs = await distribute_unallocated(s, actor="test", run_id="dist-test-new")
            await s.commit()

        after = await _book(sf)
        assert [a.to_strategy for a in allocs] == ["D-SHORT-B"]
        assert after["D-SHORT-B"] == pytest.approx(4000.0)
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_nothing_to_distribute_is_a_no_op_not_an_error(monkeypatch):
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        _targets(monkeypatch, {"D-SHORT-A": 20000})
        await _seed(sf, [("D-SHORT-A", 20000, 20000), ("UNALLOCATED", 0, 0)])
        before = await _book(sf)

        async with sf() as s:
            allocs = await distribute_unallocated(s, actor="test", run_id="dist-test-empty")
            await s.commit()

        assert allocs == []
        assert await _book(sf) == before
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_more_than_the_source_holds_is_refused_outright(monkeypatch):
    """`available` arrives from a request body, and overdrafting it is the worst defect this file guards.

    Without the cap, `POST /sleeves/distribute {"available": 200000}` credits every sleeve and drives
    UNALLOCATED negative. The book stays "conserved" on paper while the account is not: each funded
    sleeve's `deployable` then authorises notional backed only by margin, and Alpaca reports buying
    power near 3x equity — so those orders FILL rather than reject. The budget cap is the one mechanism
    bounding automated buying, and a request body could raise it past the account.

    Refused loudly rather than silently clamped: an operator who asked to move 200k and moved 39k should
    be told, not left to discover it in the book.
    """
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        _targets(monkeypatch, {"D-SHORT-A": 100000})
        await _seed(sf, [("D-SHORT-A", 100000, 0), ("UNALLOCATED", 0, 5000)])
        before = await _book(sf)

        async with sf() as s:
            with pytest.raises(ValueError, match="cannot distribute"):
                await distribute_unallocated(s, actor="test", run_id="dist-test-over", available=200000)

        assert await _book(sf) == before, "capital moved despite the refusal"
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_a_settings_flag_can_never_become_a_funded_sleeve(monkeypatch):
    """`QC345_ENABLED` was allocated $1 and a sleeve row was CREATED for it.

    The `strategies` settings domain holds knobs as well as budgets, and `float(False)` is 0.0 — so
    every boolean arrived as a zero-target strategy, harmless only while the flags were off. With the
    flag on, capital left UNALLOCATED for an address `/strategies` does not render and no strategy can
    spend from. The next numeric knob added to that domain would have been funded at its full value.
    """
    async def run():
        from api.budget_store import distribute_unallocated

        sf = _sf()
        await _clean(sf)
        # Exactly what `settings.resolve("strategies")` returns: budgets AND knobs, mixed.
        _targets(monkeypatch, {"D-SHORT-A": 20000, "QC345_ENABLED": True, "QC345_UNIVERSE_REFRESH": True,
                               "TRANSFER_TO": "", "MOMENTUM-002_SLOTS": []},
                 registered={"D-SHORT-A"})
        await _seed(sf, [("D-SHORT-A", 20000, 0), ("UNALLOCATED", 0, 5000)])

        async with sf() as s:
            allocs = await distribute_unallocated(s, actor="test", run_id="dist-test-flags")
            await s.commit()

        funded = {a.to_strategy for a in allocs}
        assert "QC345_ENABLED" not in funded and "QC345_UNIVERSE_REFRESH" not in funded, (
            f"a settings flag was funded as a strategy: {funded}"
        )
        async with sf() as s:
            rows = (await s.execute(
                text("SELECT strategy_id FROM strategy_sleeve WHERE strategy_id LIKE 'QC345\\_%'"))).scalars().all()
        assert rows == [], f"sleeve rows were created for settings keys: {rows}"
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_the_route_actually_persists_what_it_reports(monkeypatch):
    """THE DEFECT THAT MADE THE WHOLE FEATURE INERT.

    `POST /sleeves/distribute` ran `async with session_factory() as session:` and never committed.
    `distribute_unallocated` only flushes, and `AsyncSession.__aexit__` calls `close()`, which ROLLS
    BACK. The route answered `200 {"distributed": 30000.0}` with an empty database behind it: no
    transfer row, no `actual` moved, BCTROT still at `deployable 0` — and the next call reporting the
    same $30,000 again, forever.

    The route's own unit test walks its AST for the call to the primitive. That assertion is true and
    cannot see a missing commit, which is exactly why this one re-reads the book in a SEPARATE session:
    the only thing that distinguishes a committed transaction from a rolled-back one.
    """
    async def run():
        from api.app import distribute_unallocated_capital

        sf = _sf()
        await _clean(sf)
        _targets(monkeypatch, {"D-SHORT-A": 20000})
        await _seed(sf, [("D-SHORT-A", 20000, 0), ("UNALLOCATED", 0, 6000)])

        result = await distribute_unallocated_capital({"run_id": "dist-test-route", "actor": "test"})
        assert result["distributed"] == pytest.approx(6000.0), result

        after = await _book(sf)  # a NEW session — a rolled-back transaction shows nothing here
        assert after["D-SHORT-A"] == pytest.approx(6000.0), (
            f"the route reported {result['distributed']} and the database says {after} — "
            f"the transaction was never committed"
        )
        assert after["UNALLOCATED"] == pytest.approx(0.0)
        await _clean(sf)

    asyncio.run(run())
