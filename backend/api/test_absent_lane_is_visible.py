"""A lane that failed to build must be visible in health — not merely logged (#539).

WHAT HAPPENED, alpaca-paper 2026-08-25. QC345-003's build-time universe fetch timed out.
`build_optional_strategy` did exactly what #377 designed it to do: logged an ERROR, returned None, and
let the node come up with every other lane intact. Correct, and a large improvement on crash-looping.

Then health said:

    status=ok   automated_lanes_registered=3   automated_lanes_running=3

**Three of three.** A perfect ratio, because the lane that failed to build was never added to
`siblings` and is therefore in neither number. `strategy_run_counts` counts `len(siblings)` as
"registered", so a lane that does not exist cannot be counted as missing.

This is #498's defect exactly — *"The lane COUNT said 2/2, a healthy-looking ratio when the missing
two are not counted"* — fixed for the DEPLOY path in `check-stack-agrees-with-itself.sh` and never for
`/health`, which is the thing an operator watches BETWEEN deploys. The absence went unnoticed for
hours, across a market session.

THE NODE ALREADY KNOWS. `build_optional_strategy` is the one place that sees the failure. It just
throws the fact away after logging it.

`strategy_run_counts`'s own docstring has the principle: *"FAILS CLOSED. An object that cannot answer
`is_running` counts as NOT running: reporting a lane as trading when we cannot tell is the silencing
direction."* An absent lane is the same silencing direction, one step earlier.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import engine_node


@pytest.fixture(autouse=True)
def _clean():
    engine_node.clear_skipped_builds()
    yield
    engine_node.clear_skipped_builds()


def _ok_lane():
    return SimpleNamespace(is_running=True, is_armed=True)


def test_the_fixture_can_record_and_clear(monkeypatch):
    """FIXTURE FIRST. If the recorder were a no-op every assertion below would pass over nothing."""
    engine_node.build_optional_strategy("X-001", lambda: (_ for _ in ()).throw(TimeoutError("net")))
    assert engine_node.skipped_builds(), "the skip was not recorded at all"
    engine_node.clear_skipped_builds()
    assert not engine_node.skipped_builds(), "clear did not clear"


def test_a_lane_that_FAILED_TO_BUILD_is_recorded_with_its_reason():
    """THE DEFECT. It was logged and discarded."""
    out = engine_node.build_optional_strategy(
        "QC345-003", lambda: (_ for _ in ()).throw(TimeoutError("urlopen error timed out")))

    assert out is None, "the #377 contract changed — a transport failure must still return None"
    skipped = engine_node.skipped_builds()
    assert "QC345-003" in skipped, f"the absent lane is not recorded: {skipped}"
    assert "timed out" in skipped["QC345-003"], (
        f"the reason was dropped: {skipped['QC345-003']!r}. 'QC345 is absent' without a cause sends "
        f"an operator to the logs, which is where this already was")


def test_a_SUCCESSFUL_build_records_nothing():
    """An alarm that fires on the normal path gets switched off."""
    engine_node.build_optional_strategy("MOMENTUM-002", _ok_lane)
    assert not engine_node.skipped_builds()


def test_a_PROGRAMMING_error_still_propagates_a_REFUSAL_is_recorded():
    """#377's separation by TYPE survives, one notch over (#1123, 2026-09-18): a builder's RuntimeError
    is a configuration REFUSAL and is recorded loudly (health degrades, the alert pages) instead of
    crashing the node; a TypeError/AttributeError is a BUG and still propagates."""
    engine_node.build_optional_strategy(
        "BAD-001", lambda: (_ for _ in ()).throw(RuntimeError("wiring is incomplete")))
    assert engine_node.skipped_builds()["BAD-001"] == "refused: RuntimeError: wiring is incomplete"
    with pytest.raises(AttributeError):
        engine_node.build_optional_strategy(
            "BUG-001", lambda: (_ for _ in ()).throw(AttributeError("typo")))


def test_health_REPORTS_the_absent_lane_and_refuses_to_call_itself_ok():
    """THE SEAM. Recording it and not surfacing it is the same defect one layer in.

    `3/3 and ok` while a lane is missing is the exact reading that let QC345 sit absent through a
    session. The ratio must count what was EXPECTED, and the status must not be `ok`.
    """
    engine_node.build_optional_strategy(
        "QC345-003", lambda: (_ for _ in ()).throw(TimeoutError("timed out")))

    siblings = {"MOMENTUM-002": _ok_lane(), "BCTROT-004": _ok_lane(), "TECHIVOL-005": _ok_lane()}
    # ABSENT IS PASSED IN, not read from module state. My first version had `strategy_run_counts`
    # read `_SKIPPED_BUILDS` directly, which turned a pure function into one carrying hidden global
    # state — a build failure recorded by ANY test leaked into every other test's arithmetic, and
    # four unrelated tests broke at once. The existing suite defended a property I had not noticed I
    # was breaking.
    registered, running = engine_node.strategy_run_counts(siblings, engine_node.skipped_builds())

    assert registered == 4, (
        f"registered={registered}. The absent lane is not in the denominator, so 3/3 reads healthy — "
        f"which is #498's defect, and why nobody noticed QC345 was gone for a whole session"
    )
    assert running == 3, f"running={running}; an absent lane must not be counted as running"


def test_the_count_is_unchanged_when_nothing_was_skipped():
    """The regression this could introduce: inflating the denominator on a healthy node."""
    siblings = {"A-001": _ok_lane(), "B-002": _ok_lane()}
    assert engine_node.strategy_run_counts(siblings) == (2, 2)
    assert engine_node.strategy_run_counts(siblings, {}) == (2, 2)


def test_the_CONSUMER_forwards_lanes_absent():
    """The allow-list that ate `next_fire_ns` two hours ago, and `last_equity` before it.

    `consumer.py`'s health dict names every key it forwards. A key nobody names is silently gone, and
    the engine publishing it is not enough — that is the FIFTH field to meet this seam.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent / "consumer.py").read_text()
    assert '"lanes_absent"' in src, "consumer.py drops lanes_absent; /health would never see it"

    line = next(l for l in src.splitlines() if '"lanes_absent"' in l and "self._health" in l)
    assert "bridge_ok" not in line, (
        f"lanes_absent is dropped on a stale frame: {line.strip()}. UNLIKE armed_lanes and "
        f"next_fire_ns, an absent lane is STILL absent when the engine dies — forgetting it on "
        f"staleness turns a real outage into a clean-looking one")


