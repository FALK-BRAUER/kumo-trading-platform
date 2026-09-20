"""The Alpaca data client SEEDS the cache at connect with every instrument the deployment names (#1057).

THE DEFECT, measured on an Alpaca paper instance's 2026-09-13 05:39Z boot (lead): the data client loads 13,435 Alpaca
definitions into the PROVIDER (`Loaded 13435 Alpaca instruments`); the lanes resolve against the
CACHE (`symbol_resolution._resolve_symbols` -> `cache.instrument_ids()`); the cache holds 406 — only
names some earlier boot REQUESTED, because on Alpaca a definition reaches the cache only through
`_request_instrument`, which a lane cannot issue for a symbol it failed to resolve. So every pool
name never requested before is `16 of 117 symbols have no instrument on this venue and will NOT be
traded` on every boot, and every slot journals `bars missing for 16/117`. Eleven of the sixteen are
ordinary listed names Alpaca serves (BIIB, BOX, DBX, GPRK, NSIT, OKE, SLB, SUNC, UGP, UPRO, XPRO);
the other five are non-tickers (ingestion, its own ticket).

IBKR fixed the same defect with `load_contracts` for the pool and the enabled lanes' universes at
boot (#511, #871) and pushes every loaded instrument into the cache inside `_connect` (the shipped
adapter's `data.py:147`). Alpaca never got the equivalent. This mirrors it, over ONE symbol set —
the IBKR module's own `_tradeable_symbols` and `_reference_symbols`, imported, never a second list.

The REAL `AlpacaDataClient._connect` is driven, bound to a host carrying only what it touches: a
provider double loaded with equities the way `load_all_async` loads them, an http and a ws double,
a recording `_handle_data`, and the seed set the build step produced (as `__init__` stores it) — the seed set is computed by
`build_data` OUTSIDE the loop (`test_seed_set_is_computed_outside_the_loop.py` proves why: the
first version read it inside `_connect`, where the pool read refuses, and seeded 270 names and none
of the pool's on paper 2026-09-13 08:19Z). Every test here was seen red on a1f4ecd for the reason
its docstring names.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from api.providers.alpaca.config import AlpacaDataClientConfig
from api.providers.alpaca.data_client import AlpacaDataClient, deployment_seed_symbols

#: The eleven real names from the ticket, the exchange Alpaca reports for each (`/v2/assets`), and
#: two names that are NOT in the pool — the cache must not be flooded with the whole catalogue.
POOL = {"BOX": "XNYS", "DBX": "XNAS", "GPRK": "XNYS", "NSIT": "XNAS", "OKE": "XNYS",
        "SLB": "XNYS", "SUNC": "XNYS", "UGP": "XNYS", "UPRO": "ARCX", "XPRO": "XNYS"}
REFERENCE = {"SPY": "ARCX", "IEI": "XNAS"}         # compass axes (#1041) — graded, never held
NOT_NAMED = {"ZZZQ": "XNAS", "AAPL": "XNAS"}        # in the catalogue, in no set: must NOT be seeded
NON_TICKERS = ("BLLLN", "IQVIA", "JEPO")            # the ingestion fragments: not in the catalogue
#: A pool name the catalogue lists under TWO venues (Alpaca is multi-venue): BOTH definitions are
#: seeded — the venue choice belongs to the resolver (`prefer_primary_exchange`), not the seeder.
TWO_MIC = ("BIIB", ("XNAS", "XNYS"))
#: The probe the under-seeding test reads is placed LAST in the catalogue order, so a seeder that
#: walks a truncated `list_all()` cannot pass by reaching it early (review).
PROBE_LAST = "BIIB"


def _equity(sym: str, mic: str) -> Equity:
    iid = InstrumentId(Symbol(sym), Venue(mic))
    return Equity(instrument_id=iid, raw_symbol=Symbol(sym), currency=USD, price_precision=2,
                  price_increment=Price.from_str("0.01"), lot_size=Quantity.from_int(1), ts_event=0, ts_init=0)


class _Provider:
    """`AlpacaInstrumentProvider` as `_connect` uses it: `load_all_async` fills it, `list_all` /
    `find` read it. Loaded with the catalogue — pool, references, names nobody asked for, and one
    symbol under two venues — with the probe symbol LAST."""

    def __init__(self, catalogue: dict[str, str]):
        self._by_id = {}
        self.loaded = False
        self._catalogue = catalogue

    async def load_all_async(self, filters=None):
        self.loaded = True
        for sym, mic in self._catalogue.items():
            e = _equity(sym, mic)
            self._by_id[e.id] = e
        sym, mics = TWO_MIC
        for mic in mics:                                   # the two-venue name, last of all
            e = _equity(sym, mic)
            self._by_id[e.id] = e

    @property
    def count(self):
        return len(self._by_id)

    def list_all(self):
        return list(self._by_id.values())

    def find(self, iid):
        return self._by_id.get(iid)


class _Awaitable:
    async def connect(self):
        self.connected = True

    async def start(self):
        self.started = True


class _Host:
    """Only what `_connect` reads. Binding the REAL coroutine; a stand-in body would be the drifted
    double this file exists to avoid. `_config` carries the build-time seed set, as the factory's
    client does."""

    def __init__(self, catalogue: dict[str, str], seed_symbols: tuple[str, ...] = ()):
        self._http = _Awaitable()
        self._ws = _Awaitable()
        self._seed_symbols = tuple(AlpacaDataClientConfig(seed_symbols=tuple(seed_symbols)).seed_symbols)
        self._instrument_provider = _Provider(catalogue)
        self.handed: list[Equity] = []
        self.said: list[str] = []
        self._log = types.SimpleNamespace(
            info=lambda m, *a, **k: self.said.append(str(m)),
            warning=lambda m, *a, **k: self.said.append("WARN " + str(m)),
            error=lambda m, *a, **k: self.said.append("ERROR " + str(m)),
        )

    def _handle_data(self, data):
        self.handed.append(data)

    # The REAL seeding step, bound like `_connect` itself — a hand-written stand-in would be the
    # drifted double this file exists to avoid.
    _seed_cache_with_deployment_symbols = AlpacaDataClient._seed_cache_with_deployment_symbols


def _catalogue() -> dict[str, str]:
    return {**POOL, **REFERENCE, **NOT_NAMED}


def _pool() -> tuple[str, ...]:
    return tuple(sorted(POOL)) + (TWO_MIC[0],)


def _connect(monkeypatch, catalogue=None, tradeable=None, reference=None) -> _Host:
    """Drive the real `_connect` with the deployment's symbol sets pinned at their one home — the
    SAME functions the IBKR contract loader reads — computed at BUILD time by
    `deployment_seed_symbols()` (outside any loop) and handed to the client through its config."""
    import api.providers.ibkr as ibkr

    monkeypatch.setattr(ibkr, "_tradeable_symbols", lambda: _pool() if tradeable is None else tuple(tradeable))
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: tuple(sorted(REFERENCE if reference is None else reference)))
    host = _Host(_catalogue() if catalogue is None else catalogue, seed_symbols=deployment_seed_symbols())
    asyncio.run(AlpacaDataClient._connect(host))
    return host


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_provider_holds_the_pool_names_and_the_lanes_read_the_cache_not_the_provider(monkeypatch):
    """The two halves of the defect are representable: the provider KNOWS BIIB after load (so the
    fix can find it) and the lane resolver reads `cache.instrument_ids()` (so provider-only is
    invisible to it). If either stops being true the tests below measure something else."""
    import inspect

    from kumo_strategies.runtime.nautilus import symbol_resolution

    host = _connect(monkeypatch)
    assert host._instrument_provider.loaded and host._instrument_provider.count == len(_catalogue()) + len(TWO_MIC[1])
    assert host._instrument_provider.find(InstrumentId(Symbol("BIIB"), Venue("XNAS"))) is not None
    assert host._instrument_provider.list_all()[-1].id.symbol.value == PROBE_LAST, "the probe is not last in the catalogue"
    assert "instrument_ids()" in inspect.getsource(symbol_resolution), (
        "the lane resolver no longer reads the cache's instrument ids — re-derive the defect")


# ------------------------------------------------------------------ the seeding ----------------

def test_connect_hands_every_POOL_instrument_to_the_cache(monkeypatch):
    """THE DEFECT. On a1f4ecd `_connect` loads the provider and hands NOTHING to the cache; the
    eleven real pool names stay 'no instrument on this venue' on every boot."""
    host = _connect(monkeypatch)

    seeded = {e.id.symbol.value: e.id.venue.value for e in host.handed if isinstance(e, Equity)}
    missing = sorted(set(_pool()) - set(seeded))
    assert not missing, (
        f"{len(missing)} pool names never reach the cache at connect: {missing} — every boot reports "
        f"them as 'no instrument on this venue' and every slot as 'bars missing' (#1057)")
    assert all(seeded[s] == POOL[s] for s in POOL), "seeded under a different venue than the provider's"
    assert PROBE_LAST in seeded, "the LAST catalogue entry was not seeded — a truncated walk"


def test_a_symbol_listed_under_TWO_venues_is_seeded_under_BOTH(monkeypatch):
    """The venue choice belongs to the resolver (`prefer_primary_exchange`), not to the seeder:
    seeding one MIC would silently pre-decide it. Both definitions reach the cache."""
    host = _connect(monkeypatch)
    sym, mics = TWO_MIC
    venues = sorted(e.id.venue.value for e in host.handed if e.id.symbol.value == sym)
    assert venues == sorted(mics), venues


def test_connect_seeds_the_compass_REFERENCE_names_too(monkeypatch):
    """The grading universe rides the same set (IBKR's `_data_contracts` = tradeable ∪ reference):
    #1041's seven compass tickers are the same defect one plane over."""
    host = _connect(monkeypatch)
    seeded = {e.id.symbol.value for e in host.handed}
    assert set(REFERENCE) <= seeded, sorted(set(REFERENCE) - seeded)


def test_connect_does_NOT_flood_the_cache_with_the_whole_catalogue(monkeypatch):
    """13,435 definitions per boot into a durable Redis cache is not the fix; only the deployment's
    names are. A name in the catalogue but in no set stays in the provider, served on request."""
    host = _connect(monkeypatch)
    seeded = {e.id.symbol.value for e in host.handed}
    assert not (set(NOT_NAMED) & seeded), sorted(set(NOT_NAMED) & seeded)
    # Count by DEFINITIONS: every named symbol contributes each venue the provider lists it under.
    wanted = set(_pool()) | set(REFERENCE)
    expected = sum(1 for e in host._instrument_provider.list_all() if e.id.symbol.value in wanted)
    assert len(host.handed) == expected == len(POOL) + len(REFERENCE) + len(TWO_MIC[1])
    assert seeded == wanted


def test_a_named_symbol_the_provider_does_NOT_know_is_reported_by_name_not_silently_skipped(monkeypatch):
    """The five ingestion fragments. Absence must not be readable as 'seeded': one line naming
    every symbol the deployment asked for that Alpaca has no definition for."""
    host = _connect(monkeypatch, tradeable=_pool() + NON_TICKERS)
    seeded = {e.id.symbol.value for e in host.handed}
    assert set(POOL) <= seeded
    assert not (set(NON_TICKERS) & seeded)
    named = [s for s in host.said if all(t in s for t in NON_TICKERS)]
    assert named, f"no line names the {len(NON_TICKERS)} unknown symbols: {host.said}"
    assert any(s.startswith(("WARN", "ERROR")) for s in named), "the unknown names are reported below WARNING"


def test_an_unreadable_symbol_set_does_NOT_stop_the_data_client_connecting(monkeypatch):
    """The pool read can fail (Postgres down at boot); the feed must still come up — the exec's
    `_tradeable_symbols` raises on EMPTY by design, and that refusal belongs to the exec side.
    Here: one ERROR line at BUILD, the references still seed, the websocket starts, nothing raised."""
    import api.providers.ibkr as ibkr

    monkeypatch.setattr(ibkr, "_tradeable_symbols", lambda: (_ for _ in ()).throw(RuntimeError("pool unreadable")))
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: tuple(REFERENCE))
    seeds = deployment_seed_symbols()                       # build time: the tradeable set fails loudly
    host = _Host(_catalogue(), seed_symbols=seeds)

    asyncio.run(AlpacaDataClient._connect(host))

    assert host._ws.started and host._http.connected
    # HALF-SEEDED READS AS HALF-SEEDED: the references still land, nothing from the failed set.
    seeded = {e.id.symbol.value for e in host.handed}
    assert seeded == set(REFERENCE)


