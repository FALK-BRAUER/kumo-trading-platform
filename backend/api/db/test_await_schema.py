"""The engine's schema gate (#205). Offline: the revision read and the sleep are both stubbed, so these
assert the WAITING POLICY, not Postgres."""

from __future__ import annotations

import pytest

from api.db import await_schema


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(await_schema.time, "sleep", slept.append)
    return slept


def _stub(monkeypatch, head, outcomes):
    """Each `_current()` call pops the next outcome; an exception instance is raised."""
    remaining = list(outcomes)

    async def _current():
        value = remaining.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(await_schema, "_current", _current)
    monkeypatch.setattr(await_schema, "head_revision", lambda: head)
    # Default: every revision the test names is one this image knows, so the rollback branch stays out
    # of the way unless a test opts into it.
    monkeypatch.setattr(await_schema, "known_revisions",
                        lambda: {v for v in outcomes if isinstance(v, str)} | {head})


def test_returns_immediately_when_the_schema_is_already_at_head(monkeypatch, no_sleep):
    _stub(monkeypatch, "0011", ["0011"])
    await_schema.wait_for_head()
    assert no_sleep == [], "waited despite the schema already being current"


def test_waits_while_the_database_is_behind_this_image(monkeypatch, no_sleep):
    """The whole point: 0011 ships with the code that reads its columns. Booting at 0010 would select
    a column that does not exist, mid-session, on a path holding real positions."""
    _stub(monkeypatch, "0011", ["0010", "0010", "0011"])
    await_schema.wait_for_head(retry_seconds=3.0)
    assert no_sleep == [3.0, 3.0], f"expected two waits before head, got {no_sleep}"


def test_an_unmigrated_database_is_waited_out_not_crashed_on(monkeypatch, no_sleep):
    """First-ever boot: `alembic_version` does not exist yet, so the read RAISES. Exiting here would
    crash-loop the engine; waiting recovers by itself once the api migrates."""
    _stub(monkeypatch, "0011", [RuntimeError("relation alembic_version does not exist"), "0011"])
    await_schema.wait_for_head(retry_seconds=1.0)
    assert no_sleep == [1.0]


def test_it_never_applies_a_migration_itself(monkeypatch):
    """Two containers racing `alembic upgrade head` is worse than the race being fixed. One writer (the
    api), one waiter (this). Pinned against a future 'helpful' edit."""
    import inspect
    src = inspect.getsource(await_schema)
    assert "command.upgrade" not in src and "upgrade(" not in src


def test_a_rollback_onto_a_newer_schema_starts_instead_of_waiting_forever(monkeypatch, no_sleep, caplog):
    """Deploying an older image against a database the newer one already migrated. Nothing moves a
    schema backwards, so waiting could never clear it — the gate would hold the engine down over a
    condition no operator action resolves. Migrations here add columns rather than drop them, so old
    code on a new schema is the survivable direction; start, and make it loud."""
    _stub(monkeypatch, "0010", ["0011"])
    monkeypatch.setattr(await_schema, "known_revisions", lambda: {"0010", "0009"})
    with caplog.at_level("ERROR"):
        await_schema.wait_for_head()
    assert no_sleep == [], "waited on a condition that can never clear"
    assert any("rollback" in r.message.lower() or "does not know" in r.message
               for r in caplog.records), "started silently on a schema mismatch"
