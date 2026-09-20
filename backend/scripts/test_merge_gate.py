"""merge_gate — the two failures it exists for, seen red: a head that is not the pushed head, and a
summary line with a failure that a chain would have echoed past."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import merge_gate as mg


# -- 3. the summary is PARSED ---------------------------------------------------------------------------

@pytest.mark.parametrize("line, failed", [
    ("4355 passed, 7 skipped, 28 deselected, 3 xfailed, 1919 warnings in 58.73s", False),
    ("2 failed, 4251 passed, 6 skipped, 28 deselected, 3 xfailed, 1919 warnings in 58.01s", True),
    ("1 error in 0.25s", True),
    ("== 12 passed, 1 error in 1.0s ==", True),
])
def test_a_red_summary_line_is_refused_and_a_green_one_is_not(line, failed):
    counts = mg.parse_summary("noise\n" + line)
    if failed:
        with pytest.raises(mg.Refused, match="RED"):
            mg.gate_summary(counts)
    else:
        assert "4355 passed" in mg.gate_summary(counts)


def test_no_summary_line_is_NOT_green():
    with pytest.raises(mg.Refused, match="no summary line"):
        mg.parse_summary("Traceback (most recent call last):\n  boom")


def test_a_suite_that_passes_nothing_is_refused():
    with pytest.raises(mg.Refused, match="nothing that passed"):
        mg.gate_summary({"skipped": 3})


def test_the_suite_is_run_and_its_line_read_not_its_exit_code_trusted(tmp_path):
    counts, _ = mg.run_suite("echo '3 passed in 0.1s'", tmp_path)
    assert counts == {"passed": 3}
    with pytest.raises(mg.Refused, match="RED"):
        mg.gate_summary(mg.run_suite("echo '1 failed, 3 passed in 0.1s'; exit 1", tmp_path)[0])
    with pytest.raises(mg.Refused, match="without a red summary"):
        mg.run_suite("echo '3 passed in 0.1s'; exit 3", tmp_path)


# -- 1. the head that merges is the head that was tested --------------------------------------------------

def _repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"; subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"; subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (work / "f").write_text("1"); subprocess.run(["git", "add", "f"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=work, check=True, env={**env, "PATH": "/usr/bin:/bin"})
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD:main"], cwd=work, check=True); subprocess.run(["git", "branch", "-q", "-M", "main"], cwd=work, check=True); subprocess.run(["git", "branch", "-q", "-u", "origin/main"], cwd=work, check=True)
    return origin, work


def test_a_pushed_head_passes_and_an_unpushed_commit_is_refused_by_sha(tmp_path):
    _, work = _repo(tmp_path)
    head, up = mg.assert_head_is_pushed(work)
    assert head == up
    (work / "f").write_text("2"); subprocess.run(["git", "commit", "-q", "-am", "two"], cwd=work, check=True,
                                                 env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})
    with pytest.raises(mg.Refused, match="!= upstream"):
        mg.assert_head_is_pushed(work)


# -- 2. the resolved revision is stated, equal or not ---------------------------------------------------------

def test_the_pin_statement_says_equal_or_names_the_difference():
    assert "== pin" in mg.pin_statement("4d28488", "4d28488abcdef")
    s = mg.pin_statement("e600383", "4d28488abcdef")
    assert "!= pin 4d28488" in s and "NOT the deploy pin" in s


def test_the_pin_is_read_from_pyproject(tmp_path):
    p = tmp_path / "pyproject.toml"; p.write_text('deps = ["kumo-trading-strategies @ git+https://x/y.git@4d28488"]\n')
    assert mg.pinned_strategies_rev(p) == "4d28488"


# -- 6. the success string never prints on a refusal ---------------------------------------------------------

def test_main_refuses_with_exit_2_and_no_success_string_when_the_head_is_not_pushed(tmp_path, capsys):
    _, work = _repo(tmp_path)
    (work / "f").write_text("3"); subprocess.run(["git", "commit", "-q", "-am", "three"], cwd=work, check=True,
                                                 env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})
    rc = mg.main(["1", "--repo", str(work), "--no-comment", "--suite-cmd", "echo '1 passed in 0.1s'"])
    out, err = capsys.readouterr()
    assert rc == 2 and "REFUSED" in err and "MERGE GATE PASSED" not in out


def test_a_missing_interpreter_is_a_refusal_with_a_sentence_not_a_traceback(tmp_path):
    with pytest.raises(mg.Refused, match="cannot run"):
        mg.resolved_strategies_rev(tmp_path / "no-such-python")


def test_the_gate_resolves_kumo_strategies_with_the_interpreter_it_runs_under():
    import sys
    rev = mg.resolved_strategies_rev(Path(sys.executable))
    assert rev and rev != "not-a-git-tree"


def test_publication_state_distinguishes_unpushed_from_diverged(tmp_path):
    origin, work = _repo(tmp_path)
    assert mg.publication_state(work, "x") == "== published main"
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    (work / "f").write_text("9"); subprocess.run(["git", "commit", "-q", "-am", "local"], cwd=work, check=True, env=env)
    assert mg.publication_state(work, "x").startswith("AHEAD of published main by 1 UNPUSHED")
    assert mg.publication_state(None, "x") == "not a git tree"


# -- 7. the PIN is tested, not merely stated (#943) --------------------------------------------------------
# Actions is unfunded (2026-09-11); this gate is permanent. The venv's kumo_strategies is an EDITABLE
# install of a sibling checkout, so a green local run is a statement about that tree, never about the
# image. The pin run puts the pinned sha on disk, ASSERTS the interpreter resolves it there (the anchor
# was found), and runs the suite a second time. Both counts travel; both must be green.

def _strategies_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A local 'remote' with two commits, so the pin checkout is exercised without the network."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    src = tmp_path / "ks"; (src / "src" / "kumo_strategies").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "src" / "kumo_strategies" / "__init__.py").write_text("REV = 'one'\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True); subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=src, check=True, env=env)
    one = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True).stdout.strip()
    (src / "src" / "kumo_strategies" / "__init__.py").write_text("REV = 'two'\n")
    subprocess.run(["git", "commit", "-q", "-am", "two"], cwd=src, check=True, env=env)
    two = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True).stdout.strip()
    return src, one, two


def test_the_strategies_remote_is_read_from_the_same_pin_line(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('deps = ["kumo-trading-strategies @ git+https://github.com/X/kumo-trading-strategies.git@4d28488"]\n')
    assert mg.strategies_remote(p) == "https://github.com/X/kumo-trading-strategies.git"
    assert mg.pinned_strategies_rev(p) == "4d28488"


def test_pin_checkout_puts_the_sha_on_disk_reuses_it_and_refuses_an_unknown_sha(tmp_path):
    src, one, two = _strategies_repo(tmp_path)
    root = tmp_path / "throwaway"
    co = mg.pin_checkout(str(src), one[:7], root=root)
    assert co == root / f"ks-{one[:7]}"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=co, capture_output=True, text=True).stdout.strip() == one
    assert (co / "src" / "kumo_strategies" / "__init__.py").read_text() == "REV = 'one'\n"
    # reuse: the same call again lands on the same dir at the same sha, without cloning twice
    assert mg.pin_checkout(str(src), one[:7], root=root) == co
    # a different sha into the same root is a different dir, and the first is untouched
    co2 = mg.pin_checkout(str(src), two[:7], root=root)
    assert co2 != co and (co2 / "src" / "kumo_strategies" / "__init__.py").read_text() == "REV = 'two'\n"
    with pytest.raises(mg.Refused, match="checkout"):
        mg.pin_checkout(str(src), "deadbeef0", root=root)


def test_pin_run_REFUSES_when_the_interpreter_does_not_resolve_the_pin_at_the_checkout(tmp_path, monkeypatch):
    """The anchor must be found: PYTHONPATH pointing at the checkout is a claim until the interpreter
    running under that env says it imports kumo_strategies FROM THERE at THAT sha. A run whose resolved
    rev is anything else is a suite about the wrong tree wearing the pin's name."""
    src, one, _ = _strategies_repo(tmp_path)
    p = tmp_path / "pyproject.toml"; p.write_text(f'deps = ["kumo-trading-strategies @ git+file://{src}@{one[:7]}"]\n')
    monkeypatch.setattr(mg, "strategies_remote", lambda _p: str(src))
    monkeypatch.setattr(mg, "resolved_strategies_rev", lambda python, env=None, **kw: "deadbee")
    with pytest.raises(mg.Refused, match="anchor"):
        mg.pin_run(Path("/usr/bin/true"), p, "echo '1 passed in 0.1s'", tmp_path, root=tmp_path / "t")


