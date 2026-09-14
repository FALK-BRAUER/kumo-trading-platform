"""`load_contracts` follows the POOL, not a snapshot of it (#511).

Measured on staging 2026-08-29: the pool holds 106 symbols, `feed.toml`'s `[universe]` holds 93 —
a snapshot taken on 2026-08-24, and the file says so itself: "This is the POOL as it stood
2026-08-24. It is a SNAPSHOT and it does not refresh with the pool." Twenty-five pool names have no
qualified contract, and twenty of those are ordinary listable stocks (AMZN, CRWD, FTNT, JAZZ, INCY,
GMAB, RPRX, NSIT, CNK, BOX, DBX, GEN, SAN, XYZ, PBF, DK, CVI, BNY, AGEN, AAMI).

A lane deciding to buy AMZN today is refused at `broker.py:73` with "AMZN is not a subscribed
instrument" — before any order is built. It can rank the name and cannot trade it.

WHY THIS IS SAFE. Qualification is serial and happens inside `_connect()`, so the cost lands on the
node's 60s startup budget. The five unresolvable names the pool carries are EXCLUDED rather than
merely absent — see `_excluded_symbols`; a union cannot express "absent on purpose", which is what
the first version of this change got wrong.

MEASURED against staging's own gateway on 2026-08-29, driving this adapter's `get_contract_details`
on client id 9, because what IB does with a symbol is not knowable by inspection:

    the 21 symbols the pool ADDS       9-75ms each, 0.6s for all 21
    BLLLN FTNR IQVIA JEPO OVVI         60,003ms EACH
    BLLN 18ms, IQV 52ms                the correct spellings resolve instantly (#716)

And from staging's own boot log, which settles how the two clients' costs combine: `_connect_clients`
starts both concurrently and the kernel awaits them together, so the budget is spent by the SLOWER
client, not by their sum — STARTING 09:58:09.621, exec Connected 18.9s, data 19.2s, RUNNING 19.4s.
The union adds 0.6s to that.

PER-CONTRACT COST IS NOT A CONSTANT, so do not tighten anything against a single reading. The
2026-08-29 11:21 deploy qualified 231 contracts in 1.8s — about 8ms each, against ~185ms each on the
09:58 boot above. The difference is a warm IB gateway and a warm Nautilus cache ("Cached 209
instruments from database"); the earlier boot recreated the gateway container. What does NOT vary
with warmth is the five unresolvable names, which hang for their full timeout every time — which is
why the exclusion, not the arithmetic, is what keeps this inside the budget. The margin is finite and a new unresolvable name eats 5s of it (#525
bounds each request), which is why #723 — validating the door the scheduled refresh actually uses —
is what protects this going forward.

FALLING BACK TO THE SNAPSHOT IS THE SAFE DIRECTION, not the quiet one: a pool that cannot be read
must not silently shrink what the lane may trade, so the declared universe stands and the fallback
is named.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from api.providers import ibkr


#: The two sets the test below drives, named ONCE. Inline literals in a fixture-property test are
#: disconnected from the lambdas the real test uses: the property test then asserts only its own
#: copy and can be made to pass by editing itself, while the drift it claims to guarantee quietly
#: disappears from the case that matters.
_POOL = ("AAPL", "MSFT", "AMZN")
_SNAPSHOT = ("AAPL", "MSFT")


def test_the_fixture_reproduces_the_drift():
    """FIXTURE PROPERTY: pool and snapshot genuinely differ, or this test proves nothing."""
    assert set(_POOL) - set(_SNAPSHOT) == {"AMZN"}


def test_the_POOL_wins_over_the_snapshot(monkeypatch):
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: _POOL)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: _SNAPSHOT)
    assert "AMZN" in ibkr._tradeable_symbols(), (
        "the exec client still loads a snapshot, so a lane can rank AMZN and never trade it (#511)"
    )


def test_the_DECLARED_universe_is_still_included(monkeypatch):
    """A name in the deployment's declared universe but not (yet) in the pool must stay tradeable —
    the pool is a moving target and the declaration is the operator's floor."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ("AAPL",))
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AAPL", "MSFT"))
    out = ibkr._tradeable_symbols()
    assert set(out) == {"AAPL", "MSFT"}


