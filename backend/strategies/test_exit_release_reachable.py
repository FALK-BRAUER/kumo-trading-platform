"""The exit release must be reachable from the object the broker actually holds.

WHY THIS FILE EXISTS
--------------------
`release_for_exit` lives on `UiFeedStrategy`, because every helper it needs does. `NautilusBroker` holds
a DIFFERENT object — the rotation strategy — and there is no delegation between them:

    momentum.py:327   _build_rotation(MomentumRotationStrategy, strategy_id="MOMENTUM-002", ...)
    momentum.py:445   strategy = strategy_cls(...)
    momentum.py:482   broker.strategy = strategy

So `await self.strategy.release_for_exit(...)` raises AttributeError. `pgrunner._submit` does not catch
it and `run()` does not wrap `_submit`, so it aborts the WHOLE session — exits AND entries — after the
first symbol's `phase="intent"` row is already journalled. A partial rotation failure would become a
total one, which is worse than the bug #358 set out to fix.

That version was written, reviewed by two humans-worth of agents, tested green on BOTH sides of the
repo boundary, and was unreachable. Cockpit's test bound the real function to a host; kumo-trading-strategies'
patched the method onto a double. Neither used the object the broker actually holds. Eighth
"built, configured, deployed, never executed" instance of the week — inside the fix for the seventh.

WHAT THIS ASSERTS
-----------------
Not "the method exists" — it did. That the object reached through `broker.feed` is one that HAS it, with
the real classes imported, not a stub. And it is aimed at the CLASS: every rotation builder must wire
the feed, so a third one added later cannot silently repeat this. BCTROT already did — it took the
`feed` parameter and did not forward it, which is the same gap one level down.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest


def test_the_rotation_strategy_does_not_have_release_for_exit():
    """The premise. If this ever fails, the wiring below is unnecessary and this file should go.

    Stated explicitly because the original defect was believing the opposite. Uses the REAL class rather
    than a stub — that is the entire point.
    """
    from kumo_strategies.runtime.nautilus.momentum_rotation import MomentumRotationStrategy

    assert not hasattr(MomentumRotationStrategy, "release_for_exit"), (
        "the rotation strategy now carries release_for_exit — if that is real, `broker.feed` is dead "
        "weight; if it is a stub someone added to make a test pass, it is the bug"
    )
    assert not hasattr(MomentumRotationStrategy, "__getattr__"), (
        "a __getattr__ was added; attribute delegation would make the reachability question depend on "
        "runtime forwarding rather than on wiring, and this file's assertions would be vacuous"
    )


def test_the_feed_strategy_is_the_one_that_has_it():
    from api.engine_node import UiFeedStrategy

    assert hasattr(UiFeedStrategy, "release_for_exit"), (
        "release_for_exit has moved off UiFeedStrategy — the broker's `feed` reference now points at an "
        "object that cannot release anything"
    )


def _builder_source(name: str) -> str:
    from strategies import momentum

    return textwrap.dedent(inspect.getsource(getattr(momentum, name)))


@pytest.mark.parametrize("builder", ["build_momentum_strategy", "build_bctrot_strategy"])
def test_every_rotation_builder_forwards_the_feed(builder):
    """Aimed at the class, because the instance already got it wrong once.

    BCTROT accepted `feed` and did not pass it on, so `broker.feed` would have been None for BCTROT-004
    while MOMENTUM-002 worked — the failure mode that looks like a working feature.
    """
    tree = ast.parse(_builder_source(builder))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_build_rotation"),
        None,
    )
    assert call is not None, f"{builder} no longer calls _build_rotation — this test is blind"
    assert any(kw.arg == "feed" for kw in call.keywords), (
        f"{builder} does not forward `feed` to _build_rotation, so the broker's feed reference is None "
        f"and every exit raises on a None attribute instead of releasing its shares"
    )


def test_the_assembly_sets_feed_on_the_broker():
    """The seam itself. `_build_rotation` taking the parameter proves nothing if it never lands."""
    tree = ast.parse(_builder_source("_build_rotation"))
    assigns_feed = any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "feed" for t in n.targets)
        for n in ast.walk(tree)
    )
    assert assigns_feed, (
        "_build_rotation never assigns `broker.feed`, so the parameter is accepted and discarded — the "
        "exact 'built, configured, never executed' shape this whole change is about"
    )


def test_the_engine_passes_a_real_feed_to_both_builders():
    """The last hop. Both builders defaulting `feed=None` means a missed call site fails silently."""
    engine = (pathlib.Path(__file__).parent.parent / "api" / "engine_node.py").read_text()
    tree = ast.parse(engine)
    for builder in ("build_momentum_strategy", "build_bctrot_strategy"):
        call = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == builder),
            None,
        )
        assert call is not None, f"engine_node no longer calls {builder} — this test is blind"
        assert any(kw.arg == "feed" for kw in call.keywords), (
            f"engine_node calls {builder}() without a feed, so it defaults to None and every exit for "
            f"that strategy fails on a None attribute"
        )
