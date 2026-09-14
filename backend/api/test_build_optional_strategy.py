"""One strategy's data dependency must not take the whole node down (#377).

WHAT HAPPENED. Enabling QC345 on 2026-08-19 crash-looped the engine. `build_qc345_strategy` resolves
its universe's exchanges through `TradableUniverse().exchanges()`, which fetches Alpaca's entire ~13k
asset list over HTTP — inside `build_node`. The fetch timed out, the error propagated, and MANUAL-001,
MOMENTUM-002 and BCTROT-004 never registered either. The book went unmanaged over a slow HTTP call that
had nothing to do with any of them.

WHAT MUST NOT REGRESS. The codebase deliberately RAISES when a strategy is switched on and its wiring
is incomplete — "a strategy that is switched on and silently absent is the worst outcome available".
That rule is about MISCONFIGURATION and must survive intact. These tests pin both halves, because a fix
that swallowed everything would trade one silent failure for another.
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


def test_a_MISCONFIGURATION_still_takes_the_node_down() -> None:
    """The half that must NOT be softened.

    `build_qc345_strategy` raises RuntimeError when its gate is on and its universe is empty — a node
    that boots cleanly without a strategy the operator switched on is the failure the original rule
    exists to prevent. Only the transport is forgiven.
    """
    boom = RuntimeError("QC345_ENABLED is on but QC345_UNIVERSE is empty")
    with pytest.raises(RuntimeError, match="QC345_UNIVERSE"):
        build_optional_strategy("QC345-003", lambda: (_ for _ in ()).throw(boom))


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