def test_an_absent_lane_makes_health_NOT_ok():
    """The headline, not just a field. `status` is ok iff every subsystem is ok, so absence has to be
    a subsystem to move it."""
    import pathlib

    src = (pathlib.Path(__file__).parent / "app.py").read_text()
    assert 'name="lanes"' in src, (
        "no `lanes` subsystem — an absent lane cannot make /health report anything but ok, which is "
        "exactly what let QC345-003 sit missing through a session")
    assert "lanes_absent" in src, "app.py never reads lanes_absent"


def test_the_counter_stays_a_PURE_FUNCTION_of_its_arguments():
    """The regression I actually shipped for ten minutes.

    Reading `_SKIPPED_BUILDS` inside `strategy_run_counts` made it depend on module state that any
    node build can write. A build failure recorded in one test changed another test's numbers, and
    four unrelated tests failed at once — correctly.
    """
    engine_node.build_optional_strategy("GHOST-001", lambda: (_ for _ in ()).throw(TimeoutError("x")))
    siblings = {"A-001": _ok_lane()}

    assert engine_node.strategy_run_counts(siblings) == (1, 1), (
        "the counter saw a recorded skip it was never given — it is reading module state again")
    assert engine_node.strategy_run_counts(siblings, engine_node.skipped_builds()) == (2, 1), (
        "passing the absent set explicitly no longer changes the denominator")


def test_build_node_STARTS_FROM_A_CLEAN_SLATE():
    """A lane removed from config must not report absent forever.

    A successful build clears its own entry, but a lane that is never built again cannot. Without
    this, deleting a strategy from the config leaves `/health` permanently not-ok about a lane nobody
    wants — and an alarm about a deliberate change is one an operator learns to ignore.

    Source-level: `build_node` constructs a whole TradingNode, which a unit test cannot stand up.
    """
    import inspect

    src = inspect.getsource(engine_node.build_node)
    assert "clear_skipped_builds()" in src, (
        "build_node does not reset the skip record — a lane removed from config stays 'absent' for "
        "the life of the deployment")
