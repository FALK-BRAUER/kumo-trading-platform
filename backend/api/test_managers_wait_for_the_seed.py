"""Manager dispatch must not read a half-seeded projection (#568).

CODEX, blocking, on the second version of the seed fix:

    "The lock only protects `_apply_seed()` against concurrent `project()`. It does not stop
     `current_cycle_for()` from running during `_load_seed()`, while `_cycles_seeded` is still false.
     Manager dispatch is armed right after the seed thread starts, and manager guards call
     `current_cycle_for()`. That path can mint fresh cycle IDs before durable envelopes are applied."

That is the exact defect #568 exists to fix, reappearing through a different door. The seed restores the
cycle_ids from the durable envelope; anything that projects BEFORE it lands mints fresh ones, and every
subsequent envelope write for that instrument collides on `uq_trade_cycle_active_key` — the storm that
starved MOMENTUM-002's source refresh on 2026-08-26 and cost the session.

WHY THE GATE IS ON DISPATCH AND NOT ON `current_cycle_for`. Codex suggested either. It cannot be
`current_cycle_for` returning None, because at engine_node.py:6823 a caller does:

    current = strategy.current_cycle_for(instrument_id, strategy_id)
    if current is None or current.cycle_id != row.cycle_id:
        return "FAILED", "the position's cycle changed since this was queued"

so an unseeded None marks the manager FAILED at every boot. The other two call sites
(`is not None and ...`) tolerate None, which is exactly the asymmetry that makes a blanket gate unsafe.
Deferring the DISPATCH costs a tick; failing a manager costs an operator's trust in the row.
"""

from __future__ import annotations

import asyncio

from api.engine_node import UiFeedStrategy


class _Log:
    """Nautilus's Logger surface, as the engine uses it. `info` is a METHOD, not a list — the first
    version of this double had both and rebound one over the other, which is its own small lesson about
    doubles that do not match what they stand in for."""

    def __init__(self):
        self.lines: list[str] = []

    def info(self, m):
        self.lines.append(str(m))

    def warning(self, m):
        self.lines.append(str(m))

    def error(self, m):
        self.lines.append(str(m))

    def debug(self, m):
        pass


class _Fake:
    """Only what the dispatch path touches.

    `_dispatch_all_managers` is bound REAL — it is the single choke point both entry points funnel
    through (`_on_manager_tick` and `_startup_manager_check`), so gating it covers both doors. A
    stand-in here would test the stand-in's gate.
    """

    def __init__(self, *, seeded: bool):
        self._cycles_seeded = seeded
        self.dispatched: list = []
        self.log = _Log()
        self._loop = None

    async def _dispatch_managers_of_kind(self, kind):
        self.dispatched.append(kind)

    _dispatch_all_managers = UiFeedStrategy._dispatch_all_managers


def _run(fake):
    asyncio.run(fake._dispatch_all_managers())


def test_the_fixture_dispatches_once_SEEDED():
    """Fixture property first. If the harness never dispatched, every assertion below would hold
    against a manager system that simply does not work."""
    fake = _Fake(seeded=True)
    _run(fake)
    assert fake.dispatched, (
        "the seeded path dispatched nothing — this file would then prove only that a broken dispatch "
        "stays broken")


def test_dispatch_DEFERS_while_the_seed_is_still_loading():
    fake = _Fake(seeded=False)
    _run(fake)
    assert fake.dispatched == [], (
        "manager dispatch ran while `_cycles_seeded` was False — its guards call `current_cycle_for`, "
        "which projects, which mints fresh cycle_ids before the durable envelope is applied")


def test_deferring_SAYS_SO():
    """A dispatch that silently does nothing is indistinguishable from a manager system that is off,
    and 'nothing happened' is the hardest state to debug in this codebase."""
    fake = _Fake(seeded=False)
    _run(fake)
    assert any("seed" in m.lower() for m in fake.log.lines), f"deferral was silent: {fake.log.lines}"


def test_it_does_not_defer_FOREVER():
    """The discriminating half. A gate that never opens is worse than the race — managers would never
    run at all and every deferred-flatten would sit unfired."""
    fake = _Fake(seeded=False)
    _run(fake)
    assert fake.dispatched == []
    fake._cycles_seeded = True
    _run(fake)
    assert fake.dispatched, "dispatch never resumed after the seed completed"


def test_BOTH_entry_points_go_through_the_gated_function():
    """The gate is on `_dispatch_all_managers` because both doors funnel through it. If a future change
    dispatches around it, this fails rather than leaving one door open — the shape that let
    `current_cycle_for` slip past the projection lock in the first place."""
    import ast
    import inspect

    for entry in ("_on_manager_tick", "_startup_manager_check"):
        src = inspect.getsource(getattr(UiFeedStrategy, entry))
        names = {
            (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
            for n in ast.walk(ast.parse(src.strip())) if isinstance(n, ast.Call)
        }
        assert "_dispatch_managers_of_kind" not in names, (
            f"{entry} dispatches directly, bypassing the seed gate on `_dispatch_all_managers`")
        assert "_dispatch_all_managers" in names, f"{entry} no longer reaches the gated dispatch"


def test_the_seed_KICKS_a_dispatch_when_it_opens_the_gate():
    """The gate has to open ONTO something (codex round 4, Medium).

    `_startup_manager_check` runs once. If it loses the race to the seed it returns early, and nothing
    retries until the 30s timer — so a deferred_flatten whose trigger is ALREADY met fires up to 30s
    late. The seed itself is ~60ms; the gate is what turned "fires at open" into "fires eventually".

    THIS TEST EXISTS BECAUSE ITS MUTATION DID NOT BITE. Removing the kick left the whole suite green:
    the mechanism was written, reviewed and unearned. That is the shape CLAUDE.md names — a green bite
    means check the bite, not congratulate the code.
    """
    from api import engine_node

    scheduled: list = []

    class _Fake2:
        _cycle_store = object()
        _seed_cycles = None

        def __init__(self):
            import threading as _t

            self._cycles_seeded = False
            self._stopping = _t.Event()
            self._loop = object()
            self.log = _Log()

        async def _load_seed_bounded(self):
            return None                      # nothing to apply; the gate still has to open

        def _apply_seed(self, payload):      # pragma: no cover - not reached with a None payload
            raise AssertionError("should not apply a None payload")

        async def _dispatch_all_managers(self):
            scheduled.append("dispatch")

        _seed_worker = UiFeedStrategy._seed_worker
        # The REAL scheduling hop (#651 item 4): the kick now goes through `_spawn`, whose
        # done-callback is what retrieves the future's exception. Bound here so the double cannot
        # drift from what production actually calls.
        _spawn = UiFeedStrategy._spawn

    fake = _Fake2()

    def _rcts(coro, loop):
        coro.close()
        scheduled.append("scheduled")
        return type("F", (), {"add_done_callback": lambda *a: None})()

    real = engine_node.asyncio.run_coroutine_threadsafe
    engine_node.asyncio.run_coroutine_threadsafe = _rcts
    try:
        fake._seed_worker()
    finally:
        engine_node.asyncio.run_coroutine_threadsafe = real

    assert fake._cycles_seeded is True, "the gate never opened"
    assert scheduled, (
        "the seed opened the gate and scheduled NO dispatch — a deferred_flatten whose trigger is "
        "already met now waits for the next 30s timer tick")
