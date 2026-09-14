"""The deploy precheck's strategies-pin gate must fail CLOSED and must not be waivable by the cockpit
override (#850, from the merged-result review of #848).

Three gaps measured on `deploy/Makefile` `check-tree`:
  1. `ALLOW_UNPUSHED=1` — an override that exists for an UNPUSHED COCKPIT commit — also waived the
     strategies-pin refusal, because both set the same `ok=0` flag.
  2. A parse miss on the pin line (`@main`, a single-quoted line, a reformatted line) left `pinned`
     empty and the gate skipped SILENTLY — reformatting one line disabled the gate with no message.
  3. A checkout AHEAD of the pin passed (containment only): a build ships code the tests never saw,
     the exact sentence the refusal uses against "behind".

Driven through the REAL `check-tree` recipe: the define is extracted from `deploy/Makefile` verbatim
into a scratch Makefile, run with `make` inside a scratch directory laid out the way production is
(`<root>/kumo-cockpit/deploy` beside `<root>/kumo-strategies`), against two scratch git repos.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "deploy" / "Makefile"

pytestmark = pytest.mark.skipif(shutil.which("make") is None or shutil.which("git") is None, reason="needs make + git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(path: Path, n_commits: int = 1) -> list[str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    shas = []
    for i in range(n_commits):
        (path / f"f{i}.txt").write_text(str(i))
        _git(path, "add", "-A"); _git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    return shas


def _scratch(tmp_path: Path, *, pin_line: str, strategies_commits: int = 1, strategies_checkout: str | None = None):
    """A production-shaped layout. Returns the deploy dir to run make in and the strategies repo."""
    root = tmp_path / "projects"
    cockpit = root / "kumo-cockpit"
    _repo(cockpit)
    (cockpit / "backend").mkdir()
    (cockpit / "backend" / "pyproject.toml").write_text(f'[project]\ndependencies = [\n    {pin_line},\n]\n')
    deploy = cockpit / "deploy"; deploy.mkdir()
    src = MAKEFILE.read_text()
    m = re.search(r"^define check-tree\n(.*?)^endef", src, re.S | re.M)
    assert m, "check-tree define not found in deploy/Makefile — the anchor this test drives has moved"
    (deploy / "Makefile").write_text("define check-tree\n" + m.group(1) + "endef\n\nprecheck:\n\t$(check-tree)\n")
    # COMMITTED and "pushed": the cockpit tree must read clean and on origin/main so that only the
    # strategies gate is under test (the first cut wrote these files after the commit and every
    # case refused on "cockpit tree is DIRTY" — the fixture, not the gate).
    _git(cockpit, "add", "-A"); _git(cockpit, "commit", "-q", "-m", "layout")
    _git(cockpit, "update-ref", "refs/remotes/origin/main", _git(cockpit, "rev-parse", "HEAD"))
    strategies = root / "kumo-strategies"
    shas_s = _repo(strategies, strategies_commits)
    _git(strategies, "update-ref", "refs/remotes/origin/main", shas_s[-1])
    if strategies_checkout:
        _git(strategies, "checkout", "-q", "--detach", strategies_checkout)
    return deploy, shas_s


def _run(deploy: Path, **env: str) -> tuple[int, str]:
    e = {**os.environ, **env}
    p = subprocess.run(["make", "-s", "precheck"], cwd=deploy, env=e, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


@pytest.fixture
def clean(tmp_path):
    deploy, shas = _scratch(tmp_path, pin_line='"kumo-strategies @ git+https://github.com/X/kumo-strategies.git@{sha}"', strategies_commits=1)
    # rewrite the pin to the real sha now that we know it
    py = deploy.parent / "backend" / "pyproject.toml"
    py.write_text(py.read_text().replace("{sha}", shas[-1][:7]))
    _git(deploy.parent, "add", "-A"); _git(deploy.parent, "commit", "-q", "-m", "pin")
    _git(deploy.parent, "update-ref", "refs/remotes/origin/main", _git(deploy.parent, "rev-parse", "HEAD"))
    return deploy, shas


def test_the_fixture_passes_when_the_checkout_is_the_pin(clean):
    deploy, _ = clean
    rc, out = _run(deploy)
    assert rc == 0, out


def test_ALLOW_UNPUSHED_does_NOT_waive_the_pin_gate(tmp_path):
    """Gap 1. The override exists for an unpushed COCKPIT commit; the strategies pin is a different
    fact and is UNTRACEABLE-class (2087b85 reached paper this way). Refused even with the override."""
    deploy, shas = _scratch(tmp_path, pin_line='"kumo-strategies @ git+https://github.com/X/kumo-strategies.git@deadbee"')
    rc, out = _run(deploy, ALLOW_UNPUSHED="1")
    assert rc != 0, "ALLOW_UNPUSHED waived the strategies-pin refusal:\n" + out
    assert "PINNED deadbee" in out or "pinned" in out.lower(), out


def test_a_parse_miss_on_the_pin_line_FAILS_CLOSED_with_a_message(tmp_path):
    """Gap 2. `@main` or a single-quoted line leaves the sed empty; today the gate silently skips."""
    deploy, _ = _scratch(tmp_path, pin_line="'kumo-strategies @ git+https://github.com/X/kumo-strategies.git@main'")
    rc, out = _run(deploy)
    assert rc != 0, "an unparseable pin line disabled the gate silently:\n" + out
    assert "pin" in out.lower() and ("could not" in out.lower() or "not found" in out.lower() or "unparse" in out.lower()), out


def test_a_checkout_AHEAD_of_the_pin_is_refused_without_a_loud_override(tmp_path):
    """Gap 3. Containment is not enough: a tree ahead of the pin ships code the tests never saw."""
    deploy, shas = _scratch(tmp_path, pin_line='"kumo-strategies @ git+https://github.com/X/kumo-strategies.git@{sha}"', strategies_commits=2)
    py = deploy.parent / "backend" / "pyproject.toml"
    py.write_text(py.read_text().replace("{sha}", shas[0][:7]))            # pin = first commit; checkout = second
    _git(deploy.parent, "add", "-A"); _git(deploy.parent, "commit", "-q", "-m", "pin")
    _git(deploy.parent, "update-ref", "refs/remotes/origin/main", _git(deploy.parent, "rev-parse", "HEAD"))
    rc, out = _run(deploy)
    assert rc != 0, "a checkout AHEAD of the pin was accepted:\n" + out
    assert "ahead" in out.lower(), out
    rc2, out2 = _run(deploy, ALLOW_AHEAD_OF_PIN="1")
    assert rc2 == 0 and "ALLOW_AHEAD_OF_PIN" in out2, "the loud override must permit it AND say so:\n" + out2
