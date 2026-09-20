"""A protective order must not be able to OPEN a position, and today nothing stops it.

WHY THIS FILE EXISTS
--------------------
Build-spec item 6 said: pass `position_id` on submit, because `risk/engine.pyx:425` gates the entire
reduce-only check behind `if command.position_id is not None`. That is true, and **on its own it is a
no-op.**

Cockpit sets `reduce_only` on nothing. Two occurrences in the whole backend, both READS, and one of them
says so outright — `trade_cycle.py:220`: *"Matched on ORDER TYPE, not on `is_reduce_only`. The #239
backstop does not set that flag."* So enabling enforcement would enforce a flag no order carries: the
"built, configured, deployed, never executed" category, arriving inside the fix for it.

Both halves are needed, and either alone is inert:

  1. protective and exit orders are BUILT `reduce_only=True`
  2. `_submit` passes `position_id`, so the risk engine actually checks it

WHAT IT PREVENTS
----------------
A resting stop that outlives its position. If a 100-share stop rests and the position closes by another
route — a manual flatten, a PEAK exit, a reconciliation — the stop firing does not close anything. It
OPENS a 100-share short. That is #252's FIG, #245's oversell, and the phantom HSBC short in one sentence,
and it is the failure this flag exists to make impossible.

The Alpaca client does not forward the flag either (zero occurrences), so venue-side enforcement is a
separate question from engine-side. This file asserts the engine-side half, which is the one that can be
made true without a venue round trip.

Build-spec item 6.
"""

from __future__ import annotations

import ast
import pathlib

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _func_source(name: str) -> str:
    source = _ENGINE.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} no longer exists — this test is blind and must be rewritten")


def test_protective_orders_are_built_reduce_only():
    """Half one. Without this, enforcement has nothing to enforce.

    Asserted on the protection reconciler's own build, not on `_build_order` generally: an ENTRY must
    not be reduce-only, so a blanket assertion would be wrong and would push production toward a defect.
    """
    src = _func_source("_reconcile_protection_inner")
    assert "reduce_only" in src, (
        "the protection reconciler builds its stops without `reduce_only` — a stop that outlives its "
        "position does not close anything when it fires, it OPENS the opposite side. That is #252's "
        "FIG and the phantom HSBC short"
    )


def test_submit_passes_a_position_id_so_the_flag_is_enforced():
    """Half two. Without this, the flag is decoration.

    `risk/engine.pyx:425` gates the whole reduce-only check behind `command.position_id is not None`.
    `_submit` calls `submit_order(order)` with one positional argument, so the check never runs.
    """
    src = _func_source("_submit")
    import textwrap

    tree = ast.parse(textwrap.dedent(src)) if src.strip() else None
    assert tree is not None, "empty _submit"
    passes_id = any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "submit_order"
        and any(kw.arg == "position_id" for kw in n.keywords)
        for n in ast.walk(tree)
    )
    assert passes_id, (
        "`_submit` calls `submit_order` without `position_id`, so `risk/engine.pyx:425` skips the "
        "reduce-only check entirely — the flag is enforced for no order cockpit sends"
    )


def test_the_two_halves_are_pinned_together_not_separately():
    """The point of this file, stated as an assertion.

    Either half alone is inert, and inert-but-present is precisely how four defects survived unnoticed in
    this system. If a future change removes one, this names the other as the reason it mattered.
    """
    reconciler = _func_source("_reconcile_protection_inner")
    submit = _func_source("_submit")
    assert ("reduce_only" in reconciler) == ("position_id" in submit), (
        "one half of reduce-only enforcement exists without the other. A flag nothing checks, or a check "
        "for a flag nothing sets — both are inert, and both LOOK done"
    )


def test_the_fixture_can_see_both_functions():
    """An assertion over an empty string passes for the wrong reason."""
    assert _func_source("_submit").strip() and _func_source("_reconcile_protection_inner").strip()
