"""Test-suite setup: pin the engine to the synthetic node.

Tests boot the app (TestClient → lifespan → node.start). The live node needs a Databento key and
network and is non-deterministic; the synthetic BacktestEngine node is offline and gives the fixed
bars/positions the tests assert. Force it regardless of any KUMO_ENGINE in the shell, before import.
"""

import os

import pytest

os.environ["KUMO_ENGINE"] = "synthetic"


def pytest_sessionstart(session):
    """THE PIN ANCHOR, ASSERTED INSIDE THE SUITE (kumo-cockpit#947). `merge_gate --against-pin` exports
    `MERGE_GATE_EXPECT_KS_TREE=<checkout>/src`; this session refuses to run a single test unless
    `kumo_strategies` was imported from under that tree. The gate's own anchor check runs in a SEPARATE
    interpreter invocation, and a conftest `sys.path.insert`, an `__editable__*.pth` or `python -m
    pytest` (CWD first on sys.path) can make THIS process import a different tree than that check
    resolved — two invocations, two facts (l21wvpmj, cross-repo review; kumo-strategies#162 was the
    bite). Unset → no assertion, which is the ordinary local run and says so in the gate's evidence."""
    expected = os.environ.get("MERGE_GATE_EXPECT_KS_TREE")
    if not expected:
        return
    import importlib
    try:
        ks = importlib.import_module("kumo_strategies")
    except Exception as exc:  # noqa: BLE001 — the refusal names the cause; a traceback would not
        pytest.exit(f"MERGE_GATE_EXPECT_KS_TREE={expected} but kumo_strategies cannot be imported at all "
                    f"({type(exc).__name__}: {exc}) — refusing the session", returncode=4)
    actual = os.path.realpath(os.path.dirname(ks.__file__))
    root = os.path.realpath(expected)
    if not (actual == root or actual.startswith(root + os.sep)):
        pytest.exit(f"MERGE_GATE_EXPECT_KS_TREE={expected} but this session imported kumo_strategies from "
                    f"{actual} — the suite would wear the pin's name while testing another tree; refusing "
                    f"the whole session", returncode=4)


