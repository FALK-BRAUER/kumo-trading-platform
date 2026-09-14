"""`_decide` must feed `decide()` the FEATURIZED panel (#385).

WHY THIS FILE EXISTS
--------------------
QC345-003's first live rebalance, 2026-08-20 13:35:00 UTC, died and booked nothing:

    QC345 2026-08-20: TRADING: NO DECISION — decision failed: KeyError (submitted 0)
    journal: QC345 decision failed: KeyError: 'eligible'

`QC345ComputedSource.__init__` calls `build_feature_panel` and keeps the result as `self.panel` — that
is what adds `eligible`, `momentum`, `liquidity_proxy`, `realized_volatility`. `_decide` read `universe`
off that featurized frame and then sliced `day` out of the RAW argument, which has only OHLC. `decide()`
reaches `scoped["eligible"]` and raises.

It read as correct because the featurization is a SIDE EFFECT of constructing the source, the line above
works off that source, and both frames are called `panel`. The step was never missing — it was assigned
where the next line did not look.

WHY NOTHING CAUGHT IT. The engine's tests drive `decide()` directly with an already-featurized panel, so
they cannot see this. The adapter's own path slices from the featurized frame correctly. Nothing drove
`_decide` end to end — the gateway had never completed a live decision, so the line had never executed.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

_QC345 = pathlib.Path(__file__).resolve().parents[1] / "strategies" / "qc345.py"


def _decide_source() -> str:
    """Located by the `decide(` call it makes, not by name — names change."""
    tree = ast.parse(_QC345.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_decide":
            return textwrap.dedent(ast.get_source_segment(_QC345.read_text(), node) or "")
    raise AssertionError("_decide is gone — this test is blind")


def test_the_scan_reads_the_real_function():
    src = _decide_source()
    assert "QC345ComputedSource" in src
    assert "decide(" in src


def test_day_is_sliced_from_the_FEATURIZED_frame_not_the_raw_argument():
    """THE DEFECT, STATED AS A PROPERTY.

    The frame `decide()` receives and the frame `source.eligible()` was computed from must be the SAME
    one. Two frames here is exactly what produced the KeyError — and the raw one is what the live path
    passes in.
    """
    src = _decide_source()
    tree = ast.parse(src)

    # find `day = <something>.loc[...]` and check what it subscripts
    day_sources: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "day" for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute) and sub.attr == "loc" and isinstance(sub.value, ast.Name):
                day_sources.append(sub.value.id)

    assert day_sources, "`day` is no longer assigned from a `.loc` slice — this test is blind"
    for name in day_sources:
        assert name != "panel", (
            "`day` is sliced from the RAW `panel` argument, which has no `eligible` column. "
            "`decide()` will raise KeyError: 'eligible' on the first live rebalance — it did, on "
            "2026-08-20, and QC345 booked nothing."
        )


def test_the_featurized_frame_is_the_SOURCE_S_panel():
    """`source.panel` is the only frame `build_feature_panel` has run over. Binding `day` to anything
    else — a second `build_feature_panel` call, say — would featurize twice and could diverge."""
    src = _decide_source()
    assert "source.panel" in src, "nothing reads the source's featurized panel"
    # CHECKED AS A CALL, NOT A SUBSTRING. The comment above this code explains what
    # `build_feature_panel` does, and a text search cannot tell an explanation from an invocation —
    # the same false positive that fired on `market_exit` in the flatten test.
    called = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
    }
    assert "build_feature_panel" not in called, (
        "_decide featurizes a second time; the source already did it and two frames can disagree"
    )


def test_session_date_comes_from_the_same_frame_as_day():
    """A `session_date` taken from one frame and a `day` sliced from another can select nothing at all —
    an empty decision that looks like 'no candidates' rather than a bug."""
    src = _decide_source()
    tree = ast.parse(src)
    frames: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) or isinstance(sub, ast.Attribute) and sub.attr == "loc" and isinstance(sub.value, ast.Name):
                    frames.setdefault(target, sub.value.id)
    if "session_date" in frames and "day" in frames:
        assert frames["session_date"] == frames["day"], (
            f"session_date comes from {frames['session_date']} but day is sliced from {frames['day']}"
        )
