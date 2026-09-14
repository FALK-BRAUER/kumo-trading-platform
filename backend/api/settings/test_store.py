"""Offline tests for the settings store (#49) — defaults, strict save, coerce-not-crash load."""

from __future__ import annotations

import json

import pytest

from api.settings import store


@pytest.fixture
def values_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    return tmp_path


def test_alpaca_is_a_registered_domain():
    assert "alpaca" in store.domains()


def test_resolve_no_file_returns_schema_defaults(values_dir):
    assert store.resolve("alpaca") == {"feed": "sip"}  # default from the schema


def test_save_valid_then_resolve(values_dir):
    out = store.save("alpaca", {"feed": "sip"})
    assert out == {"feed": "sip"}
    assert json.loads((values_dir / "alpaca.json").read_text()) == {"feed": "sip"}
    assert store.resolve("alpaca") == {"feed": "sip"}


def test_save_invalid_enum_rejected_and_not_persisted(values_dir):
    with pytest.raises(store.SettingsError) as ei:
        store.save("alpaca", {"feed": "nasdaq"})  # not in the enum
    assert ei.value.errors and ei.value.errors[0]["loc"] == ["feed"]
    assert not (values_dir / "alpaca.json").exists()  # nothing written


def test_save_strips_unknown_keys(values_dir):
    out = store.save("alpaca", {"feed": "sip", "secret_key": "leak"})  # unknown → stripped
    assert out == {"feed": "sip"}


def test_resolve_coerces_a_hand_broken_file(values_dir):
    # a hand-edited file with a bad enum value + an unknown key → drop both, fall back to default, no crash
    (values_dir / "alpaca.json").write_text(json.dumps({"feed": "garbage", "junk": 1}))
    assert store.resolve("alpaca") == {"feed": "sip"}


def test_resolve_survives_corrupt_json(values_dir):
    (values_dir / "alpaca.json").write_text("{ not json")
    assert store.resolve("alpaca") == {"feed": "sip"}


def test_get_override_none_when_unset(values_dir):
    assert store.get_override("alpaca", "feed") is None  # no file → None → keep the operator's feed.toml


def test_get_override_returns_explicit_value(values_dir):
    store.save("alpaca", {"feed": "sip"})
    assert store.get_override("alpaca", "feed") == "sip"


def test_resolve_never_crashes_on_required_or_nested_invalid(tmp_path, monkeypatch):
    # coerce-never-crash for a NON-flat schema: a required-without-default field + a bad-typed value.
    monkeypatch.setattr(store, "_SCHEMA_DIR", tmp_path)
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    (tmp_path / "x.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "n": {"type": "integer", "default": 5}},
            }
        )
    )
    (tmp_path / "x.json").write_text(json.dumps({"name": 123, "junk": 1}))  # name wrong type, junk unknown
    out = store.resolve("x")  # must NOT raise despite the missing required + bad value
    assert out == {"n": 5}  # invalid 'name' dropped, 'junk' stripped, 'n' defaulted


def test_peak_refuses_a_tight_trail_that_is_not_tighter(tmp_path, monkeypatch):
    """JSON Schema cannot compare one property against another, so this relationship has to be checked
    here. Without it a schema-valid file saves fine and then the ENGINE refuses every PEAK arm, with the
    operator finding out at the toggle rather than at save. (codex review.)"""
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    with pytest.raises(store.SettingsError) as exc:
        store.save("peak", {"trailWidePct": 2.5, "trailTightPct": 2.5})
    assert any("tighter" in e["msg"] for e in exc.value.errors)

    with pytest.raises(store.SettingsError):
        store.save("peak", {"trailWidePct": 1.0, "trailTightPct": 3.0})


def test_peak_compares_trails_in_basis_points_not_percent(tmp_path, monkeypatch):
    """Two distinct percents can round to the SAME basis-point value, which the engine then rejects as
    tight >= wide. Comparing in the engine's own unit is what makes the two agree."""
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    with pytest.raises(store.SettingsError):
        store.save("peak", {"trailWidePct": 2.501, "trailTightPct": 2.502})  # both -> 250 bps


def test_peak_accepts_a_genuinely_tighter_trail(tmp_path, monkeypatch):
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    saved = store.save("peak", {"trailWidePct": 3.0, "trailTightPct": 1.0})
    assert saved["trailWidePct"] == 3.0 and saved["trailTightPct"] == 1.0
    assert saved["trimMax"] == 2  # defaults filled for everything not sent


def test_peak_defaults_are_self_consistent():
    """The shipped defaults must themselves pass the cross-field rule — a domain whose defaults cannot
    be saved would be a strange thing to ship."""
    from api.settings import store

    assert store._validate_peak(store.resolve("peak")) == []


