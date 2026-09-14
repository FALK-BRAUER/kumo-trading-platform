"""The market compass is kumo code. Nothing may reach outside the repo to grade a rotation.

REPLACES `test_fintrack_optional.py`, which pinned the opposite property — that the grading was a
mounted checkout whose ABSENCE was a legitimate deployment state. Every one of its five tests was
correct about the code and wrong about the intent:

    test_the_default_mount_point_is_unchanged
    test_an_instance_can_declare_where_fintrack_is
    test_an_ABSENT_mount_raises_the_CONFIGURATION_error_not_ImportError
    test_FintrackUnavailable_is_not_confusable_with_a_real_import_failure
    test_the_engine_reports_the_configuration_case_ONCE_and_separately

Together they made "this instance has no market view" a supported configuration, tested and defended.
staging-ibkr sat in that state for days showing "No rotations to show", and it read as deliberate
because a passing test said it was.

THE FEATURE WAS ALWAYS KUMO'S — the Alpaca REST fetch, the WebSocket channel, the tile. Only ~350
lines of ratio-and-Ichimoku maths lived outside, and `rotation_grade.py` brings them in.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_NOTHING_in_the_backend_reaches_a_mounted_checkout_to_grade():
    """AIMED AT THE CLASS. `sys.path.insert` plus a runtime import is how the grading got outside the
    repo in the first place; a second feature could arrive the same way."""
    # THE MECHANISM, NOT THE WORD. A first version matched the string `/fintrack/tools` and flagged
    # five files that merely RECOUNT the incident in a docstring — including `test_compose_bind_mounts`,
    # whose actual rule is "no relative host paths" and which exists BECAUSE of it. Describing what was
    # removed is not doing it again, and a test that cannot tell those apart gets suppressed.
    #
    # The hazard is precise: reading a mount location out of the environment, or putting a directory on
    # `sys.path` so a module outside the repo can be imported at runtime.
    offenders, scanned = [], 0
    for path in BACKEND.rglob("*.py"):
        if any(p in (".venv", "site-packages", "node_modules") for p in path.parts):
            continue
        if path.name == pathlib.Path(__file__).name:
            continue
        scanned += 1
        # A TEST may put a directory on `sys.path` — the comparison against the original does exactly
        # that, deliberately, and several others do it to reach a sibling package. The rule is about
        # what PRODUCTION imports at runtime, so the sys.path half is scoped to non-test modules.
        # Reading the mount out of the environment is forbidden everywhere.
        is_test = path.name.startswith("test_") or "/tests/" in str(path)
        tree = ast.parse(path.read_text(errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (not is_test
                        and isinstance(func, ast.Attribute) and func.attr in ("insert", "append")
                        and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
                        and getattr(func.value.value, "id", None) == "sys"
                        # A LITERAL PATH, not a computed one. `scripts/probe_trailing_replace.py` does
                        # `sys.path.insert(0, dirname(dirname(abspath(__file__))))` to import `api.*`
                        # from a standalone script — that points INSIDE the repo by construction and is
                        # not this hazard. A hardcoded string is a location someone chose, and that is
                        # exactly how `/fintrack/tools` got on the path.
                        and any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                                for a in node.args)):
                    offenders.append(
                        f"{path.relative_to(BACKEND)}:{node.lineno} sys.path <- literal path")
                # os.environ.get("KUMO_FINTRACK_TOOLS")
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == "KUMO_FINTRACK_TOOLS":
                        offenders.append(
                            f"{path.relative_to(BACKEND)}:{node.lineno} reads KUMO_FINTRACK_TOOLS")
    assert scanned > 100, f"only scanned {scanned} files — this would pass by finding nothing"
    assert not offenders, (
        f"{offenders} reach outside the repo to grade. The compass is kumo code; a mount location in "
        f"the environment means an env var can switch the market view off again")


def test_the_ADAPTER_computes_nothing_itself():
    """`rotation_from_cache` converts bars and shapes the payload. If it grows maths of its own there
    are two implementations of the compass and they will disagree."""
    src = (BACKEND / "strategies" / "rotation_from_cache.py").read_text()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    # `bars_measured` wraps `bars` to record how many rows each leg had — measurement, not maths.
    # Two stacks graded the same market differently and only ADX disagreed, because it is the one
    # output that reports series LENGTH; nothing in the payload said how deep either was.
    assert names <= {"bars_to_tuples", "build_payload", "rotation_tickers", "bars", "bars_measured"}, f"the adapter grew functions beyond conversion and shaping: {names}"


def test_EVERY_INSTANCE_can_grade_a_rotation():
    """There is no configuration in which the market view is absent. The exception that used to say
    so is gone, and this fails if anything reintroduces it."""
    import strategies.rotation_from_cache as adapter
    import strategies.rotation_grade as grade

    assert not hasattr(adapter, "FintrackUnavailable"), (
        "the 'this instance has no market view' exception is back — that state is not supported")
    assert len(grade.AXES) == 25
    assert len(grade.tickers()) == 27
