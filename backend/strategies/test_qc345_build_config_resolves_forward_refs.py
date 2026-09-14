"""`_build_config` must resolve FORWARD-REFERENCED nested groups (#945).

Measured on paper 2026-09-11 06:53Z, first market-aware poll after deploying cockpit f05979c /
strategies 84d09d3: QC345-003's `entries_blocked` and `emergency_exit` hooks read `fault` on every
poll — `AttributeError: 'dict' object has no attribute 'signal'` — and two WARN pages went out.

`_build_config` detects a nested group with `is_dataclass(f.type)`. On 84d09d3
`QC345RotationConfig.market_view` is annotated as the STRING `"MarketViewConfig"` (a forward
reference), so `f.type` is a `str`, `is_dataclass` is False, and the resolved read-only settings
group `{signal: "none", window: 50, action: None, dwell: 1}` is passed through RAW. The dataclass
accepts the dict; the first reader of the field (`runtime/nautilus/market_aware.py:83`, `cfg.signal`)
raises. `exits` worked only because its annotation happens to be a real class — the same latent
defect sits behind every forward-ref group anyone adds next, which is why the fix is
`typing.get_type_hints(cls)` (closes the class), not a special case for `market_view`.

The fixture property comes first: on the installed package the raw annotation IS a `str`. If it
ever is not, the reachability of this bug has changed and the test must say so rather than pass.
"""
from __future__ import annotations

import dataclasses
import typing

import pytest

from strategies.qc345 import _build_config

#: EXACTLY what `settings.resolve("qc345")` carried into the builder on paper (the read-only group
#: #928's generator offers; `resolve()` fills its defaults).
_MARKET_VIEW_VALUES = {"signal": "none", "window": 50, "action": None, "dwell": 1}


def _cfg_cls():
    return pytest.importorskip("kumo_strategies.strategies.qc345_rotation.config").QC345RotationConfig


def test_FIXTURE_the_installed_annotation_for_market_view_is_a_forward_ref_STRING():
    """The premise of the defect. On a pin that declares the class directly the dict path is
    unreachable and this file's red test would be vacuous — so say which."""
    cls = _cfg_cls()
    field = {f.name: f for f in dataclasses.fields(cls)}.get("market_view")
    if field is None:
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view (a pin before e600383)")
    assert isinstance(field.type, str), f"annotation is {field.type!r}, not a forward-ref string — is the bug still reachable?"
    assert not dataclasses.is_dataclass(field.type), "is_dataclass on a str is False — that is the hole"
    resolved = typing.get_type_hints(cls)["market_view"]
    assert dataclasses.is_dataclass(resolved) and resolved.__name__ == "MarketViewConfig"


def test_a_forward_referenced_nested_group_is_built_as_its_dataclass_not_passed_through_as_a_dict():
    cls = _cfg_cls()
    if "market_view" not in {f.name for f in dataclasses.fields(cls)}:
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view")
    mv = pytest.importorskip("kumo_strategies.strategies.market_view")
    cfg = _build_config(cls, {"market_view": dict(_MARKET_VIEW_VALUES)})
    assert isinstance(cfg.market_view, mv.MarketViewConfig), f"market_view arrived as {type(cfg.market_view).__name__}"
    assert cfg.market_view.signal is mv.MarketSignal.NONE, "the hook reads `cfg.signal` — this is the attribute that raised"
    assert cfg.market_view.window == 50


def test_EVERY_nested_group_of_EVERY_strategy_config_cockpit_builds_resolves_to_a_dataclass():
    """Aimed at the class: for each strategy domain cockpit generates, every field whose RESOLVED
    hint is a dataclass must come back as that dataclass when its group is present in the values —
    whatever the raw annotation looks like."""
    from api.settings.generated import _STRATEGY_DOMAINS
    import importlib
    seen = 0
    for domain, (module_path, class_name, _overrides) in _STRATEGY_DOMAINS.items():
        cls = getattr(importlib.import_module(module_path), class_name)
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            sub = hints[f.name]
            if not dataclasses.is_dataclass(sub):
                continue
            seen += 1
            default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
            values = {f.name: dataclasses.asdict(default)}
            built = _build_config(cls, values)
            assert isinstance(getattr(built, f.name), sub), f"{domain}.{f.name}: built as {type(getattr(built, f.name)).__name__}"
    assert seen >= 2, f"the walk found {seen} nested groups — exits and market_view were expected"


def test_the_EXACT_paper_values_reach_the_hooks_reader_without_raising():
    """The reader that faulted: `cfg.signal` on the market view. Drive it through the real
    `market_state`-style access the hook uses — attribute reads — on the built config."""
    cls = _cfg_cls()
    if "market_view" not in {f.name for f in dataclasses.fields(cls)}:
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view")
    cfg = _build_config(cls, {"market_view": dict(_MARKET_VIEW_VALUES), "exits": {}})
    _ = cfg.market_view.signal, cfg.market_view.window, cfg.market_view.action, cfg.market_view.dwell