def test_an_UNREADABLE_pool_falls_back_to_the_snapshot_and_says_so(monkeypatch, caplog):
    """THREE STATES. A pool read that fails is not an empty pool: shrinking what the lane may trade
    because Postgres hiccuped would take away exactly the names it is holding. The declared
    universe stands, and the degraded state is reported rather than passing as normal."""
    def _boom():
        raise RuntimeError("pool unreadable")

    monkeypatch.setattr(ibkr, "_pool_symbols", _boom)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AAPL", "MSFT"))
    assert set(ibkr._tradeable_symbols()) == {"AAPL", "MSFT"}
    assert "pool" in caplog.text.lower()


def test_BOTH_EMPTY_still_raises(monkeypatch):
    """The pre-2026-08-24 outage guard survives: an empty tradeable set fails at BOOT, not
    invisibly at submit time."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ())
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ())
    with pytest.raises(RuntimeError, match="neither the pool nor the feed config"):
        ibkr._tradeable_symbols()


def test_an_EXPLICITLY_EXCLUDED_symbol_is_not_reintroduced_by_the_pool(monkeypatch):
    """THE BLOCK. `feed.toml` excludes five tickers BY ABSENCE from `[universe]`, with a comment
    saying that exclusion is what keeps the boot inside its budget — and a union with the pool
    cannot express "absent on purpose". Measured on staging by running this branch's own code:
    EXEC load_contracts 93 -> 119 and DATA 114 -> 140, with BLLLN, FTNR, IQVIA, JEPO and OVVI back
    in BOTH sets.

    That is not a rounding error in the budget: per-client qualification is 185ms (exec) and 169ms
    (data), so the union costs ~47s and ~49s against a 60s budget that requires BOTH engines. Two
    more unresolvable names is a zero-strategy boot.

    So exclusion becomes a STATE — a declared list in the instance's own config — rather than an
    absence that the next union forgets. NOT `exec_pool_override` with kind='exclude': that marks a
    symbol for LIQUIDATION at the next session (app.py:1223), which is the wrong instrument
    entirely for "this ticker does not exist at the venue".
    """
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ("AAPL", "BLLLN", "JEPO"))
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AAPL",))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ("BLLLN", "JEPO"))
    assert set(ibkr._tradeable_symbols()) == {"AAPL"}, (
        "the pool reintroduced symbols the instance excluded on purpose — the denylist that keeps "
        "the boot inside its connect budget is defeated by the union"
    )


def test_an_exclusion_of_a_DECLARED_symbol_still_wins(monkeypatch):
    """Exclusion is the stronger statement: it is how an operator retires a name that is still
    sitting in the declared universe, without editing two places."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ())
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AAPL", "BLLLN"))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ("BLLLN",))
    assert set(ibkr._tradeable_symbols()) == {"AAPL"}


