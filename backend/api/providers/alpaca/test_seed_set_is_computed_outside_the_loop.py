"""The deployment's seed set is computed at BUILD time, outside the event loop — never inside `_connect` (#1057).

THE DEFECT, measured on an Alpaca paper instance's 2026-09-13 08:19Z boot, the first with #1063's seeding:

    08:19:32Z  IBKR tradeable set: the symbol pool could not be read (RuntimeError('the symbol pool
               cannot be read from inside a running event loop; the declared universe stands for
               this boot')) — falling back to the declared universe of 2 symbol(s).
    08:19:46Z  cache seeding: 270 instrument(s) for 270 deployment symbol(s) handed to the cache
    08:19:46Z  MOMENTUM-002: 16 of 117 symbols have no instrument on this venue and will NOT be traded

`_pool_symbols` (ibkr.py) is SYNCHRONOUS BY DESIGN — it runs in the client BUILDERS before the node
loop exists, and REFUSES from inside a running loop rather than nest one. On IBKR the exec builder
calls `_tradeable_symbols()` at build time, so the pool is read and cached before any loop; on
Alpaca no builder did, and #1063 made the first call from the async `_connect` — inside the loop —
so the pool was refused, the lane universes were seeded (270), and the eleven pool names were not.
The seam test that shipped it stubbed `_tradeable_symbols` and could not represent the refusal: a
double that cannot represent production is the bug.

THE FIX, same shape as IBKR: `build_data` computes the set OUTSIDE the loop (where
`_data_contracts()` runs for IBKR) and hands it to the client through its config; `_connect` seeds
from that tuple and reads nothing. Here the REAL `_tradeable_symbols` and `_pool_symbols` are driven
— only the Postgres read under `_pool_symbols` is a fake `asyncpg`, and the file/settings readers
are pinned to tuples — so the in-loop refusal is reachable, and the fixture proves it first.

Every test here was seen red on 97fd81b for the reason its docstring names.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

import api.providers.ibkr as ibkr
from api.providers.alpaca.data_client import AlpacaDataClient, build_data

POOL = ("BIIB", "BOX", "DBX", "SLB")             # the pool, read from Postgres in production
DECLARED = ("AAPL", "MSFT")                      # feed.toml [universe] — paper's real 2
LANES = ("AMAT", "NET")                          # an enabled lane's settings universe
REFERENCE = ("SPY", "IEI")
CATALOGUE = {**{s: "XNAS" for s in POOL + DECLARED + LANES + REFERENCE}, "ZZZQ": "XNAS"}


def _equity(sym: str, mic: str) -> Equity:
    return Equity(instrument_id=InstrumentId(Symbol(sym), Venue(mic)), raw_symbol=Symbol(sym), currency=USD,
                  price_precision=2, price_increment=Price.from_str("0.01"), lot_size=Quantity.from_int(1),
                  ts_event=0, ts_init=0)


class _FakeAsyncpg:
    """`asyncpg.connect` as `_pool_symbols._read` uses it: connect → fetch rows → close."""

    class _Conn:
        async def fetch(self, sql):
            return [{"symbol": s} for s in POOL]

        async def close(self):
            return None

    async def connect(self, dsn, timeout=None):
        return self._Conn()


@pytest.fixture
def real_symbol_sets(monkeypatch):
    """The REAL `_tradeable_symbols` / `_pool_symbols` over pinned file/settings readers and a fake
    Postgres. `_POOL_CACHE` reset so this boot's first read is the one under test."""
    import sys

    monkeypatch.setattr(ibkr, "_POOL_CACHE", None)
    monkeypatch.setattr(ibkr, "_declared_symbols", lambda: DECLARED)
    monkeypatch.setattr(ibkr, "_lane_universe_symbols", lambda: LANES)
    monkeypatch.setattr(ibkr, "_excluded_symbols", lambda: ())
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: REFERENCE)
    monkeypatch.setitem(sys.modules, "asyncpg", _FakeAsyncpg())
    monkeypatch.setenv("APCA_API_KEY_ID", "PKTEST")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "secret")
    import api.db.engine as dbe

    monkeypatch.setattr(dbe, "database_url", lambda: "postgresql+asyncpg://u:p@h/db", raising=False)


class _Provider:
    def __init__(self):
        self._by_id = {}
        for sym, mic in CATALOGUE.items():
            e = _equity(sym, mic)
            self._by_id[e.id] = e

    async def load_all_async(self, filters=None):
        return None

    @property
    def count(self):
        return len(self._by_id)

    def list_all(self):
        return list(self._by_id.values())


class _Awaitable:
    async def connect(self):
        return None

    async def start(self):
        return None


