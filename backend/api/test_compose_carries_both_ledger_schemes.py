"""compose.paper.yml must carry BOTH ledger-mount schemes and forward every ledger/trace key —
for every instance.env in the instances repo, not for the one that happened to be looked at.

MEASURED 2026-09-18 01:15 SGT (lead, running `bin/check-compose-honours-instance.sh` on both
instances): main mounted the ledger SAME-PATH (`${KUMO_LEDGER_DIR:?}`, #1045) and alpaca-paper's env
depended on it; the ibkr-paper branch (codex cb938f8) had REPLACED that mount with
`${KUMO_REFERENCE_BOOK_DIR:-/var/empty}:/reference-book:ro` and ibkr-paper's env depended on THAT. A
plain cherry-pick of the ibkr-paper branch onto main dropped alpaca-paper's mount; main alone refused
ibkr-paper's deploy on five undeclared keys and alpaca-paper's on one (`KUMO_LEDGER_CSV` — the name the
strategies reader asks for, ks#229; forwarding only the older name is how the ledger source failed
silently 2026-09-15..17, #1097).

TWO HALVES. The first reads this repo's compose and pins the shape (both mounts, the four keys,
each parameterised with an EMPTY default — `:?` on either mount would refuse the other instance).
The second runs the instances repo's OWN precheck — the deploy gate — against every
`instances/*/instance.env` it finds, so the rule is the gate's rule and not a second derivation of
it. When the instances repo is not beside this one the second half SKIPS BY NAME: not run is not
passed, and a skip that says so is the honest third state.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _BACKEND.parent / "deploy" / "compose.paper.yml"


def _instances_repo() -> pathlib.Path:
    """`KUMO_INSTANCES_DIR`, else the sibling of this checkout, else the sibling of a `-wt/<name>`
    worktree's main checkout. Whichever exists first; the last candidate when none does, so the
    skip reason names a real path."""
    import os

    candidates = [pathlib.Path(p) for p in (os.environ.get("KUMO_INSTANCES_DIR"),) if p]
    root = _BACKEND.parent
    candidates += [root.parent / "kumo-trading-platform/instances", root.parent.parent / "kumo-trading-platform/instances"]
    return next((c for c in candidates if (c / "bin" / "check-compose-honours-instance.sh").is_file()),
                candidates[-1])


_SKIP = (f"instances repo not at {{}} — the per-instance precheck was NOT run (a deploy-stage "
         f"`.stage/<i>/cockpit` checkout has no sibling; run from a dev worktree or set KUMO_INSTANCES_DIR)")


_INSTANCES = _instances_repo()
_PRECHECK = _INSTANCES / "bin" / "check-compose-honours-instance.sh"

#: The keys the two instance schemes declare between them. Every one must reach the engine.
LEDGER_KEYS = ("KUMO_REFERENCE_BOOK_CSV", "KUMO_LEGACY_REFERENCE_BOOK_SOURCE", "KUMO_LEDGER_CSV",
               "KUMO_PACER_TRACE")


def _engine_block() -> str:
    src = _COMPOSE.read_text()
    m = re.search(r"^  engine:$", src, re.M)
    assert m, "compose.paper.yml has no `engine` service"
    rest = src[m.end():]
    nxt = re.search(r"^  [a-z0-9-]+:$", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_the_fixture_is_the_real_compose_and_has_an_engine_environment():
    block = _engine_block()
    assert "    environment:" in block and "    volumes:" in block


@pytest.mark.parametrize("key", LEDGER_KEYS)
def test_every_ledger_and_trace_key_is_forwarded_to_the_engine_parameterised_with_an_empty_default(key):
    """`${KEY:-}` exactly: a literal would override the instance (the #581 shape), a non-empty
    default would force a path on the instance that did not declare one."""
    block = _engine_block()
    lines = [ln for ln in block.splitlines() if re.match(rf"^\s{{6}}{key}:", ln)]
    assert lines, f"{key} is not in the engine environment — a declaring instance is refused at deploy"
    assert lines == [f"      {key}: ${{{key}:-}}"], lines


def test_the_api_service_does_NOT_carry_the_ledger_keys_because_no_api_process_reads_them():
    """Engine-only is COMPLETE (review, measured on an Alpaca paper instance): the only reader is kumo-trading-strategies'
    ledger source in the engine; `grep` over api/ and strategies/ finds no cockpit reader. A key
    added to the api block "for symmetry" is two readers of one file that then drift."""
    src = _COMPOSE.read_text()
    m = re.search(r"^  api:$", src, re.M)
    assert m
    rest = src[m.end():]
    nxt = re.search(r"^  [a-z0-9-]+:$", rest, re.M)
    api_block = rest[: nxt.start()] if nxt else rest
    for key in LEDGER_KEYS:
        assert not re.search(rf"^\s{{6}}{key}:", api_block, re.M), f"{key} in the api block"


def test_both_ledger_mount_schemes_are_present_and_neither_refuses_the_other_instance():
    """alpaca-paper: same-path `${KUMO_LEDGER_DIR}`; ibkr-paper: `${KUMO_REFERENCE_BOOK_DIR}` at
    `/reference-book`. Each optional with `/var/empty` — a no-op mount for the instance that uses the
    other scheme. `:?` on either is what refused ibkr-paper on main."""
    block = _engine_block()
    mounts = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("- ${KUMO_")]
    assert "- ${KUMO_LEDGER_DIR:-/var/empty}:${KUMO_LEDGER_DIR:-/var/empty}:ro" in mounts, mounts
    assert "- ${KUMO_REFERENCE_BOOK_DIR:-/var/empty}:/reference-book:ro" in mounts, mounts
    assert not [m for m in mounts if ":?" in m], f"a required mount refuses the other instance: {mounts}"
    # TWO SCHEMES means two CONTAINER paths. Collapsing both onto /reference-book would still "carry
    # both keys" — alpaca-paper's `KUMO_LEDGER_CSV` names the host path and would then read nothing.
    container_paths = [re.match(r"^- \$\{[^}]+\}:(.+?):ro$", m).group(1) for m in mounts]
    assert len(set(container_paths)) == len(container_paths) == 2, container_paths
    assert "/reference-book" in container_paths and "${KUMO_LEDGER_DIR:-/var/empty}" in container_paths


def _instance_envs() -> list[pathlib.Path]:
    return sorted((_INSTANCES / "instances").glob("*/instance.env")) if _INSTANCES.is_dir() else []


#: THE INSTANCES COMMIT THIS TEST READS — immutable, never `HEAD` (#962: a test anchored on a moving
#: ref of a real repository passes and fails with the world, and the gate runs where the world
#: differs). 4b8c380 = kumo-trading-platform/instances main on 2026-09-18 12:00 SGT, the state the precheck
#: was measured against. Its PROPERTY is asserted before it anchors anything (below): both env files
#: at that commit declare the keys this compose exists to forward. Bump it deliberately, with the
#: measurement, when an instance's declarations change.
_INSTANCES_SHA = "4b8c380"
_EXPECTED_DECLARED = {"ibkr-paper": ("KUMO_LEDGER_CSV", "KUMO_PACER_TRACE", "KUMO_REFERENCE_BOOK_DIR"),
                      "alpaca-paper": ("KUMO_LEDGER_CSV", "KUMO_LEDGER_DIR")}


def _committed_env(envfile: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    """`git show <immutable sha>:<path>` from the instances repo — the COMMITTED instance at a named
    commit, not the lead's working tree (review: a test whose verdict moves with someone's
    uncommitted edit is the live-database-in-tests class) and not `HEAD` (#962)."""
    rel = envfile.relative_to(_INSTANCES).as_posix()
    # `cwd=` names WHICH repository is read (the instances repo, not this one) — the #962 guard's
    # fixture-scoped form; the revision is the immutable pin above.
    out = subprocess.run(["git", "show", f"{_INSTANCES_SHA}:{rel}"], cwd=str(_INSTANCES),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"instances repo could not show {_INSTANCES_SHA}:{rel}: {out.stderr}"
    dst = tmp_path / f"{envfile.parent.name}.{_INSTANCES_SHA}.instance.env"
    dst.write_text(out.stdout)
    return dst


@pytest.mark.skipif(not _PRECHECK.is_file(), reason=_SKIP.format(_INSTANCES))
@pytest.mark.parametrize("envfile", _instance_envs(), ids=lambda p: p.parent.name)
def test_the_deploy_gate_passes_for_every_instance_env_beside_this_repo(envfile, tmp_path):
    """THE GATE'S OWN RULE, not a copy of it: `bin/check-compose-honours-instance.sh` refuses a
    deploy for any declared key compose does not forward. Red on main for ibkr-paper (5 keys) and
    alpaca-paper (1 key) on 2026-09-18. Against the instances repo's COMMITTED env."""
    out = subprocess.run(["bash", str(_PRECHECK), str(_COMPOSE), str(_committed_env(envfile, tmp_path))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"{envfile.parent.name}:\n{out.stdout}{out.stderr}"


@pytest.mark.skipif(not _PRECHECK.is_file(), reason=_SKIP.format(_INSTANCES))
@pytest.mark.parametrize("name", sorted(_EXPECTED_DECLARED))
def test_the_pinned_instances_commit_declares_the_keys_this_compose_forwards(name, tmp_path):
    """The anchor's property, asserted BEFORE it anchors the gate run: the env files at
    `_INSTANCES_SHA` declare the keys. An anchor that no longer has the property is a pin that must
    be bumped with a new measurement, not a test that quietly passes on an old tree."""
    env = _committed_env(_INSTANCES / "instances" / name / "instance.env", tmp_path).read_text()
    declared = {ln.split("=", 1)[0] for ln in env.splitlines() if "=" in ln and not ln.startswith("#")}
    missing = [k for k in _EXPECTED_DECLARED[name] if k not in declared]
    assert not missing, f"{name}@{_INSTANCES_SHA} does not declare {missing} — bump _INSTANCES_SHA with a measurement"


def test_the_gate_run_above_was_not_vacuous():
    """A parametrised test over an empty list is a test that ran zero times and reported green."""
    if not _PRECHECK.is_file():
        pytest.skip(_SKIP.format(_INSTANCES))
    assert {p.parent.name for p in _instance_envs()} >= {"ibkr-paper", "alpaca-paper"}
