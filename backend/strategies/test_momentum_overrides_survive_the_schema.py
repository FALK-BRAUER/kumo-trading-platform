"""The MOMENTUM/BCTROT override keys are DECLARED by the settings schema, so a written one is read (#1054).

THE DEFECT. `momentum._live_overrides` reads `<PREFIX>_N_HOLD`, `<PREFIX>_BUFFER`,
`<PREFIX>_GIVE_BACK_FRAC` from the `strategies` domain for MOMENTUM and BCTROT — six keys. The schema
declared none of them and has `additionalProperties: false`; `_strip_additional` keeps only declared
`properties`, so every written override was erased before `resolve` and the function returned the
researched constants on every call. Measured in the paper engine container 2026-09-13 07:10Z:
`declared("strategies") == {}` for these prefixes, `_live_overrides("MOMENTUM") == (8, 5, 0.5)`, and
`_strip_additional({"MOMENTUM_GIVE_BACK_FRAC": 0.35, ...}, schema)` drops the key. ks#221 (give-back
0.50 → 0.35) had no knob to land on.

WHY IT WAS GREEN. `test_momentum_overrides.py` and `test_bctrot_overrides_are_its_own.py` seed the
domain by monkeypatching `settings.resolve` — a double that cannot represent the strip. They prove
the function reads a dict; nothing proved the dict could ever contain the key. This file seeds
through the REAL path: a values file under `store._VALUES_DIR`, read by the real `resolve`, with
nothing patched between the file and `_live_overrides`. The #574/#581/#1029 shape, one domain over.

Every test here was seen red on 7c3cbb1 for the reason its docstring names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.settings import store
from strategies.momentum import _RESEARCHED, _live_overrides

#: Values no researched constant, no default and no fixture in this repo produce — so a value that
#: comes back proves it travelled from the file (CLAUDE.md: test a knob with a value the default
#: could not produce). 0.35 is ks#221's number; the others are deliberately odd.
_PROBE = {"GIVE_BACK_FRAC": 0.35, "N_HOLD": 11, "BUFFER": 7}


@pytest.fixture
def values_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    return tmp_path


def _write(values_dir: Path, values: dict) -> None:
    (values_dir / "strategies.json").write_text(json.dumps(values))


def _keys_read_by_live_overrides(prefix: str) -> set[str]:
    """The keys `_live_overrides` ASKS the domain for — recorded off a domain that answers every
    `.get`, never enumerated here. A key the function starts reading tomorrow lands in this set."""
    from api import settings as real

    asked: set[str] = set()

    class _Recording(dict):
        def get(self, key, default=None):
            asked.add(key)
            return super().get(key, default)

    original = real.resolve
    real.resolve = lambda domain: _Recording()
    try:
        _live_overrides(prefix)
    finally:
        real.resolve = original
    return asked


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_probe_values_are_ones_the_researched_constants_cannot_produce():
    assert _PROBE["GIVE_BACK_FRAC"] != _RESEARCHED["give_back_frac"]
    assert _PROBE["N_HOLD"] != _RESEARCHED["n_hold"]
    assert _PROBE["BUFFER"] != _RESEARCHED["buffer"]


def test_FIXTURE_the_recorder_sees_the_three_keys_per_prefix():
    """If the recorder saw nothing, the coverage test below would pass vacuously."""
    asked = _keys_read_by_live_overrides("MOMENTUM")
    assert asked == {"MOMENTUM_N_HOLD", "MOMENTUM_BUFFER", "MOMENTUM_GIVE_BACK_FRAC", "MOMENTUM_TAKE_PROFIT_ATR"}, asked


def test_FIXTURE_with_NO_file_the_real_path_returns_the_researched_values(values_dir: Path):
    """The baseline the probes are judged against, through the same real path."""
    assert _live_overrides("MOMENTUM") == (_RESEARCHED["n_hold"], _RESEARCHED["buffer"], _RESEARCHED["give_back_frac"], None)


# ------------------------------------------------------------------ the wire, per prefix -------

@pytest.mark.parametrize("prefix", ["MOMENTUM", "BCTROT"])
def test_a_give_back_written_to_the_file_REACHES_live_overrides_through_the_real_resolve(values_dir: Path, prefix: str):
    """ks#221's knob. On 7c3cbb1 this returned 0.5: the key was stripped by the schema before
    `resolve` ever saw it, and the function read the researched constant."""
    _write(values_dir, {f"{prefix}_GIVE_BACK_FRAC": _PROBE["GIVE_BACK_FRAC"]})

    n_hold, buffer_, give_back, _take_profit = _live_overrides(prefix)

    assert give_back == _PROBE["GIVE_BACK_FRAC"], (
        f"{prefix}_GIVE_BACK_FRAC written as {_PROBE['GIVE_BACK_FRAC']} came back as {give_back} — "
        f"the schema strips a key it does not declare, so the knob is dead on every instance (#1054)")
    assert (n_hold, buffer_) == (_RESEARCHED["n_hold"], _RESEARCHED["buffer"]), "a partial override moved the others"


@pytest.mark.parametrize("prefix", ["MOMENTUM", "BCTROT"])
def test_all_three_written_overrides_reach_live_overrides_and_only_that_prefix(values_dir: Path, prefix: str):
    """The full set for one lane, and the SIBLING untouched — #794's reason for the prefix: a knob
    named for one lane must not steer the other."""
    other = "BCTROT" if prefix == "MOMENTUM" else "MOMENTUM"
    _write(values_dir, {f"{prefix}_N_HOLD": _PROBE["N_HOLD"], f"{prefix}_BUFFER": _PROBE["BUFFER"],
                        f"{prefix}_GIVE_BACK_FRAC": _PROBE["GIVE_BACK_FRAC"]})

    assert _live_overrides(prefix) == (_PROBE["N_HOLD"], _PROBE["BUFFER"], _PROBE["GIVE_BACK_FRAC"], None)
    assert _live_overrides(other) == (_RESEARCHED["n_hold"], _RESEARCHED["buffer"], _RESEARCHED["give_back_frac"], None)


def test_the_written_value_is_in_the_RESOLVED_domain_and_absent_from_it_when_not_written(values_dir: Path):
    """Two lines, two facts: the schema no longer strips the key (first), and it carries NO default
    (second) — a schema default would be the wrong-default failure `_live_overrides`'s docstring
    names, with the researched constant silently replaced by the schema's copy."""
    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": _PROBE["GIVE_BACK_FRAC"]})
    assert store.resolve("strategies")["MOMENTUM_GIVE_BACK_FRAC"] == _PROBE["GIVE_BACK_FRAC"]

    _write(values_dir, {})
    resolved = store.resolve("strategies")
    for key in ("MOMENTUM_GIVE_BACK_FRAC", "MOMENTUM_N_HOLD", "MOMENTUM_BUFFER", "MOMENTUM_TAKE_PROFIT_ATR",
                "BCTROT_GIVE_BACK_FRAC", "BCTROT_N_HOLD", "BCTROT_BUFFER", "BCTROT_TAKE_PROFIT_ATR"):
        assert key not in resolved, f"{key} has a schema default — the researched value in momentum.py is no longer the one that runs"


