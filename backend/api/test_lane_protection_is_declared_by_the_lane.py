"""A lane's protection stance is DECLARED ON THE LANE and read by the plane — one derivation (#1029).

THE DEFECT. `strategies/smhgld.py` declared `PROTECTION_MODE = "none"` and a docstring said the plane
"reports the exposure as `opted_out`". Nothing read that constant. The plane resolves a lane's mode from
the settings domain alone (`lane_modes(cfg)`, engine_node.py), the settings schema had no
`SMHGLD-007_protection` key, and `_strip_additional` erases any key the schema does not declare — so
both tenants' `protection.json` (`{"enabled": true}`) resolved SMHGLD-007 to TRAIL. Driven in the paper
engine container on 2026-09-12: `mode_of("SMHGLD-007") -> LaneProtection(mode='trail')`. The lane's
first live session would have had trailing stops rested on both legs within one 60 s pass; a stop-out
is a PROT fill foreign to the lane, and a lane with no flat state buys the leg back next rebalance.

This is #872 one lane over (QC345 exited by a trail its own config disabled) and the
[[agreement-is-not-connection]] shape: the declaration and the plane both existed, the wire between
them did not, and every surface was green because nothing could contradict a lane that had never held.

THE FIX IS ONE DERIVATION. `StrategyEntry.protection` is a REQUIRED field, like `cadence` (#888): the
registry states each lane's stance, `lane_modes` reads it for a lane whose settings key is ABSENT FROM
THE FILE, and the schema's per-lane default must AGREE with the registry (pinned below — two
derivations, one assertion). The settings key remains the operator override, as #872 built it. The
module constant is DELETED: a second copy that agrees is the shape that produced the defect.

WHICH PATH IS PRODUCTION. `resolve()` fills every per-lane schema default, so in the resolved dict a
filled default and an operator-written value are byte-identical. `lane_modes` therefore reads the RAW
file (`api.settings.declared`) for the operator's keys and the REGISTRY for everything else; the
schema default is never consulted by the plane — it exists for the settings round-trip and the UI,
and the agreement test is what keeps it honest. `source` on the answer says which path fired (#965:
"absent key and declared-none must remain distinguishable").

Every test here was seen red on aed1373 before the fix, for the reason its docstring names.
"""

from __future__ import annotations

import dataclasses

import pytest

from api.protection import DEFAULT_LANE_PROTECTION, PROTECTION_MODES, PROTECTION_SOURCES, LaneProtection, lane_modes
from api.settings import store
from api.strategy_registry import REGISTRY, StrategyEntry, by_id

#: Exactly what both tenants' `settings/protection.json` carried on 2026-09-12 — no lane key at all.
_TENANT_FILE = {"enabled": True}

#: The lanes this ticket moves. Everything else in the registry stays on trail.
_OPTED_OUT = ("SMHGLD-007", "CRSISHORT-006")


def test_FIXTURE_the_tenant_file_names_no_lane_so_every_answer_below_is_a_default():
    """FIXTURE PROPERTY FIRST. If the file carried a lane key, the tests below would be measuring the
    override path (#872, already covered) rather than the default path this ticket is about."""
    assert not any(k.endswith("_protection") for k in _TENANT_FILE)


def test_FIXTURE_both_opted_out_lanes_are_registered_and_the_module_constant_is_GONE():
    """The subject exists on the side of the wire the plane reads, and not on the side it does not."""
    from strategies import smhgld

    for lane in _OPTED_OUT:
        assert by_id(lane) is not None, lane
    assert not hasattr(smhgld, "PROTECTION_MODE"), "the dead second copy is back"


# ------------------------------------------------------------------ the registry declares ------

def test_protection_is_a_REQUIRED_field_so_a_new_lane_cannot_omit_it():
    """THE CLASS GUARD. `cadence` learnt this at #888: a defaulted field is what makes an omitted
    declaration invisible. A lane added without a stance must fail to construct, not resolve to trail."""
    field = {f.name: f for f in dataclasses.fields(StrategyEntry)}["protection"]
    assert field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING, (
        "`protection` has a default — a lane declared without one silently rests that default"
    )
    with pytest.raises(TypeError):
        StrategyEntry(name="NEW", tag="099", settings_domain="", external_id="NEW",  # type: ignore[call-arg]
                      title="no stance", cadence="daily")


