"""Tests for the startup migration wait (#no-ticket — 2026-07-28 crash-loop fix). Offline: the DB ping and
the sleep are both stubbed, so these assert the retry POLICY, not Postgres itself."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.exc import OperationalError

from api.db import migrate


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(migrate.time, "sleep", slept.append)
    return slept


def _stub_ping(monkeypatch, outcomes):
    """Each call pops the next outcome: an exception instance is raised, None means success."""
    remaining = list(outcomes)

    async def _ping():
        result = remaining.pop(0)
        if isinstance(result, BaseException):
            raise result

    monkeypatch.setattr(migrate, "_ping", _ping)
    return remaining


def test_retries_until_postgres_answers(monkeypatch, no_sleep):
    # gaierror is an OSError — the exact failure when the compose hostname doesn't resolve.
    remaining = _stub_ping(monkeypatch, [OSError("Name or service not known"), None])

    migrate.wait_for_db(retry_seconds=10.0)

    assert remaining == []          # both outcomes consumed → it retried, then succeeded
    assert no_sleep == [10.0]       # waited exactly one 10s interval


def test_retries_on_sqlalchemy_operational_error(monkeypatch, no_sleep):
    _stub_ping(monkeypatch, [OperationalError("SELECT 1", None, Exception("refused")), None])

    migrate.wait_for_db(retry_seconds=10.0)

    assert no_sleep == [10.0]


def test_real_fault_is_not_retried(monkeypatch, no_sleep):
    """A non-connection error must propagate — retrying a broken migration forever hides the fault."""
    _stub_ping(monkeypatch, [ValueError("bad DSN")])

    with pytest.raises(ValueError):
        migrate.wait_for_db(retry_seconds=10.0)

    assert no_sleep == []


def test_logged_url_masks_the_password(monkeypatch, caplog):
    monkeypatch.setenv("KUMO_DATABASE_URL", "postgresql+asyncpg://kumo:s3cret@postgres:5432/kumo")
    _stub_ping(monkeypatch, [OSError("Name or service not known"), None])
    monkeypatch.setattr(migrate.time, "sleep", lambda _: None)

    with caplog.at_level(logging.ERROR):
        migrate.wait_for_db(retry_seconds=10.0)

    logged = caplog.text
    assert "s3cret" not in logged
    assert "postgres:5432/kumo" in logged


@pytest.mark.needs_services
def test_one_decision_per_slot_not_per_session(tmp_path):
    """kumo-strategies#29 / peer handoff 2026-08-15.

    The migration relaxes `(strategy_id, session)` to `(strategy_id, session, slot)` so a midday
    rotation can decide in a session that already decided at the open. What must NOT relax is the race
    guard: two concurrent session runs both read "no decision yet" before either commits, so the
    partial unique index is the only place that can refuse the second atomically.

    Verified against real Postgres in both directions, because an index that permits everything and an
    index that permits nothing are equally easy to ship and equally invisible in a schema dump.
    """
    import asyncio
    import os

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    sf = async_sessionmaker(create_async_engine(os.environ["KUMO_DATABASE_URL"]),
                            expire_on_commit=False)
    row = ("INSERT INTO exec_action_log (ts, strategy_id, session, kind, summary, detail, slot) "
           "VALUES (now(), 'TEST-MIG', '2026-08-15', 'decision', :s, '{}', :slot)")

    async def run():
        async with sf() as s:
            await s.execute(text("DELETE FROM exec_action_log WHERE strategy_id='TEST-MIG'"))
            await s.commit()

        # Two slots in one session: both accepted.
        async with sf() as s:
            await s.execute(text(row), {"s": "open", "slot": "open+5m"})
            await s.execute(text(row), {"s": "midday", "slot": "midday"})
            await s.commit()

        # The same slot twice: refused by the database, not by application code.
        #
        # THE EXECUTE IS INSIDE THE TRY, and that is the fix rather than a tidy-up. asyncpg raises the
        # unique violation when the INSERT is EXECUTED, not when the transaction commits — so with the
        # execute outside, the IntegrityError escaped above the handler and the test errored instead
        # of passing. It had never run: `needs_services` is deselected by default and no CI job
        # existed for it until #734 added one, so this has been broken since it was written.
        async with sf() as s:
            try:
                await s.execute(text(row), {"s": "retry", "slot": "midday"})
                await s.commit()
                raise AssertionError("a retry of a decided slot was accepted")
            except IntegrityError:
                await s.rollback()

        async with sf() as s:
            n = (await s.execute(
                text("SELECT count(*) FROM exec_action_log WHERE strategy_id='TEST-MIG'"))).scalar()
            await s.execute(text("DELETE FROM exec_action_log WHERE strategy_id='TEST-MIG'"))
            await s.commit()
        assert n == 2, f"expected exactly the two distinct slots, got {n}"

    asyncio.run(run())
