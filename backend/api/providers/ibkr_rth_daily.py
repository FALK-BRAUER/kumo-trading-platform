"""#875 — DAY bars are requested REGULAR-HOURS on IB; every other bar type keeps the config flag.

The installed Nautilus IB data client (1.229) reads ONE flag, `use_regular_trading_hours`, and
forwards it into every historical request and every bar subscription (`adapters/interactive_brokers/
data.py:625`, `:277`, `:283`). This node sets it False for the LIVE plane — 1-minute prices must move
pre/post-market — so the DAILY bars inherit extended hours as a side effect: highs up to 14% off the
RTH bar, volume 26–40% off (#875, measured against IB useRTH=1, which equals Alpaca 1Day to the cent).
CRSISHORT was measured on RTH daily bars; the screen (prior close, high/low, volume) and the exits
read those fields.

THE WRAP SITS ON THE IB CLIENT, NOT THE DATA CLIENT. Both data-client paths bottom out in two client
methods that take `use_rth` with the bar type beside it — `get_historical_bars` (the request path,
via `get_historical_bars_chunked`, called POSITIONALLY) and `subscribe_historical_bars` (the
keepUpToDate path, called by keyword). The call is bound to the installed signature and `use_rth`
rewritten wherever it landed — one mechanism for both, reproducing none of the vendor's code
(copying `_request_bars` into our tree is how an adapter breaks silently on the next bump). The
5-second `subscribe_realtime_bars` path is untouched: nothing daily goes through it.

WHY BOTH PATHS. The lane subscribes `1-DAY-LAST-EXTERNAL` AND requests its history. If only the
history were RTH, the keepUpToDate subscription would push yesterday's bar EXTENDED and the lane
would hold two derivations of one bar that disagree, with whichever arrived last winning.

WHAT THIS CHANGES FOR THE DISPLAY. The bar type is shared — "two lanes and the feed all want the same
daily series" — and RTH-ness is not part of a BarType, so the node's daily bars become RTH for every
consumer, the chart included. #616 chose extended daily bars for the display deliberately; that
choice is superseded here for the whole node and RECORDED (ticket + RUNBOOK), not left to be
discovered from a chart whose highs moved.

Installed by `RthDailyIBDataClientFactory`, which delegates construction to the vendor factory and
wraps what it returns — the #857/#929 pattern (`BudgetGatedIBExecClientFactory`).
"""
from __future__ import annotations

import copy
import functools
from decimal import Decimal
import inspect
import logging

from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
)
from nautilus_trader.model.enums import BarAggregation

_log = logging.getLogger("kumo.providers.ibkr_rth_daily")

_MARK = "_kumo_rth_daily_installed"
_WRAPPED = ("get_historical_bars", "subscribe_historical_bars")
_VOL_MARK = "_kumo_volume_floor_installed"


def rth_for(bar_type, *, default: bool) -> bool:
    """RTH for a DAY aggregation on any venue, price type or aggregation source; `default` (the
    node's config flag) for everything else. Decided by the ENUM, never by the bar type's string.
    WEEK and MONTH were considered and deliberately left on the default: nothing on this node
    subscribes them, and a rule nobody exercises is a rule nobody has measured (l21, #951 review)."""
    return True if bar_type.spec.aggregation == BarAggregation.DAY else bool(default)


def is_rth_daily_installed(client) -> bool:
    return bool(getattr(client, _MARK, False))


