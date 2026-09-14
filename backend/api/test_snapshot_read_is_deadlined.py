"""The snapshot read must honour a deadline of its own (#568).

CODEX, blocking, on the third version of the seed fix:

    "`_load_seed_bounded()` wraps `_load_seed()` in `asyncio.wait_for`, but `_load_seed()` calls
     synchronous `read_restored_legs()` before the Postgres awaits. While Redis `scan_iter`/`lrange` is
     blocking, the coroutine cannot be cancelled, so the 20s timeout cannot fire."

Correct, and it matters more since manager dispatch now defers on `_cycles_seeded` (#568): a seed that
overruns its budget no longer just holds the book in `seeding` — it holds every deferred_flatten too.
`asyncio.wait_for` cannot interrupt a blocking call; only the loop that makes the calls can.

WHY NOT ASYNC REDIS. The seed runs on its own thread precisely so blocking is cheap there, and swapping
the client would change the engine's Redis surface for one caller. A deadline checked between keys is
smaller, and it is the thing actually needed: a bound that BINDS.

PARTIAL IS BETTER THAN NONE. Legs are per-position P&L detail; the cycle BOUNDARY comes from the
envelope, which is a separate Postgres read. A position either got its legs or did not, so stopping
early degrades the P&L of later positions rather than corrupting any of them — and it is reported, so
"some legs missing" is never mistaken for "this position had no legs".
"""

from __future__ import annotations

import time

from api.snapshot_reader import read_restored_legs


class _SlowRedis:
    """A Redis whose per-key read is slow — the shape that outruns the budget without any single call
    timing out. `socket_timeout=2.0` on the real client bounds ONE call; nothing bounded the loop."""

    def __init__(self, n_keys: int, per_key_secs: float):
        self._keys = [f"trader-T{'-snapshots:positions:'}POS{i}".encode() for i in range(n_keys)]
        self._per_key = per_key_secs
        self.reads = 0

    def scan_iter(self, match=None):
        yield from self._keys

    def lrange(self, key, start, end):
        self.reads += 1
        time.sleep(self._per_key)
        return []


def test_the_fixture_is_actually_slow_enough_to_overrun():
    """Fixture property first. If the fake were fast, a deadline test would pass against code that has
    no deadline at all — the exact vacuous shape CLAUDE.md names."""
    r = _SlowRedis(n_keys=20, per_key_secs=0.02)
    t = time.monotonic()
    read_restored_legs(r, "T", deadline=None)
    assert time.monotonic() - t > 0.2, "the fake is too fast to demonstrate an overrun"
    assert r.reads == 20


def test_the_read_STOPS_at_its_deadline():
    r = _SlowRedis(n_keys=200, per_key_secs=0.01)
    t = time.monotonic()
    out = read_restored_legs(r, "T", deadline=time.monotonic() + 0.15)
    elapsed = time.monotonic() - t
    assert elapsed < 1.0, f"the read ran {elapsed:.2f}s past a 0.15s deadline — nothing bounds the loop"
    assert r.reads < 200, f"it read every one of {r.reads} keys despite the deadline"
    # The stop travels WITH the result (#846): a log line is not a surface, and a realized figure over
    # a partial seed must be able to say it is partial.
    assert out.stopped_at is not None, "the result does not say the scan stopped early"


def test_NO_deadline_still_reads_everything():
    """The discriminating half. A deadline that always fires would silently reduce every seed to
    nothing, which is worse than the overrun — the projection would mint fresh for everything."""
    r = _SlowRedis(n_keys=15, per_key_secs=0.001)
    out = read_restored_legs(r, "T", deadline=None)
    assert r.reads == 15
    assert out.stopped_at is None, "a finished scan must not read as stopped"


def test_an_ALREADY_PASSED_deadline_reads_NOTHING():
    """The boundary. A budget already spent must stop before the first key, not after it."""
    r = _SlowRedis(n_keys=10, per_key_secs=0.001)
    read_restored_legs(r, "T", deadline=time.monotonic() - 1)
    assert r.reads == 0, f"read {r.reads} keys on an expired deadline"


