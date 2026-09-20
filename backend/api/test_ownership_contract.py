"""The ownership contract, enforced by AST rather than by review (#437).

Four defects in two days had one shape: code reached for the ACCOUNT-level view where the STRATEGY-level
view was meant, and nothing made that a mistake. Review caught none of them. These rules make it
mechanical.

TWO DESIGN CHOICES, both learned the hard way:

* **Scoped PER FUNCTION, never per module.** kumo-trading-strategies' first attempt at the matching rule was
  module-wide and produced a false positive on its first run. A guard that cries wolf gets switched off,
  and then it protects nothing at all.
* **Exemptions are named to the CONSTRUCTION, not to the module.** `engine_node.py` is not trustworthy;
  one specific stop-building block is. Naming the file would exempt nine thousand lines by accident.
"""

from __future__ import annotations

import ast
import pathlib

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"
_SRC = _ENGINE.read_text()
_TREE = ast.parse(_SRC)

#: THE ONE EXEMPTION, and it is a modelling gap rather than an oversight (#437, "the part that is NOT a
#: missing call site").
#:
#: Protective stops rest against the ACCOUNT NET. Nautilus NETTING position ids are PER-STRATEGY. When
#: two strategies hold one symbol there is no correct `position_id` to name — WHD on 2026-08-21 was
#: exactly that, `WHD.XNYS-BCTROT-004` and `WHD.XNYS-MOMENTUM-002` against one broker position.
#:
#: The alternative was rejected on measurement, not taste: per-strategy protection means N stops on one
#: symbol, and Alpaca reserves shares against a resting sell and releases them only on a CONFIRMED
#: cancel. Observed live 2026-08-21: `PROT-SELL-MRVL … 403: insufficient qty available (requested: 11,
#: available: 0)` while PEAK's own trail held all 30. So protection stays account-level, and it is
#: written down here rather than left as an absence.
#:
#: Named to the FUNCTION that builds the stop. If that construction is ever split or renamed, this stops
#: matching and the rule bites — which is the intent.
PROTECTION_EXEMPTION = "_reconcile_protection_inner"


def _functions(tree=None):
    for node in ast.walk(_TREE if tree is None else tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _by_name(name: str):
    for f in _functions():
        if f.name == name:
            return f
    raise AssertionError(f"{name} no longer exists — this rule is blind and must be rewritten")


def _sets_reduce_only(fn) -> bool:
    """Reduce-only set either as a keyword (`reduce_only=True`) or as a dict entry (`"reduce_only": True`).

    BOTH FORMS, because the only occurrence in this codebase today is the dict form — `_submit_order`
    takes a payload dict, not keywords. A rule that matched only `ast.keyword` would find nothing, pass,
    and report the contract as upheld across a codebase that never satisfies it."""
    for n in ast.walk(fn):
        if isinstance(n, ast.keyword) and n.arg == "reduce_only":
            return True
        if isinstance(n, ast.Constant) and n.value == "reduce_only":
            return True
    return False


def _submits_without_position_id(fn) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) in ("_submit", "submit_order"):
            if not any(kw.arg == "position_id" for kw in n.keywords):
                return True
    return False


def test_an_empty_parse_yields_NO_functions_so_the_guard_below_can_bite():
    """The guard's own property. Survived the first sweep: loosening `> 100` to `>= 0` changed nothing,
    because engine_node.py always parses and no fixture could make it not. Asserting that an empty tree
    really does yield zero functions is what makes the threshold below a live check rather than a
    decoration — every rule in this file is vacuously true over an empty parse."""
    assert list(_functions(ast.parse(""))) == []


def test_the_fixture_can_see_the_code_it_judges():
    """An AST rule over an empty parse passes for the wrong reason, silently, forever."""
    names = {f.name for f in _functions()}
    assert len(names) > 100, f"only {len(names)} functions parsed — engine_node.py did not load"
    assert PROTECTION_EXEMPTION in names


def test_the_exemption_names_a_function_that_ACTUALLY_sets_reduce_only():
    """Fixture property first, and it is the one that makes the next test non-vacuous.

    If the exemption named a function that never set the flag, the rule below would pass no matter what
    the rest of the file did — an exemption for a case that does not exist, guarding nothing. This is the
    same shape as kumo-trading-strategies' truncation test that passed twice with the bug reintroduced."""
    assert _sets_reduce_only(_by_name(PROTECTION_EXEMPTION))
    assert _submits_without_position_id(_by_name(PROTECTION_EXEMPTION)), (
        "the exemption is no longer needed — the protection reconciler now names a position, so this "
        "carve-out should be DELETED rather than left standing"
    )


def test_reduce_only_orders_name_their_position_except_the_one_exemption():
    """`risk/engine.pyx` gates the ENTIRE reduce-only check behind `command.position_id is not None`:

        if order.is_reduce_only:
            position = self._cache.position(command.position_id)
            if position is None or not order.would_reduce_only(...):
                self._deny_command(...)

    So a reduce-only order submitted without a position id is not protected — it is decorated. A stop
    that outlives its position does not close anything when it fires; it OPENS the opposite side. That is
    #252's FIG, #245's oversell and the phantom HSBC short, in one sentence.
    """
    offenders = [
        f.name for f in _functions()
        if f.name != PROTECTION_EXEMPTION
        and _sets_reduce_only(f)
        and _submits_without_position_id(f)
    ]
    assert offenders == [], (
        f"{offenders} build a reduce-only order and submit it without `position_id`, so the risk engine "
        f"never checks the flag. Either name the position, or add a NAMED exemption here with the reason"
    )