# `trylast` so this runs AFTER the marker plugin has deselected. Without it the hook saw every
# collected item — including `needs_services` tests that `addopts = "-m 'not needs_services'"` was
# about to drop — found live sleeves, and killed the WHOLE offline suite on any machine whose
# KUMO_DATABASE_URL points at paper, which is the normal local setup.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """REFUSE to run destructive integration tests against a database holding real sleeves.

    On 2026-08-19 I ran `api/test_budget_distribution_store.py` with `KUMO_DATABASE_URL` pointed at the
    live paper database. Those tests, like their siblings in `test_budget_store.py`, clean up with

        DELETE FROM strategy_sleeve WHERE strategy_id = ANY(:ids)

    and `UNALLOCATED` is in that list because the code under test names it as the source. So the cleanup
    deleted the live UNALLOCATED sleeve and reseeded it at the fixture's value, wiping $39,504.35 of
    real allocation and injecting three test sleeves into the operator's book. It was repaired by
    reconciling against broker equity, but nothing in the suite prevented it — the marker says these
    tests need services, not that they will destroy whatever they find.

    A `needs_services` test is only safe against a database it is allowed to empty. The check is on
    CONTENT rather than on the database name: a name convention is advice, while "this database holds
    MOMENTUM-002's sleeve" is the fact that actually matters, and it cannot be satisfied by renaming.
    """
    import os

    if not any("needs_services" in [m.name for m in item.iter_markers()] for item in items):
        return
    url = os.environ.get("KUMO_DATABASE_URL")
    if not url:
        return

    import asyncio

    async def _live_strategy_ids() -> list[str]:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                # TWO SIGNALS, because neither alone is right.
                #
                # A REGISTERED strategy sleeve is unambiguous proof of a real book — the registry is
                # generated from the strategies this deployment runs, so those ids never appear in a
                # disposable database. Keying only on a hardcoded list of four names was the first
                # version's flaw: it missed `UNALLOCATED`, the row `_clean` actually deletes and the one
                # that held the $39,504.35.
                #
                # But refusing on `UNALLOCATED`'s mere EXISTENCE is wrong in the other direction: these
                # tests seed and delete it by design, so a leftover row from an earlier run locked the
                # suite out of its own disposable database. Its BALANCE is the discriminator — a real
                # book holds unallocated capital, a test fixture cleans up to nothing.
                from api.strategy_registry import REGISTRY

                rows = (await conn.execute(
                    text("SELECT strategy_id, actual FROM strategy_sleeve"))).all()
                known = {e.strategy_id for e in REGISTRY}
                owned = {"T-DONOR", "T-RECIP", "T-FULL", "D-SHORT-A", "D-SHORT-B", "D-OVER",
                         "UNALLOCATED"}

                # A registered strategy sleeve is unambiguous: the registry is what this deployment
                # runs, so those ids never exist in a disposable database.
                found = [sid for sid, _ in rows if sid in known]

                # `UNALLOCATED` holding a balance is only evidence of a real book when something ELSE
                # unfamiliar is present too. On its own it is the normal residue of a failed test run,
                # and refusing on that locked the suite out of its own database — an alarm that fires on
                # the healthy case is one an operator learns to bypass, which is worse than no alarm.
                strangers = [sid for sid, _ in rows if sid not in owned and sid not in known]
                found += strangers
                if strangers:
                    found += [f"UNALLOCATED (holding {actual})" for sid, actual in rows
                              if sid == "UNALLOCATED" and float(actual or 0) > 0]
                return sorted(set(found))
        except Exception:  # noqa: BLE001 — no table, no server: nothing to protect
            return []
        finally:
            await engine.dispose()

    try:
        live = asyncio.run(_live_strategy_ids())
    except Exception:  # noqa: BLE001
        return
    if live:
        import pytest

        pytest.exit(
            f"REFUSING to run needs_services tests: {url.rsplit('@', 1)[-1]} holds sleeve rows this "
            f"suite does not own ({', '.join(live)}). These tests DELETE and reseed sleeve rows including "
            f"UNALLOCATED. Point KUMO_DATABASE_URL at a disposable database.",
            returncode=1,
        )


# ==================================================================================================
# THE PRIVATE STRATEGY LIBRARY MAY BE ABSENT (#581)
#
# `kumo-strategies` is a separate PRIVATE repo. `deploy/Dockerfile.backend` never installs it from git
# for exactly that reason ("the repo is private and the build has no credentials"), and CI had no
# credential either — both backend jobs died at the auth step, so the ENTIRE suite reported nothing.
#
# It is now an extra (`.[strategies]`). Where it is missing, the ~129 test files that do not import it
# still run; the 30 that do are skipped HERE rather than exploding at collection.
#
# COMPUTED, NOT LISTED. A hardcoded file list rots the moment someone adds an import, and would rot
# silently — the file would collect, fail, and look like a real defect. This greps at collect time.
# ==================================================================================================


def _strategies_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("kumo_strategies") is not None


if not _strategies_available():  # pragma: no cover - depends on the install, not on a code path
    import pathlib as _pathlib
    import re as _re

    _root = _pathlib.Path(__file__).parent

    def _modname(q: "_pathlib.Path") -> str:
        return f"{q.parent.relative_to(_root).as_posix().replace('/', '.')}.{q.stem}".lstrip(".")

    def _import_pattern(mods) -> str:
        """Match BOTH import forms for a set of dotted module names.

        `from api.app import x` and `import api.app` share the dotted spelling, but
        `from api import app as app_module` does NOT — and that third form is what two test files use,
        which is how they survived the first two versions of this detection and failed in CI anyway.
        """
        alts = []
        for m in sorted(mods):
            dotted = _re.escape(m)
            alts.append(rf"(?:from|import)\s+{dotted}\b")
            if "." in m:
                pkg, _, leaf = m.rpartition(".")
                # `from api import app`, `from api import consumer, app`, `from api import app as x`
                alts.append(rf"from\s+{_re.escape(pkg)}\s+import\s+[^\n]*\b{_re.escape(leaf)}\b")
        return "|".join(alts)

    _sources = {
        _modname(q): q.read_text(errors="ignore")
        for q in _root.rglob("*.py")
        if ".venv" not in q.parts and not q.name.startswith("test_")
    }

    # TRANSITIVE CLOSURE TO A FIXPOINT, not a fixed number of hops.
    #
    # A direct grep deselected 31 files and CI still died on three that never name kumo_strategies:
    # they import `api.app`, which does. Following ONE level caught two of the three; the third reaches
    # it two hops out. Chasing levels is the wrong shape — iterate until the set stops growing and
    # depth stops mattering.
    _tainted = {m for m, src in _sources.items() if "kumo_strategies" in src}
    while True:
        _rx = _re.compile(_import_pattern(_tainted))
        _grown = _tainted | {m for m, src in _sources.items() if _rx.search(src)}
        if _grown == _tainted:
            break
        _tainted = _grown

    _needs = _re.compile("kumo_strategies|" + _import_pattern(_tainted))

    collect_ignore = [
        str(_p.relative_to(_root))
        for _p in _root.rglob("test_*.py")
        if ".venv" not in _p.parts and _needs.search(_p.read_text(errors="ignore"))
    ]

    print(
        f"\n[conftest] kumo-strategies is NOT installed — deselecting {len(collect_ignore)} test files "
        f"that need it, directly or through {len(_tainted)} module(s) that reach it. "
        f"Install `.[strategies]` to run them.\n")


@pytest.fixture(autouse=True)
def _no_pool_cache_leaks_between_tests():
    """NO TEST READS THE LIVE SYMBOL POOL, and none can leak a cached read into the next one.

    Since kumo-cockpit#511 `providers.ibkr.build`/`build_data` union `exec_pool_source` into
    `load_contracts` through a real asyncpg connection to KUMO_DATABASE_URL — which on the normal
    local setup is the LIVE PAPER DATABASE (see the collection hook above, which already refuses
    destructive tests for that reason). A test driving those builders therefore went from hermetic
    to network-touching without a line of it changing, and passed only where port 5432 happened to
    be CLOSED, asserting against the DEGRADED branch while claiming to pin the directive.

    SEEDING AN EMPTY POOL, not merely resetting to None, and that is the whole point. Resetting
    makes each test read the database afresh; seeding means the read never happens unless a test
    asks for it. EIGHT tests were doing this. Five were found by hand across three rounds — each
    round declared the class closed — and three more only by machine, hidden behind
    `from api.providers.ibkr import build_data`, which no call-site scan sees. Enumerating the
    callers is what kept being wrong, so the property is enforced at the value instead: nothing to
    enumerate, and it covers aliases, helpers, fixtures and tests not yet written.

    A test that WANTS the real read sets `_POOL_CACHE = None` first; `_fake_asyncpg` in
    `test_load_contracts_follows_the_pool.py` does exactly that, and stubs `asyncpg.connect`
    beneath it. `test_the_seeded_pool_cache_really_stops_the_read` proves this fixture works through
    a real builder rather than assuming it.

    `_POOL_CACHE` is also MODULE state, so the same seed doubles as leak containment: without it a
    stall test's cached TimeoutError would be re-raised at an unrelated caller and read as a real
    failure.

    Imported lazily: this file is loaded before the package is importable in some invocations, and a
    conftest that fails to import takes the whole suite with it.
    """
    try:
        from api.providers import ibkr
    except Exception:                                          # noqa: BLE001
        yield
        return
    # ANCHOR ASSERTED, because assigning to a renamed attribute would create a GHOST that resets
    # nothing, forever, while every test stayed green — a replace that matches nothing (2026-08-28).
    assert hasattr(ibkr, "_POOL_CACHE"), (
        "providers.ibkr has no _POOL_CACHE — it was renamed and this fixture is now writing an "
        "attribute nothing reads, so every test is back to reading the live pool")
    ibkr._POOL_CACHE = ()
    yield
    ibkr._POOL_CACHE = ()
