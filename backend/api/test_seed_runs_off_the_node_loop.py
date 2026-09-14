"""The restart seed must not be starved by the loop it shares with the backfill (#568).

MEASURED, kumo-paper, 2026-08-26:

    14:47:01  seed queued (run_coroutine_threadsafe on the node loop)
    14:51:48  trade-cycle restart seed exceeded 20.0s (starting fresh)

Four minutes forty-seven between being QUEUED and the 20-second budget EXPIRING. `on_start` also
starts the backfill for ~500 instruments across four lanes at ~289 daily bars each, and that saturates
the node loop; the seed coroutine simply never gets scheduled. Its actual work, timed in the running
container, is 60 MILLISECONDS — 22ms of Redis, 38ms of Postgres, across 83 snapshot keys.

WHAT IT COSTS. The seed loads the active envelope rows — 31 of them on that boot. Without them the
projection mints a FRESH cycle_id for every instrument that already has a live envelope row, so every
subsequent write violates `uq_trade_cycle_active_key`. That is the collision storm #564 was bounding;
this is why there was anything to bound. It also delayed a live order by 5m10s.

THE INVARIANT: the seed runs on its own thread, so a busy node loop cannot starve it. `asyncio.wait_for`
measures WALL CLOCK, so a budget started before the coroutine is scheduled measures the scheduler's
queue and not the work — a budget measured from a point the code does not control is the deeper bug.
"""

from __future__ import annotations

import ast
import inspect
import threading

from api import engine_node
from api.engine_node import UiFeedStrategy


def test_the_seed_is_NOT_scheduled_on_the_node_loop():
    """AST, not a substring: a comment naming `run_coroutine_threadsafe` must not satisfy this. Six
    vacuous mutation bites in this repo were comments describing the removed thing.

    The seed's own call site is what matters — `on_start` legitimately schedules other work on the loop
    (`_http.connect`, `_fmp.connect`), so this asserts about the SEED call specifically.
    """
    src = inspect.getsource(engine_node.UiFeedStrategy.on_start)
    tree = ast.parse(src.strip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "run_coroutine_threadsafe":
            continue
        # What coroutine is being scheduled on the loop?
        arg = node.args[0] if node.args else None
        called = ""
        if isinstance(arg, ast.Call):
            f = arg.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        assert called != "_seed_cycles", (
            "the seed is still queued on the node loop — `on_start` starts the backfill for ~500 "
            "instruments on that same loop, and on 2026-08-26 the seed waited 4m47s for a slot that "
            "never came")


def test_the_seed_runs_on_its_own_thread():
    """The positive half. A test that only forbids the old call would pass just as well if the seed
    were deleted outright — and a deleted seed is a WORSE bug, silently: every cycle mints fresh and
    every envelope write collides, which is exactly the state this fixes."""
    src = inspect.getsource(engine_node.UiFeedStrategy.on_start)
    tree = ast.parse(src.strip())
    started = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", "") == "_start_seed_thread"
             or getattr(n.func, "attr", "") == "_start_seed_thread")
    ]
    assert started, "on_start no longer starts the seed at all — the projection would mint fresh forever"


class _SeedFake:
    """Only what `_seed_worker` touches. The REAL worker runs against it — a stand-in worker would skip
    the `finally` that releases the book, which is what these tests are about."""

    _cycle_store = object()

    #: `None` is a REAL return value from `_load_seed` (no projections, or no store), so it cannot also
    #: mean "caller did not pass one" — a default of None made the empty-seed test unable to fail.
    _UNSET = object()

    #: A threading.Event, exactly as production has it — NOT a bool. `_seed_worker` skips the apply
    #: when the strategy is stopping, and the first version of that guard read
    #: `getattr(self, "_stopping", False)`, which returns the EVENT — always truthy — so the seed would
    #: never have applied at all. A double holding a bool here would have hidden that.
    _stopping = None

    def __init__(self, *, load=_UNSET, apply_raises=None, load_raises=None):
        import threading as _t

        self._stopping = _t.Event()
        self._cycles_seeded = False
        self._seed_thread = None
        self.errors: list = []
        self.applied: list = []
        self.apply_thread: list = []
        self._projection_lock = threading.RLock()
        self._load = {"legs": {}, "cycles": {}} if load is self._UNSET else load
        self._apply_raises = apply_raises
        self._load_raises = load_raises
        self.log = type("L", (), {
            "error": lambda _s, m: self.errors.append(str(m)),
            "warning": lambda _s, m: None, "info": lambda _s, m: None})()

    async def _load_seed_bounded(self):
        if self._load_raises:
            raise self._load_raises
        return self._load

    def _apply_seed(self, payload):
        if self._apply_raises:
            raise self._apply_raises
        self.apply_thread.append(threading.current_thread().name)
        self.applied.append(payload)

    _seed_worker = UiFeedStrategy._seed_worker
    _start_seed_thread = UiFeedStrategy._start_seed_thread


