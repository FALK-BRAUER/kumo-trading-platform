"""A node that starts nothing must SAY so (#613).

THE FAILURE, measured on alpaca-paper 2026-08-27 during market hours. Reconciliation refused a
quantity mismatch — correctly, since guessing which side is right could double a position — and:

    [ERROR] ExecEngine: report.filled_qty 80 < order.filled_qty 85 ... corrupted cached state
    [ERROR] TradingNode: Execution state could not be reconciled
    [INFO]  TradingNode: RUNNING                     <- logged anyway
    PLATFORM-001.MANUAL: READY                        <- and never RUNNING

Strategies stop at READY, so `on_start` never runs, so the Redis writer thread is never created, so
NOT ONE `ui:state:*` key is written. `/positions` served 0 against 22 held at the broker, a lane due
in 30 minutes would not have fired, and the engine kept polling the account every 3.5s so the log
looked alive. py-spy showed ONE thread where a healthy node has six.

It was found by a human looking at a screenshot.

WHY THE ORDINARY HEALTH FRAME CANNOT REPORT THIS. That frame is published BY the writer thread that
never starts — the reporting path is downstream of the break. The watchdog writes to Redis directly
from a daemon thread started before `node.run()`.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

import api.engine_node as mod


def _src(name: str) -> str:
    """Source with comments and docstrings stripped.

    Learned the hard way earlier tonight: a raw-source grep is satisfied by the fix's own explanatory
    comment, and the mutant scored 5 of 5 before that was noticed.
    """
    obj = getattr(mod.UiFeedStrategy, name, None) or getattr(mod, name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    return ast.unparse(fn) if fn.body else ""


def test_the_watchdog_starts_BEFORE_the_blocking_run():
    """`node.run()` blocks and the inert state is reached inside it. Starting the watchdog after
    would never run at all — the unit could be perfect and the wiring dead."""
    src = _src("main")
    assert "_start_inert_watchdog(node)" in src, "the watchdog is never started"
    assert src.index("_start_inert_watchdog(node)") < src.index("node.run()"), (
        "the watchdog starts after the blocking run(), so it can never fire"
    )


def test_it_does_NOT_touch_nautilus_internals_off_thread():
    """CODEX BLOCKED THE FIRST VERSION ON THIS. It called `node.trader.strategy_states()` from the
    watchdog thread; Nautilus does not document that as thread-safe, so it could read a torn state or
    race internal mutation while the node runs.

    The observable fault IS "the writer never published", so that is what is measured — a Redis key,
    from a thread that owns nothing.
    """
    src = _src("_start_inert_watchdog")
    assert "strategy_states" not in src, (
        "the watchdog reads Nautilus internals from a non-main thread — undocumented as thread-safe"
    )
    assert "_writer_is_publishing(r)" in src


def test_the_probe_is_a_key_the_watchdog_never_writes():
    """`ui:state:positions`, not `ui:state:health`. Probing the key we ourselves write would see our
    own alarm and declare the fault healthy."""
    src = _src("_writer_is_publishing")
    assert "ui:state:positions" in src
    assert "ui:state:health" not in src, "the probe can be satisfied by the watchdog's own frame"


def test_the_probe_checks_FRESHNESS_not_existence():
    """CODEX BLOCKED THE SECOND VERSION ON THIS. `exists()` proves only that SOME writer wrote the
    key at SOME point — and Redis outlives the process, so a stale `ui:state:positions` left by the
    previous run would suppress the alarm entirely. The corpse of the last healthy boot would mask
    an inert node.
    """
    src = _src("_writer_is_publishing")
    assert "exists(" not in src, "the probe tests existence, which a stale key satisfies"
    # `ast.unparse` normalises quotes, so match the identifier rather than a quoted literal.
    assert "_WRITER_STALE_SECS" in src and "ts" in src, "the probe does not check freshness"
    assert 0 < mod._WRITER_STALE_SECS <= 300


def test_an_UNREADABLE_or_UNDATED_frame_counts_as_not_publishing():
    """A fault detector must fail toward reporting. Treating a malformed frame as healthy would
    suppress the alarm on exactly the kind of stack where frames go malformed."""
    src = _src("_writer_is_publishing")
    assert "return False" in src
    assert "except Exception" in src


def test_the_clear_is_ATOMIC():
    """A GET-then-DELETE has a window in which the real writer publishes a healthy frame and the
    watchdog deletes it — which is the exact class of bug this fix is for, so it does not get to
    survive inside the fix (codex, second review)."""
    src = _src("_clear_inert_state")
    assert "eval(" in src, "clear is not a server-side compare-and-delete"
    assert "get(" not in src.lower().replace("redis.call('get'", ""), "clear still reads then deletes"


def test_ONE_redis_client_for_the_life_of_the_thread():
    """A fresh connection per check is needless churn on a path whose job is to stay boring while
    everything else is broken."""
    src = _src("_start_inert_watchdog")
    assert src.count("_inert_redis()") == 1, "a Redis client is created per check"


def test_the_inert_frame_EXPIRES():
    """Without a TTL a node that dies while inert leaves a permanent alarm, and a fault that clears
    on its own needs a human to delete a key."""
    assert 0 < mod._INERT_TTL_SECS <= 300
    assert "_INERT_TTL_SECS" in _src("_publish_inert_state")
    # ...and it must be REPUBLISHED, or the TTL expires the alarm into the silence it replaced.
    assert "_publish_inert_state(r)" in _src("_start_inert_watchdog")


def test_recovery_clears_ONLY_the_watchdogs_own_frame():
    """A blind delete would remove a healthy frame the real writer had since published — trading a
    false alarm for a false outage (codex, implementation review)."""
    src = _src("_clear_inert_state")
    assert "_INERT_OWNER" in src, (
        "recovery deletes ui:state:health unconditionally and can destroy a healthy frame"
    )
    # The ownership test lives in the Lua. It must DECODE and COMPARE THE FIELD — `string.find` on
    # the raw frame would delete any health frame containing that text anywhere, which is the same
    # "close enough" that produced the blind delete it replaced (codex, third review).
    lua = mod._CLEAR_IF_OURS
    assert "cjson.decode" in lua, "the ownership check does not parse the frame"
    assert "written_by" in lua and "== ARGV[1]" in lua, "the owner field is not compared exactly"
    assert "string.find" not in lua, "ownership is a substring match on the whole frame"
    assert "DEL" in lua


def test_it_writes_redis_DIRECTLY_not_through_the_publish_queue():
    """THE CRUX. `self._publish` enqueues to the writer thread — the thread that does not exist in
    the state being reported. Using it would make the alarm depend on the fault."""
    src = _src("_publish_inert_state")
    assert "r.set(" in src, "the inert frame is not written straight to Redis"
    assert "_publish(" not in src, (
        "the inert frame goes through the publish queue, which is drained by the writer thread that "
        "never started — the report would be swallowed by the very failure it describes"
    )


def test_NOTHING_PUBLISHED_is_the_trigger():
    """The node logging RUNNING is exactly what made this invisible, so the trigger cannot be node
    state. "No data plane has ever been published" is the fault the operator actually suffers."""
    assert "_writer_is_publishing(r)" in _src("_start_inert_watchdog")


def test_it_waits_before_crying_wolf():
    """A false alarm during a slow boot teaches an operator to ignore the alarm — which is how the
    account-frame warning became noise. IBKR's provider load alone can take a minute."""
    assert mod._INERT_AFTER_SECS >= 120, (
        f"_INERT_AFTER_SECS={mod._INERT_AFTER_SECS} is short enough to fire during a normal boot"
    )
    assert mod._INERT_CHECK_SECS <= 60