def test_every_registered_lane_declares_a_stance_from_the_planners_closed_set():
    for entry in REGISTRY:
        assert entry.protection in PROTECTION_MODES, (
            f"{entry.strategy_id} declares protection {entry.protection!r}, not one of "
            f"{sorted(PROTECTION_MODES)} — the planner cannot dispatch on it"
        )


def test_validate_refuses_a_stance_outside_the_set_and_an_opt_out_with_no_reason():
    from api.strategy_registry import RegistryError, validate

    def _entry(**kw):
        return StrategyEntry(name="X", tag="098", settings_domain="", external_id="X", title="x",
                             cadence="daily", **kw)

    with pytest.raises(RegistryError, match="protection"):
        validate((_entry(protection="peak_trail_but_nicer"),))
    with pytest.raises(RegistryError, match="protection_reason"):
        validate((_entry(protection="none"),))
    validate((_entry(protection="none", protection_reason="the adapter owns the exits"),))
    validate((_entry(protection="trail"),))   # trail is the pre-#872 behaviour; no reason needed


def test_the_two_opted_out_lanes_declare_NONE_with_a_stated_reason():
    for lane in _OPTED_OUT:
        entry = by_id(lane)
        assert entry.protection == "none", lane
        assert entry.protection_reason.strip(), f"{lane}: an opt-out with no reason"


def test_every_OTHER_lane_keeps_todays_stance_which_is_trail():
    """The readback the deploy is judged on: SMHGLD-007 and CRSISHORT-006 change; no other lane does.
    Trail is what every lane did before #872 and what each still does today."""
    others = [e.strategy_id for e in REGISTRY if e.strategy_id not in _OPTED_OUT]
    assert len(others) == len(REGISTRY) - 2
    for lane in others:
        assert by_id(lane).protection == "trail", lane


# ------------------------------------------------------------------ the plane reads it ---------

def test_with_NO_lane_key_the_plane_resolves_both_opted_out_lanes_to_NONE_from_the_registry():
    """THE WIRE, at the unit. This is the number the post-deploy readback drives in-container:
    `lane_modes(cfg, declared)("SMHGLD-007")` — trail on 021c9c4, none after; same for CRSISHORT-006."""
    mode_of = lane_modes(_TENANT_FILE, declared=_TENANT_FILE)
    for lane in _OPTED_OUT:
        got = mode_of(lane)
        assert (got.mode, got.source) == ("none", "declared"), lane


def test_with_NO_lane_key_every_other_registered_lane_still_resolves_to_TRAIL_and_says_declared():
    mode_of = lane_modes(_TENANT_FILE, declared=_TENANT_FILE)
    for entry in REGISTRY:
        if entry.strategy_id not in _OPTED_OUT:
            got = mode_of(entry.strategy_id)
            assert (got.mode, got.atr_multiple, got.source) == ("trail", None, "declared"), entry.strategy_id


def test_the_read_is_from_the_REGISTRY_not_a_hardcoded_lane_name(monkeypatch):
    """KILLS THE MUTANT `if lane == "SMHGLD-007": return none`. Re-declare a lane that is trail today
    as none and watch the plane follow it — and re-declare SMHGLD as trail and watch it follow that.
    Also proves the read is at CALL time, not bound when `lane_modes` was built."""
    from api import strategy_registry

    def _with(lane: str, stance: str) -> tuple:
        return tuple(
            dataclasses.replace(e, protection=stance, protection_reason="test: re-declared")
            if e.strategy_id == lane else e
            for e in strategy_registry.REGISTRY
        )

    mode_of = lane_modes(_TENANT_FILE, declared=_TENANT_FILE)
    monkeypatch.setattr(strategy_registry, "REGISTRY", _with("BCTROT-004", "none"))
    assert mode_of("BCTROT-004").mode == "none"
    monkeypatch.setattr(strategy_registry, "REGISTRY", _with("SMHGLD-007", "entry_floor"))
    assert mode_of("SMHGLD-007").mode == "entry_floor"


