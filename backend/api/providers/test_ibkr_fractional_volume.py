"""IB daily bars with a SUB-UNIT volume must not be dropped (#1084).

MEASURED ibkr-paper 2026-09-14, boot 07:47Z, one-shot history queue: at 09:07:10Z, in 11 ms, 26 of
HUBC.XNAS's ~33 daily bars raised inside the shipped adapter —

    market_data.py:1577, _ib_bar_to_nautilus_bar
        volume=instrument.make_qty(0 if bar.volume == -1 else bar.volume),
    ValueError: Invalid `value` for quantity: 0.49828404 was rounded to zero due to size increment 1

— and the WHOLE bar was dropped, prices included. Seven bars landed. A lane ranking HUBC that night
would have run on 7 daily bars instead of 33, and nothing on /health says a symbol's history is short.

THE PREMISE, PINNED ON THE PINNED VENDOR: `Instrument.make_qty` raises on a positive value that
rounds to zero at the instrument's precision, and accepts 0. If a Nautilus bump changes that, the
first test here goes red and the wrap is redundant — which is the point of pinning it.

THE WRAP sits on the SAME seam as #875's RTH-daily wrap — the IB client instance, bound per instance,
never the class — and floors a sub-unit positive volume to 0 before the vendor's conversion runs.
The bar keeps its prices; the volume it loses was under one unit. -1 (IB's "no volume") is untouched:
the vendor already maps it to 0. Whole-unit volumes are untouched.
"""
from __future__ import annotations

import asyncio
import copy
import types
from decimal import Decimal

import pytest
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from api.providers import ibkr_rth_daily as mod

EQUITY = TestInstrumentProvider.equity(symbol="HUBC", venue="XNAS")


def test_the_PINNED_ibapi_decodes_volume_as_Decimal() -> None:
    # THE TYPE ON THE WIRE. The first cut guarded on `float`; ibapi 10.45.1 decodes BarData.volume as
    # Decimal at three sites, so the wrap was a no-op in production while its float-fed double was
    # green. If a bump changes the type, this goes red and the guard is re-read.
    import inspect
    import re

    import ibapi.decoder as decoder

    assert len(re.findall(r"volume\s*=\s*decode\(Decimal", inspect.getsource(decoder))) >= 1


def test_the_PINNED_vendor_raises_on_a_sub_unit_volume_and_accepts_zero() -> None:
    # The premise, with the wire type. Measured value from the ibkr-paper log.
    with pytest.raises(ValueError, match="rounded to zero"):
        EQUITY.make_qty(Decimal("0.49828404"))
    assert int(EQUITY.make_qty(0)) == 0
    assert int(EQUITY.make_qty(1234)) == 1234


class _Bar:
    def __init__(self, volume):
        self.date = "20260913"
        self.open = self.high = self.low = self.close = 10.0
        self.volume = volume


class _Client:
    """The one method the wrap targets, recording what reached it. The REAL method's signature:
    (bar_type, bar, ts_init, is_revision=False), called by keyword from process_historical_data."""

    def __init__(self):
        self.seen = []

    async def _ib_bar_to_nautilus_bar(self, bar_type, bar, ts_init, is_revision=False):
        self.seen.append((bar_type, bar.volume, ts_init, is_revision))
        return "bar"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_sub_unit_volume_is_floored_to_zero_and_the_bar_still_reaches_the_vendor() -> None:
    c = mod.install_volume_floor(_Client())
    out = _run(c._ib_bar_to_nautilus_bar(bar_type="BT", bar=_Bar(Decimal("0.49828404")), ts_init=7))
    assert out == "bar"
    assert c.seen == [("BT", 0, 7, False)]


def test_whole_volumes_and_the_no_volume_sentinel_are_untouched() -> None:
    c = mod.install_volume_floor(_Client())
    for v in (1234, Decimal("1234"), 1234.0, Decimal("-1"), -1, 0):
        _run(c._ib_bar_to_nautilus_bar("BT", _Bar(v), 7, True))
    assert [s[1] for s in c.seen] == [1234, Decimal("1234"), 1234.0, Decimal("-1"), -1, 0]
    assert all(s[3] is True for s in c.seen)              # positional call shape preserved


def test_a_fractional_volume_ABOVE_one_unit_is_floored_not_rounded() -> None:
    # 3.7 lots is 3, never 4: make_qty would round; a floor never invents volume.
    c = mod.install_volume_floor(_Client())
    _run(c._ib_bar_to_nautilus_bar("BT", _Bar(Decimal("3.7")), 7))
    _run(c._ib_bar_to_nautilus_bar("BT", _Bar(3.7), 7))          # a float, should one ever arrive
    assert [s[1] for s in c.seen] == [3, 3]


def test_the_incoming_bar_object_is_not_mutated() -> None:
    # The vendor may hold the BarData elsewhere (revisions); the wrap hands over a copy.
    c = mod.install_volume_floor(_Client())
    bar = _Bar(0.5)
    _run(c._ib_bar_to_nautilus_bar("BT", bar, 7))
    assert bar.volume == 0.5


def test_the_wrap_installs_once_and_the_factory_installs_it_beside_rth_daily(monkeypatch) -> None:
    c = _Client()
    assert mod.install_volume_floor(c) is c
    first = c._ib_bar_to_nautilus_bar
    mod.install_volume_floor(c)
    assert c._ib_bar_to_nautilus_bar is first                 # idempotent: no stacked wrap
    assert mod.is_volume_floor_installed(c)
    # THE SEAM: the repo factory installs BOTH wraps on the IB client of what the vendor returns.
    inner = types.SimpleNamespace(
        get_historical_bars=_noop, subscribe_historical_bars=_noop, _ib_bar_to_nautilus_bar=_noop,
    )
    fake_client = types.SimpleNamespace(_client=inner)
    monkeypatch.setattr(mod.InteractiveBrokersLiveDataClientFactory, "create",
                        staticmethod(lambda **kw: fake_client))
    out = mod.RthDailyIBDataClientFactory.create(loop=None, name="n", config=None, msgbus=None, cache=None, clock=None)
    assert out is fake_client
    assert mod.is_rth_daily_installed(inner) and mod.is_volume_floor_installed(inner)


async def _noop(*a, **k):
    return None