def test_the_frame_names_the_condition_rather_than_only_saying_not_ok():
    """`engine_ok: false` is what an ABSENT frame already implied. The point is to distinguish a node
    that is down from one that is up and has started nothing."""
    src = _src("_publish_inert_state")
    assert "ENGINE_INERT" in src, "the frame does not name the condition"
    assert "reason" in src, "the frame does not say why"


def test_recovery_CLEARS_the_state():
    """A stale alarm is its own defect: once the strategies run, the inert frame must go, or the UI
    reports a fault that has been fixed and the next real one is ignored."""
    assert "_clear_inert_state" in _src("_start_inert_watchdog")


def test_the_watchdog_never_takes_down_the_node():
    """It is a reporting thread. A raise inside it must not become an outage of its own — the thing
    it watches for is already bad enough."""
    assert "except Exception" in _src("_start_inert_watchdog")


@pytest.mark.parametrize("fn", ["_publish_inert_state", "_clear_inert_state"])
def test_a_redis_failure_does_not_propagate(fn):
    """Redis being unreachable is plausible in exactly this scenario. Reporting must degrade, not
    raise into the watchdog loop."""
    assert "except Exception" in _src(fn)


def test_the_probe_accepts_the_frame_the_PUBLISHER_ACTUALLY_EMITS():
    """FABLE'S FINDING (#644), and it is my own fix firing on every healthy node.

    The only publisher of the positions frame emitted `{"positions": [...]}` — NO `ts` — while
    `_writer_is_publishing` requires a fresh positive `ts` and treats its absence as "not
    publishing". So after 180s EVERY healthy node logged ENGINE INERT at ERROR every 15s and
    overwrote ui:state:health with engine_ok:false, fighting the real health frame forever. The
    detector built to make an inert node visible made itself the noise.

    The test that let it ship asserted only that the probe's SOURCE contained the string "ts"
    (:91) — the fixture never drove the probe against a real frame. This one builds the frame the
    way the publisher builds it and feeds it through the REAL probe.
    """
    import json as _json
    import time as _time

    frame = mod.UiFeedStrategy._positions_frame(
        SimpleNamespace(), [], now_ns=_time.time_ns())

    class _R:
        def get(self, key):
            assert key == "ui:state:positions"
            return _json.dumps(frame)

    assert mod._writer_is_publishing(_R()) is True, (
        "the probe rejects the frame the publisher actually emits — every healthy node reports "
        "ENGINE INERT forever, and the real alarm is buried in the false one (#644)"
    )


