"""Resolving the rotation universe must cost NO asset fetch at all (#598, then #622).

SUPERSEDED IN THE RIGHT DIRECTION. This file pinned "one fetch, not one per ticker" — the fix for the
wedge described below. #622 removed the fetch entirely: venues are resolved from the instruments the
attached adapter already loaded, so the count that used to be 27 and then 1 is now 0.

The property is strictly stronger, and the original number is kept below because it is what made the
cost legible.

ORIGINALLY: resolving the rotation universe must cost ONE asset fetch, not one per ticker (#598).

THE WEDGE. `_refresh_rotation` resolved instrument ids like this:

    iids = {t: i for t in wanted if (i := self._rotation_instrument_id(t)) is not None}

one call per ticker, each reaching `_instrument_ids([t])`, which does
`TradableUniverse().exchanges()` — CONSTRUCTING A FRESH INSTANCE, so the TTL cache on that object can
never apply. Alpaca's `/v2/assets?status=active` is 6.4 MB.

Measured on the live paper stack, 2026-08-27:

    rotation_tickers()          27
    27 x 6.4 MB               173 MB per refresh
    at ~3.0 s per fetch        ~1.4 minutes

and all of it runs SYNCHRONOUSLY ON THE NAUTILUS MAIN THREAD (`node.py:298` -> `_refresh_rotation`).
The rotation tick fires again before it finishes, so the node never catches up: py-spy showed
MainThread parked in `ssl.read` / `create_connection` with the log frozen, 1.4% CPU, nothing
published, on BOTH tenants. The UI showed "no feed" and the account frame never appeared.

`except Exception: return None` in `_rotation_instrument_id` meant none of it was ever reported.
"""

from __future__ import annotations

from types import SimpleNamespace


class _CountingUniverse:
    """Counts fetches the way the real object would: `_fresh()` is per-instance, so a caller that
    rebuilds the object pays full price every time. The double must be able to SHOW that."""

    fetches = 0

    def __init__(self) -> None:
        self._cached: dict[str, str] | None = None

    def exchanges(self) -> dict[str, str]:
        if self._cached is None:
            type(self).fetches += 1
            self._cached = {"AAPL": "NASDAQ", "BAC": "NYSE", "SPY": "ARCA"}
        return dict(self._cached)


def test_the_double_charges_per_INSTANCE_like_the_real_one():
    """Fixture property first. If the double cached globally, the defect could not be expressed and
    the assertion below would pass against the buggy code."""
    _CountingUniverse.fetches = 0
    _CountingUniverse().exchanges()
    _CountingUniverse().exchanges()
    assert _CountingUniverse.fetches == 2, "the double does not charge per instance"