def test_a_lane_the_registry_does_not_know_still_gets_the_documented_default():
    """`mode_of` is also handed lane ids the registry never declared — blank legs, `EXTERNAL`. Those
    keep the documented default rather than raising inside a 60 s protection tick."""
    mode_of = lane_modes(_TENANT_FILE, declared=_TENANT_FILE)
    assert mode_of("EXTERNAL") == DEFAULT_LANE_PROTECTION
    assert mode_of("") == DEFAULT_LANE_PROTECTION
    assert DEFAULT_LANE_PROTECTION.source == "default"


def test_the_settings_override_still_WINS_over_the_registry_stance():
    """#872's mechanism is untouched: an operator can put SMHGLD on a floor from the file, and the
    registry's `none` is the default under it, not a cap over it."""
    cfg = {**_TENANT_FILE, "SMHGLD-007_protection": {"mode": "entry_floor", "atrMultiple": 2.25}}
    got = lane_modes(cfg, declared=cfg)("SMHGLD-007")
    assert (got.mode, got.atr_multiple, got.source) == ("entry_floor", 2.25, "settings")


def test_a_hand_built_cfg_with_NO_declared_argument_reads_its_own_keys_as_declared():
    """Every existing caller passes one dict and expects its lane keys honoured — that contract holds.
    A key absent from the hand-built dict now answers from the registry, which is what changed."""
    mode_of = lane_modes({**_TENANT_FILE, "QC345-003_protection": {"mode": "none"}})
    assert (mode_of("QC345-003").mode, mode_of("QC345-003").source) == ("none", "settings")
    assert (mode_of("MOMENTUM-002").mode, mode_of("MOMENTUM-002").source) == ("trail", "declared")


def test_source_is_REQUIRED_on_the_policy_and_lane_modes_stamps_every_path():
    """A default `source` would be a real identity nobody chose — a hand-built `LaneProtection` in any
    `mode_of` double would render as "the operator wrote it" the moment the row exists. So it is
    keyword-only and required, and `lane_modes`'s three paths name three different sources."""
    with pytest.raises(TypeError):
        LaneProtection(mode="none")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="source"):
        LaneProtection(mode="none", source="unstamped")
    cfg = {**_TENANT_FILE, "QC345-003_protection": {"mode": "entry_floor"}}
    mode_of = lane_modes(cfg, declared=cfg)
    seen = {mode_of("QC345-003").source, mode_of("SMHGLD-007").source, mode_of("EXTERNAL").source}
    assert seen == PROTECTION_SOURCES == {"settings", "declared", "default"}


# ------------------------------------------------------------------ provenance (#965) ----------

def test_a_FILLED_default_and_an_operator_WRITTEN_identical_value_report_DIFFERENT_sources(tmp_path, monkeypatch):
    """#965: absent-key and declared-none must stay distinguishable. After `resolve` they are
    byte-identical (`{"mode": "none"}` both ways), so the distinction lives in `source`, read off
    the RAW file. This is the test the lead asked for by name."""
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)

    (tmp_path / "protection.json").write_text('{"enabled": true}')
    silent = lane_modes(store.resolve("protection"), declared=store.declared("protection"))("SMHGLD-007")

    (tmp_path / "protection.json").write_text('{"enabled": true, "SMHGLD-007_protection": {"mode": "none"}}')
    written = lane_modes(store.resolve("protection"), declared=store.declared("protection"))("SMHGLD-007")

    assert silent.mode == written.mode == "none"
    assert (silent.source, written.source) == ("declared", "settings")
    # And the resolved dicts really were identical — the reason `source` cannot come from `cfg`.
    (tmp_path / "protection.json").write_text('{"enabled": true}')
    assert store.resolve("protection")["SMHGLD-007_protection"] == {"mode": "none"}


# ------------------------------------------------------------------ the schema agrees ----------