def test_peak_rounds_like_javascript_not_like_python(tmp_path, monkeypatch):
    """Python rounds half to EVEN, JavaScript rounds half AWAY FROM ZERO, and the UI converts percent to
    basis points with `Math.round` before arming. A validator using Python's rule accepts a pair the UI
    then collapses onto one bps value, and the engine rejects it. (codex review, High.)"""
    from api.settings import store

    assert store._to_bps(2.505) == 251  # Python's round() gives 250 here
    assert store._to_bps(2.506) == 251
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    with pytest.raises(store.SettingsError):
        store.save("peak", {"trailWidePct": 2.506, "trailTightPct": 2.505})


def test_protection_refuses_an_inverted_band(tmp_path, monkeypatch):
    """An inverted floor/ceiling is not merely odd: the clamp is an if/elif, so which branch runs depends
    on the raw ATR and the width can land outside the band entirely. (codex review.)"""
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    with pytest.raises(store.SettingsError) as exc:
        store.save("protection", {"minTrailPct": 20.0, "maxTrailPct": 5.0})
    assert any("below the ceiling" in e["msg"] for e in exc.value.errors)


def test_protection_defaults_are_self_consistent():
    from api.settings import store

    assert store._validate_protection(store.resolve("protection")) == []


class TestAPutMustNotSilentlySwallowWhatTheCallerAsked:
    """A PUT that drops keys and answers 200 tells the caller their change took effect. It did not.

    Sending `{"values": {...}}` — the GET response shape, and an easy mistake — returned 200 OK and
    wrote NOTHING, because `_strip_additional` removed the unknown top-level key and the empty result
    validated fine (2026-08-24). Combined with PUT replacing the whole domain, the dangerous version
    is a payload that is PARTLY right: 200, and every key you did not send silently reverts to its
    default.

    Stripping is correct when LOADING a stale file — startup must not die on a key someone removed
    from the schema last month. It is wrong for a caller asserting intent right now.
    """

    def test_the_fixture_can_represent_the_bug(self, values_dir):
        """The schema must actually reject the unknown key, or these assertions prove nothing."""
        assert "values" not in store.load_schema("alpaca").get("properties", {})

    def test_a_lenient_save_still_strips_because_files_depend_on_it(self, values_dir):
        """The load path keeps its behaviour — this is the half that must NOT change."""
        assert store.save("alpaca", {"feed": "sip", "leftover_from_last_month": 1}) == {"feed": "sip"}

    def test_a_strict_save_refuses_the_unknown_key_and_NAMES_it(self, values_dir):
        with pytest.raises(store.SettingsError) as exc:
            store.save("alpaca", {"values": {"feed": "sip"}}, strict=True)
        msg = json.dumps(exc.value.errors)
        assert "values" in msg, f"the caller must be told WHICH key was rejected: {msg}"

    def test_a_strict_save_writes_nothing_when_it_refuses(self, values_dir):
        """The whole point: a refusal that had already written would be worse than the silent drop."""
        store.save("alpaca", {"feed": "iex"})
        with pytest.raises(store.SettingsError):
            store.save("alpaca", {"values": {"feed": "sip"}}, strict=True)
        assert store.resolve("alpaca") == {"feed": "iex"}, "a refused save must not have touched disk"

    def test_a_strict_save_accepts_a_correct_payload(self, values_dir):
        """The discriminating half — refusing everything would also pass the tests above."""
        assert store.save("alpaca", {"feed": "sip"}, strict=True) == {"feed": "sip"}


class TestALoadedFileMustSayWhatItDropped:
    """An unknown key in a values file is stripped in total silence.

    test-alpaca's notifications file has carried `quiet_start_hour: 22` and `quiet_end_hour: 8` with no
    such keys in the schema. Quiet hours were configured, did not exist, and nothing ever said so — the
    operator believes he silenced overnight alerts. An INVALID key already warns ("dropping invalid");
    an UNKNOWN one did not, which is the more likely mistake of the two.

    Dropping stays — startup must survive a key that left the schema last month. Only the silence goes.
    """

    def test_the_fixture_writes_a_key_the_schema_really_does_not_have(self, values_dir):
        (values_dir / "alpaca.json").write_text(json.dumps({"feed": "sip", "quiet_start_hour": 22}))
        assert "quiet_start_hour" not in store.load_schema("alpaca").get("properties", {})
        assert store.resolve("alpaca") == {"feed": "sip"}, "it is still dropped, deliberately"

    def test_the_drop_is_announced_and_the_key_is_NAMED(self, values_dir, caplog):
        (values_dir / "alpaca.json").write_text(json.dumps({"feed": "sip", "quiet_start_hour": 22}))
        with caplog.at_level("WARNING"):
            store.resolve("alpaca")
        assert "quiet_start_hour" in caplog.text, f"the operator must learn WHICH key vanished: {caplog.text}"

    def test_a_clean_file_stays_quiet(self, values_dir, caplog):
        """The discriminating half: warning on every load would be noise nobody reads."""
        (values_dir / "alpaca.json").write_text(json.dumps({"feed": "sip"}))
        with caplog.at_level("WARNING"):
            store.resolve("alpaca")
        assert "unknown" not in caplog.text.lower()