# ------------------------------------------------------------------ the class -----------------

@pytest.mark.parametrize("prefix", ["MOMENTUM", "BCTROT"])
def test_EVERY_key_live_overrides_reads_is_declared_by_the_schema(prefix: str):
    """Derived from the function, not enumerated: a key `_live_overrides` starts reading tomorrow
    without a schema entry is a dead knob from its first day, and this is where it goes red."""
    declared = set(store.load_schema("strategies")["properties"])
    asked = _keys_read_by_live_overrides(prefix)
    assert asked, "the recorder saw no reads — the fixture is broken, not the schema"
    missing = sorted(asked - declared)
    assert not missing, (
        f"{prefix}: _live_overrides reads {missing}, which the strategies schema does not declare — "
        f"additionalProperties:false strips them before resolve, so they cannot be set (#1054)")


def test_the_schema_BOUNDS_the_overrides_the_way_the_lane_needs_them(values_dir: Path):
    """A fraction outside (0, 1] or a count below 1 is dropped by coercion, never obeyed — and
    `_live_overrides` then falls back to the researched value, which is the documented behaviour
    for a malformed override. The bounds are the schema's; the fallback is the function's.

    ZERO IS REFUSED, NOT ACCEPTED (b4enxqbg's review). kumo-strategies `exits.py:291` gates on
    `if cfg.give_back_frac:` — 0.0 is falsy, so a written 0.0 would silently switch the give-back
    exit OFF while the operator reads a value they set. The first draft of this test asserted
    `(1, 1, 0.0)` and would have pinned that. 1.0 is legal: exit only at flat (give_back.py)."""
    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": 1.5, "MOMENTUM_N_HOLD": 0, "MOMENTUM_BUFFER": -1})
    assert _live_overrides("MOMENTUM") == (_RESEARCHED["n_hold"], _RESEARCHED["buffer"], _RESEARCHED["give_back_frac"], None)
    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": 0.0})
    assert _live_overrides("MOMENTUM")[2] == _RESEARCHED["give_back_frac"], "0.0 reached the lane — it reads as OFF"
    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": 1.0, "MOMENTUM_N_HOLD": 1, "MOMENTUM_BUFFER": 1})
    assert _live_overrides("MOMENTUM") == (1, 1, 1.0, None)


def test_the_written_values_reach_the_LIVE_CONFIG_the_lane_is_built_from(values_dir: Path):
    """THE SEAM PAST `_live_overrides`: `live_config()` consumes the tuple and builds
    `PortfolioConfig(n_hold, buffer)` and `ExitConfig(give_back_frac)` from it. A builder that
    dropped the third element would pass every test above. Real file, real resolve."""
    from strategies.momentum import live_config

    _write(values_dir, {"MOMENTUM_N_HOLD": _PROBE["N_HOLD"], "MOMENTUM_BUFFER": _PROBE["BUFFER"],
                        "MOMENTUM_GIVE_BACK_FRAC": _PROBE["GIVE_BACK_FRAC"]})
    cfg = live_config("MOMENTUM")
    assert (cfg.portfolio.n_hold, cfg.portfolio.buffer) == (_PROBE["N_HOLD"], _PROBE["BUFFER"])
    assert cfg.exits.give_back_frac == _PROBE["GIVE_BACK_FRAC"]
    # And the sibling, built from ITS prefix, is untouched by MOMENTUM's file keys.
    from strategies.momentum import bctrot_config

    sib = bctrot_config()
    assert sib.exits.give_back_frac == _RESEARCHED["give_back_frac"]


def test_a_save_through_the_HTTP_path_accepts_the_keys_strictly():
    """`save(strict=True)` is what `PUT /settings/strategies` calls; on 7c3cbb1 it REFUSED these keys
    as undeclared, so the UI could not write them either."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        original = store._VALUES_DIR
        store._VALUES_DIR = Path(d)
        try:
            current = store.resolve("strategies")
            store.save("strategies", {**current, "MOMENTUM_GIVE_BACK_FRAC": _PROBE["GIVE_BACK_FRAC"]}, strict=True)
            assert store.resolve("strategies")["MOMENTUM_GIVE_BACK_FRAC"] == _PROBE["GIVE_BACK_FRAC"]
        finally:
            store._VALUES_DIR = original
