"""#962 — NO TEST MAY READ A MOVING GIT REF OF THE REAL REPOSITORY.

`test_qc345_build_config_resolves_forward_refs.py` read the OLD builder via `git show origin/main:…`
to prove old=dict, new=dataclass. Correct for exactly as long as the fix was unmerged; the moment
#949 landed, `origin/main` carried the fix, "old" became "new", and two tests were red on main —
while the merge gate, which ran the suite on the PR branch where `origin/main` was still pre-fix,
had passed it green. A fixture anchored on a moving ref is INVISIBLE TO THE GATE BY CONSTRUCTION:
the branch is not the tree that will exist after the merge.

The guard is at the VALUE, not a list of files: every `git` invocation in every test file under
backend/ is classified off the AST. A call that passes `cwd=` targets a repository the test built
itself (a fixture under tmp_path — `scripts/test_merge_gate.py`, `api/test_deploy_precheck_gates.py`
do exactly this, and `HEAD`/`origin/main` THERE are the subject under test, not an anchor). A call
without `cwd=` runs against THE REAL REPOSITORY, and its arguments may name only immutable
revisions (≥7 hex) or no revision at all (`rev-parse --show-toplevel`). Helpers that wrap git are
classified by the wrapped call, so `_git(path, "rev-parse", "HEAD")` is fixture-scoped because
`_git` passes `cwd=`.
"""
from __future__ import annotations

import ast
import pathlib
import re

_BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: A revision an operator could type that MOVES. `refs/…` and `@{u}` included; a bare branch name
#: like `main` counts; `<sha>:<path>` with ≥7 hex is immutable and allowed.
_MOVING = re.compile(r"^(origin/\S+|HEAD(~\d*|\^\d*|@\{[^}]*\})?|@\{u\}|main|master|refs/\S+|FETCH_HEAD|ORIG_HEAD)(:.*)?$")
_IMMUTABLE = re.compile(r"^[0-9a-f]{7,40}(:.*)?$")
#: A git command in STRING form (`shell=True`, `shlex.split`, concatenation) naming a moving ref.
_STRING_CMD = re.compile(r"\bgit\s+\S+.*?(^|\s)(origin/\S+|HEAD\b\S*|@\{u\}|main\b|master\b|refs/\S+)")
#: A bare string that can only be a git object address of a moving ref — `origin/main`, `HEAD~2`,
#: `origin/main:backend/x.py`. Bare `main`/`master` are NOT here (they are also function names and
#: branch labels in fixtures); those count only inside a git list or a git command string.
#: A git READ command in string form that names NO immutable sha at all — `"git show " + ref` with
#: the ref built elsewhere. A string-form read in a test is suspect unless a sha is in the string.
_GIT_STRING_NO_SHA = re.compile(r"^\s*git\s+(show|rev-parse|log|diff|cat-file|ls-tree|merge-base|describe)\b(?!.*\b[0-9a-f]{7,40}\b)")
#: Git READ subcommands that default to HEAD when given no revision — `["git", "log", "-1"]` is a
#: moving-ref read with the ref left implicit (#979 review). Without `cwd=` such a list must carry an
#: immutable revision. The allow-set names the arguments that make the command revision-free.
_IMPLICIT_HEAD_CMDS = {"show", "log", "diff", "describe", "rev-parse", "merge-base", "cat-file", "ls-tree",
                       "branch", "symbolic-ref", "status", "rev-list", "name-rev", "ls-files"}
_NO_REVISION_ARGS = {"--show-toplevel", "--git-dir", "--is-inside-work-tree", "--show-prefix", "--absolute-git-dir"}
_REVISION_FREE_CMDS = {"config", "version", "init", "clone", "remote", "fetch", "push", "add", "commit",
                       "worktree", "update-ref", "checkout", "switch", "stash", "apply", "tag"}
#: The same anchor through the FILESYSTEM: reading `.git/HEAD` or `.git/refs/…` is a moving-ref read
#: with no git process to classify (#979 review).
_REF_FILE = re.compile(r"\.git/(HEAD|refs/|packed-refs|ORIG_HEAD|FETCH_HEAD|logs/)")
_BARE_REF = re.compile(r"^(origin/\S+|HEAD(~\d*|\^\d*|@\{[^}]*\})?|@\{u\}|refs/\S+|FETCH_HEAD|ORIG_HEAD)(:.*)?$")


def _retargets(lst: ast.List) -> bool:
    """`git -C <path>`, `--git-dir`, `--work-tree` point the command at ANOTHER repository than
    `cwd=` — a fixture cwd with `-C /real/repo` reads the real one. Never fixture-scoped."""
    return any(isinstance(e, ast.Constant) and isinstance(e.value, str)
               and (e.value == "-C" or e.value.startswith(("--git-dir", "--work-tree"))) for e in lst.elts)


