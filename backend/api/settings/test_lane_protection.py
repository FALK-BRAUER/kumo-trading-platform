"""The per-lane protection mode is a REAL settings knob, not a dead-but-authoritative one (#872).

WHY THIS FILE EXISTS AT ALL. The obvious shape for "per-strategy protection" is a map —
`perStrategy: {"QC345-003": {...}}` — and it cannot work here: `_strip_additional` (store.py:61) keeps
only keys declared under `properties`, so every lane inside a map is stripped before validation and the
domain resolves to `{}`. The file would still LOOK authoritative, the README would still list it, and
the planner would never see a byte of it. That is [[agreement-is-not-connection]] with the two sources
being the settings file and the default: both say "trail", and a severed wire is invisible.

So the keys are LITERAL, one per lane, exactly as `strategies.schema.json` does it — and this file
pins the wire end to end with a value THE DEFAULT COULD NOT PRODUCE: mode `entry_floor` and multiple
2.25. Neither is reachable by defaulting (`mode` defaults to "trail"; `atrMultiple` has no lane-level
default at all), so a mutant that ignores the file and answers from the schema fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.protection import LaneProtection, lane_modes
from api.settings import store
from api.strategy_registry import REGISTRY, by_id

#: Every lane the cockpit runs — FROM THE REGISTRY, never a hand-kept list (#1029). This tuple was
#: hand-kept and named five lanes; SMHGLD-007 and CRSISHORT-006 were registered without a schema key,
#: and a key missing here is a lane whose protection cannot be configured at all — the failure mode
#: this list exists to make loud, and the one it missed.
_LANES = tuple(e.strategy_id for e in REGISTRY)

#: A value the DEFAULT could not produce, per CLAUDE.md ("test a knob with a value the default could
#: not produce"). 1.5 would be indistinguishable from the domain-level `atrMultiple`.
_UNREACHABLE = {"mode": "entry_floor", "atrMultiple": 2.25}


@pytest.fixture
def values_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    return tmp_path


def _write(values_dir: Path, values: dict) -> None:
    (values_dir / "protection.json").write_text(json.dumps(values))


def test_the_fixture_value_is_one_the_schema_default_could_not_produce():
    """FIXTURE PROPERTY FIRST. If the schema's own default already said `entry_floor`/2.25, every
    assertion below would pass with the file unread — the test would be measuring nothing."""
    schema = store.load_schema("protection")
    for lane in _LANES:
        default = schema["properties"][f"{lane}_protection"].get("default", {})
        assert default.get("mode") != _UNREACHABLE["mode"], lane
        assert "atrMultiple" not in default, (
            f"{lane}: a lane-level atrMultiple default makes 2.25 producible without reading the file"
        )


@pytest.mark.parametrize("lane", _LANES)
def test_every_lane_has_a_protection_key_defaulting_to_ITS_REGISTRY_STANCE(lane: str):
    """Absent config is the lane's OWN declaration (#1029) — trail for the rotation lanes, none for
    SMHGLD-007 and CRSISHORT-006 — and it is stated, not implied. `resolve` fills the schema default,
    so the resolved dict always carries a known answer; the schema default and the registry stance are
    two derivations of one fact and must agree, or the resolved file and the planner disagree."""
    schema = store.load_schema("protection")
    assert f"{lane}_protection" in schema["properties"], lane
    assert schema["properties"][f"{lane}_protection"]["default"] == {"mode": by_id(lane).protection}


def test_a_lane_override_survives_strip_additional_and_resolve(values_dir: Path):
    """THE WIRE. `_strip_additional` recurses into declared properties only — a nested object whose
    own `properties` are undeclared is emptied. Both `mode` and `atrMultiple` must come back."""
    _write(values_dir, {"QC345-003_protection": dict(_UNREACHABLE)})

    resolved = store.resolve("protection")

    assert resolved["QC345-003_protection"] == _UNREACHABLE


def test_the_override_reaches_the_planner_as_a_typed_lane_policy(values_dir: Path):
    """END TO END, to the object `plan_protection` actually reads. Asserting on the resolved dict
    alone would stop one hop short of the seam — the hop where a key travels and is then not consulted
    is exactly #574/#581."""
    _write(values_dir, {"QC345-003_protection": dict(_UNREACHABLE)})

    mode_of = lane_modes(store.resolve("protection"), declared=store.declared("protection"))

    assert mode_of("QC345-003") == LaneProtection(mode="entry_floor", atr_multiple=2.25, source="settings")
    # The SIBLINGS are untouched — enumerate them rather than checking only the lane under test. Each
    # answers from its OWN registry stance (`declared`), never from a neighbour's override.
    for other in _LANES:
        if other != "QC345-003":
            assert mode_of(other) == LaneProtection(mode=by_id(other).protection, source="declared"), other


def test_an_unknown_mode_is_dropped_rather_than_carried_into_the_planner(values_dir: Path):
    """A hand-edited file naming a mode that does not exist must degrade to the DEFAULT, never to a
    mode the planner cannot dispatch on. `_coerced_file` drops a top-level key that fails its own
    subschema; the enum is what makes this one fail."""
    _write(values_dir, {"QC345-003_protection": {"mode": "peak_trail_but_nicer"}})

    mode_of = lane_modes(store.resolve("protection"), declared=store.declared("protection"))

    assert mode_of("QC345-003") == LaneProtection(mode="trail", atr_multiple=None, source="declared")


def test_an_undeclared_key_inside_a_lane_object_is_refused_at_save(values_dir: Path):
    """`additionalProperties: false` at the LANE level too, and the strict check must REACH it.

    `strict=True` is what the HTTP PUT uses (app.py). Its unknown-key check was top-level only, so a
    typo one level down was dropped by `_strip_additional` before the validator saw it and the PUT
    answered 200 having configured nothing — the same silent-wrong-answer shape the flag exists for.
    """
    with pytest.raises(store.SettingsError) as exc:
        store.save("protection", {"QC345-003_protection": {"mode": "trail", "atrMultiplier": 2.25}},
                   strict=True)

    # NAMED, and named at its own depth — "unknown setting" pointing at the lane object would send the
    # operator to diff two documents by eye, which is the failure the strict flag was added for.
    assert exc.value.errors == [
        {"loc": ["QC345-003_protection", "atrMultiplier"],
         "msg": "unknown setting 'QC345-003_protection.atrMultiplier' for domain 'protection' "
                "— not in its schema"}
    ]


def test_a_saved_lane_override_round_trips_through_the_file(values_dir: Path):
    """SAVE is the other half of the wire. A schema that resolves a value it will not accept on save
    is a knob only a hand-edited file can turn."""
    store.save("protection", {"QC345-003_protection": dict(_UNREACHABLE)})

    assert json.loads((values_dir / "protection.json").read_text())["QC345-003_protection"] == _UNREACHABLE
    assert lane_modes(store.resolve("protection"))("QC345-003").mode == "entry_floor"
