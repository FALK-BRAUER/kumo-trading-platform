"""The rotation payload must not go stale on its own, and must not travel by disk (#384).

WHY THIS FILE EXISTS
--------------------
`scripts/refresh_rotation.py` was written, run ONCE by hand, and scheduled by nothing — no crontab
entry, no launchd agent, no compose service. The Market tab served a payload generated 2026-08-19
14:05 ET for eighteen hours, and because that timestamp is two hours BEFORE that day's close, the read
never saw the session it claimed to describe. The operator had to ask "premarket or yesterday?" to find it.

That was the fourth "mechanism built, nothing drives it" in this repo: `plan_distribution` and
`distribute_unallocated` (complete, tested, zero callers), `mark_to_market` (no caller), the
managed-portfolio `strategy` config field, and that script.

THE INTERIM FIX AND WHY IT ALSO WENT
------------------------------------
A sidecar was added to run the tool every five minutes and write `rotation.json` for the api to read
back. It fixed the staleness and left two things wrong, both of which then happened:

  * A SECOND DATA SOURCE. The tool pulls daily OHLC from Yahoo, so the cockpit graded rotations off one
    feed while trading off Alpaca bars.
  * A FILE LOOKS HEALTHY WHEN IT IS NOT. On 2026-08-21 the sidecar's bind mount went stale, every
    refresh failed with `FileNotFoundError: /data/rotation/.rotation.tmp`, and the route served a
    four-hour-old payload with no signal at all — a present, parseable file reads as data.

Operator: "it should not feed from a file!!!!"

WHAT THIS FILE NOW PINS
-----------------------
The DEPLOYMENT half: no sidecar, no data mount, no path env, and ledger-tool's tooling mounted read-only
into the ENGINE, which is what now computes the read. The CODE half — that the engine refreshes on a
timer and the route reads the published plane — is `test_rotation_is_not_a_file.py`.
"""

from __future__ import annotations

import pathlib

_COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "compose.paper.yml"


def _compose() -> dict:
    import yaml

    return yaml.safe_load(_COMPOSE.read_text())


def test_the_compose_file_is_being_read():
    """The fixture's own property. A path typo would make every assertion below vacuously true."""
    assert _COMPOSE.exists(), _COMPOSE
    assert "engine:" in _COMPOSE.read_text()


def test_there_is_no_rotation_SIDECAR_any_more():
    """The service that shelled out to the tool and wrote the file. Its bind mount going stale is what
    served a four-hour-old payload on 2026-08-21."""
    assert "rotation" not in _compose()["services"]


def test_nothing_MOUNTS_the_rotation_data_directory():
    """The file is gone, so the mount that carried it must be too — otherwise the next person wires a
    reader back to a directory that nothing writes."""
    text = _COMPOSE.read_text()
    assert "data/rotation" not in text
    assert "KUMO_ROTATION_PATH" not in text


def test_the_ENGINE_no_longer_MOUNTS_A_TOOLING_DIRECTORY_to_compute_the_rotation():
    """REVERSED, 2026-08-26. This asserted the OPPOSITE — that the engine mounts the tooling — on the
    reasoning that "a second Ichimoku here would be two derivations of one fact".

    THE TRADE-OFF WAS REAL AND IT IS NOW DECIDED THE OTHER WAY, by Operator: the market compass is part of
    kumo. The cost of the old arrangement was not theoretical — an env var could switch the market view
    off, `KUMO_LEDGER_TOOL_TOOLS=/nonexistent` read as deliberate configuration, and ibkr-paper-retired showed
    "No rotations to show" for days while the tile, the stream and the data path all worked.

    The two-derivations risk is MANAGED, not dismissed: `test_rotation_grade_matches_the_original.py`
    compared both implementations field by field, and function by function on raw numbers, over the
    same bars while the mount still existed. That comparison is the reason to believe they agree; the
    verdicts it recorded are what constrain the port now the mount is gone.
    """
    vols = _compose()["services"]["engine"]["volumes"]
    tooling = [str(v) for v in vols if "ledger-tool/tools" in str(v)]
    assert not tooling, (
        f"the engine mounts a tooling directory again: {tooling}. The grading is "
        f"`backend/strategies/rotation_grade.py`; a mount means an env var can switch the market "
        f"view off")
    # DECLARED, not merely mentioned. The comment that records this removal names the variable, and a
    # substring check flagged that — the same describe-versus-do confusion the class test hit twice.
    # A YAML comment cannot switch anything off; an environment key can.
    declared = [l for l in _COMPOSE.read_text().splitlines()
                if "KUMO_LEDGER_TOOL_TOOLS" in l and not l.lstrip().startswith("#")]
    assert not declared, (
        f"the market view is switchable by env var again: {declared}")


def test_it_does_not_rebuild_or_restart_the_ENGINE_image():
    """The tool is pure stdlib — `grade_full.py` imports json/sys/urllib.request/argparse/datetime and
    nothing else — so mounting it needs no image change and adds no dependency. A rotation feature that
    required rebuilding the trading engine would be a much larger thing to ship."""
    text = _COMPOSE.read_text()
    assert "pip install" not in text
