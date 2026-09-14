"""`claims_invariant` must IMPORT on a kumo-strategies pin that predates its predicates (#903).

`backend/api/claims_invariant.py:20` did `from kumo_strategies.runtime.executor.pgrunner import
over_claimed, own_ceiling` at MODULE scope — the last module-scope import of the strategies package in
cockpit production code after PR #902 (82 others are inside functions). A pin lacking either symbol
turns into an ImportError at import time of whatever imports `claims_invariant` — `api.app` imports
`claims_endpoint`, which imports it — so a pin bump takes the API down rather than reporting "the
predicate is absent". That is the shape that crash-looped staging2's engine boot tonight (#902).

Both symbols exist on every pin we run today (0fbcfff, 4d28488, 513d813). Latent, not live — which is
exactly when to fix it. THREE STATES: computed, absent-by-name, never a silent pass.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import sys

import pytest

_PGRUNNER = "kumo_strategies.runtime.executor.pgrunner"


@pytest.fixture
def pin_without_the_predicates(monkeypatch):
    """A pin that predates the predicates: the module is made UNIMPORTABLE (`None` in `sys.modules`
    makes `from … import` raise ImportError — the interpreter's own contract, not a stub)."""
    monkeypatch.setitem(sys.modules, _PGRUNNER, None)
    yield


def test_FIXTURE_the_pin_really_lacks_the_predicates(pin_without_the_predicates):
    with pytest.raises(ImportError):
        importlib.import_module(_PGRUNNER)


def test_the_module_AND_its_importer_still_IMPORT_on_a_pin_without_the_predicates(pin_without_the_predicates):
    """The API process imports `claims_endpoint`, which imports this. Red today: ImportError at import.
    Both are reloaded under the absent pin — the state a real deploy would be in (review: reloading
    only the inner module leaves the sibling, the one `api.app` actually imports, unproven)."""
    import api.claims_endpoint as endpoint
    import api.claims_invariant as mod

    importlib.reload(mod)
    importlib.reload(endpoint)


def test_the_endpoint_degrades_BY_NAME_instead_of_taking_the_api_down(pin_without_the_predicates):
    """`build_breaches` is what `/claims` returns. Three states: `ok`, `account_unreadable`, and now
    `predicate_absent` — never an empty breaches map that reads as a clean ledger."""
    import api.claims_endpoint as endpoint
    import api.claims_invariant as mod
    importlib.reload(mod)
    importlib.reload(endpoint)   # binds the RELOADED PredicateAbsent — the class the reloaded module raises
    from api.claims_endpoint import build_breaches

    b = build_breaches([{"strategy_id": "BCTROT-004", "symbol": "BETA", "qty": 156}], {"BETA": 79})
    assert b.status == "predicate_absent"
    assert b.breaches == {}
    assert b.error and _PGRUNNER in b.error and "over_claimed" in b.error


def test_the_alerts_path_RAISES_a_named_error_so_the_checked_wrapper_counts_it(pin_without_the_predicates):
    """`_announce_stranded_claims` calls `exit_ceilings` under `_checked`, which counts a raise as a
    broken check (BROKEN_CHECK_POLLS) — the loud path. A silent {} here would be a green tick on a
    check that could not run (`_checked erases inner failure`, memory)."""
    import api.claims_invariant as mod
    importlib.reload(mod)

    for call in (lambda: mod.exit_ceilings({"BETA": 79}, {"BCTROT-004": {"BETA": 79}}),
                 lambda: mod.claim_breaches({"BETA": 79}, {"BCTROT-004": {"BETA": 156}}),
                 # EMPTY CLAIMS TOO: a predicate never consulted because there was nothing to check
                 # would read an absent pin as "nothing stranded" — absence as permission (review).
                 lambda: mod.exit_ceilings({"BETA": 79}, {})):
        with pytest.raises(mod.PredicateAbsent) as info:
            call()
        assert _PGRUNNER in str(info.value) and "over_claimed" in str(info.value) and "own_ceiling" in str(info.value)


def test_the_STRANDED_CLAIMS_CHECK_itself_records_the_failure_under_the_wrapper(pin_without_the_predicates, monkeypatch):
    """Aimed at the caller, not one level below it (review, HIGH): `_announce_stranded_claims` under
    `_checked`, the way `run()` reaches it. A `except PredicateAbsent: return` grown there would pass
    every test on the module and turn an absent predicate into a green tick. With EMPTY claims as
    well, because `exit_ceilings(...) if claims else {}` never consulted the predicate at all."""
    import asyncio

    import api.claims_endpoint as endpoint
    import api.claims_invariant as mod
    importlib.reload(mod)
    importlib.reload(endpoint)
    from api.test_alerts import FakeTx, _book
    from api.test_alerts_node_surface import _claims_in_postgres, _production_node, _svc

    for rows in ([("BCTROT-004", "ARKK", 23.0), ("MOMENTUM-002", "ARKK", 23.0)], []):
        tx = FakeTx()
        _claims_in_postgres(monkeypatch, rows)
        svc = _svc(_production_node(_book(("ARKK.BATS", "BCTROT-004", 23))), tx)
        asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
        count, reason = svc._check_failures.get("stranded_claims", (0, ""))
        assert count == 1 and "PredicateAbsent" in reason, (rows, svc._check_failures)
        assert not tx.sent, "a stranded-claims alert went out from a check that could not compute"


def test_the_predicates_are_KUMO_STRATEGIES_objects_not_a_copy():
    """Stronger than the source-string check in test_claims_invariant.py, which passes on a comment
    or on a function nobody calls (review): what `_predicates()` returns carries the upstream module."""
    import api.claims_invariant as mod
    importlib.reload(mod)

    over_claimed, own_ceiling = mod._predicates()
    assert over_claimed.__module__ == _PGRUNNER and own_ceiling.__module__ == _PGRUNNER


def test_with_the_pin_present_nothing_changed():
    """The three live breaches of 2026-08-22 still report, through the same predicate — the identity
    test in test_claims_invariant.py keeps pinning that it is kumo-strategies' and not a copy."""
    import api.claims_invariant as mod
    importlib.reload(mod)
    from api.claims_endpoint import build_breaches

    b = build_breaches(
        [{"strategy_id": "BCTROT-004", "symbol": "BETA", "qty": 156},
         {"strategy_id": "MOMENTUM-002", "symbol": "WHD", "qty": 56}],
        {"BETA": 79, "WHD": 0},
    )
    assert b.status == "ok" and set(b.breaches) == {"BETA", "WHD"}


def test_NO_production_module_imports_kumo_strategies_at_module_scope():
    """THE CLASS. PR #902 fixed one module-scope import, this fixes the other; a third one added
    tomorrow must fail here, not at the next pin bump. Every `kumo_strategies` import in cockpit's
    production modules (api/, strategies/, scripts/ excluded) must sit inside a function or class."""
    backend = pathlib.Path(__file__).parent.parent
    offenders = []
    skip_dirs = {"scripts", ".venv", "__pycache__"}   # scripts run in their own process and fail loudly
    for path in sorted(backend.rglob("*.py")):
        rel = path.relative_to(backend)
        # DIRECTORY NAMES, not a substring: `"/test" in str(rel)` would silently exempt a production
        # file such as `api/testkit.py` (review).
        if (set(rel.parts[:-1]) & skip_dirs or path.name.startswith("test_")
                or any(part.startswith("test") for part in rel.parts[:-1]) or path.name == "conftest.py"):
            continue
        tree = ast.parse(path.read_text())

        # WALK, NOT `tree.body` (review): `try: from kumo_strategies… except ImportError: x = None` at
        # module scope is a `Try` node whose import the body-only scan never saw — and that form is
        # the banned silent fallback (`NoneType is not callable` instead of a named refusal). Only an
        # import INSIDE a def/class is the sanctioned shape.
        inside_def: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for sub in ast.walk(node):
                    inside_def.add(id(sub))
            # `if TYPE_CHECKING:` imports never execute at runtime — not the hazard (review).
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                for sub in ast.walk(node):
                    inside_def.add(id(sub))
        for node in ast.walk(tree):
            if id(node) in inside_def:
                continue
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kumo_strategies"):
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.Import) and any(a.name.startswith("kumo_strategies") for a in node.names):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        f"module-scope kumo_strategies imports: {offenders} — a pin predating the symbol takes the "
        "importing process down at import time instead of degrading by name (#902, #903)"
    )


def test_the_guard_ignores_TYPE_CHECKING_and_catches_the_try_except_form(tmp_path):
    """Fixture property for the class guard: the shapes it must and must not flag, on a synthetic
    module rather than on whatever the repo happens to contain today."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kumo_strategies.x import Y\n"
        "try:\n    from kumo_strategies.runtime.executor.pgrunner import over_claimed\n"
        "except ImportError:\n    over_claimed = None\n"
        "def f():\n    from kumo_strategies.z import W\n    return W\n"
    )
    tree = ast.parse(src)
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
                isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"):
            inside.update(id(s) for s in ast.walk(node))
    flagged = [n.lineno for n in ast.walk(tree) if id(n) not in inside and isinstance(n, ast.ImportFrom)
               and (n.module or "").startswith("kumo_strategies")]
    assert flagged == [5], f"the try/except form must be flagged and only it: {flagged}"
