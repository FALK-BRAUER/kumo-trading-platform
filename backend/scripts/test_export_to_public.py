"""`bin/export-to-public.sh` — the one-way private→public snapshot, proven on throwaway repos (#1048).

Every test builds a PRIVATE repo (with `.public-exclude`, a `bin/check-public-tree.sh` copy, tracked
files) and a PUBLIC clone (with scaffold-owned files and an origin to push to — a bare repo), runs the
export with `--no-push`, and asserts on the PUBLIC tree. Real git throughout: the script archives a
ref, the check reads `git ls-files`, and the commit carries the provenance.

What must be true, one test each:
- excluded paths are absent from the public tree;
- files the public scaffold owns keep the PUBLIC content;
- every other file carries the PRIVATE content, and a file the private tree dropped is deleted;
- the commit body names the private sha, and the second export names the first;
- a hit in the export set refuses BEFORE anything touches the public clone;
- a hit that only exists in a kept public file refuses AFTER sync, before the commit;
- a dirty public clone, a missing denylist, a ref without `.public-exclude` each refuse by name;
- exporting the same ref twice is a no-op, not a second commit.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "bin" / "export-to-public.sh"
CHECK = ROOT / "bin" / "check-public-tree.sh"


def git(repo: Path, *args: str) -> str:
    # `cwd=`, not `-C`: the #962 guard reads `-C <path>` as "retargets to another repository" and
    # `cwd=` as fixture-scoped. Every call here targets a throwaway repo built by the fixture.
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT).strip()


def commit_all(repo: Path, msg: str = "c") -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", msg, "--allow-empty")
    return git(repo, "rev-parse", "@")


def write(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


@pytest.fixture
def private(tmp_path: Path) -> Path:
    r = tmp_path / "private"
    r.mkdir()
    git(r, "init", "-q")
    (r / "bin").mkdir()
    (r / "bin" / "check-public-tree.sh").write_text(CHECK.read_text())
    os.chmod(r / "bin" / "check-public-tree.sh", 0o755)
    write(r, ".public-exclude", "# private\nCLAUDE.md\nzz_handoffs/\nsecret-notes.md\n.public-exclude\n")
    write(r, "CLAUDE.md", "private manual\n")
    write(r, "zz_handoffs/h.md", "handoff\n")
    write(r, "secret-notes.md", "notes\n")
    write(r, "backend/api/app.py", "VERSION = 2\n")
    write(r, "backend/api/old.py", "will be deleted later\n")
    write(r, "README.md", "PRIVATE readme\n")
    write(r, "docs/engineering-principles.md", "rules\n")
    commit_all(r, "private tree")
    return r


@pytest.fixture
def public(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = tmp_path / "public"
    subprocess.run(["git", "clone", "-q", str(bare), str(r)], check=True, stderr=subprocess.DEVNULL)
    git(r, "checkout", "-q", "-b", "main")
    write(r, "README.md", "PUBLIC readme\n")
    write(r, "LICENSE", "LGPL\n")
    write(r, "backend/pyproject.toml", "[project]\nname='public'\n")
    # The real scaffold carries every default keep entry; this minimal one declares what it has —
    # a keep entry naming nothing is refused (P7), which its own test below exercises deliberately.
    write(r, ".export-keep", "README.md\nLICENSE\nbackend/pyproject.toml\n")
    write(r, "backend/api/app.py", "VERSION = 1\n")
    write(r, "backend/api/old.py", "will be deleted later\n")
    commit_all(r, "scaffold")
    git(r, "push", "-q", "-u", "origin", "main")
    return r


@pytest.fixture
def denylist(tmp_path: Path) -> Path:
    d = tmp_path / "deny.txt"
    d.write_text("someLogin42\n")
    return d


def run(private: Path, public: Path, ref: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # The ref handed to the script is the fixture repo's CURRENT commit as an immutable sha, resolved
    # through the fixture-scoped helper — never a moving name (#962).
    sha = ref or git(private, "rev-parse", "--verify", "@")
    e = {k: v for k, v in os.environ.items() if k != "KUMO_PUBLIC_DENYLIST"}
    e.update(env or {})
    return subprocess.run(["bash", str(EXPORT), sha, str(public)], cwd=private, capture_output=True, text=True, env=e)


def test_excluded_paths_do_not_reach_the_public_tree(private: Path, public: Path, denylist: Path) -> None:
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 0, r.stderr
    tracked = git(public, "ls-files").split("\n")
    for gone in ("CLAUDE.md", "zz_handoffs/h.md", "secret-notes.md", ".public-exclude"):
        assert gone not in tracked, gone
    assert "docs/engineering-principles.md" in tracked


def test_scaffold_owned_files_keep_the_public_content(private: Path, public: Path, denylist: Path) -> None:
    run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert (public / "README.md").read_text() == "PUBLIC readme\n"
    assert (public / "LICENSE").read_text() == "LGPL\n"
    assert "public" in (public / "backend/pyproject.toml").read_text()


def test_everything_else_is_the_private_content_and_dropped_files_are_deleted(private: Path, public: Path, denylist: Path) -> None:
    (private / "backend/api/old.py").unlink()
    commit_all(private, "drop old")
    run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert (public / "backend/api/app.py").read_text() == "VERSION = 2\n"
    assert not (public / "backend/api/old.py").exists()
    assert "backend/api/old.py" not in git(public, "ls-files")


def test_the_commit_carries_provenance_and_the_second_export_names_the_first(private: Path, public: Path, denylist: Path) -> None:
    first = git(private, "rev-parse", "@")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 0, r.stderr
    body = git(public, "log", "-1", "--format=%B")
    assert first in body and "first export" in body
    assert git(public, "branch", "--show-current") == f"export/{first[:7]}"
    assert (public / ".public-last-export").read_text().strip() == first
    # a second export, of a new private commit, names the first as previous
    write(private, "backend/api/app.py", "VERSION = 3\n")
    second = commit_all(private, "v3")
    # the public clone must be clean and on main for the next export; simulate the squash-merge
    git(public, "checkout", "-q", "main")
    git(public, "merge", "-q", "--squash", f"export/{first[:7]}")
    commit_all(public, "squash first export")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 0, r.stderr
    body = git(public, "log", "-1", "--format=%B")
    assert second in body and f"previous export:  {first}" in body


def test_exporting_the_same_ref_twice_is_a_no_op(private: Path, public: Path, denylist: Path) -> None:
    first = git(private, "rev-parse", "@")
    run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    # the export is squash-merged by a person; the next export starts from main (S1)
    git(public, "checkout", "-q", "main")
    git(public, "merge", "-q", "--squash", f"export/{first[:7]}")
    commit_all(public, "squash")
    n = git(public, "rev-list", "--count", "@")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 0
    assert "nothing to export" in r.stdout
    assert git(public, "rev-list", "--count", "@") == n


def test_a_hit_in_the_export_set_refuses_before_the_public_clone_is_touched(private: Path, public: Path, denylist: Path) -> None:
    write(private, "backend/api/cfg.py", 'LOGIN = "someLogin42"\n')
    commit_all(private, "leak")
    before = git(public, "rev-parse", "@")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "export set fails check-public-tree" in r.stderr
    assert "backend/api/cfg.py:1" in r.stderr
    assert git(public, "rev-parse", "@") == before
    assert git(public, "status", "--porcelain") == ""          # not even a working-tree change
    assert not (public / "backend/api/cfg.py").exists()


def test_a_hit_that_lives_only_in_a_kept_public_file_refuses_after_sync(private: Path, public: Path, denylist: Path) -> None:
    # The private export set is clean; the PUBLIC scaffold's own README carries the term. The second
    # check exists for exactly this: the kept files are outside the private check's view.
    write(public, "README.md", "PUBLIC readme by someLogin42\n")
    commit_all(public, "scaffold with a term")
    before = git(public, "rev-parse", "@")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "public tree fails check-public-tree after sync" in r.stderr
    assert git(public, "rev-parse", "@") == before


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("dirty", "local changes"),
        ("no-denylist", "KUMO_PUBLIC_DENYLIST is not set"),
        ("no-exclude", "has no .public-exclude"),
    ],
)
def test_preconditions_refuse_by_name(private: Path, public: Path, denylist: Path, setup: str, message: str) -> None:
    env = {"KUMO_PUBLIC_DENYLIST": str(denylist)}
    if setup == "dirty":
        write(public, "scratch.txt", "uncommitted\n")
    if setup == "no-denylist":
        env = {}
    if setup == "no-exclude":
        (private / ".public-exclude").unlink()
        commit_all(private, "no exclude list")
    r = run(private, public, env=env)
    assert r.returncode == 1
    assert message in r.stderr


def test_a_public_clone_carrying_its_own_public_exclude_is_refused(private: Path, public: Path, denylist: Path) -> None:
    # A kept `.public-exclude` in the public clone would make the SECOND check skip whatever it names
    # — a term inside a kept directory named there would ship (peer review of #1083, M1).
    write(public, ".public-exclude", "README.md\n")
    commit_all(public, "sneaky")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "carries a .public-exclude" in r.stderr


def test_a_keep_entry_that_names_nothing_in_the_public_clone_is_refused(private: Path, public: Path, denylist: Path) -> None:
    # A keep entry for a path the public clone does not have excludes that path from the sync without
    # anyone having listed it in .public-exclude — an exclusion in disguise (P7).
    write(public, ".export-keep", "README.md\nLICENSE\nbackend/pyproject.toml\ndocs/engineering-principles.md\n")
    commit_all(public, "keep list")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "exclusion in disguise" in r.stderr
    assert "docs/engineering-principles.md" in r.stderr


def test_a_tracked_symlink_in_the_private_ref_is_refused_before_the_archive(private: Path, public: Path, denylist: Path) -> None:
    # assembled, not written: this file is tracked in the tree the check scans (see the check's tests)
    os.symlink("/Users/" + "someone" + "/x", private / "docs" / "link")
    commit_all(private, "link")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "tracks symlinks" in r.stderr


def test_export_ignore_attributes_are_refused_as_an_undeclared_exclusion_list(private: Path, public: Path, denylist: Path) -> None:
    write(private, ".gitattributes", "docs/engineering-principles.md export-ignore\n")
    commit_all(private, "attr")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "export-ignore" in r.stderr
    assert "docs/engineering-principles.md" in r.stderr


def test_the_export_branches_from_main_not_from_the_previous_export(private: Path, public: Path, denylist: Path) -> None:
    run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert git(public, "branch", "--show-current").startswith("export/")
    # a second export attempted while the clone still sits on the first export branch is refused
    write(private, "backend/api/app.py", "VERSION = 9\n")
    commit_all(private, "v9")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1
    assert "is not on main" in r.stderr


def test_a_refusal_after_sync_leaves_the_clone_as_it_was_found(private: Path, public: Path, denylist: Path) -> None:
    write(public, "README.md", "PUBLIC readme by someLogin42\n")
    before = commit_all(public, "scaffold with a term")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 1 and "clone restored" in r.stderr
    assert git(public, "status", "--porcelain") == ""
    assert git(public, "rev-parse", "@") == before
    assert not (public / "docs" / "engineering-principles.md").exists()


def test_ignored_files_in_the_public_clone_survive_the_export(private: Path, public: Path, denylist: Path) -> None:
    # node_modules is not the export's to delete (P5): `--delete` with the clone's .gitignore as a filter.
    write(public, ".gitignore", "node_modules/\n")
    commit_all(public, "ignore")
    write(public, "node_modules/x.js", "installed\n")
    r = run(private, public, env={"KUMO_PUBLIC_DENYLIST": str(denylist)})
    assert r.returncode == 0, r.stderr
    assert (public / "node_modules" / "x.js").exists()
