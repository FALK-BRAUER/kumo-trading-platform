"""One strategy's data dependency must not take the whole node down (#377).

WHAT HAPPENED. Enabling QC345 on 2026-08-19 crash-looped the engine. `build_qc345_strategy` resolves
its universe's exchanges through `TradableUniverse().exchanges()`, which fetches Alpaca's entire ~13k
asset list over HTTP — inside `build_node`. The fetch timed out, the error propagated, and MANUAL-001,
MOMENTUM-002 and BCTROT-004 never registered either. The book went unmanaged over a slow HTTP call that
had nothing to do with any of them.

WHAT MUST NOT REGRESS. The builders deliberately RAISE when a strategy is switched on and its wiring
is incomplete — "a strategy that is switched on and silently absent is the worst outcome available".
Since 2026-09-18 (#1123) that raise refuses THE LANE, loudly (recorded, degraded, paged), and no
longer the node: the crash-loop punished five lanes for one lane's configuration twice. Programming
errors still propagate — a bug is not a refusal. These tests pin every half, because a fix that
swallowed everything would trade one silent failure for another.
"""

from __future__ import annotations

import urllib.error

import pytest

from api.engine_node import build_optional_strategy


class _Sentinel:
    pass


def test_a_working_builder_is_returned_untouched() -> None:
    made = _Sentinel()
    assert build_optional_strategy("X", lambda: made) is made


def test_a_builder_that_declines_still_returns_None() -> None:
    # A gated-off strategy returns None rather than raising; that path is unchanged.
    assert build_optional_strategy("X", lambda: None) is None


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        urllib.error.URLError("unreachable"),
        ConnectionResetError("reset by peer"),
        OSError("generic transport failure"),
    ],
)
def test_a_TRANSPORT_failure_costs_only_that_strategy(exc) -> None:
    """The whole point. Every one of these is what a slow or unreachable Alpaca looks like from inside
    `build_node`, and none of them may reach the caller — because the caller aborts the node.

    Parametrised across the family deliberately: `TimeoutError`, `socket.timeout` and `URLError` are all
    OSError subclasses, and a fix that caught only the one observed on the day would leave the others
    able to do exactly the same damage.
    """
    assert build_optional_strategy("QC345-003", lambda: (_ for _ in ()).throw(exc)) is None


def test_a_MISCONFIGURATION_refuses_THAT_lane_loudly_and_never_the_node() -> None:
    """The half that changed on 2026-09-18 (#1123, umbrella #854 / #1102).

    Until then a builder's RuntimeError propagated out of `build_node` and the ENGINE exited — a
    crash-loop that took every other lane with it. Twice measured: 2026-08-19 (QC345's universe
    fetch) and 2026-09-18, when `CRSI_ENABLED: true` on an Alpaca instance would have taken
    MANUAL/MOMENTUM/BCTROT/TECHIVOL/QC345/SMHGLD offline for one lane's refusal
    (`crsi_short.py:619`, "requires SPLIT-ADJUSTED daily bars").

    "A strategy that is switched on and silently absent is the worst outcome" still holds — the
    replacement for the crash is LOUD absence: the lane is recorded under `skipped_builds()` with a
    `refused:` prefix, which `lanes_absent` carries to `/health` (subsystem `lanes` not ok → degraded
    → `subsystem_down:lanes` alert) and `make verify` reads. Nothing here is silent; only the blast
    radius shrank from the node to the lane.
    """
    from api import engine_node

    engine_node.clear_skipped_builds()
    boom = RuntimeError("strategies.CRSI_ENABLED is on but this node's bars come from 'alpaca'")
    out = build_optional_strategy("CRSISHORT-006", lambda: (_ for _ in ()).throw(boom))
    assert out is None
    recorded = engine_node.skipped_builds()
    assert recorded == {"CRSISHORT-006": f"refused: RuntimeError: {boom}"}, recorded


def test_a_REFUSAL_and_a_TRANSPORT_failure_are_recorded_as_different_kinds() -> None:
    """Three states on the record, not one string. "unreachable" means retry by restarting once the
    dependency is back; "refused" means the CONFIGURATION is wrong and a restart changes nothing. An
    operator reading `lanes_absent` must be told which, or the first response to a refusal is a
    pointless recreate."""
    from api import engine_node

    engine_node.clear_skipped_builds()
    build_optional_strategy("A", lambda: (_ for _ in ()).throw(TimeoutError("timed out")))
    build_optional_strategy("B", lambda: (_ for _ in ()).throw(RuntimeError("universe is empty")))
    recorded = engine_node.skipped_builds()
    assert recorded["A"].startswith("unreachable: "), recorded
    assert recorded["B"].startswith("refused: "), recorded


def test_a_REFUSED_lane_costs_only_itself_at_the_build_node_SEAM() -> None:
    """Through the REAL `build_node`, in a subprocess (a TradingNode cannot share the suite's
    interpreter — see `test_build_node_constructs_without_exec_engine_crash`). The CRSISHORT builder
    is replaced by one that raises the exact RuntimeError `crsi_short.py:619` raises on an Alpaca
    node; the node must still build, and the refusal must be on the record the health frame reads.
    A unit test on `build_optional_strategy` alone cannot say whether `build_node` still calls it
    for this lane, or catches around it."""
    import os
    import subprocess
    import sys

    code = (
        "import os; os.environ.pop('KUMO_DURABLE_CACHE', None);"
        "import strategies.crsi_short as m;"
        "m.build_crsi_short_strategy = lambda **kw: (_ for _ in ()).throw("
        "RuntimeError(\"strategies.CRSI_ENABLED is on but this node's bars come from 'alpaca'\"));"
        "from api.engine_node import build_node, skipped_builds;"
        "n = build_node();"
        "rec = skipped_builds();"
        "assert rec == {'CRSISHORT-006': \"refused: RuntimeError: strategies.CRSI_ENABLED is on but this node's bars come from 'alpaca'\"}, rec;"
        "assert 'MANUAL' in str([s.id for s in n.trader.strategies()]), [s.id for s in n.trader.strategies()];"
        "n.dispose();"
        "print('REFUSED_LANE_ONLY_OK')"
    )
    env = {**os.environ, "APCA_API_KEY_ID": "dummy", "APCA_API_SECRET_KEY": "dummy", "KUMO_ENGINE": "alpaca"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180, env=env)
    assert result.returncode == 0, f"build_node did not survive one lane's refusal:\n{result.stderr[-2500:]}"
    assert "REFUSED_LANE_ONLY_OK" in result.stdout


def test_a_programming_error_is_not_swallowed_either() -> None:
    # A TypeError or AttributeError in the builder is a bug, not a degraded dependency. Catching it here
    # would turn "this strategy is broken" into "this strategy is quietly missing" — the exact outcome
    # the raise-on-misconfiguration rule was written against.
    with pytest.raises(AttributeError):
        build_optional_strategy("X", lambda: (_ for _ in ()).throw(AttributeError("typo")))


def test_the_skip_is_LOUD(caplog) -> None:
    """Degraded-and-saying-so is what is being bought. Quietly absent is still unacceptable, so the skip
    logs at ERROR and names both the strategy and the cause."""
    import logging

    with caplog.at_level(logging.ERROR):
        build_optional_strategy("QC345-003", lambda: (_ for _ in ()).throw(TimeoutError("timed out")))
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "QC345-003" in blob, "the log does not say WHICH strategy is missing"
    assert "TimeoutError" in blob or "timed out" in blob, "the log does not say why"
