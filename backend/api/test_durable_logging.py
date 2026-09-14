"""The engine must write a DURABLE, rotating log — Nautilus's own, not a hand-rolled writer (#758).

WHY. `LoggingConfig` already carries `log_directory`, `log_file_name`, `log_file_format`,
`log_file_max_size` and `log_file_max_backup_count`. All five were UNSET, so the engine wrote no file:
everything went to stdout, Docker's json-file driver captured it with `Config: {}` — no max-size, no
max-file — and `make up` recreated the container and destroyed it.

That is why Friday's session could not be examined on Monday, and why one symbol's history had to be
reconstructed by counting grep matches. It is also the fourth time this repo has nearly hand-rolled
something Nautilus ships: the check-Nautilus-first rule in CLAUDE.md exists because of the previous
three.
"""

from __future__ import annotations

import pathlib

import pytest

from api.engine_node import _logging_config


def test_the_fixture_can_express_the_bug(monkeypatch):
    """Vacuity guard: with no directory configured, file logging must be OFF — otherwise every
    assertion below passes against a config that was already on."""
    monkeypatch.delenv("KUMO_LOG_DIR", raising=False)
    assert _logging_config().log_directory is None


def test_a_configured_directory_turns_ON_every_file_field(monkeypatch):
    """All five together. A directory with no format, or a format with no rotation, is a file that
    either cannot be queried or grows until the disk does."""
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    c = _logging_config()
    assert c.log_directory == "/var/log/kumo"
    assert c.log_file_name
    assert c.log_file_format == "JSON", "the file exists to be QUERIED; the whole ticket is grep"
    assert c.log_file_max_size and c.log_file_max_size > 0, "unbounded file"
    assert c.log_file_max_backup_count and c.log_file_max_backup_count > 1, "one backup is not retention"


def test_the_FILE_LEVEL_is_richer_than_stdout(monkeypatch):
    """stdout stays terse for a human watching; the durable record keeps what an investigation needs.
    INFO is where the inferred fills live — the phantom's mint, which is invisible at WARNING."""
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    monkeypatch.setenv("KUMO_LOG_LEVEL", "WARNING")
    c = _logging_config()
    assert c.log_level == "WARNING" and c.log_level_file == "INFO"


@pytest.mark.parametrize("var,attr,value,expected", [
    ("KUMO_LOG_MAX_BYTES", "log_file_max_size", "12345", 12345),
    ("KUMO_LOG_BACKUPS", "log_file_max_backup_count", "17", 17),
    ("KUMO_LOG_FILE", "log_file_name", "probe-name", "probe-name"),
])
def test_each_knob_TRAVELS(monkeypatch, var, attr, value, expected):
    """Values no default could produce. Testing a knob at its default value proves nothing — that is
    the QC27 allocated-equity shape, where a settings knob was dead for a lane's entire life because
    every observation agreed with the constant standing in for it."""
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    monkeypatch.setenv(var, value)
    assert getattr(_logging_config(), attr) == expected


def test_the_COMPOSE_FILE_mounts_a_volume_and_sets_the_directory_LITERALLY():
    """Two halves, and both are needed.

    A directory with no volume writes into the container's own filesystem, which `make up` destroys —
    the exact failure being fixed, wearing a config that looks correct.

    And the variable must be a LITERAL, not `${KUMO_LOG_DIR}`: compose interpolates an unset variable
    to the EMPTY STRING, so `os.environ.get(k, default)` returns "" and the default never fires. That
    is #581, where two documented provider knobs were unreachable for the same reason.
    """
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/compose.paper.yml").read_text())
    engine = compose["services"]["engine"]

    assert any("logdata:" in v for v in engine["volumes"]), "no durable log volume is mounted"
    assert "logdata" in compose["volumes"], "the volume is mounted but never declared"

    value = engine["environment"]["KUMO_LOG_DIR"]
    assert "${" not in str(value), (
        f"KUMO_LOG_DIR is interpolated ({value!r}); an unset variable becomes the empty string and "
        f"leaves file logging OFF while the volume sits mounted and looking correct"
    )
    mount = [v for v in engine["volumes"] if v.startswith("logdata:")][0]
    assert mount.split(":")[1] == str(value), (
        f"the engine writes to {value} and the volume is mounted at {mount.split(':')[1]} — logs "
        f"would go to the container filesystem and die on recreate"
    )


def test_DOCKERS_OWN_LOGS_ARE_CAPPED():
    """Measured 2026-08-31: 1.8 MB in 13 minutes, and `docker inspect` reported `Config: {}` — the
    json-file driver was growing without limit for the life of the container."""
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/compose.paper.yml").read_text())
    opts = (compose["services"]["engine"].get("logging") or {}).get("options") or {}
    assert opts.get("max-size"), "stdout capture is unbounded"
    assert int(str(opts.get("max-file", 0))) > 1, "one file is not rotation"


def test_PER_COMPONENT_LEVELS_reach_the_config(monkeypatch):
    """Measured as a survivor: replacing `_component_levels()` with `None` left every other test in
    this file green, because nothing asserted the field arrives on the config at all."""
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    monkeypatch.setenv("KUMO_LOG_COMPONENTS", "ExecEngine=DEBUG,MOMENTUM=TRACE")
    got = _logging_config().dict()["log_component_levels"]
    assert got == {"ExecEngine": "DEBUG", "MOMENTUM": "TRACE"}, got


def test_a_MALFORMED_component_entry_is_skipped_not_fatal(monkeypatch):
    """Losing the engine to a logging typo is worse than losing the verbosity — and a half-applied
    map that looks complete is the shape this repo keeps paying for, so the valid entries still land
    and the bad one is named on stdout."""
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    monkeypatch.setenv("KUMO_LOG_COMPONENTS", "ExecEngine=DEBUG,Bogus=NOPE,Missing,")
    assert _logging_config().dict()["log_component_levels"] == {"ExecEngine": "DEBUG"}


def test_NO_component_setting_leaves_the_field_empty(monkeypatch):
    monkeypatch.setenv("KUMO_LOG_DIR", "/var/log/kumo")
    monkeypatch.delenv("KUMO_LOG_COMPONENTS", raising=False)
    assert _logging_config().dict()["log_component_levels"] == {}
