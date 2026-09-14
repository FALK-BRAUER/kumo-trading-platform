"""One mechanism that absorbs observation failures AND reports them (#758).

Operator: "think of a general logging mechanism that prevents interfering with actual functionality.
best a generic mechanism rather than try catch everywhere" — and then, on the same design:
"logging errors might get logged too" and "missing logs can be confusing".

Those three sentences are the three test groups below: it must not break its subject, its own
failures must surface, and an observer that produced NOTHING must be distinguishable from a healthy
quiet one.

The two defects that produced the module, both on 2026-08-31:
  - `_record_book_truth`'s EXCEPT branch called `self.clock.timestamp_ns()`; on an object without a
    clock the handler raised and aborted the protection tick.
  - two shipped detectors were inert for two deploys because `except Exception` around a broken
    `Notifier.send` made "no alarms" read as "nothing wrong".
"""

from __future__ import annotations

import asyncio

import pytest

from api.observation import MAX_OBSERVERS, Observations


def _boom(exc=None):
    def _fn():
        raise exc or ValueError("boom")
    return _fn


# ==================================================================================================
# 1. It must not interfere with actual functionality
# ==================================================================================================
def test_a_raising_observer_does_not_propagate():
    obs = Observations()
    assert obs.run("x", _boom()) is None


def test_the_subject_continues_after_an_observer_raises():
    """The property that matters: the caller's work completes. This is `_record_book_truth` aborting
    the protection pass, pinned."""
    obs, done = Observations(), []
    for i in range(3):
        obs.run("x", _boom())
        done.append(i)
    assert done == [0, 1, 2]


def test_a_SUCCEEDING_observer_returns_its_value():
    assert Observations().run("x", lambda: 42) == 42


def test_the_RECORDER_ITSELF_cannot_break_the_subject():
    """THE DEFECT THAT PRODUCED THIS MODULE. An error handler that raises is worse than no handler:
    the failure surfaces as an outage of the thing being observed."""
    obs = Observations()

    class _Hostile(dict):
        def get(self, *a, **k):
            raise RuntimeError("the registry itself is broken")

    obs._failures = _Hostile()
    assert obs.run("x", _boom()) is None, "a broken recorder propagated into the caller"
    assert obs.recorder_failures == 1, "the recorder failed and did not say so"


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()])
def test_BASE_EXCEPTIONS_ARE_NOT_SWALLOWED(exc):
    """These are the runtime asking the process to stop. Absorbing them turns a shutdown into a hang
    and a cancelled task into a silently wedged one."""
    with pytest.raises(type(exc)):
        Observations().run("x", _boom(exc))


# ==================================================================================================
# 2. "logging errors might get logged too" — an absorbed failure must still be able to surface
# ==================================================================================================
def test_an_absorbed_failure_is_RECORDED_with_its_cause():
    obs = Observations()
    obs.run("book_truth", _boom(ValueError("no clock")))
    row = obs.as_rows()[0]
    assert row["observer"] == "book_truth"
    assert "no clock" in row["last_error"] and "ValueError" in row["last_error"]


def test_repeated_failures_COALESCE_into_a_count():
    """275 identical refusals are one condition with a number, not 275 events."""
    obs = Observations()
    for _ in range(275):
        obs.run("x", _boom())
    assert obs.as_rows()[0]["failures"] == 275 and len(obs.as_rows()) == 1


def test_an_EXCEPTION_WITH_NO_MESSAGE_still_records_something():
    """Several common exceptions stringify to "". Recording that verbatim reads as no error at all."""
    obs = Observations()
    obs.run("x", _boom(KeyError()))
    assert obs.as_rows()[0]["last_error"].strip()


def test_the_failure_REACHES_A_LOG_LINE_as_well_as_state():
    """Standing state replaces a LOST exception, not an operator ever hearing about it."""
    obs = Observations()
    obs.run("book_truth", _boom(ValueError("no clock")))
    assert any("book_truth" in l and "no clock" in l for l in obs.log_lines())


def test_RENDERING_IS_SEPARATE_FROM_RECORDING():
    """A broken log sink must not be able to lose the record — which is the ordering the two defects
    behind this module had backwards."""
    obs = Observations()
    obs.run("x", _boom())
    assert obs.as_rows(), "the record depends on rendering having happened"


def test_a_DROPPED_failure_is_counted_not_discarded():
    """The cap is a defence against a runaway observer; a defence that hides what it discarded is a
    second bug."""
    obs = Observations()
    for i in range(MAX_OBSERVERS + 5):
        obs.run(f"observer-{i}", _boom())
    assert obs.dropped == 5
    assert any("DROPPED" in l for l in obs.log_lines())


# ==================================================================================================
# 3. "missing logs can be confusing" — absence must be readable
# ==================================================================================================
def test_an_observer_that_NEVER_RAN_is_distinguishable_from_one_that_ran_clean():
    """THE ONE THAT IS EASY TO LEAVE OUT. On every surface that reports only failures, an observer
    producing nothing looks exactly like a healthy quiet one — and that is where four defects hid."""
    obs = Observations()
    obs.declare("ran", "never")
    obs.run("ran", lambda: 1)
    assert obs.state_of("ran") == "ok"
    assert obs.state_of("never") == "never ran"
    assert obs.summary()["never_ran_names"] == ["never"]


def test_an_UNDECLARED_observer_is_not_silently_healthy():
    assert Observations().state_of("nobody") == "never ran"


def test_the_summary_says_ZERO_OF_N_not_just_zero():
    """`0 of 0` is not `0 of 4`. A clean report from a system with no observers wired must not read
    like a clean report from four working ones."""
    empty = Observations().summary()
    assert empty["declared"] == 0 and empty["failing"] == 0

    wired = Observations()
    wired.declare("a", "b", "c", "d")
    for n in ("a", "b", "c", "d"):
        wired.run(n, lambda: 1)
    assert wired.summary() == {**wired.summary(), "declared": 4, "ok": 4, "failing": 0, "never_ran": 0}


def test_a_LATER_SUCCESS_does_not_erase_an_earlier_failure():
    """Both facts are true and an operator needs both: it is working now, and it broke N times."""
    obs = Observations()
    obs.declare("x")
    obs.run("x", _boom())
    obs.run("x", lambda: 1)
    assert obs.state_of("x") == "failing"
    assert obs.as_rows()[0]["failures"] == 1