def install_rth_daily(client):
    """Wrap the two bar methods on `client` (bound, per instance — never the class, which is shared
    across every IB client in the process). "An instance" may be wider than it reads: the vendor
    caches IB clients per (host, port, client_id), so the exec client (client id 1) and the shortable
    plane (its own id) are DIFFERENT instances and untouched, while anything else sharing the DATA
    client's id would see the wrap — harmless, since only bar calls are wrapped and only DAY bars
    change. Idempotent: a second install is a no-op, so a factory called twice cannot stack two
    wraps. Returns the client."""
    if is_rth_daily_installed(client):
        return client
    for name in _WRAPPED:
        original = getattr(client, name)
        # THE DATA CLIENT CALLS ONE OF THESE POSITIONALLY (`get_historical_bars_chunked` passes
        # bar_type, contract, use_rth, end, duration as positionals) AND THE OTHER BY KEYWORD. A
        # rewrite that only looked at kwargs applied to the subscription and silently skipped the
        # request — the seam test caught it before commit. So the call is BOUND to the installed
        # signature first, and `use_rth` is rewritten wherever it landed.
        signature = inspect.signature(original)

        @functools.wraps(original)
        async def wrapped(*args, _original=original, _sig=signature, _name=name, **kwargs):
            bound = _sig.bind(*args, **kwargs)
            bar_type = bound.arguments.get("bar_type")
            if bar_type is None or "use_rth" not in bound.arguments:
                raise TypeError(f"ibkr_rth_daily: {_name} called without bar_type/use_rth — the "
                                f"installed client's signature moved; refusing to guess the RTH flag")
            bound.arguments["use_rth"] = rth_for(bar_type, default=bound.arguments["use_rth"])
            return await _original(*bound.args, **bound.kwargs)

        setattr(client, name, wrapped)
    setattr(client, _MARK, True)
    return client


def is_volume_floor_installed(client) -> bool:
    return bool(getattr(client, _VOL_MARK, False))


def install_volume_floor(client):
    """A daily bar whose IB volume is a POSITIVE FRACTION below one unit reaches the vendor with
    volume 0 instead of raising inside `make_qty` and taking the whole bar with it (#1084).

    MEASURED ibkr-paper 2026-09-14 09:07:10Z: 26 of HUBC.XNAS's ~33 daily bars raised at
    `market_data.py:1577` — `instrument.make_qty(0.49828404)` → "rounded to zero due to size
    increment 1" — and were dropped, prices included; seven landed. A lane ranking that name ran on
    seven bars, and nothing said so.

    FLOOR, NEVER ROUND: 3.7 becomes 3, 0.49 becomes 0; a floor cannot invent volume. -1 (IB's "no
    volume") and whole numbers pass untouched — the vendor already maps -1 to 0. The bar object is
    COPIED before the volume is changed: the vendor may hold the BarData for revisions. Same seam and
    same rules as `install_rth_daily` — per instance, idempotent, the vendor still owns conversion.
    Each floored bar is logged at INFO with the bar type and the value lost, so a symbol whose
    history is mostly sub-unit volumes is visible in the log until a health field exists (#1084).
    """
    if is_volume_floor_installed(client):
        return client
    original = getattr(client, "_ib_bar_to_nautilus_bar")
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def wrapped(*args, _original=original, _sig=signature, **kwargs):
        bound = _sig.bind(*args, **kwargs)
        bar = bound.arguments.get("bar")
        if bar is None:
            raise TypeError("ibkr volume floor: _ib_bar_to_nautilus_bar called without `bar` — the "
                            "installed client's signature moved; refusing to guess")
        volume = getattr(bar, "volume", None)
        # DECIMAL, NOT FLOAT. ibapi 10.45.1 decodes BarData.volume as `decimal.Decimal` (decoder.py,
        # three sites). The first cut guarded on `float` and was a NO-OP on the live wire — its
        # double fed a Python float, which is the "double more forgiving than production" shape the
        # engineering rules name. Caught in review before deploy (#1085); the tests feed Decimal now.
        if isinstance(volume, (float, Decimal)) and volume > -1 and volume != int(volume):
            floored = int(volume)  # toward zero == floor for a positive value
            _log.info("ibkr volume floor: %s bar.date=%s volume %s -> %s (sub-unit volume, bar kept)",
                      bound.arguments.get("bar_type"), getattr(bar, "date", "?"), volume, floored)
            bar = copy.copy(bar)
            bar.volume = floored
            bound.arguments["bar"] = bar
        return await _original(*bound.args, **bound.kwargs)

    setattr(client, "_ib_bar_to_nautilus_bar", wrapped)
    setattr(client, _VOL_MARK, True)
    return client


class RthDailyIBDataClientFactory(InteractiveBrokersLiveDataClientFactory):
    """The shipped IBKR data factory, with the RTH-daily wrap installed on the IB client of what it
    returns. THE VENDOR STILL OWNS CONSTRUCTION."""

    rth_daily_bars = True

    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        client = InteractiveBrokersLiveDataClientFactory.create(
            loop=loop, name=name, config=config, msgbus=msgbus, cache=cache, clock=clock,
        )
        install_rth_daily(client._client)
        install_volume_floor(client._client)
        return client