def test_the_seed_thread_actually_seeds_and_marks_completion():
    """DRIVE IT. The structural tests above say WHERE it runs; this says it actually loads, applies,
    and releases the book — the flag `_publish_trades` gates on."""
    fake = _SeedFake()
    t = fake._start_seed_thread()
    t.join(timeout=10)
    assert not t.is_alive(), "the seed thread did not finish"
    assert fake.applied, "the seed loaded but never applied"
    assert fake.apply_thread[0] != threading.main_thread().name, (
        f"the seed applied on {fake.apply_thread[0]} — it must not share a thread with the node loop")
    assert fake._cycles_seeded is True, (
        "`_cycles_seeded` was never set — `_publish_trades` would serve `seeding` forever")


def test_a_seed_whose_LOAD_raises_still_releases_the_book():
    """A seed failure means 'mint fresh', never 'never publish'. On 2026-08-26 a held book behind a
    `seeding` frame is exactly what an operator cannot act on."""
    fake = _SeedFake(load_raises=RuntimeError("postgres is down"))
    fake._start_seed_thread().join(timeout=10)
    assert fake._cycles_seeded is True, "a failed load left the book stuck in `seeding` forever"
    assert fake.errors, "a failed load logged nothing"
    assert not fake.applied, "a failed load still applied something"


def test_a_seed_whose_APPLY_raises_still_releases_the_book():
    """The second exit path. `_apply_seed` mutates five projections; one bad row must not wedge the book."""
    fake = _SeedFake(apply_raises=RuntimeError("bad envelope row"))
    fake._start_seed_thread().join(timeout=10)
    assert fake._cycles_seeded is True, "a failed apply left the book stuck in `seeding` forever"
    assert fake.errors, "a failed apply logged nothing"


def test_a_seed_with_NOTHING_to_restore_still_releases_the_book():
    """`_load_seed` returns None when there are no projections or no store. That is the ordinary
    fresh-database case, and it must not read as a failure that holds the book."""
    fake = _SeedFake(load=None)
    fake._start_seed_thread().join(timeout=10)
    assert fake._cycles_seeded is True
    assert not fake.applied
    assert not fake.errors, f"an empty seed logged an error: {fake.errors}"


def test_the_budget_bounds_the_WORK_and_not_the_wait():
    """THE POINT OF THE WHOLE FIX, as a number. On the loop the 20s budget elapsed while the coroutine
    waited to be SCHEDULED — it measured the queue. A seed whose work is 60ms must not time out because
    something else was busy for five minutes."""
    import time

    fake = _SeedFake()
    t = fake._start_seed_thread()
    time.sleep(0.3)          # busy the main thread the way backfill busies the node loop
    t.join(timeout=10)
    assert fake._cycles_seeded is True
    assert not [e for e in fake.errors if "exceeded" in e], (
        f"the seed timed out despite trivial work: {fake.errors}")


# ==================================================================================================
# THE SHARED ENGINE MUST NOT CROSS EVENT LOOPS
#
# Raised by codex against the first version of this fix, and it would have been worse than the bug it
# fixes. `asyncio.run()` creates a NEW event loop in the seed thread. asyncpg stores the loop on the
# connection, and SQLAlchemy documents that a default-pooled AsyncEngine must not be shared across
# loops. The seed would check a connection out of the SHARED pool, bind it to its temporary loop,
# return it, and then `asyncio.run()` would close that loop — leaving a poisoned connection for the
# main loop to pick up. Trading a seed timeout for engine-wide DB failures.
# ==================================================================================================


def test_the_seed_does_not_use_the_shared_session_factory():
    """AST over `_load_seed`: it must construct its OWN engine, not reach for the shared one.

    Asserted structurally rather than by substring, because a comment mentioning `session_factory`
    would satisfy a text search — six vacuous bites in this repo had exactly that shape.
    """
    src = inspect.getsource(engine_node.UiFeedStrategy._load_seed)
    tree = ast.parse(src.strip())
    names = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "create_async_engine" in names, (
        "the seed does not build its own engine — on its own thread it would bind a connection from "
        "the SHARED pool to a temporary loop and poison it for the main loop")
    assert "CycleEnvelopeStore" in names, "the seed no longer builds a store with its own factory"


def test_the_seed_engine_uses_NullPool_and_is_DISPOSED():
    """Both halves matter. `NullPool` means no connection outlives the temporary loop; `dispose()` in a
    `finally` means even the transient one is closed before that loop goes away. Either alone still
    leaves a connection bound to a dead loop."""
    src = inspect.getsource(engine_node.UiFeedStrategy._load_seed)
    tree = ast.parse(src.strip())

    kwargs = [
        k for n in ast.walk(tree) if isinstance(n, ast.Call)
        for k in n.keywords if k.arg == "poolclass"
    ]
    assert kwargs, "the seed engine does not set poolclass — it would use the default QueuePool"
    assert any(getattr(k.value, "id", "") == "NullPool" for k in kwargs), (
        "the seed engine's poolclass is not NullPool")

    disposed = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "dispose"
    ]
    assert disposed, "the seed engine is never disposed — its connections outlive the loop that made them"

    finallies = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.finalbody]
    assert any(
        isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "dispose"
        for f in finallies for stmt in f.finalbody for c in ast.walk(stmt)
    ), "dispose() is not in a `finally` — a seed that raises would leak the engine"
