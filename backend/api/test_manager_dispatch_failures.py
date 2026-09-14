"""Manager dispatch: a DB blip after the claim must be loud, and the tick's future inspected (#651 item 4).

Two faces of one silence. `run_coroutine_threadsafe(self._dispatch_all_managers(), ...)` dropped its
future, so an exception anywhere in a dispatch tick was never retrieved and never logged — the
fire-and-forget shape that hid `_maybe_receipt`'s NameError for fourteen sessions. And inside
`_dispatch_managers_of_kind` the post-claim writes (claim, intent, outcome) were bare `await`s: a
transient DB error after `handler.apply` left a REAL ORDER at the venue with the row stuck APPLYING
until restart, and the exception vanished into that same dropped future.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from api.engine_node import UiFeedStrategy


# -- part A: the dropped future ---------------------------------------------------------------------
def test_a_dispatch_tick_that_raises_is_logged_not_lost(caplog):
    """The future's exception must be retrieved and logged. On main nothing ever looked at it."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        async def boom():
            raise RuntimeError("dispatch tick exploded")

        probe = SimpleNamespace(_loop=loop)
        with caplog.at_level(logging.ERROR):
            fut = UiFeedStrategy._spawn(probe, boom(), "manager dispatch")
            with pytest.raises(RuntimeError):
                fut.result(timeout=5)
            # The done-callback runs on the loop thread; give it a beat.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any("manager dispatch" in r.message for r in caplog.records):
                    break
                time.sleep(0.01)
        assert any("manager dispatch" in r.message and "exploded" in r.message
                   for r in caplog.records), (
            "the scheduled coroutine raised and nothing said so — the dropped-future silence again"
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)


def test_both_dispatch_sites_inspect_their_future():
    """The wiring: the timer tick and the post-seed kick must schedule through `_spawn`, not through
    a bare run_coroutine_threadsafe whose exception no one ever reads."""
    import api.engine_node as en

    src = inspect.getsource(en)
    assert "_dispatch_all_managers" in src, "dispatch is gone — this test is blind; rewrite it"
    assert "run_coroutine_threadsafe(self._dispatch_all_managers()" not in src, (
        "a dispatch site still drops its future — its exceptions are never retrieved or logged"
    )
    assert src.count("_spawn(self._dispatch_all_managers()") >= 2, (
        "both dispatch entry points (30s timer + post-seed kick) must go through _spawn"
    )


# -- part B: bare DB writes after the claim ---------------------------------------------------------
class _Ctx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


class _Handler:
    def __init__(self):
        self.applied: list[str] = []

    async def trigger_met(self, s, row):
        return True

    def client_order_id_for(self, mid):
        return f"coid-{mid}"

    async def apply(self, s, row):
        self.applied.append(row.manager_id)
        return "APPLIED", "ok"


def _rows(*ids):
    return [SimpleNamespace(manager_id=i, leash="AUTO", instrument_id=f"{i}.XNAS", params={})
            for i in ids]


def _probe():
    return SimpleNamespace(
        id="MANUAL-001",
        clock=SimpleNamespace(timestamp_ns=lambda: 0),
        _active_exit_suppressions=lambda ns: set(),
        _dispatch_managers_of_kind=UiFeedStrategy._dispatch_managers_of_kind,
    )


def _wire(monkeypatch, handler, rows, record_event):
    import api.db.engine as dbe
    import api.managers as mg

    monkeypatch.setattr(dbe, "session_factory", lambda: _Ctx())
    monkeypatch.setattr(mg, "handler_for", lambda kind: handler)

    async def armed_of_kind(session, kind, sid):
        return rows

    async def claim(session, mid):
        return True

    monkeypatch.setattr(mg, "armed_of_kind", armed_of_kind)
    monkeypatch.setattr(mg, "claim", claim)
    monkeypatch.setattr(mg, "record_event", record_event)


def test_an_outcome_write_that_fails_after_apply_is_an_ERROR_naming_the_stuck_row(monkeypatch, caplog):
    """The worst case: `handler.apply` succeeded — a real order is at the venue — and the outcome
    write dies. On main the exception propagated into the dropped future: never logged, row stuck
    APPLYING until restart, and the rest of the kind's rows never dispatched."""
    handler = _Handler()
    calls = []

    async def record_event(session, mid, event, leash, detail=None):
        calls.append(event)
        if event != "INTENT_RECORDED":
            raise RuntimeError("pg connection reset")

    probe = _probe()
    _wire(monkeypatch, handler, _rows("m1"), record_event)
    with caplog.at_level(logging.ERROR):
        asyncio.run(probe._dispatch_managers_of_kind(probe, "peak"))

    assert handler.applied == ["m1"], "fixture property: apply DID run — the order is at the venue"
    assert "INTENT_RECORDED" in calls and len(calls) >= 2, (
        "fixture property: the outcome write must have been attempted and failed"
    )
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("m1" in m and "APPLYING" in m for m in errors), (
        f"a DB error after apply left a real order at the venue with the row stuck APPLYING — and "
        f"nothing was logged. errors: {errors}"
    )


def test_an_intent_write_that_fails_means_NO_apply_and_the_next_row_still_runs(monkeypatch, caplog):
    """Intent-before-effect: if the intent cannot be recorded, the effect must not happen — and one
    row's DB blip must not kill the whole kind's dispatch loop."""
    handler = _Handler()

    async def record_event(session, mid, event, leash, detail=None):
        if mid == "m1" and event == "INTENT_RECORDED":
            raise RuntimeError("pg blip")

    probe = _probe()
    _wire(monkeypatch, handler, _rows("m1", "m2"), record_event)
    with caplog.at_level(logging.ERROR):
        asyncio.run(probe._dispatch_managers_of_kind(probe, "peak"))

    assert "m1" not in handler.applied, (
        "applied without a recorded intent — the discipline the intent row exists for"
    )
    assert handler.applied == ["m2"], (
        "m1's DB blip took m2's dispatch down with it — one row's failure ended the loop"
    )
    assert any("m1" in r.message for r in caplog.records if r.levelno >= logging.ERROR)


def test_a_claim_that_raises_skips_the_row_loudly_and_the_loop_survives(monkeypatch, caplog):
    """A claim that RAISES (as opposed to returning False) placed no order and changed no state —
    skip it loudly and keep going; the next tick retries."""
    handler = _Handler()

    async def record_event(session, mid, event, leash, detail=None):
        return None

    probe = _probe()
    _wire(monkeypatch, handler, _rows("m1", "m2"), record_event)
    import api.managers as mg

    async def claim(session, mid):
        if mid == "m1":
            raise RuntimeError("pg down")
        return True

    monkeypatch.setattr(mg, "claim", claim)
    with caplog.at_level(logging.ERROR):
        asyncio.run(probe._dispatch_managers_of_kind(probe, "peak"))

    assert handler.applied == ["m2"]
    assert any("m1" in r.message for r in caplog.records if r.levelno >= logging.ERROR)
