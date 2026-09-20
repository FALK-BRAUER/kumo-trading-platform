"""Skip-by-name for tests whose SUBJECT the public export omits (#1043) — and the guards on it.

`.public-exclude` keeps `deploy/compose.prod.yml`, `backend/uv.lock`, the captured `api/fixtures/repair_*`
directories and the operating record out of the public tree, and the public scaffold ships its own
`.github/`; the public tree also has no git history before its first commit. A test that reads one of
those in a file that otherwise tests shipped code must SAY the artefact is absent — a skip named
"not shipped in the public tree: <path>" — never fail as if the shipped code were wrong, and never
pass silently. A test FILE whose only subject is an omitted artefact is itself listed in
`.public-exclude` instead (test_uv_lock_matches_the_pin.py, test_captured_fixtures_are_declared.py).

Measured 2026-09-18 on the first public build of cockpit main: 11 reds, every one of this class.

THE HELPERS LIVE IN THIS TEST MODULE, not under api/ as production code: they have no production
caller by design (test_no_orphan_mechanisms.py would list them), and other test files import them the
way they import `api.test_alerts.FakeNode`.

Two vacuity guards. (1) In THE PRIVATE tree every guarded artefact exists, so the skips are never
taken here — a helper that skipped privately would hide a real red behind "not shipped". (2) The two
test files listed in `.public-exclude` really are single-purpose: every test in them reads the omitted
artefact or the file's own helper over it, so excluding them loses no test of shipped code. Both guards
are properties of the private tree — the one that carries `.public-exclude` (the export's P1 rule
refuses a public clone that has one) — and skip by name elsewhere.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]


def require_shipped(relpath: str) -> None:
    """Skip, by name, when `<repo>/<relpath>` is not in this tree."""
    if not (_REPO / relpath).exists():
        pytest.skip(f"not shipped in the public tree: {relpath}")


def require_history(revision: str) -> None:
    """Skip, by name, when this tree's git history does not reach `revision` (the public tree starts
    at its first export commit; the private tree carries the incident the test replays)."""
    out = subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=_REPO,
                         capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip(f"not shipped in the public tree: git history at {revision}")


# =================================================================================================
# The guards
# =================================================================================================
#: Every path a `require_shipped` site names — the SHIPPED compose too, so a typo in a parametrised
#: name cannot skip silently on both trees (review).
GUARDED = ("deploy/compose.paper.yml", "deploy/compose.prod.yml", ".github/workflows-unfunded")
#: file -> the tokens that name its one subject: the artefact, or the file's own helper over it
#: (`_lock_revs` parses uv.lock text; a test of that parser is still a test of the omitted lock).
EXCLUDED_TESTS = {
    "backend/api/test_uv_lock_matches_the_pin.py": ("uv.lock", "_lock_revs"),
    "backend/scripts/test_captured_fixtures_are_declared.py": ("repair_", "CAPTURES", "_readers"),
}
_PRIVATE = (_REPO / ".public-exclude").is_file()
_NOT_PRIVATE = "not the private tree (no .public-exclude): this guard is a private-tree property"
#: An IMMUTABLE sha this repository carries — THE one the #945 replay anchors on, imported rather
#: than copied (review: two copies of one sha drift the day one is bumped) — never a moving ref (#962).
#: Its property is asserted before it anchors anything.
from strategies.test_qc345_build_config_resolves_forward_refs import _PRE_FIX_REVISION as _KNOWN_SHA  # noqa: E402


@pytest.mark.skipif(not _PRIVATE, reason=_NOT_PRIVATE)
@pytest.mark.parametrize("relpath", GUARDED)
def test_every_guarded_artefact_exists_in_the_private_tree_so_no_skip_is_taken_here(relpath):
    assert (_REPO / relpath).exists(), f"{relpath} is missing HERE — the skip would hide a real red"
    require_shipped(relpath)          # must return, not skip


def test_the_helper_skips_BY_NAME_when_the_artefact_is_absent():
    with pytest.raises(pytest.skip.Exception) as info:
        require_shipped("deploy/does-not-exist.yml")
    assert "not shipped in the public tree: deploy/does-not-exist.yml" in str(info.value)


def test_the_history_helper_skips_by_name_for_an_unknown_revision_and_returns_for_a_known_one():
    if _PRIVATE:
        assert subprocess.run(["git", "cat-file", "-e", f"{_KNOWN_SHA}^{{commit}}"], cwd=_REPO).returncode == 0
        require_history(_KNOWN_SHA)   # must return, not skip
    with pytest.raises(pytest.skip.Exception) as info:
        require_history("0000000000000000000000000000000000000000")
    assert "git history at 0000000" in str(info.value)


@pytest.mark.skipif(not _PRIVATE, reason=_NOT_PRIVATE)
def test_the_excluded_test_files_are_single_purpose_and_listed():
    listed = {ln.strip() for ln in (_REPO / ".public-exclude").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")}
    for rel, tokens in EXCLUDED_TESTS.items():
        assert rel in listed, f"{rel} is not in .public-exclude"
        src = (_REPO / rel).read_text()
        tests = re.findall(r"^def (test_\w+)", src, re.M)
        assert tests, rel
        bodies = re.split(r"^def test_\w+", src, flags=re.M)[1:]
        for name, body in zip(tests, bodies):
            assert any(t in body for t in tokens), f"{rel}::{name} touches none of {tokens} — not single-purpose"