@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.strategy_id)
def test_the_schema_declares_a_key_for_EVERY_registered_lane_and_its_default_is_the_registry_stance(entry):
    """`_strip_additional` keeps only declared `properties`, so a lane without a schema key cannot be
    configured from the instances repo at all — the file looks authoritative and the value is erased.
    On aed1373 SMHGLD-007 and CRSISHORT-006 had no key. Derived from the REGISTRY, not a hand-kept
    list: the hand-kept `_LANES` tuple in `settings/test_lane_protection.py` is exactly how the two
    new lanes were missed."""
    schema = store.load_schema("protection")
    key = f"{entry.strategy_id}_protection"
    assert key in schema["properties"], f"{key} is not a declared property — the tenant cannot set it"
    assert schema["properties"][key]["default"] == {"mode": entry.protection}, (
        f"{key}: schema default {schema['properties'][key].get('default')!r} disagrees with the "
        f"registry's {entry.protection!r} — two derivations of one stance"
    )


def test_a_declared_NONE_for_smhgld_survives_the_strip_and_resolves(tmp_path, monkeypatch):
    """THE INSTANCES REPO CAN NOW SAY IT. On aed1373 this exact file resolved to `{'enabled': True}`.
    Two assertions on two lines: the schema default is applied by `resolve` (first), and the plane
    reads the written key as an operator override (second) — so a declared-but-never-applied default
    and a read-but-ignored key each fail on their own line."""
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    (tmp_path / "protection.json").write_text('{"enabled": true, "SMHGLD-007_protection": {"mode": "none"}}')

    assert store.resolve("protection")["SMHGLD-007_protection"] == {"mode": "none"}
    got = lane_modes(store.resolve("protection"), declared=store.declared("protection"))("SMHGLD-007")
    assert (got.mode, got.source) == ("none", "settings")


def test_the_RESOLVED_tenant_file_puts_both_opted_out_lanes_on_none_and_every_other_lane_on_trail(tmp_path, monkeypatch):
    """END TO END on the settings side: the tenant's real file shape, through `resolve` AND `declared`,
    into the planner's callable — the production call shape (engine_node passes both)."""
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    (tmp_path / "protection.json").write_text('{"enabled": true}')

    mode_of = lane_modes(store.resolve("protection"), declared=store.declared("protection"))

    for lane in _OPTED_OUT:
        assert (mode_of(lane).mode, mode_of(lane).source) == ("none", "declared"), lane
    for entry in REGISTRY:
        if entry.strategy_id not in _OPTED_OUT:
            assert (mode_of(entry.strategy_id).mode, mode_of(entry.strategy_id).source) == ("trail", "declared"), entry.strategy_id


def test_the_engine_passes_BOTH_the_resolved_and_the_declared_file_to_lane_modes():
    """The wiring, not the unit. `lane_modes(cfg)` alone would make `source` always `declared` for a
    written key — the #965 distinction dead on the production path while every unit test passed."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "lane_modes"]
    assert calls, "engine_node no longer calls lane_modes"
    for c in calls:
        assert any(k.arg == "declared" for k in c.keywords), "lane_modes called without declared="


# ------------------------------------------------------------------ the import graph -----------

def test_the_registry_imports_clean_in_a_fresh_interpreter():
    """`strategy_registry` imports `api.protection.PROTECTION_MODES` at module level and
    `protection.lane_modes` imports the registry per call. Safe today because `protection.py` has no
    `api.*` import above its constants — one `from api.settings import ...` added at its top later
    closes the loop (settings -> registry -> protection half-initialised) and the failure shows only on
    a cold start. Pinned in a subprocess, because this interpreter has both modules warm."""
    import pathlib
    import subprocess
    import sys

    for first in ("api.strategy_registry", "api.protection", "api.settings"):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {first}; import api.strategy_registry, api.protection; "
                                   f"from api.protection import lane_modes; "
                                   f"assert lane_modes({{}})('SMHGLD-007').mode == 'none'"],
            cwd=pathlib.Path(__file__).parent.parent, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, f"importing {first} first:\n{proc.stderr[-1500:]}"