def test_pin_run_runs_the_suite_under_PYTHONPATH_at_the_checkouts_src_and_returns_both_facts(tmp_path, monkeypatch):
    src, one, _ = _strategies_repo(tmp_path)
    p = tmp_path / "pyproject.toml"; p.write_text(f'deps = ["kumo-trading-strategies @ git+file://{src}@{one[:7]}"]\n')
    monkeypatch.setattr(mg, "strategies_remote", lambda _p: str(src))
    seen = {}

    def _resolved(python, env=None, **kw):
        seen["env"] = dict(env or {}); return one[:7]

    monkeypatch.setattr(mg, "resolved_strategies_rev", _resolved)
    sha, counts, out = mg.pin_run(Path("/usr/bin/true"), p, "echo \"PP=$PYTHONPATH\"; echo '2 passed in 0.1s'",
                                  tmp_path, root=tmp_path / "t")
    assert sha == one[:7] and counts == {"passed": 2}
    expected = str(tmp_path / "t" / f"ks-{one[:7]}" / "src")
    assert seen["env"]["PYTHONPATH"].split(":")[0] == expected, "the anchor check ran under the same env"
    assert f"PP={expected}" in out, "the suite ran under PYTHONPATH at the checkout's src"


def test_two_green_runs_with_different_counts_are_a_FINDING_and_a_red_pin_run_is_a_refusal():
    assert mg.compare_runs({"passed": 10, "xfailed": 1}, {"passed": 10, "xfailed": 1}) is None
    finding = mg.compare_runs({"passed": 10, "xfailed": 1}, {"passed": 11})
    assert finding and "xfailed" in finding and "editable" in finding and "pin" in finding
    with pytest.raises(mg.Refused, match="PIN run RED"):
        mg.gate_both({"passed": 10}, {"failed": 1, "passed": 9})
    with pytest.raises(mg.Refused, match="RED"):
        mg.gate_both({"failed": 1, "passed": 9}, {"passed": 10})
    assert "10 passed" in mg.gate_both({"passed": 10}, {"passed": 10})[1]


