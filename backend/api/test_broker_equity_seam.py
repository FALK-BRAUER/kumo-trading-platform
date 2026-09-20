"""Every strategy `NautilusBroker` serves must expose `broker_equity` the way the broker CALLS it.

WHY THIS FILE EXISTS
--------------------
2026-08-21, session 09:35 ET. QC345-003 journalled a decision — `enter [AMAT, DELL, INTC, LRCX, MRVL]`,
`exit []` — and submitted ZERO orders. The only trace anywhere:

    [ERROR] PLATFORM-001.QC345: QC345: rebalance 2026-08-21 failed: TypeError: 'NoneType' object is not callable

THE CHAIN. `NautilusBroker.equity()` is written as `return self.strategy.broker_equity()`, which assumes
the attribute is a METHOD. It is on `MomentumRotationStrategy`. On `QC345RotationStrategy` it is a
`@property` returning `float | None` — so the property EVALUATES first, and the `()` is then applied to
its result. When it evaluates to `None`, `None()` raises exactly the error above.

The property returns None when `self._broker_account` is empty. That is not rare: kumo-trading-platform issue 382,
deployed the day before, made the engine deliberately publish NO account frame when equity cannot be
derived — the correct fix for reporting cash as equity — which leaves `_broker_account` empty at exactly
the moment a strategy asks. #382 did not cause this; it made a latent seam defect reachable.

WHY IT IS TESTED FROM COCKPIT
-----------------------------
Both halves live in kumo-trading-strategies, so neither is cockpit's to fix. But cockpit is what DIES: the
gateway's `_equity_per_position` already handles a None equity correctly (`self._broker.equity() or
0.0`) and never gets the chance, because the TypeError is raised inside `equity()` before that guard.
A test on either kumo-trading-strategies class alone would pass — each is internally consistent. The defect
exists only in the relationship between them, which is the definition of a seam.

This is the same shape as `test_exit_seam_conformance.py`, which caught three seam defects the night it
was written, including a method that lived on a different object entirely. Aimed at the CLASS: every
rotation strategy the broker can hold, not just the one that failed tonight.

FIXED UPSTREAM in kumo-trading-strategies `e26556f` — `broker_equity` is a plain method on QC345 now. This file
was marked `xfail(strict=True)` until that landed, precisely so the fix could not arrive silently on
this side of the seam: it XPASSed, the cockpit build failed, and the marker had to be removed
deliberately. It stays as a live assertion because the NEXT strategy is otherwise free to make the same
choice, and the seam is silent again until an account frame goes missing.
"""

from __future__ import annotations

import inspect

import pytest


def _rotation_strategies():
    """Every strategy class the broker can serve, DISCOVERED — never listed.

    THIS FUNCTION WAS THE BUG IN THIS FILE. It used to name two modules explicitly,
    momentum_rotation and qc345_rotation. QC27/TECHIVOL-005 was wired months later and nobody
    added it, so the file went on passing while `QC27RotationStrategy.broker_equity` was a
    `@property` — the exact defect these tests exist to forbid, in the strategy that was about to
    go live. A hardcoded list makes a seam test cover the instances someone remembered, which is
    the opposite of what a seam test is for: the question is never "is THIS one right", it is
    "could ANY of them be wrong".

    Discovery is over the installed package, so a strategy added upstream is covered here the
    moment it exists, with no edit to this file and nobody to remember.
    """
    import importlib
    import pkgutil

    import kumo_strategies

    out, seen = [], set()
    for info in pkgutil.walk_packages(kumo_strategies.__path__, "kumo_strategies."):
        try:
            mod = importlib.import_module(info.name)
        except Exception:  # pragma: no cover - an unimportable optional module is not this seam
            continue
        for name, cls in vars(mod).items():
            if not inspect.isclass(cls) or cls.__module__ != mod.__name__:
                continue
            if name in seen or not hasattr(cls, "broker_equity"):
                continue
            seen.add(name)
            out.append((name, cls))
    return sorted(out)


def test_discovery_sees_every_strategy_cockpit_ACTUALLY_WIRES():
    """The guard on the guard: discovery that silently finds nothing passes every test above.

    Cross-checked against cockpit's own `broker.strategy = ...` sites rather than a second list —
    if cockpit wires a strategy whose class discovery cannot see, these tests are blind about the
    one strategy that is definitely live.
    """
    found = {name for name, _ in _rotation_strategies()}
    assert found, "discovery found NO strategy classes — every assertion in this file is vacuous"
    for expected in ("MomentumRotationStrategy", "QC345RotationStrategy", "QC27RotationStrategy"):
        assert expected in found, (
            f"{expected} is wired by cockpit (`broker.strategy = ...`) but discovery did not find "
            f"it — the seam is unchecked for a strategy that is actually running. Found: {sorted(found)}"
        )


def test_the_broker_calls_broker_equity_as_a_method():
    """The premise, read from the installed source rather than assumed.

    If `NautilusBroker.equity()` ever stops calling it, this file is measuring nothing and must be
    rewritten rather than left passing.
    """
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker

    src = inspect.getsource(NautilusBroker.equity)
    assert "broker_equity()" in src, (
        "NautilusBroker.equity no longer invokes `broker_equity()` — this test is blind"
    )


@pytest.mark.parametrize("name,cls", _rotation_strategies())
def test_broker_equity_is_callable_on_every_strategy_the_broker_serves(name, cls):
    """THE DEFECT, stated as an assertion.

    A `@property` here is not a style difference. `self.strategy.broker_equity()` evaluates the property
    and calls its RESULT — a float on a good day (`TypeError: 'float' object is not callable`) and None
    on the day the account frame is withheld (`TypeError: 'NoneType' object is not callable`, which is
    what took QC345's whole rebalance down).
    """
    attr = inspect.getattr_static(cls, "broker_equity")
    assert not isinstance(attr, property), (
        f"{name}.broker_equity is a @property, but NautilusBroker.equity() calls it as a method — the "
        f"property evaluates and `()` lands on its result. When it evaluates to None that is "
        f"`TypeError: 'NoneType' object is not callable`, and the strategy submits nothing"
    )
    assert callable(attr), f"{name}.broker_equity is {type(attr).__name__}, not callable"


@pytest.mark.parametrize("name,cls", _rotation_strategies())
def test_an_unavailable_equity_is_returned_not_raised(name, cls):
    """The second half, and the one that decides whether a missing account frame is survivable.

    Cockpit's `_equity_per_position` is written `self._broker.equity() or 0.0` — it ALREADY handles a
    None equity. It just never gets the chance, because the failure happens inside `equity()`. So the
    contract that matters is: an unavailable equity comes back as a value, it does not raise.

    Asserted on the SIGNATURE rather than by calling it, because constructing a live Nautilus strategy
    needs a node. `float | None` is the honest return type; a strategy declaring plain `float` while its
    body can return None is the same disagreement one level down.
    """
    attr = inspect.getattr_static(cls, "broker_equity")
    fn = attr.fget if isinstance(attr, property) else attr
    ann = inspect.signature(fn).return_annotation
    assert ann is not inspect.Signature.empty, f"{name}.broker_equity declares no return type"
    text = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    src = inspect.getsource(fn)
    returns_none = "return None" in src
    assert not (returns_none and "None" not in str(text)), (
        f"{name}.broker_equity is annotated `{text}` but its body returns None — a caller trusting the "
        f"annotation will not guard, and this is the value that appears exactly when the account frame "
        f"is withheld (#382)"
    )