def test_resolving_N_tickers_costs_NO_fetch(monkeypatch):
    """THE DEFECT, as arithmetic. 27 tickers must not mean 27 downloads of a 6.4 MB asset list."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.engine_node import UiFeedStrategy

    _CountingUniverse.fetches = 0
    monkeypatch.setattr("strategies.momentum._universe", _CountingUniverse(), raising=False)

    class _Cache:
        def instrument_ids(self):
            return [InstrumentId.from_str(x) for x in ("AAPL.XNAS", "BAC.XNYS", "SPY.ARCX", "ZZZ.XNAS")]

    # Bound to a plain host, not an instance: `UiFeedStrategy` is a Nautilus `Strategy`, so
    # `object.__new__` is refused and `cache` is a read-only Cython attribute that cannot be set on a
    # real one. Running the unbound function against a stand-in executes the SAME bytecode with only
    # the cache replaced — which is the single thing this test is about.
    host = SimpleNamespace(cache=_Cache())
    ids = UiFeedStrategy._resolve_from_cache(host, ["AAPL", "BAC", "SPY"])

    assert len(ids) == 3
    # AND THE VENUES ARE THE ADAPTER'S. An all-XNAS result would mean something defaulted, which is
    # the original bug this whole path exists to prevent.
    assert {str(i) for i in ids} == {"AAPL.XNAS", "BAC.XNYS", "SPY.ARCX"}
    assert _CountingUniverse.fetches == 0, (
        f"{_CountingUniverse.fetches} asset fetches for 3 tickers — resolution reads the instruments "
        f"the attached adapter already loaded, so a vendor fetch here means the Alpaca dependency is "
        f"back and an IBKR-only node cannot boot (#622)"
    )


def test_the_refresh_resolves_the_whole_universe_with_NO_vendor_call():
    """THE SEAM. A per-ticker loop in `_refresh_rotation` reintroduces the wedge even if
    `_instrument_ids` itself is perfect — the unit was never the problem, the call site was."""
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._refresh_rotation)
    assert "_rotation_instrument_id(t)" not in src, (
        "_refresh_rotation resolves one ticker at a time again; each call re-fetches the asset list"
    )
    # STRONGER THAN "ONE CALL" NOW. Collapsing 27 fetches to 1 made the wedge survivable, not
    # impossible: the fetch is a synchronous 6.4 MB HTTPS GET whose `timeout=60` guards socket IDLE
    # time rather than total time, so one trickling response still parks `node.run()`.
    assert "_instrument_ids(" not in src, (
        "_refresh_rotation calls the deleted Alpaca resolver again (#598/#622)"
    )
    assert "_rotation_ids_cached(" in src, "the tick should read the cache the worker fills"

    worker = inspect.getsource(UiFeedStrategy._rotation_ids_cached)
    assert "_resolve_from_cache(" in worker, "nothing resolves the universe at all"
    assert "threading.Thread" in worker, "the resolve must happen off the trading thread"
    assert "is_alive()" in worker, (
        "a slow fetch must not stack up behind itself across ticks — that amplification is what made "
        "the original defect fatal rather than merely slow"
    )



# ==================================================================================================
# The cache: refuse rather than wait, never stack workers, and NEVER touch the real resolver (#598).
# ==================================================================================================

class _Log:
    def __init__(self): self.lines = []
    def info(self, msg): self.lines.append(str(msg))
    def warning(self, msg): self.lines.append(str(msg))


class _Stub:
    """The attributes `_rotation_ids_cached` touches. `UiFeedStrategy` is a Cython Strategy subclass
    and cannot be instantiated bare, so the unbound method is driven against this — still the REAL
    code under test, not a reimplementation of it."""

    def __init__(self): self.log = _Log()


class _Id:
    def __init__(self, sym): self.symbol = type("S", (), {"value": sym})()


def _cached(stub, wanted, resolver=None):
    from api.engine_node import UiFeedStrategy

    return UiFeedStrategy._rotation_ids_cached(stub, wanted, resolver)


def test_a_COLD_cache_refuses_the_tick_instead_of_blocking_it():
    """THE WHOLE POINT. A cold cache returns None IMMEDIATELY — it does not wait for the fetch, and
    it does not report an unresolved universe as an empty one."""
    stub = _Stub()
    assert _cached(stub, ("AAPL", "BAC"), resolver=lambda w: []) is None
    assert any("off-thread" in m for m in stub.log.lines), "the refusal did not say why"


def test_the_worker_fills_the_cache_and_the_NEXT_tick_gets_it():
    """The refusal must be transient. If the worker never populated the cache, this would refuse
    forever and the compass would be permanently dark — a worse failure than the wedge."""
    stub = _Stub()
    assert _cached(stub, ("AAPL",), resolver=lambda w: [_Id("AAPL")]) is None
    stub._rotation_id_thread.join(timeout=5)
    got = _cached(stub, ("AAPL",), resolver=lambda w: [_Id("AAPL")])
    assert got is not None and set(got) == {"AAPL"}, f"the worker never filled the cache: {got}"


def test_a_WARM_cache_starts_no_worker():
    stub = _Stub()
    stub._rotation_iids = {"AAPL": "id"}
    stub._rotation_iids_key = ("AAPL",)
    assert _cached(stub, ("AAPL",), resolver=lambda w: []) == {"AAPL": "id"}
    assert getattr(stub, "_rotation_id_thread", None) is None, "a warm cache spawned a worker"


def test_a_CHANGED_universe_invalidates_the_cache():
    """The key is the ticker set — a pool that gains a symbol must not keep serving the old ids."""
    stub = _Stub()
    stub._rotation_iids = {"AAPL": "id"}
    stub._rotation_iids_key = ("AAPL",)
    assert _cached(stub, ("AAPL", "NVDA"), resolver=lambda w: []) is None


def test_a_RUNNING_worker_is_not_stacked_behind_itself():
    """A slow fetch must not spawn a second, third, nth worker on later ticks. That amplification is
    what turned a slow call into a wedged node."""
    stub = _Stub()

    class _Forever:
        def is_alive(self): return True

    stub._rotation_id_thread = _Forever()
    assert _cached(stub, ("AAPL",), resolver=lambda w: []) is None
    assert isinstance(stub._rotation_id_thread, _Forever), "a second worker was started"


def test_a_FAILING_resolver_leaves_the_last_good_ids_alone():
    """A transient failure must not blank a universe that resolved fine a minute ago."""
    stub = _Stub()
    stub._rotation_iids = {"AAPL": "id"}
    stub._rotation_iids_key = ("OLD",)

    def _boom(_w): raise RuntimeError("alpaca 503")

    assert _cached(stub, ("AAPL",), resolver=_boom) is None
    stub._rotation_id_thread.join(timeout=5)
    assert stub._rotation_iids == {"AAPL": "id"}, "a failed resolve wiped the cache"
