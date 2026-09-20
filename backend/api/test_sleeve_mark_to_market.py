"""`actual` must track net asset value, and something must actually call the thing that makes it.

WHY THIS FILE EXISTS
--------------------
`budget_store.mark_to_market` was written, exported in `__all__`, and **called by nothing**. Its own
docstring states the requirement it exists to satisfy:

    "`actual` is defined as net asset value, so P&L must reach it or a profitable sleeve would show the
     same number forever. This is the only writer besides transfers."

Nothing calls it, so `actual` moves only on transfers — a decrement-only figure that drifts from reality
the moment a position gains or loses value.

That was cosmetic while a paced reducer was planned. It is not cosmetic now: `budget_gate.may_submit`
refuses **every entry** for a sleeve whose `is_reducing` is true (`budget_gate.py:119`), and
`is_reducing` is derived from `actual`. So the mechanism that IS the wind-down gates on a number that
never tracks P&L.

WHY THE OBVIOUS TEST WOULD NOT HAVE CAUGHT IT
---------------------------------------------
A unit test on `mark_to_market` passes today — the function is correct. The defect is that no caller
exists, which is invisible to any test of the function itself. This is the "built, configured, deployed,
never executed" category, and an unfailed unit test is how that category is created. So the first test
here asserts the SEAM: that a production caller exists at all.

Cockpit issue: the build spec's item 1.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_API = pathlib.Path(__file__).parent


def _production_sources() -> list[pathlib.Path]:
    """Every non-test module under `api/`. Tests calling it prove nothing about production."""
    return [p for p in _API.rglob("*.py") if not p.name.startswith("test_")]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "build-spec item 1 is BLOCKED. Wiring a caller on a position-reading tick supplies only the "
        "market-value half of `actual` — budget.py defines it as cash PLUS market value and there is "
        "no per-sleeve cash ledger — so a sleeve that goes fully flat gets actual=0 permanently and "
        "deployable = max(0, min(actual, target) - deployed) is 0 FOREVER. BCTROT-004 is in that state "
        "now. strict=True on purpose: if someone wires it, this XPASSes and the build FAILS, forcing "
        "them to read this reason before removing the marker."
    ),
)
def test_something_in_production_calls_mark_to_market():
    """The seam. `mark_to_market` existing is not the same as `actual` being maintained.

    Reintroduce the defect by deleting the caller and this goes red; a unit test on the function stays
    green throughout, which is precisely why the defect shipped.
    """
    callers: list[str] = []
    for path in _production_sources():
        if path.name == "budget_store.py":
            continue  # its own definition and `__all__` entry are not callers
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a broken module is a different failure
            continue
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if isinstance(node, ast.Call) and name == "mark_to_market":
                callers.append(f"{path.relative_to(_API)}:{node.lineno}")

    assert callers, (
        "`budget_store.mark_to_market` has no production caller, so a sleeve's `actual` never tracks "
        "net asset value — and `budget_gate` refuses every entry based on `is_reducing`, which is "
        "derived from it. The wind-down gates on a number nothing maintains."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "build-spec item 1 is BLOCKED. Wiring a caller on a position-reading tick supplies only the "
        "market-value half of `actual` — budget.py defines it as cash PLUS market value and there is "
        "no per-sleeve cash ledger — so a sleeve that goes fully flat gets actual=0 permanently and "
        "deployable = max(0, min(actual, target) - deployed) is 0 FOREVER. BCTROT-004 is in that state "
        "now. strict=True on purpose: if someone wires it, this XPASSes and the build FAILS, forcing "
        "them to read this reason before removing the marker."
    ),
)
def test_the_caller_is_on_a_recurring_path_not_a_one_shot():
    """Marking to market once at startup would satisfy the test above and still leave `actual` stale.

    Pins the REASON: NAV moves continuously, so the caller has to sit on something that recurs. Asserted
    against the module that owns the engine's periodic ticks rather than against a named function, so a
    rename does not silently retire the guarantee.

    NOTE, from code review: this first asserted `"mark_to_market" in engine_node.py` — a raw substring
    test. Under `xfail(strict=True)` that is worse than useless in both directions: merely MENTIONING
    `mark_to_market` in a comment there makes this XPASS and hard-fails the build with a reason that does
    not describe what happened, while a real caller added in another module plus a comment here passes
    vacuously. Same prose-matching class the sibling tests were rewritten three times to escape.
    """
    source = (_API / "engine_node.py").read_text()
    tree = ast.parse(source)
    called_in_engine = any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "mark_to_market"
        for n in ast.walk(tree)
    )
    assert called_in_engine, (
        "`mark_to_market` is never CALLED in engine_node.py, which owns every recurring tick — a "
        "one-shot mark elsewhere leaves `actual` stale between calls"
    )


def test_over_budget_is_derived_in_exactly_one_place():
    """Law 1 at the seam that matters — and this test's own first draft was wrong.

    It originally asserted that `budget_gate` reads `actual`. It does not, and should not: the gate reads
    `sleeve.is_reducing`, and the Sleeve owns the derivation. Asserting the gate touches `actual` would
    have been demanding the second derivation this rule exists to forbid — a test pushing production
    toward the defect it was written to prevent.

    What is actually worth pinning: `is_reducing` is derived from `actual` in ONE place. If a second
    module ever computes over-budget itself, the two will disagree, and the gate refusing entries will
    disagree with the panel reporting why.
    """
    derivers = [
        p.relative_to(_API)
        for p in _production_sources()
        if "is_reducing" in p.read_text() and "def is_reducing" in p.read_text()
    ]
    assert len(derivers) == 1, (
        f"`is_reducing` is defined in {len(derivers)} places ({derivers}) — over-budget must have one "
        f"derivation, or the gate and the panel will disagree about the same sleeve"
    )