def _host(config) -> types.SimpleNamespace:
    """A host for the REAL `_connect` built the way the factory builds the client: the config the
    build step produced is what the client carries."""
    host = types.SimpleNamespace(_http=_Awaitable(), _ws=_Awaitable(), _instrument_provider=_Provider(),
                                 _seed_symbols=tuple(config.seed_symbols), handed=[], said=[])
    host._log = types.SimpleNamespace(info=lambda m, *a, **k: host.said.append(str(m)),
                                      warning=lambda m, *a, **k: host.said.append("WARN " + str(m)),
                                      error=lambda m, *a, **k: host.said.append("ERROR " + str(m)))
    host._handle_data = lambda d: host.handed.append(d)
    host._seed_cache_with_deployment_symbols = AlpacaDataClient._seed_cache_with_deployment_symbols.__get__(host)
    return host


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_real_pool_read_REFUSES_inside_a_running_loop_and_succeeds_outside(real_symbol_sets):
    """The two behaviours the fix depends on, proven on the REAL `_pool_symbols`: outside a loop it
    reads the pool (through the fake asyncpg); inside one it raises the exact RuntimeError paper
    logged. If either stops being true, the tests below measure a different function."""
    assert ibkr._pool_symbols() == POOL
    ibkr._POOL_CACHE = None

    async def _inside():
        with pytest.raises(RuntimeError, match="inside a running event loop"):
            ibkr._pool_symbols()

    asyncio.run(_inside())


def test_FIXTURE_the_real_tradeable_set_FALLS_BACK_to_the_declared_universe_inside_a_loop(real_symbol_sets):
    """The paper log line, reproduced: inside the loop `_tradeable_symbols` loses the pool and keeps
    declared ∪ lanes — 4 names, not 8. That is what #1063 seeded."""
    async def _inside():
        return ibkr._tradeable_symbols()

    inside = asyncio.run(_inside())
    assert set(inside) == set(DECLARED) | set(LANES), inside
    assert not (set(POOL) & set(inside))


# ------------------------------------------------------------------ the seam -------------------

def test_the_seed_set_is_computed_at_BUILD_time_and_carries_the_POOL(real_symbol_sets):
    """`build_data` runs in the builders, before any loop: the pool is readable there.
    The spec's config must carry the full deployment set — pool included. Red on 97fd81b: the config
    has no seed set at all."""
    spec = build_data({"feed": "sip"})
    seeds = set(getattr(spec.config, "seed_symbols", ()))
    assert set(POOL) <= seeds, f"the pool is not in the build-time seed set: {sorted(seeds)}"
    assert seeds == set(POOL) | set(DECLARED) | set(LANES) | set(REFERENCE)


def test_connect_INSIDE_a_running_loop_seeds_the_pool_names_from_the_build_time_set(real_symbol_sets):
    """THE DEFECT AT THE SEAM. Build outside the loop, connect inside it — the production order.
    On 97fd81b `_connect` read the set in-loop, the pool was refused, and BIIB/BOX/DBX/SLB were not
    seeded (the boot line stayed '16 of 117')."""
    spec = build_data({"feed": "sip"})          # outside the loop, as the builders run
    host = _host(spec.config)

    asyncio.run(AlpacaDataClient._connect(host))            # inside the loop, as the node runs

    seeded = {e.id.symbol.value for e in host.handed}
    missing = sorted(set(POOL) - seeded)
    assert not missing, (
        f"pool names not seeded when connect runs inside the loop: {missing} — the set was read "
        f"in-loop and the pool refused (paper 2026-09-13 08:19:32Z) (#1057)")
    assert seeded == set(POOL) | set(DECLARED) | set(LANES) | set(REFERENCE)
    assert not any("could not be read" in s for s in host.said), host.said


def test_connect_reads_NO_symbol_set_of_its_own(real_symbol_sets, monkeypatch):
    """The property, not the instance: the seeding path must not call anything that refuses in-loop.
    With the pool read made to RAISE on any call after build, `_connect` still seeds the pool —
    because it never asks. On 97fd81b it asked, every boot."""
    spec = build_data({"feed": "sip"})

    def _boom():
        raise AssertionError("_connect read a symbol set in-loop")

    for name in ("_tradeable_symbols", "_reference_symbols", "_pool_symbols"):
        monkeypatch.setattr(ibkr, name, _boom)     # monkeypatch, never setattr: a bare setattr here leaked into the next test
    host = _host(spec.config)

    asyncio.run(AlpacaDataClient._connect(host))

    assert set(POOL) <= {e.id.symbol.value for e in host.handed}


