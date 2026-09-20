"""Both gateways must RECEIVE the venue's terminal answer (#383).

WHAT THIS DEFENDS
-----------------
`momentum_rotation._record_terminal` does:

    record = getattr(self._runner, "record_terminal", None)
    if record is None:
        return

Neither gateway defined it, so that returned None and no-opped on every fill — for WEEKS, on BOTH
strategies, with nothing in any log saying so. Measured 2026-08-20: **22 OrderFilled events, ZERO
terminal rows journalled**. The #51 retry loop has never closed in production.

The kumo-trading-strategies session initially told me MOMENTUM was the working reference and QC345 was the gap.
It was not: `grep -c record_terminal backend/strategies/momentum.py` returned 0. Both halves were inert.

This is the INERT-GETATTR shape — a call that silently does nothing because the receiving end was never
written. A test asserting the HANDLER calls `record_terminal` passes against exactly that, which is why
these tests assert on the GATEWAYS instead.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_STRATEGIES = pathlib.Path(__file__).resolve().parents[1] / "strategies"
GATEWAYS = [("momentum.py", "SessionGateway"), ("qc345.py", "QC345SessionGateway")]


def _method(filename: str, cls: str, name: str):
    tree = ast.parse((_STRATEGIES / filename).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return sub
    return None


@pytest.mark.parametrize("filename,cls", GATEWAYS)
def test_the_gateway_class_is_actually_there(filename, cls):
    """Fixture's own property — a renamed class would make every assertion below vacuous."""
    tree = ast.parse((_STRATEGIES / filename).read_text())
    assert any(isinstance(n, ast.ClassDef) and n.name == cls for n in ast.walk(tree)), (
        f"{cls} not found in {filename} — this test is blind"
    )


@pytest.mark.parametrize("filename,cls", GATEWAYS)
def test_the_gateway_defines_record_terminal(filename, cls):
    """THE DEFECT. Without this the adapter's getattr returns None and every fill is unrecorded."""
    assert _method(filename, cls, "record_terminal") is not None, (
        f"{cls} has no record_terminal — `_record_terminal`'s getattr returns None and no-ops on "
        f"every fill, exactly as it did for 22 fills on 2026-08-20"
    )


@pytest.mark.parametrize("filename,cls", GATEWAYS)
def test_the_signature_matches_the_caller_POSITIONALLY(filename, cls):
    """`record(session, sym, ok, detail)` is called positionally from inside a Nautilus event handler.
    A different arity or order fails there, which is the worst place to discover it."""
    fn = _method(filename, cls, "record_terminal")
    args = [a.arg for a in fn.args.args]
    assert args == ["self", "session", "symbol", "ok", "detail"], (
        f"{cls}.record_terminal{tuple(args)} does not match record(session, symbol, ok, detail)"
    )
    assert isinstance(fn, ast.AsyncFunctionDef), "the caller awaits it"


@pytest.mark.parametrize("filename,cls", GATEWAYS)
def test_it_cannot_raise_into_the_event_handler(filename, cls):
    """Called from a live Nautilus dispatch, outside any session's control flow. An exception here can
    kill the subscription — and a dead subscription looks exactly like 'no fills happened'."""
    src = ast.unparse(_method(filename, cls, "record_terminal"))
    assert "try:" in src and "except" in src, f"{cls}.record_terminal can raise into Nautilus's dispatch"


@pytest.mark.parametrize("filename,cls", GATEWAYS)
def test_the_row_matches_the_predicate_the_retry_counter_READS(filename, cls):
    """`pgrunner`'s attempt counter selects on `phase == "terminal" and not ok`. A row written with
    different keys is a row nothing can find — two writers of one fact, disagreeing."""
    src = ast.unparse(_method(filename, cls, "record_terminal"))
    assert "'phase': 'terminal'" in src or '"phase": "terminal"' in src, "no phase=terminal key"
    assert "'ok'" in src or '"ok"' in src, "no ok key"


def test_both_gateways_write_the_SAME_row():
    """One contract. Two gateways whose terminal rows differ would make the counter correct for one
    strategy and blind for the other — which is the state this issue is fixing."""
    bodies = []
    for filename, cls in GATEWAYS:
        src = ast.unparse(_method(filename, cls, "record_terminal"))
        bodies.append(src[src.index("self._journal.write"):] if "self._journal.write" in src else src)
    assert bodies[0] == bodies[1], "the two gateways journal terminal rows differently"
