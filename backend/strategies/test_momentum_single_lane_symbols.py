"""Dead and duplicated code in momentum (#652 item 7).

- `_universe` was the TTL cache for the Alpaca-backed `_instrument_ids`, which #622 deleted; the
  variable (and its "monkeypatched in tests" docstring) outlived its only reader. A knob nothing
  reads is the agreement-is-not-connection hazard: a test can patch it forever and prove nothing
  (api/test_rotation_resolves_in_one_fetch.py patches it with raising=False — a canary, unaffected).
- `_lane_symbols` was defined TWICE with identical bodies; the second silently shadowed the first.
  Two definitions of one fact drift — the whole subject of the verification-by-disagreement rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import strategies.momentum as momentum


def _module_tree() -> ast.Module:
    return ast.parse(Path(momentum.__file__).read_text())


def test_the_dead_universe_cache_is_gone():
    assert not hasattr(momentum, "_universe"), (
        "_universe is read by nothing since #622 deleted _instrument_ids — a module var that exists "
        "only to be monkeypatched by a test measures the test, not the code"
    )


def test_lane_symbols_is_defined_exactly_once():
    defs = [
        n for n in _module_tree().body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_lane_symbols"
    ]
    assert len(defs) == 1, (
        f"_lane_symbols has {len(defs)} module-level definitions — the later one shadows the "
        "earlier, and identical twins drift the moment one is edited"
    )


def test_lane_symbols_still_works_and_copies():
    """Coverage, not just absence: the surviving definition is the live one the lanes call."""
    src = ["AAPL", "MSFT"]
    out = momentum._lane_symbols(src)
    assert out == ["AAPL", "MSFT"]
    assert out is not src, "_lane_symbols must return a copy — the pool is finalised here, not aliased"