def test_EXCLUDING_EVERYTHING_still_raises(monkeypatch):
    """The empty-set guard must survive the new subtraction: an exclusion list that empties the
    tradeable set fails at BOOT, not invisibly at submit."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ("AAPL",))
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AAPL",))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ("AAPL",))
    with pytest.raises(RuntimeError, match="neither the pool nor the feed config|excluded"):
        ibkr._tradeable_symbols()


# ==================================================================================================
# `_pool_symbols` ITSELF (#511 review rev3). Everything above stubs it, so the function that does the
# actual work had ZERO coverage: rename `exec_pool_source` and the whole suite stays green while
# production silently falls back to the four-day-old snapshot forever, logging a warning nobody
# reads. That is #574's dead-knob shape wearing a fallback, and the fallback is what hides it.
# ==================================================================================================
class _FakeConn:
    """Rejects what a real asyncpg connection rejects: `fetch` is awaitable and returns Records that
    are indexed BY COLUMN NAME. A double returning plain strings would let `r["symbol"]` disappear."""

    def __init__(self, rows, *, stall=False):
        self._rows, self._stall, self.queries, self.closed = rows, stall, [], False

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if self._stall:
            await asyncio.sleep(30)          # accepted, then never answers
        return [{"symbol": s} for s in self._rows]

    async def close(self):
        self.closed = True


def _fake_asyncpg(monkeypatch, conn, *, connect_kw=None):
    import asyncpg

    from api.providers import ibkr

    # OPT IN TO THE REAL READ. The conftest fixture SEEDS an empty pool so no test touches the
    # database by accident; these tests exist to drive `_pool_symbols` for real, so they clear the
    # seed and stub one layer lower, at `asyncpg.connect`.
    monkeypatch.setattr(ibkr, "_POOL_CACHE", None)

    async def _connect(dsn, **kw):
        (connect_kw if connect_kw is not None else {}).update({"dsn": dsn, **kw})
        return conn

    monkeypatch.setattr(asyncpg, "connect", _connect, raising=True)


def test_the_double_REJECTS_what_a_real_asyncpg_Record_rejects():
    """Fixture property first. `_pool_symbols` reads `r["symbol"]`; a double whose rows were bare
    strings would accept a mutant that read `r[0]`, or one that dropped the column entirely."""
    row = {"symbol": "AMZN"}
    assert row["symbol"] == "AMZN"
    with pytest.raises((KeyError, TypeError)):
        row["ticker"]


def test_it_reads_the_TABLE_THE_POOL_IS_ACTUALLY_WRITTEN_TO(monkeypatch):
    """Named, not inferred. `PgSymbolPool.refresh_source` is the only writer and it writes
    `exec_pool_source`; if this query drifts to another table the fallback swallows it and the
    tradeable set silently reverts to the snapshot."""
    from api.providers import ibkr

    conn = _FakeConn(["AMZN", "CRWD"])
    _fake_asyncpg(monkeypatch, conn)
    assert ibkr._pool_symbols() == ("AMZN", "CRWD")
    assert "exec_pool_source" in conn.queries[0]
    assert conn.closed, "the connection must be closed even on the happy path"


def test_a_STALLED_query_is_bounded_and_does_not_hang_the_boot(monkeypatch):
    """A Postgres that ACCEPTS and then never answers. `connect(timeout=5)` does not cover this —
    the connect succeeded. Unbounded, `build_node` hangs forever: before the node's 60s budget
    starts and before the inert-node watchdog exists, so nothing downstream can time it out."""
    from api.providers import ibkr

    conn = _FakeConn(["AMZN"], stall=True)
    _fake_asyncpg(monkeypatch, conn)
    monkeypatch.setattr(ibkr, "_POOL_READ_TIMEOUT_SECS", 0.2)   # the production value is 5
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        ibkr._pool_symbols()
    assert time.monotonic() - t0 < 3, "the read must be bounded, not merely slow"


def test_a_STALLED_pool_leaves_the_DECLARED_universe_standing(monkeypatch):
    """The seam, not the unit. The timeout above is only useful if `_tradeable_symbols` survives it —
    a boot that raises here trades nothing at all, which is worse than trading the snapshot."""
    from api.providers import ibkr

    _fake_asyncpg(monkeypatch, _FakeConn(["AMZN"], stall=True))
    monkeypatch.setattr(ibkr, "_POOL_READ_TIMEOUT_SECS", 0.2)   # the production value is 5
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AEM", "WPM"))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ())
    assert ibkr._tradeable_symbols() == ("AEM", "WPM")


def test_the_pool_is_read_ONCE_so_exec_and_data_cannot_DISAGREE(monkeypatch):
    """`build_node` asks twice — once for the exec client, once inside `_data_contracts`. Two
    independent reads can return different answers, and then a pool-added name is tradeable on exec
    while the data client refuses its bars as 'instrument not found' (#606's symptom, intermittent)."""
    from api.providers import ibkr

    conn = _FakeConn(["AMZN"])
    _fake_asyncpg(monkeypatch, conn)
    first, second = ibkr._pool_symbols(), ibkr._pool_symbols()
    assert first == second == ("AMZN",)
    assert len(conn.queries) == 1, f"one boot must read the pool once, got {len(conn.queries)}"


def test_a_LOWERCASE_pool_row_cannot_evade_the_exclusion(monkeypatch):
    """The pool is NOT guaranteed uppercase. `POST /pool/source/{name}` forwards `body.symbols`
    verbatim whenever nothing was refused, and on an instance with no venue catalog nothing ever IS
    refused (#663) — while `george.py` writes `str(r.symbol)` straight from the same unvalidated CSV
    that minted BLLLN. An uppercase-only denylist compared against a raw pool is two spellings of one
    identity, and the cheaper one wins: the junk contract is minted anyway and costs 5s on BOTH
    clients' connect."""
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AEM",))
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ("blllN", " IQVIA ", "amzn"))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ("BLLLN", "IQVIA"))
    got = ibkr._tradeable_symbols()
    assert "BLLLN" not in got and "blllN" not in got, f"exclusion evaded by case: {got}"
    assert "IQVIA" not in got and " IQVIA " not in got, f"exclusion evaded by padding: {got}"
    assert got == ("AEM", "AMZN"), got


