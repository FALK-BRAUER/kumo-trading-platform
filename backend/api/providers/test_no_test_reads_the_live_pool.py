"""No test reads the LIVE symbol pool — proved through a real builder, not by scanning callers.

Since #511 `ibkr.build`/`build_data` union `exec_pool_source` into `load_contracts` through a real
asyncpg connection to `KUMO_DATABASE_URL`, which on the normal local setup points at the LIVE PAPER
DATABASE (`backend/conftest.py` already refuses destructive integration tests for that reason). A
test driving those builders went from hermetic to network-touching without a line of it changing,
and passed only where port 5432 happened to be CLOSED — the failed read falls back to the declared
universe, so the assertion described the DEGRADED branch while claiming to pin the directive.

EIGHT tests were doing this. How they were found is the point:

    rev3   1 by hand   -> "the class is closed"
    rev5   2 by hand   -> "the class is closed"
    then   3 by an AST scan over call sites, in a file neither enumeration had reached
    then   3 more only after the scan was widened to IMPORTS — hidden behind
           `from api.providers.ibkr import build_data`, which no call-site scan can see

Every hand enumeration was wrong, and so was the first machine one. The scan was also EVADABLE in a
way its own failure message taught: review replaced a stub with `# TODO: stub _pool_symbols here`
and the substring check went green while the test did live I/O.

So the property is no longer enforced by finding the callers. `conftest.py` SEEDS `_POOL_CACHE` with
an empty tuple before every test, so the read simply never happens — nothing to enumerate, and it
covers aliases, helper functions, fixtures, `getattr` indirection and tests not yet written. This
module is what proves that seed actually works, through the real entry point.
"""

from __future__ import annotations

import asyncpg
import pytest

from api.providers import ibkr


@pytest.fixture(autouse=True)
def attempts(monkeypatch):
    """RECORDS connection attempts; it does not raise to signal one.

    The first version of this raised from a fake `asyncpg.connect`, and the mutation bite exposed it
    as vacuous: `_tradeable_symbols` wraps the pool read in `except Exception` — deliberately, so a
    database hiccup cannot stop the node booting — and swallowed the tripwire whole. Seeding was
    reverted to the old behaviour and all three tests stayed GREEN while the read happened on every
    one of them.

    A detector whose signal travels by exception cannot work through a fallback built to absorb
    exceptions. The observable has to be the attempt itself."""
    seen = []

    async def _record(dsn, **kw):
        seen.append(dsn)
        raise RuntimeError("no live pool in tests")   # after recording; the fallback may eat this

    monkeypatch.setattr(asyncpg, "connect", _record)
    return seen


def test_the_recorder_can_SEE_a_read_at_all(attempts):
    """VACUITY GUARD. The tests below prove a negative — no connection was opened — and a negative is
    what a broken detector reports for free. Show the recorder FIRES when the read is allowed, or its
    silence afterwards means nothing. This is the assertion the exception-based version could not
    make, and its absence is why that version passed with the seed removed."""
    ibkr._POOL_CACHE = None                      # undo the conftest seed: allow the read
    try:
        ibkr._tradeable_symbols()                # the REAL caller, fallback and all
    finally:
        ibkr._POOL_CACHE = ()
    assert attempts, "the recorder saw no connection even though the read was allowed"


def test_the_seeded_pool_cache_really_stops_the_read_in_the_DATA_builder(attempts):
    """The real `build_data`, nothing stubbed but the socket. Asserting on the RECORD, not on an
    exception, because the production fallback would swallow the exception."""
    spec = ibkr.build_data({"ibg_host": "h", "ibg_port": 4002})
    assert spec.config.instrument_provider.load_contracts, "the builder must still declare contracts"
    assert not attempts, f"build_data opened a connection to the live symbol pool: {attempts}"


def test_the_seeded_pool_cache_really_stops_the_read_in_the_EXEC_builder(monkeypatch, attempts):
    """Same for the exec client, which reaches `_pool_symbols` by a different call path."""
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DUTEST001")
    spec = ibkr.build({"ibg_host": "h", "ibg_port": 4002, "account_id_env": "IBKR_ACCOUNT_ID"})
    assert spec.config.instrument_provider.load_contracts, "the builder must still declare contracts"
    assert not attempts, f"build opened a connection to the live symbol pool: {attempts}"


def test_PRODUCTION_still_ships_an_UNSEEDED_cache():
    """The seed that makes every test safe is also what makes this pin necessary.

    `_POOL_CACHE` starts as None in production, and the FIRST caller of a boot performs the read.
    Change that declaration to `()` and #511 becomes a permanent silent no-op: the pool is never
    read on any boot, `load_contracts` reverts to the four-day-old snapshot forever, and nothing
    logs a fallback because nothing failed. Review proved it — the one-character change passes 254
    tests across every pool-adjacent module.

    No test can catch it any other way, because every test now sets the cache explicitly (the
    conftest seeds it, `_fake_asyncpg` clears it), so the SHIPPED default is the one value the
    suite never observes. That is the dead-knob shape this ticket spent its whole review fighting,
    and the seeding design is what opened it.

    Read off the SOURCE rather than the imported module, which the fixtures have already mutated.
    Structural (AST) rather than textual, so reformatting the declaration does not fail it — but the
    anchor is asserted, so a RENAME fails loudly instead of passing vacuously."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(ibkr.__file__).read_text())
    decls = [n for n in tree.body
             if isinstance(n, ast.AnnAssign)
             and isinstance(n.target, ast.Name) and n.target.id == "_POOL_CACHE"]
    assert len(decls) == 1, (
        f"expected exactly one module-level `_POOL_CACHE` declaration, found {len(decls)} — this "
        f"test's anchor is gone and it is no longer pinning anything")
    value = decls[0].value
    assert isinstance(value, ast.Constant) and value.value is None, (
        "production must ship `_POOL_CACHE = None` so the first caller of a boot actually reads the "
        f"pool; it is declared as {ast.unparse(value)!r}, which makes #511 a silent no-op")
