"""What this INSTALLED cockpit actually is, computed from its own bytes.

WHY COCKPIT NEEDS ITS OWN, when `kumo_strategies.provenance` already exists: because only ONE HALF of
what runs here can currently prove itself. Read out of the live container on 2026-08-23:

    env    KUMO_GIT_SHA         = e94a463            a CLAIM, stamped at build time
    env    KUMO_STRATEGIES_SHA  = f7b8ec5            a CLAIM
    label  com.kumo.strategies.revision = f7b8ec5    the same claim, twice
    call   kumo_strategies.digest() = a977ae4…       a FACT, hashed from installed bytes

Strategies can say what it is. Cockpit can only say what someone told it at build time.

THAT ASYMMETRY IS NOT COSMETIC — IT DEFEATS THE DEPLOY GATE. #486 compares a PLANNED ref against what
the containers ACTUALLY run. A label comparison passes trivially for a wrong-tree build, because the
image is stamped with the SHA of the tree it was WRONGLY BUILT FROM. On 2026-08-22 exactly that
happened: `98d264c` shipped when `80f6571` was intended, and the build log was byte-identical to a
correct one. A claim cannot catch a build that lies about itself; only content can.

`-dirty` is the same failure in miniature. `68201a1-dirty` reached the paper engine and told us the
tree was dirty — never WHAT was in it. Establishing the delta was harmless required diffing two
commits by hand.

MIRRORS `kumo_strategies.provenance` DELIBERATELY, including its three hard-won corrections, because
two implementations of one measurement that differ in their rules are two measurements:

  EVERY SHIPPED FILE, not `*.py`. A wheel is not only its modules. Their first version hashed `*.py`
  alone while fifteen non-`.py` files shipped, so a universe list or a pinned CSV could differ between
  builds and be called identical — "a provenance tool that is silently partial is worse than none,
  because it turns 'we do not know' into 'we checked'."

  ENUMERATE ONCE. `digest_tree` and `files` share `_source_files`. Theirs did not, and a mutation
  narrowing `rglob` to `glob` hashed two files out of eighty-two while the count still reported
  eighty-two — every guard green, because THE COUNT IS WHAT MAKES A DIGEST LOOK TRUSTWORTHY.

  REFUSE ZERO. `sha256()` of an empty walk is a stable, plausible hash that every empty tree shares.
  A worktree that failed to materialise produced a convincing MISMATCH rather than reporting a broken
  measurement — a comparison that cannot tell "different code" from "no code" is the failure this
  module exists to remove.

WHAT IT DOES NOT SEE, stated so nobody assumes otherwise:

  `__pycache__`      excluded ON PURPOSE. Bytecode is written on first import, so counting it would
                     make a container's digest change simply by having been USED, and two identical
                     deployments would disagree.
  install location   invisible ON PURPOSE. Paths are relative to the package root, so a container, a
                     venv and a working tree agree — the property the whole comparison depends on.
  dependencies       INVISIBLE, and this is the real limit. The digest covers `api` and nothing else.
                     Two images with identical digests can run different `nautilus_trader` builds and
                     behave differently. It answers "is this my code", never "is this the same
                     environment". `pip freeze` inside the container is that observation and belongs
                     BESIDE this number, not folded into it: one number meaning two things cannot say
                     which one changed.

    docker exec <api> python -c "from api.provenance import digest; print(digest())"

kumo-cockpit#486, step 2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Never hashed. Nothing timestamp-derived, nothing written at runtime — see the module docstring.
_IGNORED_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache", ".ruff_cache"})


def _source_files(root: Path) -> list[Path]:
    """The files that constitute this package, enumerated ONCE.

    `digest_tree` and `files` must never be able to describe different sets.
    """
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and not _IGNORED_DIRS & set(p.relative_to(root).parts)
    )


def digest_tree(root: Path) -> str:
    """A stable content hash of every shipped file under `root`.

    Both the path and the bytes go into the hash: a file RENAMED with identical contents is a
    different package, and a rename is how a module stops being imported.
    """
    paths = _source_files(root)
    if not paths:
        raise ValueError(
            f"no files under {root} — refusing to return a digest of nothing, which is a "
            f"valid-looking hash that every empty tree shares"
        )
    h = hashlib.sha256()
    for path in paths:
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def digest() -> str:
    """The content hash of THIS installed cockpit, wherever it is running from."""
    return digest_tree(Path(__file__).resolve().parent)


def files() -> int:
    """How many files were hashed. Reported alongside the digest rather than left implicit."""
    return len(_source_files(Path(__file__).resolve().parent))