def test_an_EXCLUDED_symbol_cannot_re_enter_through_the_REFERENCE_universe(monkeypatch):
    """`_data_contracts` unions the compass's grading universe onto the tradeable set. Subtracting
    the exclusion only inside `_tradeable_symbols` leaves that union as a second door into the same
    denylist. The two sets are disjoint today — a fact about today's rotation universe, not a
    property of the code, and #511's whole lesson is that a union cannot express 'absent on purpose'."""
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AEM",))
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: ())
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ("JEPO",))
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: ("SPY", "JEPO"))
    got = sorted(c.symbol for c in ibkr._data_contracts())
    assert got == ["AEM", "SPY"], f"an excluded ticker re-entered via the reference set: {got}"


def test_an_UNREADABLE_exclusion_list_REFUSES_rather_than_permitting_everything(monkeypatch):
    """'The exclusion list could not be read' is not 'nothing is excluded'. That conversion turns a
    failed read into permission to load five contracts that hang the connect for 60s each — a
    fallback producing exactly the outage the exclusion exists to prevent."""
    import api.feed_config as fc

    def _boom(*a, **k):
        raise OSError("feed.toml unreadable")

    monkeypatch.setattr(fc, "load_feed_config", _boom)
    with pytest.raises(OSError):
        ibkr._excluded_symbols()


