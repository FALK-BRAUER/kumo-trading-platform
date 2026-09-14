"""`_submit_trailing_stop` is the SECOND unstamped path, and nothing was watching it (#748).

The protection dispatch was fixed to refuse a stop it cannot stamp. This function was not, and it
mints the same phantom by the same mechanism:

    ctx = OrderBuildContext(self.order_factory, ...)      <- ALWAYS MANUAL-001

`stamp_for` appears in it zero times. It is reached from PEAK's arm (`peak ... PKW-` coids), the
manager tick's tighten, and `_replace_trailing_stop` — so any lane-held instrument those paths touch
gets a stop stamped with the display strategy, whose fill resolves to a position that never existed.

Its own comment claims "every trailing stop in this engine is placed through here — the protection
reconciler, PEAK's arm, the manager tick's tighten". The reconciler does NOT route through it; it
builds via `_build_order`. A comment that reads as coverage while being false is how this path stayed
invisible while the reconciler half was being fixed twice.

WHY IT RAISES RATHER THAN RETURNING None: the callers submit the replacement BEFORE cancelling the
order being replaced, so a silent None cancels the old stop and places nothing — turning a refusal
into a naked position. That contract already exists here for the exit-release case; this reuses it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import api.engine_node as mod


def _source() -> str:
    return textwrap.dedent(inspect.getsource(mod.UiFeedStrategy._submit_trailing_stop))


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the function must actually build an order context, or the assertions below are
    about a function that no longer does the thing."""
    assert "OrderBuildContext" in _source()


def test_it_does_NOT_build_with_the_display_strategys_own_factory():
    """`self.order_factory` is MANUAL-001's. Under NETTING that stamp decides which position a fill
    belongs to, so using it on a lane-held instrument is the mint."""
    tree = ast.parse(_source())
    ctxs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "OrderBuildContext"]
    assert ctxs, "no OrderBuildContext construction found — this test is blind"
    first_arg = ast.unparse(ctxs[0].args[0])
    assert first_arg != "self.order_factory", (
        "the trailing stop is built with the display strategy's factory, so every order it places is "
        "stamped MANUAL-001 regardless of who holds the instrument"
    )


def test_it_resolves_the_OWNER_and_uses_the_LANE_factory():
    src = _source()
    assert "stamp_for" in src, "the owner is never resolved"
    assert "_lane_order_factory" in src, (
        "the resolved owner is not used to build — resolving a lane and then not stamping with it is "
        "the mint with an extra function call"
    )


def test_it_REFUSES_rather_than_falling_back_when_there_is_no_owner():
    """Same rule as the protection dispatch. A fallback here is not weaker protection — it is an
    order that corrupts the book when it fires."""
    src = _source()
    assert "raise" in src
    tree = ast.parse(src)
    raises = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert any("owner" in r.lower() or "stamp" in r.lower() or "lane" in r.lower() for r in raises), (
        f"nothing raises about a missing owner: {raises}"
    )


def test_the_STALE_COMMENT_about_the_reconciler_is_gone():
    """It claimed the protection reconciler routes through here. It does not — it builds via
    `_build_order`. A comment that reads as coverage while being false is why this path stayed
    unexamined while the reconciler half was fixed twice; CLAUDE.md calls that the most dangerous
    kind of wrong because it survives review by sounding careful."""
    src = _source()
    assert "the protection reconciler, PEAK's arm" not in src, (
        "the false coverage claim is still in the docstring"
    )