def test_the_seed_passes_a_deadline_DERIVED_from_its_own_budget():
    """TWO ENFORCERS, ONE NUMBER. `wait_for` bounds the awaits; the deadline bounds the blocking Redis
    loop it cannot interrupt. If they were two literals they would drift — the shape this codebase has
    paid for repeatedly (`test_the_dispatch_guard_and_the_attach_check_are_the_same_predicate`).

    AST, not substring: a comment naming `_SEED_TIMEOUT_SECS` must not satisfy a check for it being
    USED. Six mutation bites died that way in one session.
    """
    import ast
    import inspect

    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._load_seed)
    tree = ast.parse(src.strip())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", "") == "read_restored_legs"
             or getattr(n.func, "attr", "") == "read_restored_legs")
    ]
    assert calls, "the seed no longer reads snapshots at all"
    kw = {k.arg: k for c in calls for k in c.keywords}
    assert "deadline" in kw, (
        "the seed reads snapshots with NO deadline — `asyncio.wait_for` cannot interrupt a blocking "
        "Redis loop, so the 20s budget does not bind and manager dispatch defers behind it")
    # The call may pass a NAME (`deadline=_deadline`) rather than the expression, so check both halves:
    # the value handed over, and the assignment it came from. Checking only the call site broke the
    # moment the deadline was hoisted to a variable — a test that pins a spelling, not a fact.
    passed = {getattr(n, "id", "") or getattr(n, "attr", "") for n in ast.walk(kw["deadline"].value)}
    assigns = {
        t.id: n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    derived = any(
        getattr(x, "attr", "") == "_SEED_TIMEOUT_SECS"
        for name in passed if name in assigns
        for x in ast.walk(assigns[name])
    ) or "_SEED_TIMEOUT_SECS" in passed
    assert derived, (
        f"the deadline handed to the leg scan ({passed}) is not derived from `_SEED_TIMEOUT_SECS` — a "
        f"second literal will drift from the budget `wait_for` uses")


# ==================================================================================================
# CRITICAL WORK FIRST (codex round 4, High)
#
# `_load_seed` read the LEGS (optional, Redis, slow) before the ENVELOPE ROWS (critical, Postgres,
# 38ms). If Redis spent the budget, the first `await store.load_active(...)` was cancelled, the seed
# returned nothing, `_cycles_seeded` flipped true anyway — and the projection minted FRESH cycle ids
# over instruments that already had live envelope rows.
#
# That is precisely the collision storm #568 exists to prevent, reintroduced by the ordering of the fix
# for it. The envelope is what makes the seed worth doing; the legs are P&L decoration.
# ==================================================================================================


def test_the_ENVELOPE_is_loaded_before_the_legs():
    """AST over `_load_seed`: `load_active` must be awaited before `read_restored_legs` is called.

    Structural because the failure is an ORDER, and an order cannot be asserted by driving the happy
    path — both calls happen either way. Only the sequence differs, and only under a budget squeeze.
    """
    import ast
    import inspect

    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._load_seed)
    tree = ast.parse(src.strip())

    def _first_line(pred):
        return min(
            (n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and pred(n)),
            default=None)

    legs = _first_line(lambda n: getattr(n.func, "id", "") == "read_restored_legs"
                       or getattr(n.func, "attr", "") == "read_restored_legs")
    env = _first_line(lambda n: getattr(n.func, "attr", "") == "load_active")
    assert legs is not None and env is not None, f"calls not found: legs={legs} envelope={env}"
    assert env < legs, (
        f"the legs are read at line {legs} and the envelope at {env} — the OPTIONAL Redis work runs "
        f"first and can spend the whole budget, so the CRITICAL envelope load gets cancelled and the "
        f"projection mints fresh ids over live rows. That is the storm this ticket exists to end.")


def test_the_legs_get_only_the_time_the_envelope_LEFT():
    """The deadline handed to the leg scan must be what REMAINS, not a fresh full budget — otherwise
    the two halves can together take twice `_SEED_TIMEOUT_SECS` and the bound stops bounding."""
    import ast
    import inspect

    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._load_seed)
    tree = ast.parse(src.strip())
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", "") == "read_restored_legs"
             or getattr(n.func, "attr", "") == "read_restored_legs"))
    kw = {k.arg: k for k in call.keywords}
    assert "deadline" in kw, "the leg scan lost its deadline"
    names = {getattr(n, "id", "") for n in ast.walk(kw["deadline"].value)}
    assert "_deadline" in names or "deadline" in names, (
        "the leg scan computes a FRESH deadline rather than using the seed's single one — two budgets "
        "that together exceed the bound")
