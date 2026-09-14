"""`_reconcile_stuck_managers` ignored its own precondition (#652 item 5).

Its docstring declares it "depends on the durable cache … without it, 'not in cache' after a restart
means nothing, since the whole cache reset, not that the order never reached the venue". The body
then reverted "not in cache" rows to ARMED WITHOUT checking `durable_cache_enabled()` — so on a
non-durable deployment a restart would re-arm a manager whose order may already be LIVE at the
venue, and the re-fired trigger places it twice. Absence must not be readable as permission: an
unreadable cache is "never told us", not "never applied".

Drives the REAL method (`UiFeedStrategy._reconcile_stuck_managers.__get__(fake_self)`) — the seam,
not a helper — with the DB layer doubled at the exact call surface the method uses.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import api.db.engine as db_engine
from api import engine_node
from api import managers as mg
from api.engine_node import UiFeedStrategy


class _NullSession:
    pass


class _SessionCM:
    async def __aenter__(self):
        return _NullSession()

    async def __aexit__(self, *exc):
        return False


def _drive(monkeypatch, *, durable: bool) -> list[str]:
    """One stuck APPLYING row whose recorded coid is NOT in the cache — the exact evidence the
    docstring says is meaningless without the durable cache. Returns the manager ids reverted."""
    row = SimpleNamespace(manager_id="m-stuck-1", leash="AUTO")
    reverted: list[str] = []

    async def applying_of_kind(session, kind, strategy_id):
        return [row]

    async def last_event_detail(session, manager_id, kind):
        return {"client_order_id": "O-20260829-PYR-1"}

    async def revert_to_armed(session, manager_id, detail=None):
        reverted.append(manager_id)

    async def record_event(session, *a, **k):  # pragma: no cover — the applied half, not this test's path
        raise AssertionError("coid is not in the cache — the applied half must not run")

    monkeypatch.setattr(mg, "applying_of_kind", applying_of_kind)
    monkeypatch.setattr(mg, "last_event_detail", last_event_detail)
    monkeypatch.setattr(mg, "revert_to_armed", revert_to_armed)
    monkeypatch.setattr(mg, "record_event", record_event)
    monkeypatch.setattr(db_engine, "session_factory", lambda: _SessionCM())
    monkeypatch.setattr(engine_node, "durable_cache_enabled", lambda: durable)

    fake_self = SimpleNamespace(
        id="MOMENTUM-001",
        _seen_orders=set(),
        _lookup_order=lambda coid: None,  # NOT in cache
    )
    bound = UiFeedStrategy._reconcile_stuck_managers.__get__(fake_self)
    asyncio.run(bound("pyramid_watch"))
    return reverted


def test_fixture_property_with_the_durable_cache_the_row_IS_reverted(monkeypatch):
    """The fixture must be able to reach the revert, or the refusal test below is vacuous. Note the
    method swallows every exception by design — a broken double here would ALSO produce an empty
    `reverted`, which is exactly why this half exists."""
    assert _drive(monkeypatch, durable=True) == ["m-stuck-1"]


def test_without_the_durable_cache_nothing_is_reverted_to_ARMED(monkeypatch, caplog):
    """Seen red pre-fix: the revert ran regardless of `durable_cache_enabled()`."""
    with caplog.at_level(logging.WARNING, logger="kumo.engine_node"):
        reverted = _drive(monkeypatch, durable=False)
    assert reverted == [], (
        "'not in cache' on a NON-durable deployment is 'the cache reset', not 'never applied' — "
        "reverting to ARMED re-fires an order that may already be live at the venue"
    )
    # Degrade LOUDLY: the skipped reconcile is its own named condition, not silence.
    assert any("KUMO_DURABLE_CACHE" in r.getMessage() for r in caplog.records), (
        "refusing to reconcile must say WHY, or 'nothing reconciled' reads as 'nothing was stuck'"
    )
