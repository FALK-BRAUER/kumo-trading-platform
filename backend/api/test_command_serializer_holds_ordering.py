"""The command serializer's 30s timeout broke its own ordering invariant (#652 item 6).

`_drain_commands` documents the invariant itself: "orders must apply in order, never concurrently".
But `fut.result(timeout=30)` swallowed the timeout via `except Exception` and the reader PROCEEDED
to the next entry while the slow handler kept running on the loop — a flatten legitimately exceeds
30s waiting on venue confirms, so a later order for the same instrument could interleave mid-cancel.

The fix: `_await_command_serialized` keeps waiting past the interval, logging that it is HOLDING the
queue (degrade loudly, don't abandon the invariant), and gives up only when the node is stopping.
A TimeoutError raised BY the handler itself (fut.done()) is that handler's error, not our wait
expiring — it must not spin the wait loop forever.

Every behaviour test runs the helper on a side thread with a join timeout, so a regression hangs the
helper, not the suite.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import logging
import threading
from types import SimpleNamespace

from api import engine_node
from api.engine_node import UiFeedStrategy


def _run_helper(fake_self, fut, timeout=2.0):
    done = threading.Event()
    bound = UiFeedStrategy._await_command_serialized.__get__(fake_self)
    t = threading.Thread(target=lambda: (bound(fut, "entry-1"), done.set()), daemon=True)
    t.start()
    t.join(timeout)
    return done.is_set()


def _fake_self():
    return SimpleNamespace(_stopping=threading.Event())


def test_the_reader_actually_uses_the_serialized_wait():
    """The seam, not the unit: a green helper proves nothing if `_drain_commands` still calls
    `fut.result(timeout=30)` bare."""
    src = inspect.getsource(UiFeedStrategy._drain_commands)
    assert "_await_command_serialized(" in src, (
        "_drain_commands does not route through _await_command_serialized — the reader can still "
        "abandon a running handler after 30s and interleave the next command"
    )
    assert "fut.result(timeout" not in src, "the bare timed wait is still in the reader"


def test_a_slow_handler_holds_the_queue_instead_of_being_abandoned(monkeypatch, caplog):
    """Handler finishes after ~3 wait intervals: the helper must still be waiting when it does
    (returns only after completion), and must have SAID it is holding the queue."""
    monkeypatch.setattr(engine_node, "_CMD_SERIALIZE_WAIT_SECS", 0.05)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    threading.Timer(0.18, fut.set_result, [None]).start()
    with caplog.at_level(logging.WARNING, logger="kumo.engine_node"):
        finished = _run_helper(_fake_self(), fut)
    assert finished, "helper never returned after the handler completed"
    assert fut.done()
    held = [r for r in caplog.records if "holding" in r.getMessage()]
    assert held, "waiting past the interval must be NAMED, or a wedged queue is indistinguishable from an idle one"


def test_a_handler_that_itself_raises_TimeoutError_is_not_mistaken_for_a_slow_one(monkeypatch):
    """fut.result re-raises the handler's own TimeoutError. `fut.done()` distinguishes the two; a
    helper that treats it as its wait expiring loops forever on a completed future."""
    monkeypatch.setattr(engine_node, "_CMD_SERIALIZE_WAIT_SECS", 0.05)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_exception(TimeoutError("asyncio.wait_for inside the handler"))
    assert _run_helper(_fake_self(), fut), (
        "a handler-raised TimeoutError wedged the wait loop — it is the handler's ERROR, not our wait"
    )


def test_a_failed_handler_does_not_wedge_the_reader(monkeypatch):
    monkeypatch.setattr(engine_node, "_CMD_SERIALIZE_WAIT_SECS", 0.05)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_exception(RuntimeError("handler blew up"))
    assert _run_helper(_fake_self(), fut), "a failed handler must be logged and released, not re-waited"


def test_shutdown_releases_the_wait(monkeypatch):
    """A handler stuck FOREVER must not block node shutdown — stopping is the one condition that
    outranks the ordering invariant (there is no next command to mis-order)."""
    monkeypatch.setattr(engine_node, "_CMD_SERIALIZE_WAIT_SECS", 0.05)
    fake = _fake_self()
    fake._stopping.set()
    fut: concurrent.futures.Future = concurrent.futures.Future()  # never completes
    assert _run_helper(fake, fut), "helper must return once the node is stopping"