#: Module tree for resolving `GIT = "git"` constants used as argv[0]; set per file by `_real_repo_git_refs`.
_TREE: list[ast.Module] = []


def _argv0(node: ast.AST) -> str | None:
    """argv[0] of a list/tuple literal, resolving a Name through the module's constants and a
    `["git"] + [...]` concatenation through its left operand. None when it is not a sequence."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _argv0(node.left)
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    head = node.elts[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return head.value
    if isinstance(head, ast.Name) and _TREE:
        return _module_constant(_TREE[0], head.id)
    return None


def _is_git_list(node: ast.AST) -> bool:
    """A git INVOCATION, not one spelling of one (#967): list OR tuple, argv[0] `git` or a path ending
    in `/git`, a module constant naming it, or a `["git"] + [...]` concatenation."""
    head = _argv0(node)
    return head is not None and (head == "git" or head.endswith("/git"))


def _real_repo_git_refs(source: str) -> list[tuple[int, str]]:
    """(lineno, argument) for every argument of a git call WITHOUT `cwd=` that names a MOVING ref.
    Calls with `cwd=` target a fixture repository and are not the hazard."""
    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    # EVERY git list literal in the file, not only those sitting inside a call: `cmd = ["git", ...];
    # subprocess.run(cmd)` must not slip past by being built one statement earlier. A list is
    # fixture-scoped only when it is DIRECTLY an argument of a call that passes `cwd=`; a list bound
    # to a name is checked regardless (false positive is the safe direction — inline it).
    parent: dict[ast.AST, ast.AST] = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    _TREE[:] = [tree]
    for lst in ast.walk(tree):
        if not _is_git_list(lst):
            continue
        if isinstance(lst, ast.BinOp):
            # `["git", "show"] + [ref]`: the pieces are the elements of both sides, if literal
            lst = ast.List(elts=[e for side in (lst.left, lst.right)
                                 if isinstance(side, (ast.List, ast.Tuple)) for e in side.elts], lineno=lst.lineno)
        call = parent.get(lst)
        if (isinstance(call, ast.Call) and lst in call.args and any(k.arg == "cwd" for k in call.keywords)
                and not _retargets(lst)):
            continue
        if True:
            words = []
            for el in lst.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    words.append(el.value)
                    if _MOVING.match(el.value):
                        hits.append((lst.lineno, el.value))
                elif isinstance(el, ast.Name):
                    # `B = "main"; run(["git", "rev-parse", B])` — a Name is judged INSIDE the list context
                    bound = _module_constant(tree, el.id)
                    if bound is None or _MOVING.match(bound):
                        hits.append((lst.lineno, f"{{{el.id}}}={bound!r}"))
                    else:
                        words.append(bound)
                elif isinstance(el, ast.JoinedStr):
                    folded = _fold_joined(el, tree)     # f"{REV}:path" with REV a module constant
                    if folded is not None:
                        words.append(folded)
            # IMPLICIT HEAD: a read subcommand with NO revision spelled at all reads HEAD by default.
            # (A moving revision that IS spelled is reported above as itself, not twice.)
            sub = next((w for w in words[1:] if not w.startswith("-")), None)
            spelled = any(_IMMUTABLE.match(w) or _MOVING.match(w) for w in words[2:])
            if (sub in _IMPLICIT_HEAD_CMDS and not spelled
                    and not any(w in _NO_REVISION_ARGS for w in words)):
                hits.append((lst.lineno, f"git {sub} (implicit HEAD, no revision spelled)"))
                if isinstance(el, ast.JoinedStr):  # f"{rev}:path" — the rev must be an immutable literal
                    for v in el.values:
                        if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
                            bound = _module_constant(tree, v.value.id)
                            if bound is None or not _IMMUTABLE.match(bound):
                                hits.append((lst.lineno, f"{{{v.value.id}}}={bound!r}"))
    # EVERY OTHER string constant: a git command in string form (shell=True / shlex.split /
    # concatenation) or a bare object address of a moving ref. Exempt: docstrings; elements of git
    # lists (handled above); arguments of a call that passes `cwd=`; arguments of a call to a LOCAL
    # helper that wraps git with `cwd=` (`_git(path, "rev-parse", "HEAD")`). Positional cwd is not
    # recognised — a call must say `cwd=` by keyword to be fixture-scoped (false positive is safe).
    helpers = _fixture_helpers(tree)
    exempt: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            exempt.add(node.value)                                    # docstring
        if _is_git_list(node) and isinstance(node, (ast.List, ast.Tuple)):
            exempt.update(node.elts)
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else None
            if any(k.arg == "cwd" for k in node.keywords) or callee in helpers:
                for a in node.args:
                    exempt.update(ast.walk(a))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in exempt:
            if (_STRING_CMD.search(node.value) or _BARE_REF.match(node.value.strip())
                    or _GIT_STRING_NO_SHA.match(node.value) or _REF_FILE.search(node.value)):
                hits.append((node.lineno, node.value))
        # `"origin/" + "main"`, `"git show " + ref`: fold constant concatenations and judge the result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            folded = _fold(node, tree)
            if folded is not None and (_STRING_CMD.search(folded) or _BARE_REF.match(folded.strip()) or _GIT_STRING_NO_SHA.match(folded)):
                hits.append((node.lineno, folded))
    return sorted(set(hits))


def _fold_joined(node: ast.JoinedStr, tree: ast.Module) -> str | None:
    """An f-string whose formatted values are module string constants, folded; else None."""
    out = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.append(v.value)
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
            bound = _module_constant(tree, v.value.id)
            if bound is None:
                return None
            out.append(bound)
        else:
            return None
    return "".join(out)


def _fold(node: ast.AST, tree: ast.Module) -> str | None:
    """A string built from constants and module-level string constants, or None if any piece is not."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return _module_constant(tree, node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, r = _fold(node.left, tree), _fold(node.right, tree)
        return None if l is None or r is None else l + r
    return None


def _fixture_helpers(tree: ast.Module) -> set[str]:
    """Local functions whose body runs a git list with `cwd=` — their callers are fixture-scoped."""
    out = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and any(_is_git_list(a) for a in call.args)
                        and any(k.arg == "cwd" for k in call.keywords)):
                    out.add(fn.name)
    return out