def test_a_STALE_frame_still_reads_as_not_publishing():
    """The other direction must survive: freshness is the point of the probe, and a frame from the
    previous boot must not mask a genuinely inert node — the exact corpse-of-last-boot case codex
    blocked the exists() probe over."""
    import json as _json
    import time as _time

    frame = mod.UiFeedStrategy._positions_frame(
        SimpleNamespace(), [], now_ns=_time.time_ns() - 3600 * 1_000_000_000)

    class _R:
        def get(self, key):
            return _json.dumps(frame)

    assert mod._writer_is_publishing(_R()) is False


def test_the_publisher_actually_USES_the_frame_builder():
    """THE WIRING — bite B: re-inlining `{"positions": positions}` at the publish site left the
    builder correct, tested, and bypassed, with the suite green. Fourth instance of the class today
    (_lane_symbols, _schedule_bar_drain, supplies_trading_calendar). Docstring stripped."""
    src = _src("_on_snapshot") if hasattr(mod.UiFeedStrategy, "_on_snapshot") else ""
    if "_positions_frame" not in src:
        # the publish site may live elsewhere; find the one method that publishes positions
        import inspect as _i
        import re as _re

        whole = _i.getsource(mod.UiFeedStrategy)
        m = _re.search(r'def (\w+)\([^)]*\).*?self\._publish\(\s*"positions"', whole, _re.DOTALL)
        assert m, "no method publishes the positions frame at all"
        src = _src(m.group(1))
    assert "_positions_frame" in src, (
        "the positions publish site does not use _positions_frame — the ts is gone from the live "
        "frame and every healthy node reports ENGINE INERT again (#644)"
    )
