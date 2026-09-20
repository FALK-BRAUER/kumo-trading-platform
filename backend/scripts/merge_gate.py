#!/usr/bin/env python3
"""merge_gate — the enforcement point CI used to be, while merges run on the local suite.

Two merge-process failures in one hour (2026-09-11, coordinator): a push REJECTED non-fast-forward
whose step echoed "pushed" without checking the exit status, so `gh pr merge` squashed a STALE head and
dropped 59 lines of tests; and a merge chain that did not GATE on the suite result and merged with two
red tests. Same shape: a hand-rolled chain reporting success it never verified. CI was not only a test
runner, it was the thing that made a red or unpushed head UNMERGEABLE. This is that thing, as a tool.

    python scripts/merge_gate.py <pr-number> [--merge] [--suite-cmd "..."] [--no-comment] [--skip-pin]

  1. HEAD == @{u}, or refuse and name both shas                     (the stale-head failure)
  2. print the RESOLVED kumo_strategies revision and whether it equals the pin in pyproject
  3. run the suite; PARSE the summary line; refuse on any "failed" or "error"   (the ungated merge)
  3b. run it AGAIN against the PIN (#943): the pinned sha in a throwaway checkout under PYTHONPATH,
      the anchor asserted (the interpreter resolves kumo_strategies THERE at THAT sha), both counts
      printed; a difference between two green runs is a FINDING, a red pin run is a refusal
  4. print the counts in the form the PR comment wants, and post them (unless --no-comment)
  4b. before --merge: the head must not have MOVED since the suite ran (local and the PR's headRefOid)
  5. after --merge: assert `git diff <reviewed> origin/main -- <the PR's files>` is EMPTY and say so
  6. exit non-zero at every refusal; the success string is printed on the zero path only

THIS IS THE PERMANENT GATE. GitHub Actions will not be funded (2026-09-11). CI did one thing the
local suite does not: it INSTALLED THE PIN. `backend/.venv` imports kumo_strategies from an EDITABLE
sibling checkout — whatever that tree is at — so a green local run is a statement about one machine,
never about the image. Step 3b is that difference, made a step: two derivations of one answer, both
printed, both required.

Every refusal is a sentence naming what disagreed. Nothing here prints "ok" it did not check.

TWO LESSONS THE FIRST DOGFOOD RUNS TAUGHT, kept here so nobody simplifies them back:
  * A PROCESS'S EXIT CODE IS NOT A STATEMENT ABOUT THE WORLD. `gh pr merge --delete-branch` merged
    remotely, then failed on a LOCAL checkout of main (another worktree held it) — a non-zero exit for
    a merge that had happened; the mirror of a rejected push that echoed "pushed". The merge is verified
    by the PR's STATE (`gh pr view --json state == MERGED`) and by `git diff reviewed..origin/main`.
  * THE THING BEING EXAMINED MUST NOT BE CHOSEN BY SOMETHING INCIDENTAL. Three instances in one
    evening: a resolver that knew one lane family's attribute spelling; a scan whose roots were
    CWD-relative; this gate guessing its venv from its own file path (a worktree has none). It resolves
    kumo_strategies with the interpreter it runs under and runs the suite from backend/, and SAYS so.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
#: THE INTERPRETER RUNNING THE GATE supplies pytest — not a venv path guessed from this file's location. A
#: worktree has no .venv of its own; the guess is the same defect the resolver had (05:05Z, first dogfood).
DEFAULT_SUITE = f"{Path(sys.executable).parent / 'pytest'} api strategies scripts -q -p no:cacheprovider"


class Refused(SystemExit):
    """A refusal is an exit code AND a sentence; `str()` is the sentence, so a test can match it."""

    def __init__(self, why: str):
        super().__init__(2)
        self.why = why

    def __str__(self) -> str:
        return self.why


def sh(cmd: list[str] | str, *, cwd: Path | None = None, must_pass: bool = True,
       env: dict | None = None) -> str:
    try:
        res = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True,
                             env=env)
    except OSError as exc:  # a missing interpreter or binary is a refusal with a sentence, not a traceback
        raise Refused(f"cannot run `{cmd if isinstance(cmd, str) else ' '.join(cmd)}`: {exc}") from None
    if must_pass and res.returncode != 0:
        raise Refused(f"`{cmd if isinstance(cmd, str) else ' '.join(cmd)}` exited {res.returncode}: {res.stderr.strip()[:300]}")
    return res.stdout


# -- 1. the head the reviewer saw is the head that merges --------------------------------------------------

def assert_head_is_pushed(repo: Path) -> tuple[str, str]:
    sh(["git", "fetch", "-q", "origin"], cwd=repo)
    head = sh(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    try:
        upstream = sh(["git", "rev-parse", "@{u}"], cwd=repo).strip()
    except Refused as exc:
        raise Refused(f"no upstream for this branch — push it first ({exc.why})") from None
    if head != upstream:
        raise Refused(f"HEAD {head[:9]} != upstream {upstream[:9]} — the head you tested is not the head that would merge; push (or pull) first")
    return head, upstream


# -- 2. what the suite actually runs against ---------------------------------------------------------------

def resolved_strategies_rev(python: Path, env: dict | None = None, *, cwd: Path | None = None) -> str:
    """The revision the interpreter resolves kumo_strategies to. `cwd` MUST be the suite's (#969 f.7):
    `python -c` puts cwd first on sys.path, so an anchor checked from another directory and a suite
    run from backend/ are two derivations of "which package" that differ in an input that decides
    the answer."""
    code = ("import kumo_strategies, subprocess, os; p=os.path.dirname(kumo_strategies.__file__); "
            "print(subprocess.run(['git','-C',p,'rev-parse','--short','HEAD'],capture_output=True,text=True).stdout.strip() or 'not-a-git-tree')")
    return sh([str(python), "-c", code], env=env, cwd=cwd).strip()


def resolved_strategies_tree(python: Path) -> Path | None:
    code = "import kumo_strategies, os; print(os.path.dirname(kumo_strategies.__file__))"
    p = Path(sh([str(python), "-c", code]).strip())
    return p if (p / ".git").exists() or any(q.joinpath(".git").exists() for q in p.parents) else None


def publication_state(tree: Path | None, resolved: str) -> str:
    """AHEAD OF main BY UNPUSHED COMMITS is a different risk from A DIFFERENT PUBLISHED REVISION — the
    first is code that exists on one machine, the second is a skew somebody could reproduce."""
    if tree is None:
        return "not a git tree"
    try:
        sh(["git", "-C", str(tree), "fetch", "-q", "origin"], must_pass=False)
        base = sh(["git", "-C", str(tree), "merge-base", "origin/main", "HEAD"]).strip()
        main = sh(["git", "-C", str(tree), "rev-parse", "origin/main"]).strip()
        head = sh(["git", "-C", str(tree), "rev-parse", "HEAD"]).strip()
    except Refused as exc:
        return f"publication unknown ({exc.why[:80]})"
    if head == main:
        return "== published main"
    if base == main:
        n = sh(["git", "-C", str(tree), "rev-list", "--count", "origin/main..HEAD"]).strip()
        return f"AHEAD of published main by {n} UNPUSHED commit(s) — code that exists on this machine only"
    if base == head:
        n = sh(["git", "-C", str(tree), "rev-list", "--count", "HEAD..origin/main"]).strip()
        return f"BEHIND published main by {n} commit(s)"
    return "on a DIFFERENT published line (diverged from main) — a skew somebody could reproduce"


def pinned_strategies_rev(pyproject: Path) -> str | None:
    m = re.search(r"kumo-trading-strategies[^\n]*?@([0-9a-f]{7,40})", pyproject.read_text())
    return m.group(1) if m else None


def pin_statement(resolved: str, pinned: str | None) -> str:
    if pinned and resolved and (pinned.startswith(resolved) or resolved.startswith(pinned)):
        return f"kumo_strategies resolved {resolved} == pin {pinned[:9]}"
    return f"kumo_strategies resolved {resolved} != pin {pinned[:9] if pinned else '?'} — the suite is a statement about {resolved}, NOT the deploy pin"


# -- 3b. the PIN is tested, not merely stated (#943) --------------------------------------------------------

def strategies_remote(pyproject: Path) -> str | None:
    """The remote on the SAME line as the pin: `kumo-trading-strategies @ git+<url>@<sha>` -> `<url>`."""
    m = re.search(r"kumo-trading-strategies[^\n]*?git\+([^@\s\"']+)@[0-9a-f]{7,40}", pyproject.read_text())
    return m.group(1) if m else None


def pin_checkout(remote: str, sha: str, *, root: Path = Path("/tmp")) -> Path:
    """`<root>/ks-<sha>`, a detached checkout of exactly that sha. Cloned once, fetched on reuse.
    Refuses — never guesses — when the sha cannot be checked out: a run against the wrong tree wearing
    the pin's name is worse than no run."""
    co = root / f"ks-{sha}"
    if not (co / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        try:
            sh(["git", "clone", "-q", "--no-checkout", remote, str(co)])
        except Refused as exc:
            raise Refused(f"pin checkout: cannot clone {remote}: {exc.why[:200]}") from None
    sh(["git", "-C", str(co), "fetch", "-q", "origin"], must_pass=False)
    try:
        sh(["git", "-C", str(co), "checkout", "-q", "--detach", sha])
    except Refused as exc:
        raise Refused(f"pin checkout: {sha} is not a revision of {remote}: {exc.why[:200]}") from None
    return co


def pin_env(checkout: Path) -> dict:
    """The suite's environment with the checkout's `src` FIRST on PYTHONPATH. Measured 2026-09-11:
    PYTHONPATH precedes the editable finder, so the pin wins the import — which is exactly what the
    anchor check below then has to PROVE rather than assume."""
    import os
    env = dict(os.environ)
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(checkout / "src") + (f":{prior}" if prior else "")
    # THE SUITE ASSERTS THE ANCHOR ITSELF (#947): backend/conftest.py refuses the whole session unless
    # kumo_strategies imported from under this tree. The outer check below is the earlier, cheaper
    # refusal; this one is the proof, because it runs in the same process as the tests.
    env["MERGE_GATE_EXPECT_KS_TREE"] = str(checkout / "src")
    return env


def pin_run(python: Path, pyproject: Path, suite_cmd: str, cwd: Path, *,
            root: Path = Path("/tmp")) -> tuple[str, dict[str, int], str]:
    """(pin sha, counts, output). The ANCHOR IS ASSERTED before the suite runs: the interpreter, under
    the pin env, must resolve kumo_strategies to the checkout at the pinned sha. `**kw`-style silence
    here — PYTHONPATH set, import resolved elsewhere — would be a suite about the wrong tree."""
    sha = pinned_strategies_rev(pyproject)
    remote = strategies_remote(pyproject)
    if not sha or not remote:
        raise Refused(f"no kumo-trading-strategies pin line in {pyproject} — the pin run has nothing to test")
    co = pin_checkout(remote, sha, root=root)
    env = pin_env(co)
    resolved = resolved_strategies_rev(python, env, cwd=BACKEND)
    if not (sha.startswith(resolved) or resolved.startswith(sha)):
        raise Refused(f"pin run anchor NOT found: under PYTHONPATH={env['PYTHONPATH'].split(':')[0]} the "
                      f"interpreter resolves kumo_strategies at {resolved}, not the pin {sha} — refusing "
                      f"to run a suite that would wear the pin's name")
    counts, out = run_suite(suite_cmd, cwd, env=env)
    return sha, counts, out


def compare_runs(editable: dict[str, int], pin: dict[str, int]) -> str | None:
    """Two green runs that COUNT differently are a finding, not noise: an xfail that passes on one tree
    is the generator-canary shape, and under a local-only gate it is the only way that class shows."""
    keys = sorted(set(editable) | set(pin))
    diff = {k: (editable.get(k, 0), pin.get(k, 0)) for k in keys if editable.get(k, 0) != pin.get(k, 0)}
    if not diff:
        return None
    return "FINDING: the editable tree and the pin disagree — " + ", ".join(
        f"{k}: editable {a} vs pin {b}" for k, (a, b) in diff.items())


def gate_both(editable: dict[str, int], pin: dict[str, int]) -> tuple[str, str]:
    """Both runs must be green. The pin run's red is named as the PIN's, because that is the one that
    says the deploy is broken while the local tree is fine."""
    editable_summary = gate_summary(editable)
    if pin.get("failed", 0) or pin.get("error", 0):
        raise Refused(f"PIN run RED: {pin.get('failed', 0)} failed, {pin.get('error', 0)} errors against the "
                      f"deploy pin while the editable tree passed — the image would carry this")
    return editable_summary, gate_summary(pin)


# -- 4b. the head that merges is the head the suite saw ------------------------------------------------------

def pr_head(pr: str, repo: Path) -> str:
    """The PR's head on GitHub — the sha the evidence will be read against."""
    out = sh(["gh", "pr", "view", pr, "--json", "headRefOid", "-q", ".headRefOid"], cwd=repo).strip()
    if not out:
        raise Refused(f"PR {pr}: gh returned no headRefOid — cannot bind the gate to a commit")
    return out


def assert_pr_head_is_tested(head: str, remote_head: str) -> str:
    """STEP 1, BOTH MODES (#969 f.4): the checkout the suite will run on IS the PR's head. Without
    this a gate-only run tested whatever was checked out and posted its evidence on the PR number
    it was given — a comment describing a run on unrelated code."""
    if remote_head != head:
        raise Refused(f"PR head {remote_head[:9]} is not the tested head {head[:9]} — the checkout is "
                      f"not the PR; refusing before the suite runs and posting nothing")
    return head


def merge_command(pr: str, head: str) -> list[str]:
    """The merge, ATOMIC with the head check (#969 f.5): `--match-head-commit` makes GitHub refuse the
    merge if the PR's head is no longer the sha the suite ran on. The window between our check and
    the merge is closed by the server, not narrowed by re-reading."""
    return ["gh", "pr", "merge", pr, "--merge", "--match-head-commit", head]


def assert_head_unmoved(repo: Path, head: str, *, remote_head: str | None) -> str:
    """Local HEAD and the PR's head on GitHub must both still be the sha the suite ran on."""
    now = sh(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    if now != head:
        raise Refused(f"head moved during the gate: suite ran on {head[:9]}, HEAD is now {now[:9]} — the "
                      f"evidence is about a sha that is no longer the head")
    if remote_head is not None and remote_head != head:
        raise Refused(f"the PR's remote head {remote_head[:9]} != the tested head {head[:9]} — a push landed "
                      f"between the suite and the merge")
    return head


def evidence_line(*, head: str, pin: str, editable: str, pin_sha: str | None, pin_counts: str | None,
                  finding: str | None, merge: bool) -> str:
    """The sentence the PR carries. An untested pin is SAID, never omitted."""
    pin_part = (f"; against PIN {pin_sha} (throwaway checkout, anchor asserted INSIDE the suite): {pin_counts}"
                if pin_sha else "; PIN NOT TESTED (--skip-pin) — this run says nothing about the image")
    return (f"merge_gate: head {head[:9]} pushed; {pin}; local suite from backend/ against the editable "
            f"tree: {editable}{pin_part}" + (f"; {finding}" if finding else "")
            + (" — merged after this gate" if merge else " — gate only, not merged"))


# -- 3./4. the suite, parsed, never inferred -----------------------------------------------------------------

_SUMMARY = re.compile(r"^(?:=+ )?(?P<body>(?:\d+ (?:passed|failed|skipped|xfailed|xpassed|deselected|error|errors|warnings?)(?:, )?)+)(?: in [\d.]+s)?")


def parse_summary(output: str) -> dict[str, int]:
    """The LAST pytest summary line, as counts. Refuses if there is none — 'no line' is not 'green'."""
    for line in reversed(output.strip().splitlines()):
        m = _SUMMARY.search(line.strip(" ="))
        if m:
            counts: dict[str, int] = {}
            for n, kind in re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?)", m.group("body")):
                counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
            return counts
    raise Refused("the suite printed no summary line — refusing to call that green")


def gate_summary(counts: dict[str, int]) -> str:
    if counts.get("failed", 0) or counts.get("error", 0):
        raise Refused(f"suite RED: {counts.get('failed', 0)} failed, {counts.get('error', 0)} errors — not merging")
    if not counts.get("passed", 0):
        raise Refused(f"suite ran nothing that passed: {counts} — not merging")
    return ", ".join(f"{v} {k}" for k, v in counts.items() if k in ("passed", "skipped", "xfailed", "xpassed", "deselected"))


def run_suite(cmd: str, cwd: Path, env: dict | None = None) -> tuple[dict[str, int], str]:
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
    out = res.stdout + "\n" + res.stderr
    if "MERGE_GATE_EXPECT_KS_TREE=" in out and "refusing the" in out:
        # the suite refused its own session (#947): say WHICH tree it imported, not "no summary line"
        line = next((ln for ln in out.splitlines() if "MERGE_GATE_EXPECT_KS_TREE=" in ln), out[-400:])
        raise Refused(f"the suite refused its session — {line.strip()[:400]}")
    counts = parse_summary(out)
    if res.returncode != 0 and not (counts.get("failed") or counts.get("error")):
        raise Refused(f"suite exited {res.returncode} without a red summary — refusing: {res.stderr[-300:]}")
    return counts, res.stdout + res.stderr


# -- 5. what merged is what was reviewed ---------------------------------------------------------------------

def assert_merged_equals_reviewed(repo: Path, reviewed: str, files: list[str]) -> str:
    sh(["git", "fetch", "-q", "origin"], cwd=repo)
    main = sh(["git", "rev-parse", "origin/main"], cwd=repo).strip()
    diff = sh(["git", "diff", "--stat", reviewed, main, "--"] + files, cwd=repo)
    if diff.strip():
        raise Refused(f"MERGED main {main[:9]} differs from the reviewed head {reviewed[:9]} on the PR's files:\n{diff}")
    return f"merged main {main[:9]} == reviewed head {reviewed[:9]} on {len(files)} file(s)"


def pr_files(pr: str, repo: Path) -> list[str]:
    out = sh(["gh", "pr", "view", pr, "--json", "files", "-q", ".files[].path"], cwd=repo)
    return [line.strip() for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr")
    ap.add_argument("--merge", action="store_true", help="merge after a green gate (default: gate only)")
    ap.add_argument("--suite-cmd", default=DEFAULT_SUITE)
    ap.add_argument("--no-comment", action="store_true", help="do not post the evidence on the PR")
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--skip-pin", action="store_true",
                    help="do NOT run the suite against the pyproject pin (the evidence line says so)")
    ap.add_argument("--pin-root", default="/tmp", help="where throwaway pin checkouts live (ks-<sha>)")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    try:
        head, _ = assert_head_is_pushed(repo)
        remote_head = pr_head(a.pr, repo)
        assert_pr_head_is_tested(head, remote_head)
        print(f"1. head {head[:9]} == upstream (pushed) == PR {a.pr} head")
        # THE INTERPRETER RUNNING THE GATE resolves kumo_strategies — not a venv path guessed from this
        # file's location: a worktree has no .venv of its own and the guess died with a traceback (05:05Z).
        resolved = resolved_strategies_rev(Path(sys.executable), cwd=BACKEND)
        pin = pin_statement(resolved, pinned_strategies_rev(BACKEND / "pyproject.toml"))
        pin = f"{pin}; tree {publication_state(resolved_strategies_tree(Path(sys.executable)), resolved)}"
        print(f"2. {pin}")
        # THE SUITE'S MEANING DEPENDS ON WHERE IT RAN (coordinator, 05:03Z): test_no_orphan_mechanisms globs
        # `api/ scripts/ strategies/` relative to CWD, so from the repo root it finds nothing and its vacuity
        # guards read as "main is red". The gate runs from backend/ ALWAYS and prints that beside the counts.
        counts, _ = run_suite(a.suite_cmd, BACKEND)
        summary = gate_summary(counts)
        print(f"3. suite GREEN from {BACKEND} against the editable tree {resolved}: {summary}")
        pin_sha = pin_counts_summary = finding = None
        if not a.skip_pin:
            pin_sha, pin_counts, _ = pin_run(Path(sys.executable), BACKEND / "pyproject.toml", a.suite_cmd,
                                             BACKEND, root=Path(a.pin_root))
            summary, pin_counts_summary = gate_both(counts, pin_counts)
            finding = compare_runs(counts, pin_counts)
            print(f"3b. suite GREEN against the PIN {pin_sha}: {pin_counts_summary}"
                  + (f"\n    {finding}" if finding else ""))
        else:
            print("3b. PIN NOT TESTED (--skip-pin)")
        evidence = evidence_line(head=head, pin=pin, editable=summary, pin_sha=pin_sha,
                                 pin_counts=pin_counts_summary, finding=finding, merge=a.merge)
        print(f"4. {evidence}")
        if not a.no_comment:
            sh(["gh", "pr", "comment", a.pr, "--body", evidence], cwd=repo)
        if a.merge:
            print(f"4b. head {assert_head_unmoved(repo, head, remote_head=pr_head(a.pr, repo))[:9]} unmoved since the suite ran")
            files = pr_files(a.pr, repo)
            # NO --delete-branch: gh then checks out main LOCALLY, which fails in any worktree layout
            # where another worktree holds main — and the remote merge has already happened by then, so
            # the CLI's exit code lies about what occurred (05:07Z, first dogfood). The merge is verified
            # by the PR's STATE, and the remote branch is deleted separately, best-effort.
            sh(merge_command(a.pr, head), cwd=repo, must_pass=False)
            state = sh(["gh", "pr", "view", a.pr, "--json", "state", "-q", ".state"], cwd=repo).strip()
            if state != "MERGED":
                raise Refused(f"PR {a.pr} is {state or 'unknown'} after the merge command — not merged; nothing to verify")
            print(f"5. {assert_merged_equals_reviewed(repo, head, files)}")
            branch = sh(["gh", "pr", "view", a.pr, "--json", "headRefName", "-q", ".headRefName"], cwd=repo).strip()
            if branch:
                sh(["git", "push", "-q", "origin", "--delete", branch], cwd=repo, must_pass=False)
        print("MERGE GATE PASSED" + (" AND MERGED" if a.merge else ""))
        return 0
    except Refused as exc:
        print(f"REFUSED: {exc.why}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — never a traceback where a sentence is owed, never a success string
        print(f"REFUSED: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
