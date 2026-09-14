"""`backend/uv.lock` must agree with the kumo-strategies pin in `backend/pyproject.toml` (#850).

The pin was moved by hand in #848/#874/#885 and nothing in CI checks the lock follows it — the lock
can drift on the next bump and no one would know until an install resolves the wrong revision.
CI installs with pip, so `uv lock --check` is not in the pipeline; this test is the check.
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _pin(pyproject: str) -> str:
    m = re.search(r'kumo-strategies\.git@([0-9a-f]{7,40})["\']', pyproject)
    assert m, "the kumo-strategies pin line was not found in pyproject.toml — the deploy gate refuses on the same miss"
    return m.group(1)


def _lock_revs(lock: str) -> tuple[set[str], set[str]]:
    short = set(re.findall(r'kumo-strategies\.git\?rev=([0-9a-f]{7,40})["#]', lock))
    full = set(re.findall(r'kumo-strategies\.git\?rev=[0-9a-f]{7,40}#([0-9a-f]{40})"', lock))
    return short, full


def test_the_fixture_reads_both_files():
    """Fixture property: both files carry a pin and the lock carries the resolved 40-char revision."""
    short, full = _lock_revs((BACKEND / "uv.lock").read_text())
    assert short and full, "uv.lock carries no kumo-strategies revision — the lock is not pinning the package"


def test_the_lock_rev_matches_the_pyproject_pin():
    pin = _pin((BACKEND / "pyproject.toml").read_text())
    short, full = _lock_revs((BACKEND / "uv.lock").read_text())
    assert short == {pin}, f"uv.lock pins {sorted(short)} but pyproject pins {pin} — bump both together"
    assert all(f.startswith(pin) for f in full), f"uv.lock resolved revision {sorted(full)} does not start with the pin {pin}"


def test_a_drifted_lock_is_caught():
    """The check must be able to fail: a lock at the previous pin against a bumped pyproject."""
    lock = 'source = { git = "https://github.com/X/kumo-strategies.git?rev=0fbcfff#0fbcfffd944e490c736819e38d95f76f24987cb0" }\n' \
           '    { name = "kumo-strategies", git = "https://github.com/X/kumo-strategies.git?rev=0fbcfff" },'
    short, full = _lock_revs(lock)
    assert short == {"0fbcfff"} and not all(f.startswith("4d28488") for f in full)