def test_a_head_that_MOVED_during_the_gate_is_refused_with_both_shas(tmp_path):
    """The suite is evidence about ONE sha. A push between the run and the merge would merge a head the
    suite never saw — the only window left once the gate produces its own evidence in one invocation."""
    _, work = _repo(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
    assert mg.assert_head_unmoved(work, head, remote_head=head) == head
    # the PR's head on GitHub moved (a push landed) while the local head did not
    with pytest.raises(mg.Refused, match="remote"):
        mg.assert_head_unmoved(work, head, remote_head="0000000000")
    # the local head moved
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    (work / "f").write_text("moved"); subprocess.run(["git", "commit", "-q", "-am", "moved"], cwd=work, check=True, env=env)
    with pytest.raises(mg.Refused, match=f"{head[:9]}.*moved|moved.*{head[:9]}"):
        mg.assert_head_unmoved(work, head, remote_head=head)


def test_skipping_the_pin_run_states_the_absence_in_the_evidence_line():
    line = mg.evidence_line(head="abcdef123", pin="p", editable="10 passed", pin_sha=None, pin_counts=None,
                            finding=None, merge=False)
    assert "PIN NOT TESTED" in line and "--skip-pin" in line
    line = mg.evidence_line(head="abcdef123", pin="p", editable="10 passed", pin_sha="84d09d3",
                            pin_counts="10 passed", finding=None, merge=True)
    assert "PIN 84d09d3" in line and "10 passed" in line and "merged" in line


def test_no_workflow_file_remains_where_Actions_would_read_it_and_the_retired_dir_says_why():
    """Actions is unfunded. A `.github/workflows/*.yml` that looks like CI and never runs is a stale
    green badge waiting to be cited; the retired copy lives beside a README that says so.

    The PUBLIC scaffold ships its own `.github/` (funded CI there) and no `workflows-unfunded/`; this
    is the private tree's rule and skips by name where that directory is absent."""
    from api.test_not_shipped import require_shipped
    require_shipped(".github/workflows-unfunded")
    workflows = mg.REPO / ".github" / "workflows"
    assert not list(workflows.glob("*.yml")) and not list(workflows.glob("*.yaml")), list(workflows.iterdir())
    readme = mg.REPO / ".github" / "workflows-unfunded" / "README.md"
    assert readme.exists() and "merge_gate" in readme.read_text() and "unfunded" in readme.read_text().lower()


def test_the_default_suite_runs_the_interpreters_own_pytest_not_a_path_guessed_from_this_file():
    """A worktree has no .venv; a gate that guesses `<backend>/.venv/bin/pytest` from its own location
    cannot run there (the resolver had the same defect, fixed 2026-09-11 05:05Z). The suite command is
    derived from the interpreter the gate runs under, and it includes scripts/ so this file gates itself."""
    import sys
    assert mg.DEFAULT_SUITE.startswith(str(Path(sys.executable).parent / "pytest"))
    assert " scripts " in mg.DEFAULT_SUITE + " "


# -- 8. the anchor is asserted INSIDE the suite (#947) -------------------------------------------------------
# l21wvpmj (cross-repo review of #944): a conftest `sys.path.insert`, an `__editable__*.pth`, or
# `python -m pytest` putting CWD first can make the SUITE import a different kumo_strategies than the
# anchor check resolved in its own interpreter invocation. Two invocations, two facts. So the gate
# exports the expected tree and the suite refuses its whole session unless it imported from there.

def test_pin_env_exports_the_expected_tree_for_the_suites_own_anchor(tmp_path):
    env = mg.pin_env(tmp_path / "ks-abc")
    assert env["MERGE_GATE_EXPECT_KS_TREE"] == str(tmp_path / "ks-abc" / "src")
    assert env["PYTHONPATH"].split(":")[0] == str(tmp_path / "ks-abc" / "src")


def _trivial_test_under_backend() -> Path:
    """A conftest applies to tests UNDER its directory — a file in tmp_path never loads backend/conftest.py
    (the first version of this test passed for that reason: the hook it tested was never in force)."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="_tmp947_", dir=mg.BACKEND / "scripts"))
    t = d / "test_trivial_947.py"; t.write_text("def test_ok():\n    assert True\n")
    return t


def _pytest_under(env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    """A real pytest session over one trivial test placed UNDER backend/, so its conftest is in force."""
    import os, shutil, sys
    t = _trivial_test_under_backend()
    env = {**os.environ, **env_extra}
    try:
        return subprocess.run([str(Path(sys.executable).parent / "pytest"), str(t), "-q", "-p", "no:cacheprovider"],
                              cwd=mg.BACKEND, capture_output=True, text=True, env=env)
    finally:
        shutil.rmtree(t.parent, ignore_errors=True)


def test_the_suite_REFUSES_its_whole_session_when_kumo_strategies_did_not_import_from_the_expected_tree(tmp_path):
    """The variable names a tree kumo_strategies is NOT imported from; the session must stop before
    any test runs and say BOTH paths. Bitten by: an anchor that only checks outside the suite."""
    res = _pytest_under({"MERGE_GATE_EXPECT_KS_TREE": str(tmp_path / "not-the-tree" / "src")}, tmp_path)
    out = res.stdout + res.stderr
    assert res.returncode != 0
    assert "MERGE_GATE_EXPECT_KS_TREE" in out and str(tmp_path / "not-the-tree") in out
    assert "kumo_strategies" in out and "passed" not in out.splitlines()[-1]


def test_the_suite_runs_normally_when_the_expected_tree_is_where_kumo_strategies_lives(tmp_path):
    import kumo_strategies
    real = str(Path(kumo_strategies.__file__).resolve().parents[1])
    res = _pytest_under({"MERGE_GATE_EXPECT_KS_TREE": real}, tmp_path)
    assert res.returncode == 0 and "1 passed" in res.stdout + res.stderr


def test_a_refused_session_is_a_gate_REFUSAL_that_names_the_tree_not_a_missing_summary(tmp_path):
    """`run_suite` must turn the in-suite refusal into a sentence about the anchor, not the generic
    'no summary line' — the operator has to see WHICH tree the suite imported."""
    import os, shutil, sys
    t = _trivial_test_under_backend()
    try:
        with pytest.raises(mg.Refused, match="MERGE_GATE_EXPECT_KS_TREE|imported kumo_strategies from"):
            mg.run_suite(f"{Path(sys.executable).parent / 'pytest'} {t} -q -p no:cacheprovider", mg.BACKEND,
                         env={**os.environ, "MERGE_GATE_EXPECT_KS_TREE": str(tmp_path / "nope" / "src")})
    finally:
        shutil.rmtree(t.parent, ignore_errors=True)


# -- 9. the gate binds to THE COMMIT it tested (#969 findings 5, 4, 7) ------------------------------------
# (5) the merge is atomic with the head check: `gh pr merge --match-head-commit <tested sha>` exists in
#     the installed gh (2.98.0) and refuses server-side if the PR's head moved — closing, not narrowing,
#     the window between the check and the merge.
# (4) gate-only mode never bound the PR to the tested head: the suite ran on whatever was checked out
#     and the evidence posted on the PR number given. The PR's headRefOid is read at STEP 1, beside
#     HEAD == @{u}, in BOTH modes, and a mismatch refuses before the suite runs and posts nothing.
# (7) the anchor check and the suite ran in different cwds; `python -c` puts cwd first on sys.path, so
#     the two derivations of "which kumo_strategies" differed in an input that decides the answer.

def test_the_merge_command_matches_the_TESTED_head_commit():
    argv = mg.merge_command("944", "2aa89946c0ffee")
    assert argv[:4] == ["gh", "pr", "merge", "944"] and "--merge" in argv
    i = argv.index("--match-head-commit")
    assert argv[i + 1] == "2aa89946c0ffee", "the flag must carry the sha the suite ran on, not HEAD re-read"
    import inspect
    assert "merge_command(" in inspect.getsource(mg.main), "main must merge through merge_command"


def test_a_gate_only_run_REFUSES_before_the_suite_when_the_PR_head_is_not_the_tested_head(tmp_path, monkeypatch, capsys):
    """The PR says one sha, the checkout is another: today the suite runs and the comment posts on that
    PR. Now: refusal at step 1, no suite, no comment."""
    _, work = _repo(tmp_path)
    calls = []

    def fake_sh(cmd, *, cwd=None, must_pass=True, env=None):
        calls.append(cmd)
        if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
            return "0000000000000000000000000000000000000000\n"
        return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, env=env).stdout

    monkeypatch.setattr(mg, "sh", fake_sh)
    monkeypatch.setattr(mg, "run_suite", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the suite ran")))
    rc = mg.main(["7", "--repo", str(work), "--skip-pin", "--suite-cmd", "echo '1 passed in 0.1s'"])
    err = capsys.readouterr().err
    assert rc == 2 and "0000000" in err and "PR" in err, f"the refusal must NAME the PR head mismatch, got: {err!r}"
    assert "the suite ran" not in err, "refused at step 1, before the suite — not by the stub raising"
    assert not any(isinstance(c, list) and c[:3] == ["gh", "pr", "comment"] for c in calls), "posted evidence on an untested PR"


def test_the_PR_head_check_refuses_by_naming_both_shas():
    with pytest.raises(mg.Refused, match="abcdef123.*0000000|0000000.*abcdef123"):
        mg.assert_pr_head_is_tested("abcdef1234", "0000000000")
    assert mg.assert_pr_head_is_tested("abcdef1234", "abcdef1234") == "abcdef1234"


def test_the_anchor_check_runs_in_the_SUITES_cwd(tmp_path, monkeypatch):
    """Two processes, one input that decides the answer: `python -c` puts cwd first on sys.path. The
    anchor subprocess must run where the suite runs, so the only difference is the command."""
    seen = {}

    def fake_sh(cmd, *, cwd=None, must_pass=True, env=None):
        seen["cwd"] = cwd; return "abc1234\n"

    monkeypatch.setattr(mg, "sh", fake_sh)
    assert mg.resolved_strategies_rev(Path("/usr/bin/true"), cwd=mg.BACKEND) == "abc1234"
    assert seen["cwd"] == mg.BACKEND
    import inspect
    src = inspect.getsource(mg.pin_run) + inspect.getsource(mg.main)
    assert src.count("cwd=BACKEND") >= 2, "both the pin run's and the editable run's anchor checks pass the suite's cwd"
