"""An emergency flatten must close the STRATEGY's position, not open a new one beside it.

WHY THIS FILE EXISTS
--------------------
2026-08-19. The operator tried an emergency flatten on WHD, a MOMENTUM-002 position, and reported "I flatten it
it became short". The book afterwards:

    WHD.XNYS  MANUAL-001    FLAT  qty 0     realized -16.32 USD
    WHD.XNYS  MOMENTUM-002  LONG  136 @ 71.78

The flatten sold 136 shares MANUAL-001 had never held — opening a short — then bought them back. It cost
$16.32 and left MOMENTUM-002's 136 shares completely untouched. The operator could not emergency-exit a
strategy position AT ALL, and the UI still showed "Flattening WHD — 1372s" twenty minutes later.

THE MECHANISM
-------------
Under NETTING the position id is `{instrument}-{strategy_id}`. An order submitted with no `position_id`
is attributed to the strategy that SUBMITS it — the feed strategy, MANUAL-001 — not to the strategy that
owns the position being closed. The flatten looked the position up correctly, by strategy, and then
threw that away at the submit.

The same argument is what makes `reduce_only` enforceable: `risk/engine.pyx:425` gates the entire
reduce-only check behind `command.position_id is not None`. Without it the flag is decoration, and an
oversized close flips the position instead of closing it — #252's FIG.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _func_source(name: str) -> str:
    src = _ENGINE.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} no longer exists — this test is blind and must be rewritten")


def _flatten_source() -> str:
    """The handler that owns the immediate flatten path.

    Located by the deterministic client-order-id prefix rather than by a function name, because the name
    has changed once already and a rename must not silently retire this guarantee.
    """
    src = _ENGINE.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if 'f"FL-{cid[:20]}"' in seg:
            return seg
    raise AssertionError("no handler builds an FL- client order id — this test is blind")


def test_the_flatten_submits_against_the_positions_own_id():
    """The defect, stated as an assertion.

    `self._submit(order)` attributes the close to MANUAL-001 and opens a fresh position beside the one
    the operator asked to close.
    """
    tree = ast.parse(textwrap.dedent(_flatten_source()))
    submits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_submit"
    ]
    assert submits, "the flatten no longer calls _submit — this test is blind"
    for call in submits:
        assert any(kw.arg == "position_id" for kw in call.keywords), (
            "the flatten submits without a position_id, so under NETTING the close is attributed to the "
            "SUBMITTING strategy (MANUAL-001) and opens a new position instead of closing the "
            "strategy's — WHD sold 136 shares MANUAL-001 never held, and MOMENTUM kept all 136"
        )


def test_the_position_id_is_the_looked_up_positions_not_a_reconstruction():
    """It must be the id of the position the flatten actually resolved.

    Rebuilding `f"{instrument}-{strategy_id}"` by hand would be a second derivation of a fact Nautilus
    already owns, and the two would disagree the first time the format changed.
    """
    tree = ast.parse(textwrap.dedent(_flatten_source()))
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_submit"
    )
    kw = next(k for k in call.keywords if k.arg == "position_id")
    assert isinstance(kw.value, ast.Attribute) and kw.value.attr == "id", (
        "position_id is not the resolved position's own `.id` — a hand-built id is a second derivation "
        "of something Nautilus already owns"
    )


def test_submit_still_forwards_the_position_id_to_nautilus():
    """The other half of the seam. `_submit` accepting the argument and dropping it would be inert."""
    tree = ast.parse(textwrap.dedent(_func_source("_submit")))
    forwards = any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "submit_order"
        and any(kw.arg == "position_id" for kw in n.keywords)
        for n in ast.walk(tree)
    )
    assert forwards, (
        "`_submit` never passes position_id to `submit_order`, so both the attribution and the "
        "reduce-only check (risk/engine.pyx:425) are skipped for every order this engine sends"
    )