def test_an_UNREADABLE_pool_at_build_time_is_TWO_rows_one_cause_and_the_rest_of_the_set_still_seeds(real_symbol_sets, monkeypatch, caplog):
    """Postgres down at build: the declared universe, the lanes and the references still reach the
    cache; the pool's absence is `_tradeable_symbols`' own fallback ERROR (ibkr.py) — and because
    `_POOL_CACHE` holds the EXCEPTION for the rest of the boot (ibkr.py:643), the exec client's read
    later in the same boot sees the same answer: declared universe only, on BOTH clients, until a
    restart. Two rows, one cause — both must be readable, neither may be the silent narrowing."""
    import logging
    import sys

    class _Down:
        async def connect(self, dsn, timeout=None):
            raise OSError("postgres unreachable")

    monkeypatch.setitem(sys.modules, "asyncpg", _Down())
    monkeypatch.setattr(ibkr, "_POOL_CACHE", None)
    with caplog.at_level(logging.ERROR):
        spec = build_data({"feed": "sip"})
    fallback = [r for r in caplog.records if "symbol pool could not be read" in r.getMessage()]
    assert len(fallback) == 1 and "postgres unreachable" in fallback[0].getMessage(), [r.getMessage() for r in caplog.records]
    assert isinstance(ibkr._POOL_CACHE, Exception), "the failed read is not cached for the boot — the exec builder would retry a dead Postgres"
    host = _host(spec.config)
    asyncio.run(AlpacaDataClient._connect(host))
    seeded = {e.id.symbol.value for e in host.handed}
    assert seeded == set(DECLARED) | set(LANES) | set(REFERENCE)
    assert not (set(POOL) & seeded)


def test_the_REFERENCE_set_failing_leaves_the_compass_names_ABSENT_and_the_pool_seeded(real_symbol_sets, monkeypatch):
    """Half a set is half a set, the other way round: the tradeable set reads, the references do
    not — the pool seeds, the compass axes are ABSENT (never quietly included), one ERROR names it."""
    import logging

    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: (_ for _ in ()).throw(RuntimeError("compass universe unreadable")))
    spec = build_data({"feed": "sip"})
    host = _host(spec.config)
    asyncio.run(AlpacaDataClient._connect(host))
    seeded = {e.id.symbol.value for e in host.handed}
    assert set(POOL) <= seeded
    assert not (set(REFERENCE) & seeded), "compass names seeded from a set that could not be read"


def test_END_TO_END_the_factory_built_client_seeds_the_pool_from_the_spec_it_was_built_from(real_symbol_sets, monkeypatch):
    """Kills the mutant where the factory constructs the client on a FRESH default config: the spec's
    config → `AlpacaLiveDataClientFactory.create` → the real client → `_connect` (with its http,
    ws and provider swapped for doubles on the INSTANCE) must seed the pool names."""
    from api.providers.alpaca.data_client import AlpacaLiveDataClientFactory
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.model.identifiers import TraderId

    spec = build_data({"feed": "sip"})
    loop = asyncio.new_event_loop()
    try:
        clock = LiveClock()
        msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
        client = AlpacaLiveDataClientFactory.create(loop=loop, name="ALPACA", config=spec.config,
                                                    msgbus=msgbus, cache=Cache(), clock=clock)
        client._http = _Awaitable()
        client._ws = _Awaitable()
        client._instrument_provider = _Provider()
        handed = []
        client._handle_data = lambda d: handed.append(d)
        loop.run_until_complete(client._connect())
    finally:
        loop.close()
    seeded = {e.id.symbol.value for e in handed}
    assert set(POOL) <= seeded, f"the factory-built client did not seed the pool: {sorted(seeded)}"


def test_the_seeding_step_names_no_symbol_set_function_and_the_builder_names_them():
    """AST, the class guard: `_seed_cache_with_deployment_symbols` must not reference
    `_tradeable_symbols` / `_pool_symbols` / `_reference_symbols`; `build_data` must."""
    import ast
    import inspect
    import textwrap

    def names(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return ({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                | {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)})

    from api.providers.alpaca.data_client import deployment_seed_symbols

    readers = {"_tradeable_symbols", "_pool_symbols", "_reference_symbols"}
    assert not (names(AlpacaDataClient._seed_cache_with_deployment_symbols) & readers), "the seeding step reads a symbol set in-loop"
    assert not (names(AlpacaDataClient._connect) & readers), "_connect reads a symbol set in-loop"
    assert "deployment_seed_symbols" in names(build_data), "build_data does not compute the seed set"
    assert {"_tradeable_symbols", "_reference_symbols"} <= names(deployment_seed_symbols), "the build-time helper does not read the shared sets"