def _git_call_counts(source: str) -> tuple[int, int]:
    """(git calls WITHOUT cwd = real repository, git calls WITH cwd = fixture repository)."""
    real = fixture = 0
    for call in ast.walk(ast.parse(source)):
        if isinstance(call, ast.Call) and any(_is_git_list(a) for a in call.args):
            if any(k.arg == "cwd" for k in call.keywords):
                fixture += 1
            else:
                real += 1
    return real, fixture


def _module_constant(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
            if isinstance(node.value, ast.BinOp):
                return _fold(node.value, tree)
    return None


def _test_files() -> list[pathlib.Path]:
    return sorted(p for p in _BACKEND.rglob("test_*.py") if ".venv" not in p.parts)


# -- the classifier itself, before it is trusted with the tree -------------------------------------------

def test_FIXTURE_the_classifier_flags_a_real_repo_read_of_origin_main_and_HEAD():
    """The exact shape that was red on main, plus its siblings; a guard that cannot see its subject is
    the #962 defect one level out."""
    src = 'import subprocess\nsubprocess.run(["git", "show", "origin/main:backend/x.py"], capture_output=True)\n'
    assert _real_repo_git_refs(src) == [(2, "origin/main:backend/x.py")]
    for ref in ("HEAD", "HEAD~1", "main", "@{u}", "refs/heads/main", "origin/main"):
        assert _real_repo_git_refs(f'import subprocess\nsubprocess.run(["git", "rev-parse", "{ref}"])\n'), ref
    # f-string with a module constant: immutable passes, moving fails, unbound fails
    assert _real_repo_git_refs('import subprocess\nR = "aeed851"\nsubprocess.run(["git", "show", f"{R}:b/x.py"])\n') == []
    assert _real_repo_git_refs('import subprocess\nR = "origin/main"\nsubprocess.run(["git", "show", f"{R}:b/x.py"])\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "show", f"{R}:b/x.py"])\n')
    # string form: shell=True, shlex.split, concatenation — the list rule never sees these
    assert _real_repo_git_refs('import subprocess\nsubprocess.run("git show origin/main:b/x.py", shell=True)\n')
    assert _real_repo_git_refs('import shlex, subprocess\nsubprocess.run(shlex.split("git rev-parse HEAD"))\n')
    assert _real_repo_git_refs('import subprocess\nref = "origin/" + "main"\nsubprocess.run("git show " + ref, shell=True)\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "show", "origin/main:b/x.py".split()[0]])\n')
    # -C / --git-dir under a fixture cwd retargets the command at the real repository
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "-C", "/r", "show", "origin/main:b/x.py"], cwd=tmp)\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "--git-dir=/r/.git", "rev-parse", "HEAD"], cwd=tmp)\n')
    # a no-cwd helper with the ref at the call site
    assert _real_repo_git_refs('import subprocess\ndef _g(*a):\n    return subprocess.run(["git", *a])\n_g("show", "origin/main:b/x.py")\n')
    # #979 review: implicit-HEAD list reads (no revision spelled, so HEAD is the revision)
    for cmd in ('["git", "log", "-1", "--format=%H"]', '["git", "show", "-s", "--format=%H"]', '["git", "describe", "--always"]',
                '["git", "branch", "--show-current"]', '["git", "diff", "--stat"]', '["git", "rev-parse", "--abbrev-ref"]'):
        assert _real_repo_git_refs(f'import subprocess\nsubprocess.run({cmd})\n'), cmd
    # …but revision-free commands and immutable revisions pass
    for cmd in ('["git", "rev-parse", "--show-toplevel"]', '["git", "rev-parse", "--git-dir"]', '["git", "config", "user.name"]',
                '["git", "version"]', '["git", "show", "aeed851:backend/x.py"]', '["git", "log", "-1", "aeed851"]'):
        assert _real_repo_git_refs(f'import subprocess\nsubprocess.run({cmd})\n') == [], cmd
    # ref FILE reads — the same anchor through the filesystem, no git process
    assert _real_repo_git_refs('import pathlib\nx = pathlib.Path(".git/refs/remotes/origin/main").read_text()\n')
    assert _real_repo_git_refs('x = open(".git/HEAD").read()\n')
    # a Name element bound to a moving ref, judged inside the list
    assert _real_repo_git_refs('import subprocess\nB = "main"\nsubprocess.run(["git", "rev-parse", B])\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "rev-parse", UNBOUND])\n')
    assert _real_repo_git_refs('import subprocess\nR = "aeed851"\nsubprocess.run(["git", "rev-parse", R])\n') == []
    # OUT OF SCOPE, stated: pieces joined at runtime — `"".join(["origin/", "main"])` — are not folded.
    # positional cwd is NOT recognised as fixture scope — only the keyword is
    assert _real_repo_git_refs('import subprocess\nsubprocess.Popen(["git", "rev-parse", "HEAD"], 0, None, None, None, None, None, True, False, work)\n')
    # #967's table: a TUPLE argv, a `GIT` module constant as argv[0], a list concatenation, a /usr/bin/git path
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(("git", "show", "origin/main:b/x.py"))\n')
    assert _real_repo_git_refs('import subprocess\nGIT = "git"\nsubprocess.run([GIT, "show", "origin/main:b/x.py"])\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git"] + ["show", "origin/main:b/x.py"])\n')
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["/usr/bin/git", "rev-parse", "HEAD"])\n')
    # built one statement earlier and run without cwd — the evasion a reviewer named
    assert _real_repo_git_refs('import subprocess\ncmd = ["git", "show", "origin/main:b/x.py"]\nsubprocess.run(cmd)\n') == [(2, "origin/main:b/x.py")]


