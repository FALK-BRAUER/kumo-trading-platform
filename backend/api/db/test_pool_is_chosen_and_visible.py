"""The connection pool must be CHOSEN and MEASURABLE, not inherited and invisible (#542).

WHAT HAPPENED, alpaca-paper 2026-08-25 16:36 UTC, repeating:

    [ERROR] MANUAL: cycle-envelope write failed:
      TimeoutError('QueuePool limit of size 5 overflow 10 reached,
                    connection timed out, timeout 30.00')

5 + 10 = 15 is SQLAlchemy's DEFAULT, not a number anyone picked — `create_async_engine(url,
pool_pre_ping=True)` and nothing else. So the engine's concurrency ceiling was set by a library
default that no one had weighed against five lanes, a reconciler, a projection and an alerting loop.

WHY THE BLAST RADIUS IS LARGER THAN A FAILED WRITE. The cycle-envelope write feeds the trade-cycle
projection, and a previous failure in that same projection — a `NameError` — killed the whole feed and
the UI showed an empty book while 8 positions were held. Same reach, different cause, failing into a
log line.

WHY NOT JUST RAISE IT. Measured at rest afterwards: `Pool size: 5, Checked out: 0`. Idle, so nothing
is leaking a connection permanently — the exhaustion was a BURST under startup load. But one idle
reading is not proof, and raising `pool_size` without knowing which it is would HIDE a leak rather
than fix a ceiling. So: choose the numbers deliberately, and expose the pool so the next exhaustion is
diagnosable from outside instead of inferred.
"""

from __future__ import annotations

import inspect

from api.db import engine as dbe


def test_the_fixture_can_see_the_engine_and_its_pool():
    """FIXTURE FIRST. Everything below reads one object; if it moved these would pass over nothing."""
    assert hasattr(dbe, "engine"), "no engine — this test is blind"
    assert dbe.engine.pool is not None


def test_the_pool_SIZE_IS_CHOSEN_not_inherited():
    """THE DEFECT. `pool_size`/`max_overflow` absent means SQLAlchemy's 5+10 — the exact numbers in
    the production error, picked by nobody."""
    src = inspect.getsource(dbe)
    assert "pool_size=" in src, (
        "create_async_engine sets no pool_size, so the ceiling is SQLAlchemy's default 5 — which is "
        "the number that exhausted on 2026-08-25, chosen by a library rather than by us")
    assert "max_overflow=" in src, "max_overflow is not set either; the 10 in '5 overflow 10' is a default"


def test_the_pool_is_BIG_ENOUGH_for_the_engine_s_known_concurrent_users():
    """Not arbitrary: five lanes + reconciler + projection + alerting + the sleeve ledger, and each
    can hold a connection across an await. 15 was demonstrably not enough."""
    assert dbe.engine.pool.size() >= 10, (
        f"pool_size is {dbe.engine.pool.size()}; the engine has more concurrent DB users than that "
        f"and 5 already exhausted in production")


def test_the_pool_is_OBSERVABLE_so_a_leak_can_be_told_from_a_burst():
    """The half that matters more than the number.

    A pool that climbs monotonically is a LEAK; one that spikes at known moments is a SIZING problem.
    From outside they look identical — which is why #542 could not be answered when it was filed, and
    why raising the ceiling blindly was the wrong move.
    """
    assert hasattr(dbe, "pool_stats"), "no pool_stats(); an exhaustion is undiagnosable from outside"

    stats = dbe.pool_stats()
    for key in ("size", "checked_out", "overflow"):
        assert key in stats, f"pool_stats() omits {key!r}: {sorted(stats)}"
        assert isinstance(stats[key], int), f"{key} is not an int: {stats[key]!r}"


def test_pool_stats_NEVER_RAISES():
    """It is read from the health path. An observability call that can take health down is the
    #377 shape — the reporter killing the thing it reports on."""
    import unittest.mock as m

    with m.patch.object(type(dbe.engine.pool), "checkedout", side_effect=RuntimeError("boom")):
        stats = dbe.pool_stats()
    assert stats.get("checked_out") is None or isinstance(stats.get("checked_out"), int), (
        "pool_stats propagated an error instead of reporting unknown")


def test_pool_stats_IS_ACTUALLY_SURFACED_not_just_available():
    """THE SEAM. A measurement nobody reads is the orphan-mechanism shape this repo has a test for.

    #542 exists because an exhaustion was invisible from outside. Adding `pool_stats()` and not
    putting it anywhere a reader looks would leave that exactly as true.
    """
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(path.read_text())

    # CALLED, not merely MENTIONED. My first version asserted the string `pool_stats` appeared in the
    # file — and a mutation that changed the import to `pool_stats as _unused_pool_stats` and dropped
    # the call left it green. Substring presence is a fact ADJACENT to the one that matters, which is
    # the ninth instance of that shape today.
    called = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "pool_stats"]
    assert called, (
        "/health imports pool_stats but never CALLS it — the exhaustion stays as invisible as it was "
        "when #542 was filed, and the function is an orphan")

    # And the result has to reach the reader, not be computed and dropped.
    src = path.read_text()
    assert "checked_out" in src, "pool_stats() is called and its result never rendered"
