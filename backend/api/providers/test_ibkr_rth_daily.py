"""#875 — the DAY bar type is requested REGULAR-HOURS on IB while the live plane stays extended.

The installed Nautilus IB data client (1.229) forwards ONE config flag, `use_regular_trading_hours`,
into every historical request and every bar subscription (`data.py:625`, `:283`), so the node that
needs pre/post-market prices to move (the live 1-minute plane) gets DAILY bars with pre/post-market
folded in — highs up to 14% off the RTH bar, volume 26–40% off (#875 measurement). CRSISHORT was
measured on RTH daily bars; today it registers on staging2 only under a STATED deviation
(`CRSI_ACCEPT_EXTENDED_DAILY_BARS`).

The fix is one wrap at the IB CLIENT level — `install_rth_daily(client)` rewrites `use_rth` on
`get_historical_bars` and `subscribe_historical_bars` for DAY bar types and leaves every other bar
type on the config flag — installed by the cockpit's own data-client factory on what the vendor
factory returns (the #857/#929 pattern), and the `daily_bars_cover` declaration follows the wrap.

THE SEAM IS DRIVEN, NOT THE HELPER: the tests run the INSTALLED `_request_bars` and `_subscribe_bars`
on a data-client instance whose `_client` is a recorder, and read what reaches the client. A double
that recorded at the helper would pass with the wrap installed on nothing.

Seen red 2026-09-11 against 80b5b7f: `api.providers.ibkr_rth_daily` does not exist; the factory in
`build_data` is the vendor's; `daily_bars_cover` reads "extended".
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

pytest.importorskip("nautilus_trader.adapters.interactive_brokers.data")

from nautilus_trader.adapters.interactive_brokers.data import InteractiveBrokersDataClient  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402

DAY = BarType.from_str("RGTI.XNAS-1-DAY-LAST-EXTERNAL")
MINUTE = BarType.from_str("RGTI.XNAS-1-MINUTE-LAST-EXTERNAL")
FIVE_SEC = BarType.from_str("RGTI.XNAS-5-SECOND-LAST-EXTERNAL")


# ----------------------------------------------------------------------------- doubles ---------
class _Recorder:
    """The IB client as the data client reaches it: the two bar methods that carry `use_rth`, by
    keyword, with the installed signature's parameter names (pinned below)."""

    _request_timeout_secs = 5

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get_historical_bars(self, bar_type, contract, use_rth, end_date_time, duration,
                                  timeout=60):
        # POSITIONAL-OR-KEYWORD, as installed: `get_historical_bars_chunked` calls this one
        # POSITIONALLY. A keyword-only double here passed a wrap that never rewrote the request path.
        self.calls.append(("get_historical_bars", {"bar_type": bar_type, "use_rth": use_rth}))
        return []

    async def subscribe_historical_bars(self, bar_type, contract, use_rth, handle_revised_bars,
                                        params=None):
        self.calls.append(("subscribe_historical_bars", {"bar_type": bar_type, "use_rth": use_rth}))

    async def subscribe_realtime_bars(self, bar_type, contract, use_rth):
        self.calls.append(("subscribe_realtime_bars", {"bar_type": bar_type, "use_rth": use_rth}))

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]


