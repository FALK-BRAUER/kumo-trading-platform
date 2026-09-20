"""No module-level definition may be silently shadowed by a later duplicate (#640).

Found during the #640 wiring audit: `momentum.py` defined `_lane_symbols` TWICE — once at line 297
and again at line 352, identical bodies. Commit 8dddc43 (#632) re-added a definition #627 had
already added: an edit that anchored blindly and duplicated instead of matching, the exact failure
class #640 names ("assert the anchor was found").

Why an identical duplicate is a live hazard and not a style nit: Python binds the LAST definition,
so the first is dead code that LOOKS live. The next fix edits whichever definition its author finds
first — and if that is the shadowed one, the change deploys cleanly, the suite stays green, and the
behaviour does not change. That is #640's class exactly: built, deployed, never executed. The
`_lane_symbols` docstring calls itself "the single place a lane's pool is finalised"; two copies of
the single place is the two-derivations trap with extra steps.

AIMED AT THE CLASS. This scans every production module under api/, scripts/, strategies/, actions/,
adapters/, config/ — not just momentum.py — because the defect is a property of how edits land, not
of that file. `test_no_orphan_mechanisms` catches a function nothing calls; this catches a
DEFINITION nothing can reach, which that tripwire cannot see because the name does have callers —
they just all reach the other copy.

Deliberate redefinitions are not flagged: `@overload` stubs, and conditional definitions inside
`if`/`try` blocks (TYPE_CHECKING guards, import fallbacks) are not direct children of the module
body and never scanned. Only an unconditional module-level def/class that unconditionally clobbers
an earlier one fails here — there is no legitimate reason for that shape, so there is no allowlist.

Fixture property first: the scanner is run against a synthetic module carrying the momentum shape,
and must find it — otherwise the sweep passing on the repo proves only that the scanner is blind
(non-empty is not complete; a detector that cannot see its subject can never fire).
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOTS = ("api", "scripts", "strategies", "actions", "adapters", "config")


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    for dec in getattr(node, "decorator_list", []):
        name = dec
        if isinstance(name, ast.Call):
            name = name.func
        if isinstance(name, ast.Attribute):
            name = name.attr
        elif isinstance(name, ast.Name):
            name = name.id
        if name == "overload" or (isinstance(name, str) and name == "overload"):
            return True
    return False


def _shadowed(tree: ast.Module) -> list[tuple[str, int, int]]:
    """(name, first_lineno, shadowing_lineno) for every unconditional module-level duplicate."""
    seen: dict[str, int] = {}
    out: list[tuple[str, int, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _is_overload(node):
            continue
        if node.name in seen:
            out.append((node.name, seen[node.name], node.lineno))
        seen[node.name] = node.lineno
    return out


def _production_modules() -> list[pathlib.Path]:
    files = []
    for root in ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            name = p.name
            if name.startswith("test_") or name == "conftest.py" or "__pycache__" in p.parts:
                continue
            files.append(p)
    return files


def test_the_scanner_sees_the_momentum_shape():
    """FIXTURE PROPERTY FIRST. A synthetic module with the exact defect (#632's duplicate
    `_lane_symbols`: identical body, docstring and all) must be found — if the scanner cannot see
    its subject, the repo sweep below is an assertion about nothing."""
    tree = ast.parse(
        "def _lane_symbols(symbols):\n"
        "    '''the single place'''\n"
        "    return list(symbols)\n"
        "\n"
        "def _other():\n"
        "    return 1\n"
        "\n"
        "def _lane_symbols(symbols):\n"
        "    '''the single place'''\n"
        "    return list(symbols)\n"
    )
    assert _shadowed(tree) == [("_lane_symbols", 1, 8)]


def test_the_scanner_does_not_flag_deliberate_redefinition_shapes():
    """Conditional defs (TYPE_CHECKING / import-fallback) and @overload stubs are not duplicates."""
    tree = ast.parse(
        "from typing import overload\n"
        "try:\n"
        "    def fast(): ...\n"
        "except ImportError:\n"
        "    def fast(): ...\n"
        "if True:\n"
        "    def cond(): ...\n"
        "def cond(): ...\n"  # the real def after a guarded stub is the guarded-def idiom
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x): return x\n"
    )
    assert _shadowed(tree) == []


def test_the_sweep_covers_every_production_root():
    """NON-EMPTY IS NOT COMPLETE (#640): the sweep must actually be reading the trees it claims to.
    Every root that exists must contribute parsed modules, and the two known-largest modules must be
    among them — a path typo would otherwise pass the sweep by scanning nothing."""
    files = _production_modules()
    scanned = {f.relative_to(ROOT).parts[0] for f in files}
    for root in ("api", "strategies", "scripts"):
        assert root in scanned, f"sweep lost the '{root}' tree entirely — the scan is blind there"
    names = {f.name for f in files}
    assert "engine_node.py" in names and "momentum.py" in names


def test_no_module_level_definition_is_shadowed_anywhere():
    """The sweep. Seen red 2026-08-29: momentum.py defined `_lane_symbols` at 297 and again at 352
    (8dddc43 re-added what 2eafb69 already had). Identical today — which is exactly when the next
    divergent edit to the dead copy ships green and does nothing."""
    offenders = []
    for p in _production_modules():
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError as exc:  # a production module that does not parse is its own emergency
            offenders.append(f"{p}: does not parse: {exc}")
            continue
        for name, first, second in _shadowed(tree):
            offenders.append(
                f"{p.relative_to(ROOT)}: '{name}' defined at line {first} and again at line "
                f"{second} — the second silently shadows the first; edits to the first copy "
                f"deploy cleanly and run never (#640)"
            )
    assert not offenders, "\n".join(offenders)