def test_a_FAILED_first_read_does_not_let_the_SECOND_split_the_universe(monkeypatch):
    """Review rev4, proven by execution before this was fixed: exec=['AEM'] data=['AEM','AMZN'].

    Caching only SUCCESSFUL reads left the exact split the cache exists to prevent. `build` asks for
    the exec client, the read fails, the declared universe stands; `_data_contracts` then asks again
    and the retry succeeds, so the data client gets declared|pool. Direction is what makes it
    dangerous: data a superset of exec means a lane sees AMZN's bars, ranks it, and the submit dies
    on the None-instrument AttributeError — the 2026-08-24 outage back as an intermittent,
    boot-dependent one.

    One boot must give ONE answer to both clients, whichever way it went."""
    import asyncpg

    monkeypatch.setattr(ibkr, "_POOL_CACHE", None)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: ("AEM",))
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ())
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: ())

    calls = {"n": 0}

    async def _connect(dsn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient: connection reset")
        return _FakeConn(["AMZN", "AEM"])

    monkeypatch.setattr(asyncpg, "connect", _connect)
    exec_set = set(ibkr._tradeable_symbols())
    data_set = {c.symbol for c in ibkr._data_contracts()}
    assert exec_set == data_set, f"SPLIT UNIVERSE: exec={sorted(exec_set)} data={sorted(data_set)}"
    assert exec_set == {"AEM"}, "a boot whose pool read failed trades the declared universe"


def test_the_DSN_is_rewritten_for_asyncpg_which_does_not_speak_SQLAlchemys_scheme(monkeypatch):
    """`database_url()` returns SQLAlchemy's `postgresql+asyncpg://`; asyncpg itself rejects it.
    Untested, this is a one-word rewrite whose failure looks exactly like a database that is down —
    the fallback logs a warning, the declared universe stands, and the pool is silently never read
    on any boot. `_fake_asyncpg` grew `connect_kw` for this; a collector nothing asserts on is an
    anchor built and never used."""
    import api.db.engine as dbe

    # FIXTURE PROPERTY: pin the INPUT, or this test is environment-conditional. On a machine whose
    # KUMO_DATABASE_URL is already a plain `postgresql://`, there is nothing to rewrite and the
    # assertions pass without exercising the rewrite at all.
    monkeypatch.setattr(dbe, "database_url", lambda: "postgresql+asyncpg://u:p@h:5432/kumo")
    assert "+asyncpg" in dbe.database_url(), "the fixture must present a scheme that needs rewriting"

    seen = {}
    _fake_asyncpg(monkeypatch, _FakeConn(["AMZN"]), connect_kw=seen)
    ibkr._pool_symbols()
    assert seen["dsn"].startswith("postgresql://"), seen["dsn"]
    assert "+asyncpg" not in seen["dsn"], seen["dsn"]


# -- #871: a lane's SETTINGS universe reaches load_contracts ---------------------------------------
_LANE = ("ZZQA", "ZZQB", "ZZQC")      # names the declared snapshot and the pool cannot produce


def test_the_871_fixture_names_are_absent_from_declared_and_pool():
    assert not set(_LANE) & set(_SNAPSHOT) and not set(_LANE) & set(_POOL)


def test_an_ENABLED_lanes_settings_universe_is_in_the_tradeable_set(monkeypatch):
    """staging2 2026-09-10: `QC27_UNIVERSE` 138 names, TECHIVOL-005 booted "125 of 138 symbols have
    no instrument on this venue" — IB was never asked. Bitten by: dropping the lane union."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: _POOL)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: _SNAPSHOT)
    monkeypatch.setattr("api.settings.resolve",
                        lambda _d: {"QC27_ENABLED": True, "QC27_UNIVERSE": list(_LANE)})
    out = set(ibkr._tradeable_symbols())
    assert set(_LANE) <= out, "an enabled lane's universe never reaches load_contracts"
    assert set(_SNAPSHOT) | set(_POOL) <= out, "the union must not drop what it had"
    data = {c.symbol for c in ibkr._data_contracts()}
    assert set(_LANE) <= data, "the DATA client must resolve them too, or the lane gets no bars"


def test_a_DISABLED_lanes_universe_is_NOT_loaded(monkeypatch):
    """Contract resolution on IB is serial and paced; 138 needless lookups cost the connect budget of
    the lanes that run. Bitten by: ignoring the gate."""
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: _POOL)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: _SNAPSHOT)
    monkeypatch.setattr("api.settings.resolve",
                        lambda _d: {"QC27_ENABLED": False, "QC27_UNIVERSE": list(_LANE)})
    assert not set(_LANE) & set(ibkr._tradeable_symbols())


def test_EVERY_lane_universe_in_the_table_is_read_and_the_lanes_read_the_same_table():
    import pytest

    from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

    if not crsishort_installed():
        pytest.skip("CRSISHORT adapter absent from the installed kumo-strategies (loud test is red)")
    """Aimed at the class: the connector reads `LANE_UNIVERSES`; every lane whose universe is a
    settings key reads it through `api.lane_universes.universe_symbols` with a key that IS in the
    table. Two lists of one fact drift — this pins that there is one."""
    import inspect

    from api.lane_universes import LANE_UNIVERSES

    from strategies import crsi_short, qc27, qc345

    keys = {u for _g, u in LANE_UNIVERSES}
    assert keys == {"QC27_UNIVERSE", "QC345_UNIVERSE", "CRSI_UNIVERSE"}
    for mod, key in ((qc27, "QC27_UNIVERSE"), (qc345, "QC345_UNIVERSE"), (crsi_short, "CRSI_UNIVERSE")):
        src = inspect.getsource(mod._universe_symbols)
        assert "universe_symbols(" in src and key in src, f"{mod.__name__} reads its universe elsewhere"


def test_an_UNREADABLE_settings_domain_degrades_LOUDLY_not_silently(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: _POOL)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: _SNAPSHOT)

    def _boom(_d):
        raise RuntimeError("settings store down")

    monkeypatch.setattr("api.settings.resolve", _boom)
    with caplog.at_level(logging.ERROR):
        out = set(ibkr._tradeable_symbols())
    assert set(_SNAPSHOT) | set(_POOL) <= out
    assert any("lane universes unreadable" in r.getMessage() for r in caplog.records)
