"""The cockpit digest must be a measurement, not a plausible number.

Three of these exist because kumo-trading-strategies hit them first and told us. Inheriting their fixes is
cheap; rediscovering them in a deploy gate is not — two of the three would have failed every deploy
under #486 on its first run.
"""

from __future__ import annotations

import pytest

from api.provenance import digest, digest_tree, files


def test_a_digest_of_nothing_is_REFUSED_not_returned(tmp_path):
    """`sha256()` of an empty walk is stable, plausible, and shared by EVERY empty tree.

    kumo-trading-strategies hit this: a worktree that failed to materialise produced a convincing MISMATCH
    rather than reporting a broken measurement. A comparison that cannot tell "different code" from
    "no code" is the failure this module exists to remove — so the empty case must raise, not hash.
    """
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ValueError, match="digest of nothing"):
        digest_tree(empty)


def test_the_count_and_the_digest_describe_the_SAME_SET(tmp_path):
    """THE COUNT IS WHAT MAKES A DIGEST LOOK TRUSTWORTHY, so it must not be able to disagree with it.

    kumo-trading-strategies' `files()` and `digest_tree()` walked the tree separately. A mutation narrowing
    the digest's walk from `rglob` to `glob` hashed 2 files out of 82 while the count still reported
    82 — every guard stayed green while the digest covered almost nothing.

    Asserted structurally: both must come from `_source_files`, so they cannot drift.
    """
    import inspect

    from api import provenance

    for fn in (provenance.digest_tree, provenance.files):
        assert "_source_files" in inspect.getsource(fn), (
            f"{fn.__name__} does not enumerate through `_source_files` — the count and the digest "
            f"can then describe different sets, and the count is the thing that makes the digest "
            f"look trustworthy"
        )


def test_EVERY_shipped_file_is_hashed_not_only_python(tmp_path):
    """A wheel is not only its modules.

    kumo-trading-strategies' first version hashed `*.py` alone while fifteen non-`.py` files shipped — a
    universe list, a JSON config or a pinned CSV could differ between two builds and be called
    identical. "A provenance tool that is silently partial is worse than none: it turns 'we do not
    know' into 'we checked'."
    """
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mod.py").write_text("x = 1")
    (root / "universe.json").write_text('["AAPL"]')
    before = digest_tree(root)

    (root / "universe.json").write_text('["MSFT"]')          # NOT a .py file
    assert digest_tree(root) != before, (
        "changing a non-.py shipped file did not move the digest — a data file can differ between "
        "two builds while the digest calls them identical"
    )


def test_a_RENAME_with_identical_contents_moves_the_digest(tmp_path):
    """Path and bytes both go into the hash.

    A file renamed with identical contents is a different package, and a rename is exactly how a
    module stops being imported. Hashing contents alone would call the two identical.
    """
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "a.py").write_text("x = 1")
    before = digest_tree(root)

    (root / "a.py").rename(root / "b.py")
    assert digest_tree(root) != before, "a rename with identical bytes did not move the digest"


def test_the_digest_does_not_depend_on_WHERE_it_is_installed(tmp_path):
    """The property the whole comparison depends on.

    A container, a venv and a working tree must agree, or planned-vs-actual compares two things that
    were never comparable.
    """
    a, b = tmp_path / "one" / "pkg", tmp_path / "two" / "pkg"
    for root in (a, b):
        root.mkdir(parents=True)
        (root / "mod.py").write_text("x = 1")
        (root / "data.json").write_text("{}")
    assert digest_tree(a) == digest_tree(b), (
        "the same content at two paths produced different digests — a container and a working tree "
        "would never agree"
    )


def test_bytecode_is_NOT_counted(tmp_path):
    """Excluded deliberately, and the reason is the opposite of carelessness.

    `__pycache__` is written on first import, so counting it would make a container's digest change
    simply by having been USED — two identical deployments would disagree, and the number would be
    useless precisely when someone finally looked at it.
    """
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "mod.py").write_text("x = 1")
    before = digest_tree(root)

    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
    assert digest_tree(root) == before, "bytecode moved the digest — using a container would change it"


def test_the_real_installed_cockpit_answers(tmp_path):
    """The seam: it must work on the ACTUAL package, not only on fixtures.

    A digest that is correct over `tmp_path` and raises over `api/` is a test suite passing about
    nothing — the same "green helper, dead wiring" shape this repo has been bitten by repeatedly.
    """
    d, n = digest(), files()
    assert len(d) == 16 and int(d, 16) >= 0, f"not a hex digest: {d!r}"
    assert n > 50, f"only {n} files hashed for the whole api package — the walk is not reaching it"


def test_the_digest_is_reproducible_within_a_process():
    """Two calls, same answer. A digest that drifts cannot gate anything."""
    assert digest() == digest()


# ---------------------------------------------------------------------------
# THE SEAM: /health must always carry all four, and an unavailable measurement
# must read as None rather than as agreement or as a 500.
# ---------------------------------------------------------------------------


def test_health_provenance_always_reports_ALL_FOUR_KEYS():
    """SERVED UNCONDITIONALLY, including when everything agrees.

    A field that appears only when something is wrong is a field nobody knows the normal value of —
    which is how `68201a1-dirty` was read past twice in one weekend. If a key can be absent, a reader
    cannot tell "not measured" from "not reported".
    """
    from api.app import _provenance

    got = _provenance()
    assert set(got) == {"cockpit_sha", "cockpit_digest", "strategies_sha", "strategies_digest"}, got


def test_an_UNAVAILABLE_measurement_is_None_and_does_not_raise(monkeypatch):
    """The third state. Absent is not mismatch and is not agreement.

    Demonstrated live rather than imagined: this venv has a kumo_strategies that PREDATES its
    provenance module, so `strategies_digest` is None here while the deployed container returns
    `a977ae4ad73080b9`. That is the intended behaviour — a version skew must report "unknown", never
    take `/health` down and never quietly read as a match.
    """
    import builtins

    from api.app import _provenance

    real_import = builtins.__import__

    def _no_provenance(name, *a, **k):
        if "provenance" in name:
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_provenance)
    got = _provenance()          # must not raise
    assert got["cockpit_digest"] is None and got["strategies_digest"] is None, got


def test_an_UNSET_sha_is_None_rather_than_an_empty_string(monkeypatch):
    """`""` and `None` compare differently and mean the same thing, so only one may reach the gate.

    An empty string is falsy but is not None, and a three-state comparison written against `is None`
    would classify it as a real value that mismatches everything.
    """
    from api.app import _provenance

    monkeypatch.setenv("KUMO_GIT_SHA", "")
    monkeypatch.setenv("KUMO_STRATEGIES_SHA", "")
    got = _provenance()
    assert got["cockpit_sha"] is None and got["strategies_sha"] is None, got


def test_a_PRESENT_sha_is_passed_through_unchanged(monkeypatch):
    """The fixture must be able to go the other way, or the test above proves only that it returns None."""
    from api.app import _provenance

    monkeypatch.setenv("KUMO_GIT_SHA", "e94a463")
    assert _provenance()["cockpit_sha"] == "e94a463"