def test_FIXTURE_the_classifier_allows_fixture_repos_and_immutable_revisions():
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "rev-parse", "HEAD"], cwd=work)\n') == []
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "show", "aeed851:backend/x.py"])\n') == []
    assert _real_repo_git_refs('import subprocess\nsubprocess.run(["git", "rev-parse", "--show-toplevel"])\n') == []
    # a local helper that wraps git with cwd= makes its callers fixture-scoped
    assert _real_repo_git_refs('import subprocess\ndef _git(cwd, *a):\n    return subprocess.run(["git", *a], cwd=cwd)\n_git(p, "update-ref", "refs/remotes/origin/main", _git(p, "rev-parse", "HEAD"))\n') == []
    # a docstring or a non-git string that merely mentions main is not a read
    assert _real_repo_git_refs('def f():\n    """reads origin/main? no — prose"""\n    return _src("main")\n') == []


# -- the tree ----------------------------------------------------------------------------------------------

def test_FIXTURE_the_scan_sees_both_kinds_of_git_reading_test():
    """Non-empty is not complete, but empty is blind: the scan must find the real-repo reader that
    started this (qc345) and at least one fixture-repo reader, or it is looking in the wrong place."""
    real, fixture = [], []
    for p in _test_files():
        n_real, n_fixture = _git_call_counts(p.read_text())
        (real if n_real else []).append(p.name)
        (fixture if n_fixture else []).append(p.name)
    assert "test_qc345_build_config_resolves_forward_refs.py" in real, real
    assert fixture, "no fixture-repo git test found — the scan root is wrong"


def test_no_test_under_backend_reads_a_MOVING_git_ref_of_the_real_repository():
    offenders = {}
    for p in _test_files():
        if p.name == pathlib.Path(__file__).name:
            continue
        hits = _real_repo_git_refs(p.read_text())
        if hits:
            offenders[str(p.relative_to(_BACKEND))] = hits
    assert not offenders, (
        "a test anchors on a MOVING git ref of the real repository — pin an immutable sha and assert "
        f"the anchor's property first (#962): {offenders}")
