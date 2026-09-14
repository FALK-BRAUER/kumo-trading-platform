"""Cockpit must refuse to flatten another strategy's position, before it cancels anything.

WHY THIS FILE EXISTS
--------------------
The operator tried to emergency-flatten WHD, held by MOMENTUM-002. Two attempts, two different failures, both
worse than a refusal:

  WITHOUT a position_id — the sell is attributed to the SUBMITTING strategy, MANUAL-001, and OPENS a
  short beside the position it was meant to close. MOMENTUM-002 kept all 136 shares, MANUAL-001 booked
  -$16.32, and the UI reported "Flattening WHD — 1372s" long after it had finished doing the wrong thing.

  WITH a position_id — Nautilus denies it outright:

      `position_id` PositionId('WHD.XNYS-MOMENTUM-002') is not valid for NETTING OMS;
      expected 'WHD.XNYS-MANUAL-001' (use HEDGING for custom position IDs)

  and by then the resting protection has ALREADY been cancelled, because the cancel comes first in the
  sequence. Measured on 2026-08-19: 28 seconds with a live 136-share position and nothing resting.

Under NETTING a strategy may only submit against its own position. That is a property of the OMS, not a
bug to route around, and both attempts to route around it damaged something. Closing a strategy's
position has to be done BY that strategy.

THAT ROUTE NOW EXISTS (kumo-strategies#49, #374). `register_strategy` records the sibling INSTANCE and the
flatten calls `Strategy.close_position` on it, so the order and the position id both belong to the owner
and Nautilus accepts it. The refusal survives for the case it was actually written for — an owner this
node does not run — and is no longer the blanket answer that left an operator unable to exit a strategy
position at all.

What has NOT changed, and is what these tests defend: whatever the outcome, it is decided BEFORE anything
is cancelled. A refusal that arrives after the cancel has stripped the protection off a live position is
the incident, not the fix.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _flatten_source() -> str:
    """Located by the `FL-` client-order-id prefix, not by name — the name has changed once already."""
    src = _ENGINE.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if 'f"FL-{cid[:20]}"' in seg:
            return seg
    raise AssertionError("no handler builds an FL- client order id — this test is blind")


def _line_of(seg: str, predicate) -> int | None:
    tree = ast.parse(textwrap.dedent(seg))
    for node in ast.walk(tree):
        if predicate(node):
            return node.lineno
    return None


def test_a_foreign_strategys_position_is_still_discriminated():
    """The ownership check exists at all — it now decides where to send the close, not only whether to."""
    seg = textwrap.dedent(_flatten_source())
    guard = _line_of(
        seg,
        lambda n: isinstance(n, ast.Compare)
        and isinstance(n.ops[0], ast.NotEq)
        and getattr(n.left, "id", None) == "strategy_id",
    )
    assert guard is not None, (
        "the flatten does not check whether the position belongs to this strategy — without a "
        "position_id it opens a short attributed to MANUAL-001, and with one Nautilus denies it"
    )


def test_the_refusal_happens_BEFORE_anything_is_cancelled():
    """The whole point, and the difference between a refusal and an incident.

    The cancel comes early in this handler. A guard placed after it leaves the position bare and then
    fails anyway — which is exactly what the denied attempt did, for 28 measured seconds.
    """
    seg = textwrap.dedent(_flatten_source())
    guard = _line_of(
        seg,
        lambda n: isinstance(n, ast.Compare)
        and isinstance(n.ops[0], ast.NotEq)
        and getattr(n.left, "id", None) == "strategy_id",
    )
    cancel = _line_of(
        seg,
        lambda n: isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_cancel_reducing_leg",
    )
    assert cancel is not None, "the flatten no longer cancels resting exits — this test is blind"
    assert guard < cancel, (
        f"the ownership guard is at line {guard} but the first cancel is at {cancel} — a refusal that "
        f"arrives after the cancel has stripped the protection off a live position it then refuses to "
        f"close is the incident, not the fix"
    )


def test_the_message_names_the_owner_rather_than_failing_vaguely():
    """An operator staring at a red banner needs to know WHY and what to do, not that something failed."""
    seg = _flatten_source()
    tree = ast.parse(textwrap.dedent(seg))
    texts = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ] + [
        v.value for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
        for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]
    blob = " ".join(texts)
    assert "belongs to" in blob, "the refusal does not say whose position it is"
    assert "Nothing was cancelled" in blob, (
        "the refusal does not tell the operator the position still has its protection — which is the one "
        "fact that decides whether they need to act right now"
    )


def test_a_position_whose_OWNER_IS_REGISTERED_is_routed_rather_than_refused():
    """The capability, not just the guard (kumo-strategies#49).

    The blanket refusal was correct about the damage and wrong as a final answer: it left the operator unable to
    emergency-exit any strategy position, which is worse than it sounds when MOMENTUM-002 holds six of
    eight and is 3.4x over its sleeve. `register_strategy` now records the sibling instance, so the close
    can be submitted BY the owner.

    Asserted on the source because there is no rendered path to drive here, and the property is which
    object submits — exactly the wiring a helper test cannot see.
    """
    seg = textwrap.dedent(_flatten_source())
    assert "_sibling_strategies" in seg, (
        "the flatten does not look up the owning strategy — it can only ever refuse"
    )
    assert "close_position" in seg, (
        "nothing calls close_position on the owner; a self-submitted order carries this strategy's "
        "position id and is exactly what Nautilus denied"
    )
    # NOT market_exit: it closes ALL of that strategy's positions and cancels ALL its orders, so
    # flattening one name would dump the other five. kumo-strategies#49 proposed it; it is the wrong
    # granularity for what an operator asked for.
    #
    # Checked as a CALL, not as a substring. The code comment above the fix explains why market_exit was
    # rejected, and a text search cannot tell an explanation from an invocation — the same false positive
    # that fired on `periodPnlSource` when a comment quoted the line it had just removed.
    called = _line_of(
        seg,
        lambda n: isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "market_exit",
    )
    assert called is None, (
        "market_exit closes the strategy's WHOLE book — flattening AEM would also exit the other five "
        "MOMENTUM positions"
    )


def test_the_refusal_is_now_only_for_an_owner_this_node_does_not_run():
    """The narrowed refusal still exists, and still says the position kept its protection."""
    seg = _flatten_source()
    tree = ast.parse(textwrap.dedent(seg))
    texts = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ] + [
        v.value for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
        for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]
    blob = " ".join(texts)
    assert "does not run" in blob, (
        "the refusal no longer names WHY it refuses — an operator needs to know this is an unreachable "
        "owner rather than a rule that applies to every strategy position"
    )