def test_enum_fields_arrive_from_settings_as_VALUE_STRINGS_and_are_built_as_members():
    """The second half of #945: `resolve()` returns "none" / "exit_only", not the members. A dataclass
    accepts the string; the next reader of `.name` raises. `Optional[Enum]` (action) unwraps; None
    stays None; an unknown value is refused by name, never stored as a string."""
    cls = _cfg_cls()
    if "market_view" not in {f.name for f in dataclasses.fields(cls)}:
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view")
    mv = pytest.importorskip("kumo_strategies.strategies.market_view")
    cfg = _build_config(cls, {"market_view": {"signal": "index_vs_ma", "window": 50, "action": "exit_only", "dwell": 1}})
    assert cfg.market_view.signal is mv.MarketSignal.INDEX_VS_MA and cfg.market_view.action is mv.MarketAction.EXIT_ONLY
    assert cfg.market_view.signal.name == "INDEX_VS_MA", "the read that raised"
    with pytest.raises(ValueError, match="not a member of MarketSignal"):
        _build_config(cls, {"market_view": {"signal": "breadth_count", "window": 50, "action": None, "dwell": 1}})


# -- blast radius: every other field, and every other lane, built IDENTICALLY before and after ------------

#: The last main commit BEFORE #949 landed the fix (first parent of the #949 merge). An IMMUTABLE
#: revision, on purpose: this fixture read `origin/main` until #962 — correct for exactly as long as
#: the fix was unmerged, and red on main from the moment it merged, because "the old builder" had
#: silently become the new one. A "before" artifact must name a revision that cannot move.
_PRE_FIX_REVISION = "aeed851"


def _old_build_config():
    """The builder as it was on main before #945, taken from git — the second derivation."""
    import subprocess, types
    src = subprocess.run(["git", "show", f"{_PRE_FIX_REVISION}:backend/strategies/qc345.py"],
                         capture_output=True, text=True, check=True).stdout
    # ANCHOR PROPERTY FIRST (#962): the resolved source must be the string-annotation builder, so a
    # revision that carries the fix fails HERE by name, not downstream as a dict/dataclass mismatch.
    # And it must be SOMETHING (#967 review): an empty `git show` satisfies "not in src" by vacuity.
    assert "def _build_config(cls, values: dict):" in src, f"{_PRE_FIX_REVISION} did not yield the old builder's source"
    assert "get_type_hints" not in src, (
        f"{_PRE_FIX_REVISION} is not a pre-#945 revision — the 'old builder' anchor has moved")
    start = src.index("def _build_config(cls, values: dict):")
    end = src.index("\nasync def _held_claims", start)
    ns: dict = {}
    exec(src[start:end], ns)
    return ns["_build_config"]


def test_FIXTURE_build_config_has_exactly_one_production_caller_and_the_old_builder_is_the_one_from_main():
    """If a second lane called this builder, its config would need its own before/after row below."""
    import subprocess
    # From the repo ROOT, whatever the suite's cwd is (CI and merge_gate run from backend/).
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    callers = subprocess.run(["git", "grep", "-n", "_build_config(", "--", "backend"], capture_output=True, text=True, cwd=root).stdout.splitlines()
    prod = [c for c in callers if "def _build_config" not in c and "/test_" not in c]
    assert {c.split(":")[0] for c in prod} == {"backend/strategies/qc345.py"}, prod   # its own recursion, one lane
    # The old builder IS the defective one — proved by behaviour, not by reading its source: it passes
    # the market_view dict through untouched on the installed package.
    cls = _cfg_cls()
    if "market_view" in {f.name for f in dataclasses.fields(cls)}:
        assert isinstance(_old_build_config()(cls, {"market_view": dict(_MARKET_VIEW_VALUES)}).market_view, dict)


def test_every_field_of_the_LIVE_qc345_config_is_identical_before_and_after_except_the_two_repaired_groups():
    """Explicit per-field equality on the exact values `resolve("qc345")` produces on this machine,
    old builder vs new. The only permitted differences: `exits` and `market_view` go from dict to
    their dataclass, and `asdict` of the new equals the old dict with enum members as their values."""
    from api import settings
    cls = _cfg_cls()
    values = settings.resolve("qc345")
    old_cfg = _old_build_config()(cls, values)
    new_cfg = _build_config(cls, values)
    changed = []
    for f in dataclasses.fields(cls):
        o, n = getattr(old_cfg, f.name), getattr(new_cfg, f.name)
        if o == n:
            continue
        changed.append(f.name)
        assert isinstance(o, dict) and dataclasses.is_dataclass(n), f"{f.name}: {type(o).__name__} -> {type(n).__name__} is not the repaired shape"
        def _plain(x):
            return {k: (v.value if hasattr(v, "value") else v) for k, v in x.items()}
        assert _plain(dataclasses.asdict(n)) == _plain(o), f"{f.name}: content changed, not just its type"
    assert set(changed) <= {"exits", "market_view"}, f"fields changed beyond the two repaired groups: {changed}"
    assert "exits" in changed, "exits was a dict on the live values before the fix — the class this ticket names"