class _Log:
    def error(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass


class _Bus:
    def publish(self, *a, **k): pass
    def send(self, *a, **k): pass
    def response(self, *a, **k): pass


class _Contracts:
    def __init__(self):
        self.contract = {DAY.instrument_id: object()}


class _Unconstructed(InteractiveBrokersDataClient):
    """The INSTALLED data client's methods, on an instance that skipped `__init__` (it needs a loop,
    a bus, a cache and a live IB connection). `_log` is a read-only Cython attribute on Component,
    so it is shadowed here by a property — found first in the MRO."""

    _log = property(lambda self: _Log())
    # `instrument_provider` and `_msgbus` are read-only on the base too; shadowed the same way. The
    # bus takes the empty DataResponse the client sends when the recorder returns no bars.
    instrument_provider = property(lambda self: self._kumo_contracts)
    _msgbus = property(lambda self: _Bus())

    def _handle_bars(self, *a, **k):
        pass


def _data_client(*, use_regular_trading_hours=False, client=None) -> InteractiveBrokersDataClient:
    """Exactly the attributes `_request_bars`/`_subscribe_bars` read, and nothing else invented."""
    dc = _Unconstructed.__new__(_Unconstructed)
    dc._client = client if client is not None else _Recorder()
    dc._use_regular_trading_hours = use_regular_trading_hours
    dc._handle_revised_bars = True
    dc._kumo_contracts = _Contracts()
    return dc


def _request(bar_type):
    import pandas as pd
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.data.messages import RequestBars

    end = pd.Timestamp("2026-09-10T20:00:00Z")
    return RequestBars(bar_type=bar_type, start=end - pd.Timedelta(days=200), end=end, limit=0,
                       client_id=None, venue=bar_type.instrument_id.venue, callback=lambda *_: None,
                       request_id=UUID4(), ts_init=0, params={})


def _subscribe(bar_type):
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.data.messages import SubscribeBars

    return SubscribeBars(bar_type=bar_type, client_id=None, venue=bar_type.instrument_id.venue,
                         command_id=UUID4(), ts_init=0, params={})


# ------------------------------------------------------------------ fixture properties ---------
def test_the_INSTALLED_client_still_forwards_the_one_config_flag_into_both_paths():
    """The seam this fix sits on. If a Nautilus bump gives `_request_bars` a per-request `use_rth`,
    this turns red and the wrap can go."""
    src_req = inspect.getsource(InteractiveBrokersDataClient._request_bars)
    src_sub = inspect.getsource(InteractiveBrokersDataClient._subscribe_bars)
    assert "use_rth=self._use_regular_trading_hours" in src_req
    assert src_sub.count("use_rth=self._use_regular_trading_hours") == 2, "realtime + historical"
    assert "params" not in src_req.split("get_historical_bars_chunked(")[1].split(")")[0]


def test_the_recorder_carries_the_installed_clients_parameter_names():
    """A double whose keywords drift from the installed client would record calls production
    could not make."""
    from nautilus_trader.adapters.interactive_brokers.client import InteractiveBrokersClient

    for name in ("get_historical_bars", "subscribe_historical_bars", "subscribe_realtime_bars"):
        real = [(p.name, p.kind) for p in inspect.signature(getattr(InteractiveBrokersClient, name)).parameters.values() if p.name != "self"]
        mine = [(p.name, p.kind) for p in inspect.signature(getattr(_Recorder, name)).parameters.values() if p.name != "self"]
        assert real == mine, (name, real, mine)   # names AND kinds: the request path calls positionally


def test_the_unwrapped_client_sends_the_config_flag_for_DAY_bars_too():
    """Fixture property: without the wrap, a DAY request under `use_regular_trading_hours=False`
    reaches IB with useRTH=0 — the defect, reachable through the real `_request_bars`."""
    dc = _data_client(use_regular_trading_hours=False)
    asyncio.run(dc._request_bars(_request(DAY)))
    assert dc._client.of("get_historical_bars") == [{"bar_type": DAY, "use_rth": False}]


# ------------------------------------------------------------------------- the wrap ------------
def test_a_DAY_request_reaches_IB_with_useRTH_and_a_MINUTE_request_keeps_the_config_flag():
    from api.providers.ibkr_rth_daily import install_rth_daily

    dc = _data_client(use_regular_trading_hours=False)
    install_rth_daily(dc._client)

    asyncio.run(dc._request_bars(_request(DAY)))
    asyncio.run(dc._request_bars(_request(MINUTE)))

    got = {str(kw["bar_type"]): kw["use_rth"] for kw in dc._client.of("get_historical_bars")}
    assert got == {str(DAY): True, str(MINUTE): False}, got


def test_a_DAY_SUBSCRIPTION_is_RTH_and_a_MINUTE_one_is_not_and_realtime_bars_are_untouched():
    """Both paths, one mechanism: the keepUpToDate DAY subscription pushes the forming/last daily
    bar, and if it stayed extended the lane would hold two derivations of yesterday's bar that
    disagree (the RTH history and the extended push) — whichever arrived last would win."""
    from api.providers.ibkr_rth_daily import install_rth_daily

    dc = _data_client(use_regular_trading_hours=False)
    install_rth_daily(dc._client)

    asyncio.run(dc._subscribe_bars(_subscribe(DAY)))
    asyncio.run(dc._subscribe_bars(_subscribe(MINUTE)))
    asyncio.run(dc._subscribe_bars(_subscribe(FIVE_SEC)))

    hist = {str(kw["bar_type"]): kw["use_rth"] for kw in dc._client.of("subscribe_historical_bars")}
    assert hist == {str(DAY): True, str(MINUTE): False}, hist
    assert dc._client.of("subscribe_realtime_bars") == [{"bar_type": FIVE_SEC, "use_rth": False}]


def test_the_wrap_installs_ONCE_and_says_so():
    from api.providers.ibkr_rth_daily import install_rth_daily, is_rth_daily_installed

    rec = _Recorder()
    assert is_rth_daily_installed(rec) is False
    assert install_rth_daily(rec) is rec and is_rth_daily_installed(rec) is True
    first = rec.get_historical_bars
    install_rth_daily(rec)
    assert rec.get_historical_bars is first, "a second install must not stack a second wrap"


def test_rth_for_decides_by_AGGREGATION_not_by_bar_type_string():
    """A DAY bar on any venue, any price type, any aggregation source is RTH; nothing else is."""
    from api.providers.ibkr_rth_daily import rth_for

    assert rth_for(DAY, default=False) is True
    assert rth_for(BarType.from_str("GLD.ARCX-1-DAY-MID-INTERNAL"), default=False) is True
    assert rth_for(MINUTE, default=False) is False
    assert rth_for(BarType.from_str("RGTI.XNAS-1-HOUR-LAST-EXTERNAL"), default=False) is False
    assert rth_for(MINUTE, default=True) is True, "the default is the config flag, passed through"


# ---------------------------------------------------- the factory and the declaration ----------
def test_the_REPO_FACTORY_returns_a_data_client_whose_IB_client_carries_the_wrap(monkeypatch):
    """Driven for real: the vendor factory is called (delegated to, never reproduced) and the wrap is
    installed on the client it returns. Bitten by: a factory that subclasses and forgets to wrap."""
    import api.providers.ibkr_rth_daily as mod
    from nautilus_trader.adapters.interactive_brokers import factories

    built = _data_client()

    def _vendor_create(loop, name, config, msgbus, cache, clock):
        return built

    monkeypatch.setattr(factories.InteractiveBrokersLiveDataClientFactory, "create",
                        staticmethod(_vendor_create))
    out = mod.RthDailyIBDataClientFactory.create(loop=None, name="IB", config=None, msgbus=None,
                                                 cache=None, clock=None)
    assert out is built and mod.is_rth_daily_installed(out._client)


def test_build_data_declares_rth_daily_bars_AND_uses_the_factory_that_makes_it_true(monkeypatch):
    """Two derivations of one fact pinned together: the declaration `daily_bars_cover` and the
    factory that installs the wrap. Either alone is the comment-that-reads-as-safety class."""
    from api.providers import ibkr
    from api.providers.ibkr_rth_daily import RthDailyIBDataClientFactory

    monkeypatch.setattr(ibkr, "_ensure_info_sanitiser", lambda: None)
    monkeypatch.setattr(ibkr, "_data_contracts", lambda: [])
    monkeypatch.setattr(ibkr, "_symbol_mic_overrides", lambda: {})
    spec = ibkr.build_data({})
    assert spec.factory is RthDailyIBDataClientFactory
    assert spec.daily_bars_cover == "rth"
    assert spec.config.use_regular_trading_hours is False, "the live plane stays extended"


def test_the_declaration_cannot_say_rth_while_the_factory_is_the_vendors():
    """The pin, by source: the two lines sit together and name each other."""
    from api.providers import ibkr

    src = inspect.getsource(ibkr.build_data)
    assert 'daily_bars_cover="rth"' in src and "RthDailyIBDataClientFactory" in src
    assert 'daily_bars_cover="extended"' not in src