def test_an_EMPTY_build_time_set_is_its_own_ERROR_not_a_quiet_no_op():
    """Nothing to seed must never read as seeded: an empty set (every read failed at build) is
    reported as such at ERROR, and the client still connects."""
    host = _Host(_catalogue(), seed_symbols=())

    asyncio.run(AlpacaDataClient._connect(host))

    assert host.handed == [] and host._ws.started
    assert any(s.startswith("ERROR") and "EMPTY" in s for s in host.said), host.said


def test_the_ws_starts_AFTER_the_seeding_so_a_live_frame_never_precedes_its_instrument(monkeypatch):
    """Ordering: connect http, load, SEED, then start the stream — a trade frame for a name whose
    definition is not yet in the cache is the `_parse_trade` refusal path."""
    import api.providers.ibkr as ibkr

    monkeypatch.setattr(ibkr, "_tradeable_symbols", lambda: _pool())
    monkeypatch.setattr(ibkr, "_reference_symbols", lambda: tuple(REFERENCE))
    order: list[str] = []
    host = _Host(_catalogue(), seed_symbols=deployment_seed_symbols())

    async def _start():
        order.append("ws")
        host._ws.started = True

    host._ws.start = _start
    original = host._handle_data
    host._handle_data = lambda d: (order.append("seed"), original(d))
    asyncio.run(AlpacaDataClient._connect(host))

    assert "seed" in order and order.index("ws") > max(i for i, o in enumerate(order) if o == "seed")


# ------------------------------------------------------------------ one list, not two ----------

def test_the_alpaca_seed_set_is_the_IBKR_modules_own_functions_not_a_copy():
    """ONE derivation of 'what the deployment names'. The build-time helper must read the symbol set
    through `api.providers.ibkr._tradeable_symbols` / `_reference_symbols` — the functions
    `load_contracts` reads — so the two providers cannot drift on which names get an instrument."""
    import ast
    import inspect
    import textwrap

    connect_src = inspect.getsource(AlpacaDataClient._connect)
    connect_names = {n.attr for n in ast.walk(ast.parse(textwrap.dedent(connect_src))) if isinstance(n, ast.Attribute)}
    assert "_seed_cache_with_deployment_symbols" in connect_names, "_connect no longer calls the seeding step"
    src = inspect.getsource(deployment_seed_symbols)
    tree = ast.parse(textwrap.dedent(src))
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert {"_tradeable_symbols", "_reference_symbols"} <= (consts | attrs), (
        f"the build-time helper does not read the shared symbol-set functions; seen: {sorted(consts | attrs)[:30]}")
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "api.providers" for a in n.names}
    assert "ibkr" in imported, "the symbol set is not imported from api.providers.ibkr — a second list is possible"
