"""Tests for PEAK (#46) — adaptive trailing stop with partial-exit trims, on the generic manager framework
(#55, `api.managers`).

Offline unit tests: a `_FakeStrategy`/`_FakeCache`/`_FakePosition`/_FakeOrder` double for the
`ManagerHandler.trigger_met`/`apply` tests, plus direct calls into the module-level signal helpers
(`_session_high`/`_sma_daily`/`_ext_pct`/`_vertical_slope_pct`/`_sustained_fade`) with real ET-session
timestamps — those helpers read `strategy.cache.bars(...)`/`strategy.clock` directly, not via strategy
methods, so they need a cache double, not just a black-box strategy.

No `pytest-asyncio` in this repo — same `asyncio.run()`-in-a-sync-test pattern as `test_stop_reenter.py`."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from api.engine_node import (
    _ext_pct,
    _PeakWatch,
    _session_high,
    _sma_daily,
    _sustained_fade,
    _today_session_bars_1m,
    _validate_peak_params,
    _vertical_slope_pct,
)
from api.managers import ManagerRow

_ET = ZoneInfo("America/New_York")


def _rth_ts_ns(hour: int, minute: int, day_offset: int = 0) -> int:
    """A real RTH (09:30-16:00 ET, weekday) nanosecond timestamp — `_et_session_ts` (real code, not
    mocked) must accept it, so tests build genuine ET wall-clock times, not arbitrary epoch offsets. Anchors
    on a known Wednesday so `day_offset` stays safely inside the same trading week."""
    base = datetime(2026, 8, 5, hour, minute, tzinfo=_ET) + timedelta(days=day_offset)  # 2026-08-05 = Wed
    return int(base.astimezone(UTC).timestamp() * 1e9)


class _FakeBar:
    def __init__(self, high: float, close: float, ts_event: int):
        self.high = high
        self.close = close
        self.ts_event = ts_event


class _FakeCache:
    """Fixtures are authored oldest-first (natural, readable) — `bars()` returns them reversed to match
    Nautilus's REAL `cache.bars()` convention (newest-first), which `_today_session_bars_1m` (real,
    unmocked production code) un-reverses back to chronological via its own `reversed()` call. Getting this
    backwards silently breaks every test that depends on bar ORDER (slope, lower-highs) without breaking
    order-agnostic ones (max, sum) — caught by `test_today_session_bars_1m_excludes_extended_hours...`."""

    def __init__(self, bars_1m: list | None = None, bars_1d: list | None = None, orders_open: list | None = None):
        self._bars_1m = bars_1m or []
        self._bars_1d = bars_1d or []
        self._orders_open = orders_open or []

    def bars(self, bt):
        return list(reversed(self._bars_1m if "1-MINUTE" in str(bt) else self._bars_1d))

    def orders_open(self):
        return self._orders_open


class _FakeClock:
    def __init__(self, ts_ns: int):
        self._ts_ns = ts_ns

    def timestamp_ns(self) -> int:
        return self._ts_ns


class _FakePosition:
    def __init__(self, quantity: float = 100, account_id: str = "ACC-1"):
        self.quantity = quantity
        self.account_id = account_id


class _Enum:
    def __init__(self, name: str):
        self.name = name


class _FakeOrder:
    def __init__(self, is_open: bool = True):
        self.is_open = is_open


class _FakeCycleDTO:
    def __init__(self, instrument_id: str, strategy_id: str, cycle_id: str):
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.cycle_id = cycle_id


class _FakeTradeCycles:
    """Stand-in for `strategy._trade_cycles` — mirrors `test_stop_reenter.py`'s own double."""

    def __init__(self, dtos: list | None = None):
        self._dtos = dtos or []

    def project(self, cache, ts_ns):
        return self._dtos


class _FakeStrategy:
    def __init__(self, *, cache: _FakeCache, now_ts_ns: int, position=None, last_price=None, trade_cycles=None):
        self.cache = cache
        self.clock = _FakeClock(now_ts_ns)
        self._position = position
        self._last_price = last_price
        # DICT, keyed by strategy id — the production shape. `None` stays None so the guard's falsy
        # check is still exercised.
        self._trade_cycles = ({"MANUAL-001": trade_cycles} if trade_cycles is not None else trade_cycles)  # None by default — matches the guard's own `is not None` check
        self.built_orders: list[dict] = []
        self.submitted: list[object] = []
        self.trailing_stops: list[dict] = []
        self.replaced_trailing_stops: list[dict] = []
        self.canceled: list[object] = []
        self._seen_orders: set[str] = set()
        self._orders_by_coid: dict[str, _FakeOrder] = {}
        self.submit_call_order: list[str] = []  # "market:<coid>" / "trail:<coid>", in actual call order


    def current_cycle_for(self, instrument_id: str, strategy_id: str):
        """Mirrors production: look up ONE strategy's projection in the dict, then select by
        instrument AND strategy. A double that answered `.project()` directly is what hid the
        multi-strategy break; one that folded every projection would hide the cross-strategy
        mis-selection codex flagged."""
        proj = (self._trade_cycles or {}).get(str(strategy_id))
        if proj is None:
            return None
        return next(
            (d for d in proj.project(self.cache, 0)
             if d.instrument_id == instrument_id and d.strategy_id == strategy_id),
            None,
        )

    def _position_for(self, instrument_id, strategy_id, side):
        return self._position

    def _last_price_for(self, instrument_id):
        return self._last_price

    def _build_order(self, payload: dict):
        self.built_orders.append(payload)
        return object()

    def _submit(self, order):
        self.submitted.append(order)
        if self.built_orders:
            self.submit_call_order.append(f"market:{self.built_orders[-1]['client_order_id']}")

    def _submit_trailing_stop(self, **kwargs):
        self.trailing_stops.append(kwargs)
        self.submit_call_order.append(f"trail:{kwargs.get('coid')}")
        return object()

    def _replace_trailing_stop(self, **kwargs):
        self.replaced_trailing_stops.append(kwargs)
        return object()

    def _lookup_order(self, coid):
        return self._orders_by_coid.get(coid)

    def _cancel(self, order):
        self.canceled.append(order)


def _row(**overrides) -> ManagerRow:
    base = dict(
        manager_id="m1",
        kind="peak_watch",
        kind_version=1,
        account_id="ACC-1",
        client_id="CLIENT-1",
        instrument_id="PENG.XNAS",
        strategy_id="MANUAL-001",
        cycle_id="cyc-1",
        leash="AUTO",
        params={
            "expected_side": "LONG",
            "qty": 100,
            "trail_wide_bps": 250,
            "trail_tight_bps": 100,
            "tightened": False,
            "current_trail_coid": "PKW-init",
            "ext_pct_threshold": 40.0,
            "slope_pct_threshold": 3.0,
            "off_hod_pct_threshold": 2.0,
            "lower_high_bars": 3,
            "trim_count": 0,
            "trim_max": 2,
            "trim_fraction": 0.45,
        },
        state="ARMED",
        # Anchored to the SAME session the bar fixtures use (2026-08-05, via `_rth_ts_ns`), just before
        # the open. `datetime.now()` here silently placed every armed row years after its own bars, so
        # once the fade window became arm-scoped (#253) no bar qualified and every fade test failed. A
        # double whose clock disagrees with its own data is the same drift that has bitten this file
        # twice already — see the module docstring on `_daily_ts_ns`.
        created_at=datetime.fromtimestamp(_rth_ts_ns(9, 25) / 1e9, tz=UTC),
    )
    base.update(overrides)
    return ManagerRow(**base)


def _sync(run) -> None:
    asyncio.run(run())


# --- _validate_peak_params ------------------------------------------------------------------------------


def test_validate_rejects_missing_field():
    params = _row().params.copy()
    del params["trail_tight_bps"]
    assert "trail_tight_bps" in _validate_peak_params(params)


def test_validate_accepts_well_formed_params():
    assert _validate_peak_params(_row().params) is None


def test_validate_rejects_short_v1_signals_are_long_only():
    """codex review (Medium #6): every signal formula (HoD, lower-highs, off-HoD%) is LONG-only math —
    refuse SHORT explicitly rather than silently apply wrong-direction signals."""
    params = {**_row().params, "expected_side": "SHORT"}
    reason = _validate_peak_params(params)
    assert reason is not None
    assert "LONG-only" in reason


# --- signal helpers — real ET-session timestamps, real _et_session_ts filtering ---------------------------


def test_today_session_bars_1m_excludes_extended_hours_and_other_days():
    bars_1m = [
        _FakeBar(high=10, close=10, ts_event=_rth_ts_ns(8, 0)),  # premarket — excluded
        _FakeBar(high=20, close=19, ts_event=_rth_ts_ns(10, 0)),  # today RTH
        _FakeBar(high=21, close=20, ts_event=_rth_ts_ns(10, 1)),  # today RTH
        _FakeBar(high=99, close=99, ts_event=_rth_ts_ns(10, 0, day_offset=-1)),  # yesterday — excluded
    ]
    cache = _FakeCache(bars_1m=bars_1m)
    strategy = _FakeStrategy(cache=cache, now_ts_ns=_rth_ts_ns(10, 5))
    session = _today_session_bars_1m(strategy, "PENG.XNAS")
    assert [b.high for b in session] == [20, 21]  # oldest-first, only today's RTH


def test_session_high_is_max_of_todays_rth_bars():
    bars_1m = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(10, i)) for i, h in enumerate([50, 55, 52])]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5))
    assert _session_high(strategy, "PENG.XNAS") == 55


def test_session_high_none_with_no_bars_yet():
    strategy = _FakeStrategy(cache=_FakeCache(), now_ts_ns=_rth_ts_ns(10, 0))
    assert _session_high(strategy, "PENG.XNAS") is None


def test_sma_daily_none_with_insufficient_history():
    bars_1d = [_FakeBar(high=10, close=10, ts_event=_rth_ts_ns(10, 0, day_offset=-i)) for i in range(5)]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 0))
    assert _sma_daily(strategy, "PENG.XNAS", period=20) is None


def test_sma_daily_averages_the_period_window():
    bars_1d = [_FakeBar(high=10, close=c, ts_event=_rth_ts_ns(10, 0, day_offset=-i)) for i, c in enumerate([10] * 20)]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 0))
    assert _sma_daily(strategy, "PENG.XNAS", period=20) == 10


def test_ext_pct_extension_above_baseline():
    assert _ext_pct(140.0, 100.0) == 40.0


def test_ext_pct_none_with_no_baseline():
    assert _ext_pct(140.0, None) is None


def test_vertical_slope_pct_over_recent_bars():
    bars_1m = [_FakeBar(high=c, close=c, ts_event=_rth_ts_ns(10, i)) for i, c in enumerate([100, 101, 103, 106])]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5))
    # bars_back=3 -> the window spans 3 INTERVALS -> 4 bars (100..106), % change start(100) -> end(106).
    assert _vertical_slope_pct(strategy, "PENG.XNAS", bars_back=3) == 6.0


def test_sustained_fade_ignores_a_single_wiggle():
    """PENG's own lesson (#46 ticket): a single red bar off a fresh HoD must NOT confirm a fade."""
    bars_1m = [
        _FakeBar(high=89, close=88, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 1)),  # fresh HoD
        _FakeBar(high=88, close=87.5, ts_event=_rth_ts_ns(10, 2)),  # one wiggle down
        _FakeBar(high=89.2, close=89.1, ts_event=_rth_ts_ns(10, 3)),  # resumes — NOT a fade
    ]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5))
    hod = _session_high(strategy, "PENG.XNAS")
    assert _sustained_fade(strategy, "PENG.XNAS", hod, off_hod_pct=2.0, lower_high_bars=3) is False


def test_sustained_fade_confirms_on_consecutive_lower_highs():
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),  # HoD
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
        _FakeBar(high=86.5, close=86, ts_event=_rth_ts_ns(10, 3)),
    ]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5))
    hod = _session_high(strategy, "PENG.XNAS")
    assert _sustained_fade(strategy, "PENG.XNAS", hod, off_hod_pct=99.0, lower_high_bars=3) is True


def test_sustained_fade_confirms_on_sustained_off_hod_pct():
    bars_1m = [
        _FakeBar(high=100.0, close=100.0, ts_event=_rth_ts_ns(10, 0)),  # HoD
        _FakeBar(high=97.5, close=97.0, ts_event=_rth_ts_ns(10, 1)),  # 3% off, but higher than prev? no
        _FakeBar(high=97.6, close=97.2, ts_event=_rth_ts_ns(10, 2)),  # still ~2.8% off, sustained
    ]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5))
    hod = _session_high(strategy, "PENG.XNAS")
    # lower_high_bars=5 so the lower-highs branch can't fire (not enough bars) — isolates the off-hod-pct branch
    assert _sustained_fade(strategy, "PENG.XNAS", hod, off_hod_pct=2.0, lower_high_bars=5) is True


# --- _PeakWatch.trigger_met / apply ----------------------------------------------------------------------


def _armed_bars(hod_high=89.5):
    """A simple, non-fading, non-blowoff bar set — 'just riding', nothing should fire."""
    return [_FakeBar(high=hod_high, close=hod_high - 0.1, ts_event=_rth_ts_ns(10, i)) for i in range(3)]


def test_trigger_met_FIRES_when_flat_so_the_row_can_terminalize():
    """This test previously asserted the opposite, and in doing so certified the bug (#255).

    Returning False when flat left the row ARMED forever. The old code's comment said "apply() will find
    nothing to do and fail cleanly" — but apply() is never reached when the trigger says no. `peak_watch`
    on SAP sat ARMED at qty 16 from the moment the position closed on 2026-08-12, which is the live
    proof. Firing lets apply() run once and close the row out.
    """
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=_armed_bars()), now_ts_ns=_rth_ts_ns(10, 5), position=None)

    async def run():
        assert await _PeakWatch().trigger_met(strategy, _row()) is True

    _sync(run)


def test_apply_reports_a_closed_position_as_APPLIED_not_FAILED():
    """The position closing is this manager's job finishing, not an error. FAILED renders red and reads
    as "something went wrong" — which is how the SAP zombie would have surfaced once it terminalized."""
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=_armed_bars()), now_ts_ns=_rth_ts_ns(10, 5), position=None)

    async def run():
        state, detail = await _PeakWatch().apply(strategy, _row())
        assert state == "APPLIED"
        assert "closed" in detail

    _sync(run)


def test_a_successor_waits_for_the_previous_trims_fills_to_land():
    """The OKTA over-sell (#255). Trim 1 sold 26 at 13:33:16; trim 2 fired 30 seconds later and sold 26
    AGAIN, sizing off 58 because the first trim's fills had not landed. It should have sold 14.

    The bars here DO fade — three consecutive lower highs — so without the wait the trigger fires and the
    successor would size off a stale 58. An earlier version of this test used non-fading bars, so it
    passed with the guard removed and proved nothing.
    """
    fading = [
        _FakeBar(high=h, close=h, ts_event=_rth_ts_ns(10, i))
        for i, h in enumerate([200, 190, 180, 170])
    ]
    # Armed ONE MINUTE ago — a trim-successor fires seconds after the trim, not forty minutes later.
    # The wait is deliberately time-bounded, so a fixture armed long ago would time out instantly and
    # exercise nothing.
    row = _row(
        params={**_row().params, "awaiting_qty": 32.0, "tightened": True},
        created_at=datetime.fromtimestamp(_rth_ts_ns(10, 0) / 1e9, tz=UTC),
    )

    async def run():
        # Fills have NOT landed — the position still reports its pre-trim size.
        stale = _FakeStrategy(
            cache=_FakeCache(bars_1m=fading), now_ts_ns=_rth_ts_ns(10, 5),
            position=_FakePosition(quantity=58), last_price=170.0,
        )
        assert await _PeakWatch().trigger_met(stale, row) is False, "must wait for the fill"

        # Same fade, same row — but the position now confirms the reduction. Free to act.
        landed = _FakeStrategy(
            cache=_FakeCache(bars_1m=fading), now_ts_ns=_rth_ts_ns(10, 5),
            position=_FakePosition(quantity=32), last_price=170.0,
        )
        assert await _PeakWatch().trigger_met(landed, row) is True, "fill landed — the fade should fire"

    _sync(run)


def test_a_successor_stops_waiting_once_the_position_confirms_the_reduction():
    """Equal means the fill arrived. Smaller means something ELSE reduced the position too (a manual
    sell, another manager) — waiting on a number that will never come would strand the chain, so act on
    what is actually held."""
    from api.engine_node import _awaiting_fill

    row = _row(
        params={**_row().params, "awaiting_qty": 32.0},
        created_at=datetime.fromtimestamp(_rth_ts_ns(9, 30) / 1e9, tz=UTC),
    )
    clock = _FakeStrategy(cache=_FakeCache(), now_ts_ns=_rth_ts_ns(9, 30) + 30_000_000_000)  # 30s later
    assert _awaiting_fill(clock, row, _FakePosition(quantity=58)) is True   # not landed
    assert _awaiting_fill(clock, row, _FakePosition(quantity=32)) is False  # landed
    assert _awaiting_fill(clock, row, _FakePosition(quantity=20)) is False  # someone else sold more

    # A row with no prior trim never waits.
    assert _awaiting_fill(clock, _row(), _FakePosition(quantity=58)) is False


def test_trigger_met_false_while_calmly_riding():
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=_armed_bars(), bars_1d=[_FakeBar(80, 80, _rth_ts_ns(10, 0, -i)) for i in range(20)]),
        now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=85.0,
    )

    async def run():
        assert await _PeakWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def test_apply_full_exit_when_trim_cap_reached():
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
    ]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=87.0,
    )
    strategy._orders_by_coid["PKW-init"] = _FakeOrder(is_open=True)
    row = _row(params={**_row().params, "trim_count": 2, "trim_max": 2})  # already at cap

    async def run():
        state, detail = await _PeakWatch().apply(strategy, row)
        assert state == "APPLIED"
        assert "full exit" in detail
        assert len(strategy.built_orders) == 1
        assert strategy.built_orders[0]["quantity"] == 100
        assert strategy.trailing_stops == []  # no new trail placed — position is fully closed
        assert len(strategy.canceled) == 1  # the old trail got canceled

    _sync(run)


def test_apply_partial_trim_places_market_sell_and_tighter_trail_then_chains():
    from unittest.mock import AsyncMock, patch

    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
    ]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=87.0,
    )
    strategy._orders_by_coid["PKW-init"] = _FakeOrder(is_open=True)
    row = _row()  # trim_count=0, trim_max=2, trim_fraction=0.45, qty=100 -> trim 45, remain 55

    async def run():
        captured = {}

        async def fake_attach(session, **kwargs):
            captured.update(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert len(strategy.built_orders) == 1
        assert strategy.built_orders[0]["quantity"] == 45  # trim
        assert len(strategy.trailing_stops) == 1
        assert strategy.trailing_stops[0]["quantity"] == 55  # remainder
        # codex re-review (the operator's call, round 2): the TRAIL — not the trim — carries the coid
        # `client_order_id_for` tracks, and it's submitted BEFORE the trim (order matters here: asserting
        # both makes an accidental re-reordering regression fail loudly). A crash between the two leaves the
        # remainder protected by a resting trail even if the trim itself never executed.
        assert strategy.trailing_stops[0]["coid"] == _PeakWatch().client_order_id_for(row.manager_id)
        assert strategy.built_orders[0]["client_order_id"] != strategy.trailing_stops[0]["coid"]
        # trail submitted BEFORE the market trim — a crash between the two leaves the remainder protected.
        assert strategy.submit_call_order == [
            f"trail:{strategy.trailing_stops[0]['coid']}", f"market:{strategy.built_orders[0]['client_order_id']}"
        ]
        assert len(strategy.canceled) == 1  # old trail canceled
        assert captured["params"]["qty"] == 55
        assert captured["params"]["trim_count"] == 1
        assert captured["params"]["tightened"] is True

    _sync(run)


def test_apply_blowoff_tightens_via_replace_and_chains():
    from unittest.mock import AsyncMock, patch

    # ext% > 40 threshold: price 145 vs sma 100 -> 45% extension. No fade bars (flat highs).
    bars_1m = [_FakeBar(high=145, close=145, ts_event=_rth_ts_ns(10, i)) for i in range(3)]
    bars_1d = [_FakeBar(high=100, close=100, ts_event=_rth_ts_ns(10, 0, day_offset=-i)) for i in range(20)]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d),
        now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=145.0,
    )
    row = _row()  # tightened=False

    async def run():
        captured = {}

        async def fake_attach(session, **kwargs):
            captured.update(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert "tightened" in detail
        assert len(strategy.replaced_trailing_stops) == 1
        assert strategy.replaced_trailing_stops[0]["trail_bps"] == 100  # trail_tight_bps
        assert strategy.built_orders == []  # no exit order — just a retighten
        assert captured["params"]["tightened"] is True

    _sync(run)


def test_apply_fade_takes_priority_over_blowoff_when_both_hold():
    from unittest.mock import AsyncMock, patch

    # Extended (ext% high) AND fading (lower highs) simultaneously — fade must win, not a mere retighten.
    bars_1m = [
        _FakeBar(high=145, close=144, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=140, close=139, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=135, close=134, ts_event=_rth_ts_ns(10, 2)),
    ]
    bars_1d = [_FakeBar(high=100, close=100, ts_event=_rth_ts_ns(10, 0, day_offset=-i)) for i in range(20)]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d),
        now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=134.0,
    )
    row = _row()

    async def run():
        async def fake_attach(session, **kwargs):
            pass

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert "trimmed" in detail or "full exit" in detail  # the FADE branch, not "tightened"
        assert strategy.replaced_trailing_stops == []  # blowoff branch never reached

    _sync(run)


def test_apply_refuses_when_a_different_cycle_is_now_open():
    """Cycle-drift guard (codex review, High #2) — mirrors `test_stop_reenter.py`'s own version. A manager
    left ARMED across a close->reopen must not act against the NEW cycle as if it were its own episode."""
    bars_1m = [_FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, i)) for i in range(3)]
    drifted = _FakeTradeCycles([_FakeCycleDTO("PENG.XNAS", "MANUAL-001", "cyc-DIFFERENT")])
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(), last_price=87.0, trade_cycles=drifted,
    )

    async def run():
        state, detail = await _PeakWatch().apply(strategy, _row())  # _row()'s cycle_id is "cyc-1"
        assert state == "FAILED"
        assert "cycle" in detail
        assert strategy.built_orders == []
        assert strategy.trailing_stops == []
        assert strategy.replaced_trailing_stops == []

    _sync(run)


def test_apply_refuses_on_cycle_drift_even_when_a_fade_would_otherwise_fire():
    """codex review (Low) — the previous drift test's bars happened not to trigger anything anyway; this
    proves the guard actually short-circuits BEFORE the fade/blowoff logic runs, using bars that WOULD
    confirm a sustained fade (same shape as `test_sustained_fade_confirms_on_consecutive_lower_highs`) if the
    cycle-drift guard didn't refuse first."""
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),  # HoD
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
        _FakeBar(high=86.5, close=86, ts_event=_rth_ts_ns(10, 3)),  # 3 consecutive lower highs -> fade
    ]
    drifted = _FakeTradeCycles([_FakeCycleDTO("PENG.XNAS", "MANUAL-001", "cyc-DIFFERENT")])
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(), last_price=86.0, trade_cycles=drifted,
    )
    strategy._orders_by_coid["PKW-init"] = _FakeOrder(is_open=True)

    async def run():
        state, detail = await _PeakWatch().apply(strategy, _row())  # _row()'s cycle_id is "cyc-1"
        assert state == "FAILED"
        assert "cycle" in detail
        assert strategy.built_orders == []
        assert strategy.trailing_stops == []
        assert strategy.replaced_trailing_stops == []
        assert strategy.canceled == []  # never even got to canceling the old trail

    _sync(run)


def test_apply_full_exit_coid_matches_client_order_id_for():
    """codex review (High #3) — the coid `apply()` actually submits under MUST equal what
    `client_order_id_for` returns, since the dispatch loop records THAT value as this row's crash-recovery
    intent BEFORE apply() runs; a mismatch means recovery can never find the order and would resubmit it."""
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
    ]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=87.0,
    )
    strategy._orders_by_coid["PKW-init"] = _FakeOrder(is_open=True)
    row = _row(params={**_row().params, "trim_count": 2, "trim_max": 2})  # already at cap -> full exit

    async def run():
        state, detail = await _PeakWatch().apply(strategy, row)
        assert state == "APPLIED"
        assert strategy.built_orders[0]["client_order_id"] == _PeakWatch().client_order_id_for(row.manager_id)

    _sync(run)


def test_apply_blowoff_tighten_coid_matches_client_order_id_for():
    bars_1m = [_FakeBar(high=145, close=145, ts_event=_rth_ts_ns(10, i)) for i in range(3)]
    bars_1d = [_FakeBar(high=100, close=100, ts_event=_rth_ts_ns(10, 0, day_offset=-i)) for i in range(20)]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d),
        now_ts_ns=_rth_ts_ns(10, 5), position=_FakePosition(), last_price=145.0,
    )
    row = _row()

    async def run():
        from unittest.mock import AsyncMock, patch

        async def fake_attach(session, **kwargs):
            pass

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)
        assert state == "APPLIED"
        assert strategy.replaced_trailing_stops[0]["new_coid"] == _PeakWatch().client_order_id_for(row.manager_id)

    _sync(run)


def test_apply_sizes_trim_off_live_quantity_not_stale_params():
    """codex review (High #5) — `row.params["qty"]` is a manager-tracked snapshot that can drift from
    reality; sizing must come from the LIVE position `apply()` already fetches."""
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
    ]
    # row.params["qty"] says 100 (stale), but the live position only holds 60 (drifted).
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(quantity=60), last_price=87.0,
    )
    row = _row()  # params["qty"] == 100

    async def run():
        from unittest.mock import AsyncMock, patch

        async def fake_attach(session, **kwargs):
            pass

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)
        assert state == "APPLIED"
        # 45% of the LIVE 60, not the stale 100
        assert strategy.built_orders[0]["quantity"] == round(60 * 0.45)
        assert strategy.trailing_stops[0]["quantity"] == 60 - round(60 * 0.45)

    _sync(run)


# --- _handle_attach_manager_command's PEAK-specific arm step -----------------------------------------------


def _bare_strategy():
    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    s = UiFeedStrategy(load_feed_config(), ClientId("DATABENTO"), "test-key")
    # A CONNECTED broker, because production has one and the arm now asks it (#269). Empty: these fixtures
    # rest nothing at the venue, and with nothing resting the availability wait is never reached. Leaving
    # `_http` at None would instead exercise the "broker unreadable, refuse to arm" branch on every test.
    s._http = _VenueOrders([], held=1_000_000.0)
    return s


_FAKE_ORDER_SEQ = itertools.count()


class _FakeOpenOrder:
    def __init__(self, instrument_id: str, strategy_id: str, side, order_type, tags, coid=None):
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.side = side
        self.order_type = order_type
        self.tags = tags
        # A REAL `ClientOrderId`, because every production order has one and the clear path reads it: it
        # keys which venue rows the native cancel already covered, so a double without it made
        # `_cancel_reducing_leg` raise rather than cancel. Nautilus's own type, so truncation and
        # stringification behave exactly as they do in production.
        from nautilus_trader.model.identifiers import ClientOrderId

        self.client_order_id = ClientOrderId(coid or f"BR-fake-{next(_FAKE_ORDER_SEQ)}")


class _FakeOrdersCacheHolder:
    """A plain (non-Cython) stand-in for `self` when calling `_bracket_protective_stop_open`/
    `_any_reducing_order_open`/`_reducing_orders_open` UNBOUND off the class — these are ordinary Python
    methods (this codebase's own subclass, not Nautilus's compiled base), so `UiFeedStrategy._method(
    fake_self, ...)` works and lets `.cache` just be a plain attribute here, sidestepping the real class's
    unwritable Cython slot."""

    def __init__(self, orders_open: list):
        self.cache = self
        self._orders_open = orders_open

    def orders_open(self):
        return self._orders_open

    def _reducing_orders_open(self, instrument_id, strategy_id, reducing_side):
        """`_any_reducing_order_open` (real, unmocked) delegates to `self._reducing_orders_open(...)` —
        needed here too so calling it unbound off `UiFeedStrategy` with this fake `self` doesn't AttributeError."""
        from api.engine_node import UiFeedStrategy

        return UiFeedStrategy._reducing_orders_open(self, instrument_id, strategy_id, reducing_side)


_FULL_PEAK_ARM_PARAMS = {
    "expected_side": "LONG",
    "qty": 1,  # overridden by the live position inside _handle_attach_manager_command
    "trail_wide_bps": 250,
    "trail_tight_bps": 100,
    "ext_pct_threshold": 40.0,
    "slope_pct_threshold": 3.0,
    "off_hod_pct_threshold": 2.0,
    "lower_high_bars": 3,
    "trim_max": 2,
    "trim_fraction": 0.45,
}


def test_bracket_protective_stop_open_finds_the_tagged_stop():
    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    stop = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET, ["bracket:g1"])
    fake_self = _FakeOrdersCacheHolder([stop])
    found = UiFeedStrategy._bracket_protective_stop_open(fake_self, "PENG.XNAS", "MANUAL-001", OrderSide.SELL)
    assert found is stop


def test_bracket_protective_stop_open_none_when_tag_missing():
    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    stop = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET, None)
    fake_self = _FakeOrdersCacheHolder([stop])
    assert UiFeedStrategy._bracket_protective_stop_open(fake_self, "PENG.XNAS", "MANUAL-001", OrderSide.SELL) is None


def test_bracket_protective_stop_open_none_when_wrong_order_type():
    """A LIMIT order (e.g. a take-profit leg) on the reducing side must not be mistaken for the stop."""
    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    tp_leg = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.LIMIT, ["bracket:g1"])
    fake_self = _FakeOrdersCacheHolder([tp_leg])
    assert UiFeedStrategy._bracket_protective_stop_open(fake_self, "PENG.XNAS", "MANUAL-001", OrderSide.SELL) is None


def test_any_reducing_order_open_true_false():
    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    stop = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET, None)
    assert UiFeedStrategy._any_reducing_order_open(
        _FakeOrdersCacheHolder([stop]), "PENG.XNAS", "MANUAL-001", OrderSide.SELL
    ) is True
    assert UiFeedStrategy._any_reducing_order_open(
        _FakeOrdersCacheHolder([]), "PENG.XNAS", "MANUAL-001", OrderSide.SELL
    ) is False


def test_reducing_orders_open_returns_every_match_not_just_a_boolean():
    """codex review (Critical #1) — the arm guard needs the full list to tell "only the bracket stop
    rests" apart from "the bracket stop rests ALONGSIDE something else"."""
    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    bracket = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET, ["bracket:g1"])
    other = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.LIMIT, None)
    found = UiFeedStrategy._reducing_orders_open(
        _FakeOrdersCacheHolder([bracket, other]), "PENG.XNAS", "MANUAL-001", OrderSide.SELL
    )
    assert found == [bracket, other]


def _fake_reserve_proceed(ledger_cid="ledger-1"):
    """Mirrors production's arity, INCLUDING the canonicalised params it hands back (#401).

    The fourth element is the whole point of that signature: `_reserve_attach_command` puts every price
    param on the instrument's tick and returns the corrected dict, and the caller must persist THAT
    rather than its own. A double returning three values could not express the contract at all — and one
    returning four but echoing the input unchanged still could not tell "the caller used the returned
    dict" from "the caller kept its own". This echoes the input, so it keeps every existing test honest;
    the test that proves the caller actually reads it uses a double that CHANGES the params.
    """

    async def fake(cid, entry_id, **kwargs):
        return "proceed", "", ledger_cid, kwargs.get("params")

    return fake


async def _noop_async_reject(ledger_cid, reason):
    return None


def test_attach_peak_cancels_the_identified_bracket_stop_and_arms():
    """Happy path: a resting reducing-side STOP_MARKET tagged `bracket:...` is reliably identifiable as
    THIS position's protective stop — canceled, replaced by the new wide trailing stop, manager attaches
    with `current_trail_coid` pointing at the new one.

    `.cache` is a Cython slot on the real `Actor`/`Strategy` base — not writable via plain attribute
    assignment on a bare instance, so these tests monkeypatch the DISCRIMINATION METHODS
    (`_bracket_protective_stop_open`/`_reducing_orders_open`) and the reserve/commit ledger halves
    (`_reserve_attach_command`/`_commit_attach_command`) rather than faking the cache/ledger underneath
    them — "stub the seam, not the internals"."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        # A real shape, not `object()`: every Nautilus order carries `tags`, and the arm guard reads them
        # to tell this position's own bracket legs from an unrelated resting sell (#265). A bare object
        # raised AttributeError instead of exercising the guard — fix the double, never loosen production.
        from nautilus_trader.model.enums import OrderSide, OrderType
        bracket_stop = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                                      ["bracket:entry-1"])
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: bracket_stop
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: bracket_stop  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [bracket_stop]

        # The arm now CANCELS FIRST and waits for the venue to confirm, because Alpaca reserves shares
        # against a resting sell and would reject the trail on `available: 0` (#245/#252, #265). Without
        # this stub the test drives the real six-second poll and times out — which is the honest signal
        # that the ordering changed, not a test-only detail.
        async def _cleared(instrument_id, strategy_id, reducing_side, timeout_s=6.0):
            return True

        s._await_reducing_orders_clear = _cleared

        async def _resting(coid, timeout_s=6.0):
            return True

        s._await_protection_resting = _resting
        placed_order = _FakeOrder(is_open=True)
        submitted_trails = []

        def fake_submit(**kw):
            submitted_trails.append(kw)
            return placed_order

        s._submit_trailing_stop = fake_submit
        canceled = []
        s._cancel = lambda order: canceled.append(order)
        captured = {}

        s._reserve_attach_command = _fake_reserve_proceed()

        async def fake_commit(cid, ledger_cid, **kwargs):
            captured.update(kwargs)
            return "ok", "stubbed"

        s._commit_attach_command = fake_commit

        status, _ = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "ok"
        assert len(submitted_trails) == 1
        assert submitted_trails[0]["trail_bps"] == 250.0
        assert canceled == [bracket_stop]
        assert captured["params"]["current_trail_coid"] == "PKW-cid-1"
        assert captured["params"]["tightened"] is False
        assert captured["params"]["trim_count"] == 0
        assert captured["params"]["qty"] == 100.0  # bound to the live position, not the payload's "1"

    _sync(run)


def test_attach_peak_refuses_when_resting_order_is_not_identifiable_as_the_bracket_stop():
    """A resting reducing-side order exists but ISN'T bracket-tagged (manual stop, partial take-profit,
    anything) — refuse rather than guess which one to cancel. No trailing stop submitted, nothing canceled.
    Also: since a ledger row was already RESERVED before this check ran, it must be marked REJECTED (codex
    review, Medium) — otherwise a same-command_id redelivery reports a confusing "interrupted mid-attach"
    instead of mirroring the original refusal."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        other = object()
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [other]
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: submitted_trails.append(kw)
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        canceled = []
        s._cancel = lambda order: canceled.append(order)
        s._reserve_attach_command = _fake_reserve_proceed(ledger_cid="ledger-xyz")
        rejected = []

        async def fake_reject(ledger_cid, reason):
            rejected.append((ledger_cid, reason))

        s._reject_reserved_ledger_entry = fake_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "cancel it manually" in error
        assert submitted_trails == []
        assert canceled == []
        assert rejected == [("ledger-xyz", error)]

    _sync(run)


def test_attach_peak_refuses_when_bracket_stop_rests_alongside_another_order():
    """codex review (Critical #1) — the OLD guard let this through as long as a bracket stop was findable
    at all, even with a SECOND, unidentified resting order also live: only the bracket stop got canceled,
    leaving the other one resting alongside PEAK's own fresh trail. Must refuse — there is more than one
    resting reducing order, not just "one, and it happens to be identifiable"."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        # Real shapes, not bare object(): production orders carry `tags`, and the arm guard now reads
        # them to tell a leg of THIS position's bracket from an unrelated resting sell. A tagless double
        # would raise AttributeError instead of exercising the guard — the drifted-double shape CLAUDE.md
        # names explicitly.
        from nautilus_trader.model.enums import OrderSide, OrderType
        bracket_stop = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                                      ["bracket:entry-1"])
        other = _FakeOpenOrder("PENG.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.LIMIT, [])
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: bracket_stop
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: bracket_stop  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [bracket_stop, other]
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: submitted_trails.append(kw)
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        canceled = []
        s._cancel = lambda order: canceled.append(order)
        s._reserve_attach_command = _fake_reserve_proceed()
        s._reject_reserved_ledger_entry = _noop_async_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "cancel it manually" in error
        assert submitted_trails == []
        assert canceled == []

    _sync(run)


def test_attach_peak_arms_directly_when_nothing_rests():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: []
        placed_order = _FakeOrder(is_open=True)
        submitted_trails = []

        def fake_submit(**kw):
            submitted_trails.append(kw)
            return placed_order

        s._submit_trailing_stop = fake_submit
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        canceled = []
        s._cancel = lambda order: canceled.append(order)
        captured = {}
        s._reserve_attach_command = _fake_reserve_proceed()

        async def fake_commit(cid, ledger_cid, **kwargs):
            captured.update(kwargs)
            return "ok", "stubbed"

        s._commit_attach_command = fake_commit

        status, _ = await s._handle_attach_manager_command(
            "cid-2",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "ok"
        assert len(submitted_trails) == 1
        assert canceled == []
        assert captured["params"]["current_trail_coid"] == "PKW-cid-2"

    _sync(run)


def test_attach_peak_refuses_incomplete_params_before_placing_any_order():
    """codex review (High #4) — full param validation must run BEFORE the initial trailing stop is
    submitted (`_reserve_attach_command` validates via `handler.validate_params` before anything else runs).
    Without this, a missing field would place a real order and only then discover the attach can't proceed.
    Uses the REAL `_reserve_attach_command` (not mocked) — it fails closed on `_cmd_ledger is None` too, but
    validation happens even earlier, before that check."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: []
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: submitted_trails.append(kw)
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting

        incomplete = {**_FULL_PEAK_ARM_PARAMS}
        del incomplete["trail_tight_bps"]

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "PENG.XNAS", "strategy_id": "MANUAL-001", "params": incomplete},
            "entry-1",
        )

        assert status == "error"
        assert "trail_tight_bps" in error
        assert submitted_trails == []  # never got as far as placing the order

    _sync(run)


def test_attach_peak_reserves_the_ledger_before_checking_resting_orders_or_placing_anything():
    """codex review (High) — the idempotency reserve must run BEFORE the resting-order check and BEFORE the
    initial order submit, not after. A redelivered/duplicate/ledger-unavailable command must never touch
    live orders. Uses the REAL `_reserve_attach_command` — `_cmd_ledger` is None on a bare strategy, so it
    fails closed with the ledger-unavailable error — proving the ledger gate is reached and enforced before
    the resting-order check would even run (that check is stubbed to error out if ever called)."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)

        def must_not_be_called(*args, **kwargs):
            raise AssertionError("resting-order check ran before the ledger reserve")

        s._bracket_protective_stop_open = must_not_be_called
        s._reducing_orders_open = must_not_be_called
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: submitted_trails.append(kw)
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "ledger unavailable" in error
        assert submitted_trails == []

    _sync(run)


def test_attach_peak_rejects_the_reserved_ledger_entry_when_initial_submit_fails():
    """codex review (Medium) — the reserve already happened (RESERVED in the ledger) before the initial
    order submit is attempted; if that submit raises, the ledger row must be marked REJECTED, not left
    stuck — otherwise a same-command_id redelivery reports "interrupted mid-attach" instead of the real
    reason."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: []

        def failing_submit(**kw):
            raise RuntimeError("instrument not found")

        s._submit_trailing_stop = failing_submit
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        s._reserve_attach_command = _fake_reserve_proceed(ledger_cid="ledger-abc")
        rejected = []

        async def fake_reject(ledger_cid, reason):
            rejected.append((ledger_cid, reason))

        s._reject_reserved_ledger_entry = fake_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "instrument not found" in error
        assert rejected == [("ledger-abc", error)]

    _sync(run)


def test_attach_peak_cancels_the_initial_order_when_attach_fails_after_submit():
    """codex review (High #4) — the DB-attach (`_commit_attach_command`) can still fail AFTER the initial
    trailing stop already went out (DB error). Must not leave a live, unmanaged order with no manager row
    tracking it — cancel it (using the order OBJECT `_submit_trailing_stop` returned, not a fresh cache
    lookup — codex review: Nautilus doesn't guarantee synchronous cache population right after submit) and
    surface why."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: []
        placed_order = _FakeOrder(is_open=True)
        submitted_trails = []

        def fake_submit(**kw):
            submitted_trails.append(kw)
            return placed_order

        s._submit_trailing_stop = fake_submit
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        canceled = []
        s._cancel = lambda order: canceled.append(order)
        s._reserve_attach_command = _fake_reserve_proceed()

        async def fake_commit(cid, ledger_cid, **kwargs):
            return "error", "db unavailable"

        s._commit_attach_command = fake_commit

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "peak_watch",
                "instrument_id": "PENG.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PEAK_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert len(submitted_trails) == 1  # the order WAS placed...
        assert canceled == [placed_order]  # ...then rolled back, via the returned order object
        assert "canceled" in error

    _sync(run)


# --- cross-strategy cycle selection (codex review, High) ------------------------------------------


def test_guard_ignores_another_strategys_cycle_for_the_same_instrument():
    """A name two strategies both hold must not fail this row as stale.

    FIG has been held by MANUAL and MOMENTUM at the same time. The first version of this fix folded
    EVERY strategy's projection and selected on instrument alone, so MOMENTUM's cycle for PENG could
    be picked while judging a MANUAL row — and the row would be refused as "the position's cycle
    changed" when nothing about it had changed.
    """

    async def run():
        cycles = _FakeTradeCycles(
            [
                _FakeCycleDTO("PENG.XNAS", "MOMENTUM-002", "cyc-SOMEONE-ELSE"),
                _FakeCycleDTO("PENG.XNAS", "MANUAL-001", "cyc-1"),  # ours, matching the row
            ]
        )
        strategy = _FakeStrategy(
            cache=_FakeCache(), now_ts_ns=0, position=_FakePosition(), last_price=87.0, trade_cycles=cycles
        )
        row = _row()  # strategy_id MANUAL-001, cycle_id cyc-1
        state, _ = await _PeakWatch().apply(strategy, row)
        # Reaching ANY state other than the stale-cycle refusal proves the guard selected our cycle.
        assert state != "FAILED" or "cycle changed" not in (_ or "")

    _sync(run)


def test_guard_refuses_when_only_another_strategy_holds_the_instrument():
    """The mirror: if OUR strategy has no cycle for the name, the row is genuinely stale — another
    strategy's cycle must not stand in for it and let the manager act."""

    async def run():
        cycles = _FakeTradeCycles([_FakeCycleDTO("PENG.XNAS", "MOMENTUM-002", "cyc-1")])
        strategy = _FakeStrategy(
            cache=_FakeCache(), now_ts_ns=0, position=_FakePosition(), last_price=87.0, trade_cycles=cycles
        )
        state, detail = await _PeakWatch().apply(strategy, _row())
        assert state == "FAILED"
        assert "cycle" in detail

    _sync(run)


def test_apply_does_not_hand_off_when_the_operator_turned_peak_off():
    """The toggle's OFF path, from the chain's side.

    PEAK spawns a successor after every trim, so cancelling the one row the UI named never stopped it —
    the successor was already attached (or was attached moments later by an apply already in flight) and
    the toggle sprang back to ON. `chain_cancelled` is consulted BEFORE the handoff, so OFF ends the
    chain. The trim itself still stands: those orders are already at the venue and cannot be unsent.
    """
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0)),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1)),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2)),
    ]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(quantity=60), last_price=87.0,
    )
    row = _row()
    attached: list = []

    async def run():
        from unittest.mock import AsyncMock, patch

        async def fake_attach(session, **kwargs):
            attached.append(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=True)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PeakWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert attached == [], "a cancelled chain must not attach a successor"
        assert "turned off" in detail
        # The trim already went to the venue — cancelling the chain does not (and cannot) unsend it.
        assert len(strategy.built_orders) == 1

    _sync(run)


# --- #255: the baseline must not contain the spike it is measuring -------------------------------------


def _daily_ts_ns(day_offset: int = 0) -> int:
    """A DAILY bar's timestamp, in the shape Alpaca actually sends: **00:00 ET**, not an RTH time.

    Verified against the live feed — `2026-08-06T04:00:00Z` is `2026-08-06 00:00 -04:00`. This matters
    more than it looks: the first version of `_is_todays_session` filtered through `_et_session_ts`, which
    returns None outside 09:30-16:00 ET, so it answered False for every REAL daily bar. The fix passed its
    tests and did nothing in production, purely because the fixtures used 10:00 ET stamps that no daily bar
    ever carries. A double that cannot represent production is worse than no double. (codex review, High.)
    """
    base = datetime(2026, 8, 5, 0, 0, tzinfo=_ET) + timedelta(days=day_offset)
    return int(base.astimezone(UTC).timestamp() * 1e9)


def _daily_bars(closes: list[float]) -> list:
    """`closes` oldest-first, ending with TODAY. `_FakeCache.bars()` reverses, so authoring oldest-first is
    what comes back newest-first the way Nautilus really returns them."""
    n = len(closes)
    return [
        _FakeBar(high=c, close=c, ts_event=_daily_ts_ns(day_offset=-(n - 1 - i)))
        for i, c in enumerate(closes)
    ]


def test_sma_daily_excludes_todays_still_forming_bar():
    """The ext% baseline is what the price is extended FROM, so it can only be built from COMPLETED
    sessions. Including today's forming bar — whose close IS the current price — drags the SMA toward the
    spike and suppresses PEAK's blowoff trigger exactly when the move is biggest.

    The two pre-existing `_sma_daily` tests use all-equal closes, which is order-agnostic: they cannot see
    this, or an ordering mistake either.
    """
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1d=_daily_bars([10.0] * 20 + [200.0])), now_ts_ns=_rth_ts_ns(10, 0)
    )
    # 10.0, not (200 + 19*10)/20 = 19.5 — which is what including today's bar would give.
    assert _sma_daily(strategy, "PENG.XNAS", period=20) == 10.0


def test_the_baseline_still_works_when_there_is_no_bar_for_today_yet():
    """Before the first daily bar of the session prints, the newest cached bar is YESTERDAY's — and it is
    a completed session that belongs in the window. Dropping it unconditionally would silently shorten the
    baseline by a day."""
    bars = _daily_bars([10.0] * 20)[:-1]  # ...through yesterday, nothing for today
    bars.insert(0, _FakeBar(high=10.0, close=10.0, ts_event=_daily_ts_ns(day_offset=-20)))
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=bars), now_ts_ns=_rth_ts_ns(10, 0))
    assert _sma_daily(strategy, "PENG.XNAS", period=20) == 10.0


def test_the_suppressed_blowoff_trigger_is_the_point():
    """The consequence as a number, at the threshold that actually decides: with today's bar inside the
    baseline a real blowoff reads as BELOW the 40% default and the branch never fires."""
    closes = [100.0] * 20 + [141.0]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=_daily_bars(closes)), now_ts_ns=_rth_ts_ns(10, 0))

    correct = _ext_pct(141.0, _sma_daily(strategy, "PENG.XNAS", period=20))
    self_baselined = _ext_pct(141.0, sum(closes[-20:]) / 20)  # what the buggy window computed

    assert correct == 41.0  # over the 40 default -> blowoff fires, trail tightens
    assert self_baselined is not None and self_baselined < 40.0  # under it -> silently never fires


# --- #255: numeric params that invert PEAK's behaviour -------------------------------------------------


def _params(**over) -> dict:
    base = {
        "expected_side": "LONG", "qty": 100,
        "trail_wide_bps": 250, "trail_tight_bps": 100, "ext_pct_threshold": 40,
        "slope_pct_threshold": 3, "off_hod_pct_threshold": 2, "lower_high_bars": 3,
        "trim_max": 2, "trim_fraction": 0.45,
    }
    return {**base, **over}


def test_the_shipped_defaults_validate():
    assert _validate_peak_params(_params()) is None


def test_a_tight_trail_that_is_wider_than_the_wide_one_is_refused():
    """The blowoff branch exists to TIGHTEN. With these values it would widen the stop instead — the
    reverse of the one thing it does, and silent."""
    err = _validate_peak_params(_params(trail_tight_bps=300))
    assert err is not None and "TIGHTER" in err


def test_trim_max_below_one_is_refused():
    """0 makes the first fade a full exit, with no ladder at all."""
    err = _validate_peak_params(_params(trim_max=0))
    assert err is not None and "trim_max" in err


def test_lower_high_bars_below_two_is_refused():
    """One bar cannot describe a sequence of lower highs, so the fade condition would be trivially true
    and PEAK would trim the moment it armed."""
    err = _validate_peak_params(_params(lower_high_bars=1))
    assert err is not None and "lower_high_bars" in err


def test_a_negative_threshold_is_refused():
    err = _validate_peak_params(_params(ext_pct_threshold=-1))
    assert err is not None and "negative" in err


def test_trim_fraction_outside_zero_to_one_is_refused():
    for bad in (0, -0.5, 1.5):
        assert _validate_peak_params(_params(trim_fraction=bad)) is not None
    assert _validate_peak_params(_params(trim_fraction=1.0)) is None  # 1.0 = always full exit, legal


def test_a_non_numeric_param_is_refused_not_crashed():
    err = _validate_peak_params(_params(trail_wide_bps="wide"))
    assert err is not None and "non-numeric" in err


# --- #255: a leash the engine does not honour is refused, not ignored ----------------------------------


def test_auto_is_the_only_accepted_leash():
    from api.engine_node import _validate_leash

    assert _validate_leash("AUTO") is None
    # Exact match, not case-folded: normalizing would rewrite the value the attach idempotency hash is
    # computed over, so a redelivered command would hash differently and read as a changed payload.
    assert _validate_leash("auto") is not None


def test_confirm_and_alert_are_refused_rather_than_silently_treated_as_auto():
    """`mg.claim` is an unconditional ARMED -> APPLYING update with no leash predicate, so these used to be
    accepted and then applied exactly like AUTO. A safety setting that silently does nothing is worse than
    one that is not offered."""
    from api.engine_node import _validate_leash

    for leash in ("CONFIRM", "ALERT"):
        err = _validate_leash(leash)
        assert err is not None and "not honoured" in err


def test_non_finite_params_are_refused():
    """NaN survives `float()` and makes every comparison False, so a NaN threshold passes an unguarded
    range check and then never fires. inf is the mirror image. (codex review.)"""
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert _validate_peak_params(_params(ext_pct_threshold=bad)) is not None, bad
        assert _validate_peak_params(_params(trim_fraction=bad)) is not None, bad


def test_the_dispatch_guard_and_the_attach_check_are_the_same_predicate():
    """Pinning that the two agree (#255). They were briefly different — attach demanded an exact "AUTO"
    while dispatch case-folded — so a stored `"auto"` was refused at the door and applied anyway by the
    loop. One predicate cannot disagree with itself; this asserts the dispatch guard still calls it.
    """
    import inspect

    from api.engine_node import UiFeedStrategy, _validate_leash

    src = inspect.getsource(UiFeedStrategy._dispatch_managers_of_kind)
    assert "_validate_leash(row.leash)" in src, (
        "the dispatch loop must reuse _validate_leash, not re-implement the check"
    )
    for bad in ("auto", "Auto", "CONFIRM", "ALERT", ""):
        assert _validate_leash(bad) is not None, bad


# --- #253: the fade window is scoped to THIS manager's arm --------------------------------------------


def test_the_fade_window_starts_at_arm_not_at_the_session_open():
    """The defect that mauled OKTA and FIG on 2026-08-12.

    Measured against the whole session, "2% off the high" is true for any name that is off its opening
    high — most names, most afternoons. So arming PEAK fired a trim within seconds, on a high set minutes
    after the open:

        OKTA  armed 13:48:12, trimmed 13:48:16 and 13:48:46 at 150.75
              (the earlier ladder had sold 52 shares at ~148.61 that morning)

    Here the session spikes to 200 early, falls back to 150, and PEAK arms afterwards. Against the
    session high of 200 the price is 25% off and the fade is instantly true. Against the high SINCE ARM
    it is at its high and there is nothing to fade from.
    """
    early_spike = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(9, 31 + i)) for i, h in enumerate([180, 200, 160])]
    since_arm = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(11, i)) for i, h in enumerate([150, 150, 150])]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=early_spike + since_arm), now_ts_ns=_rth_ts_ns(11, 3)
    )
    armed_ns = _rth_ts_ns(10, 55)

    assert _session_high(strategy, "PENG.XNAS") == 200.0
    assert _session_high(strategy, "PENG.XNAS", since_ns=armed_ns) == 150.0


def test_a_rearm_does_not_inherit_the_previous_chains_high():
    """Why this also blunts the re-ladder (#266): a fresh arm starts from a fresh high, so the second
    chain cannot fire on the reason the first one already acted upon."""
    bars = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(10, i)) for i, h in enumerate([200, 150, 150])]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars), now_ts_ns=_rth_ts_ns(10, 3))
    assert _session_high(strategy, "PENG.XNAS", since_ns=_rth_ts_ns(10, 1)) == 150.0


def test_an_unreadable_arm_time_falls_back_to_the_session_high():
    """None must mean "behave as before", never "no high" — a row with an unreadable timestamp should
    keep protecting rather than silently stop."""
    from api.engine_node import _armed_at_ns

    class _NoClock:
        created_at = None

    assert _armed_at_ns(_NoClock()) is None
    bars = [_FakeBar(high=200, close=200, ts_event=_rth_ts_ns(10, 0))]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars), now_ts_ns=_rth_ts_ns(10, 1))
    assert _session_high(strategy, "PENG.XNAS", since_ns=None) == 200.0


def test_trigger_and_apply_read_the_same_window():
    """Two derivations of one fact will disagree — that has already bitten leash validation and the
    manager-armed check. Here a disagreement would let apply() act on a fade trigger_met never saw."""
    import inspect

    from api.engine_node import _PeakWatch

    for fn in (_PeakWatch.trigger_met, _PeakWatch.apply):
        src = inspect.getsource(fn)
        assert "_armed_at_ns(row)" in src, f"{fn.__name__} must use the arm-scoped high"
        # Derived ONCE per path and reused, matching PYRAMID (#283). Merely asserting the call appears
        # somewhere passed while each path derived it twice — and two derivations of one fact is the
        # exact failure this test exists to prevent, one level down. (codex review, Medium.)
        assert src.count("_armed_at_ns(row)") == 1, f"{fn.__name__} must derive the arm instant once"
        assert src.count("since_ns=armed_ns") == 2, (
            f"{fn.__name__} must pass the arm instant to BOTH `_session_high` and `_sustained_fade` — "
            f"scoping only the high leaves the fade's own lower-high branch reading pre-arm bars"
        )


def test_the_fade_itself_reads_the_arm_scoped_window_not_just_the_high():
    """Scoping only `hod` left half the bug in place (codex review, High). `_sustained_fade`'s own
    lower-high branch reads bars directly, so a fresh arm could still trim on a descending sequence that
    completed before it armed.

    Three descending bars finish BEFORE the arm, then ONE bar prints after it. Session-wide the last
    three bars are 190/180/175 — strictly descending, so the fade fires. Arm-scoped the window is a
    single bar sitting at its own high, so there is nothing to fade from.

    The one-bar tail matters: an earlier version of this test put three flat bars after the arm, which
    made the session-wide last-three flat too, so it passed with the bug reintroduced and proved nothing.
    """
    pre = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(9, 31 + i)) for i, h in enumerate([200, 190, 180])]
    post = [_FakeBar(high=175, close=175, ts_event=_rth_ts_ns(11, 0))]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=pre + post), now_ts_ns=_rth_ts_ns(11, 1))

    assert _sustained_fade(
        strategy, "PENG.XNAS", 200.0, off_hod_pct=2, lower_high_bars=3
    ), "session-wide, the pre-arm descent is the last three bars and fires"
    assert not _sustained_fade(
        strategy, "PENG.XNAS", 175.0, off_hod_pct=2, lower_high_bars=3, since_ns=_rth_ts_ns(10, 55)
    ), "arm-scoped, only the post-arm bar is visible and it is at its high"


def test_the_bar_containing_the_arm_is_included():
    """Alpaca stamps a minute bar at the LEFT edge of its interval, so an arm at 13:48:12 sits INSIDE the
    13:48:00 bar. A plain `>=` dropped that bar and any post-arm high in it, so the scoped high read too
    low — or None until the next minute printed, disabling the fade for up to a minute. (codex, High.)"""
    bar_open = _rth_ts_ns(13, 48)
    bars = [_FakeBar(high=175, close=175, ts_event=bar_open)]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars), now_ts_ns=_rth_ts_ns(13, 49))

    armed_mid_bar = bar_open + 12_000_000_000  # 13:48:12
    assert _session_high(strategy, "PENG.XNAS", since_ns=armed_mid_bar) == 175.0


def test_a_bar_that_closed_before_the_arm_minute_is_still_excluded():
    """The flooring must not swallow the PREVIOUS bar too — otherwise it would reintroduce exactly the
    pre-arm history this is meant to exclude."""
    bars = [
        _FakeBar(high=900, close=900, ts_event=_rth_ts_ns(13, 47)),
        _FakeBar(high=175, close=175, ts_event=_rth_ts_ns(13, 48)),
    ]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars), now_ts_ns=_rth_ts_ns(13, 49))
    armed = _rth_ts_ns(13, 48) + 12_000_000_000
    assert _session_high(strategy, "PENG.XNAS", since_ns=armed) == 175.0


def test_trigger_met_does_not_fire_on_a_fade_that_finished_before_arming():
    """Behavioural, not a source-string check (codex review, Medium).

    The `_armed_at_ns(row)` guard above passes as long as EITHER call site is scoped, so dropping
    `since_ns` from the `_sustained_fade` call alone would slip past it while PEAK reproduced the
    production bug. This exercises the real path: three descending bars complete before the arm, one bar
    prints after it. Session-wide the last three are 190/180/175 and the fade fires. Arm-scoped, PEAK is
    looking at a single bar sitting at its own high and must not trim.
    """
    pre = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(9, 31 + i)) for i, h in enumerate([200, 190, 180])]
    post = [_FakeBar(high=175, close=175, ts_event=_rth_ts_ns(11, 0))]
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=pre + post, bars_1d=[]),
        now_ts_ns=_rth_ts_ns(11, 1),
        position=_FakePosition(quantity=100),
        last_price=175.0,
    )
    # `tightened` already true, so the blow-off branch is spent and only the fade can fire.
    base = _row()
    row = _row(
        created_at=datetime.fromtimestamp(_rth_ts_ns(10, 55) / 1e9, tz=UTC),
        params={**base.params, "tightened": True},
    )

    import asyncio

    assert asyncio.run(_PeakWatch().trigger_met(strategy, row)) is False


def test_the_trim_hands_its_successor_what_to_wait_for():
    """End-to-end wiring: `_awaiting_fill` is inert unless the trim actually records `awaiting_qty` on
    the successor it attaches. Asserting the helper alone would pass with the wiring removed."""
    import inspect

    from api.engine_node import _PeakWatch

    src = inspect.getsource(_PeakWatch.apply)
    assert '"awaiting_qty": remaining_qty' in src, (
        "the trim must record the size the successor should wait for"
    )


def test_the_wait_for_a_fill_is_bounded_in_time():
    """A quantity-only wait strands the chain forever when the trim partially fills and never reaches the
    exact remainder, or when a PYRAMID add / upward reconciliation pushes the position back above it. The
    position would be left running with a trail sized for a smaller remainder and no manager willing to
    act — the opposite of what this protects. After the deadline PEAK proceeds and sizes off what is
    actually held, which is never worse than not acting. (codex review, High.)"""
    from api.engine_node import _AWAIT_FILL_TIMEOUT_NS, _awaiting_fill

    armed = _rth_ts_ns(10, 0)
    row = _row(
        params={**_row().params, "awaiting_qty": 32.0},
        created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
    )
    stuck = _FakePosition(quantity=58)  # never comes down

    just_inside = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + _AWAIT_FILL_TIMEOUT_NS - 1)
    assert _awaiting_fill(just_inside, row, stuck) is True

    past_deadline = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + _AWAIT_FILL_TIMEOUT_NS + 1)
    assert _awaiting_fill(past_deadline, row, stuck) is False, "must not wait forever"


def test_a_quantity_a_hair_above_the_expected_remainder_counts_as_landed():
    """Quantities arrive as Decimal/float; a value that should equal the remainder can render just above
    it, and an exact `>` would read that as still pending. (codex review, Medium.)"""
    from api.engine_node import _awaiting_fill

    armed = _rth_ts_ns(10, 0)
    row = _row(
        params={**_row().params, "awaiting_qty": 32.0},
        created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
    )
    clock = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + 1_000_000_000)
    assert _awaiting_fill(clock, row, _FakePosition(quantity=32.0000000001)) is False
    # A fresh row each time — the predicate must not depend on call order.
    row2 = _row(
        params={**_row().params, "awaiting_qty": 32.0},
        created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
    )
    assert _awaiting_fill(clock, row2, _FakePosition(quantity=33.0)) is True


def test_a_closed_position_terminalizes_even_after_its_cycle_is_gone():
    """The flat check must run BEFORE the cycle-drift guard (codex review, High).

    When a position closes, the projection emits the CLOSED cycle and then POPS it, so by the next
    dispatch tick `current_cycle_for()` returns None. The drift guard reads that as "the cycle changed"
    and returns FAILED — for a manager whose position simply finished. The row would surface red, as an
    error, when nothing went wrong.

    The earlier version of this test ran with `trade_cycles=None`, which skips the guard entirely, so it
    passed with the ordering reversed and proved nothing.
    """
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=_armed_bars()),
        now_ts_ns=_rth_ts_ns(10, 5),
        position=None,                       # flat
        trade_cycles=_FakeTradeCycles([]),   # cycle already popped -> current_cycle_for() is None
    )
    row = _row(cycle_id="cycle-1")

    async def run():
        state, detail = await _PeakWatch().apply(strategy, row)
        assert state == "APPLIED", "a closed position is the job finishing, not a drift failure"
        assert "closed" in detail

    _sync(run)


def test_a_position_that_FLIPPED_is_not_reported_as_a_clean_close():
    """`_position_for` filters by SIDE, so a LONG row whose position went SHORT reads as None exactly
    like a closed one. Calling that APPLIED would hide a reopen behind a green completion — it has to
    fall through to the cycle-drift guard, which is what says "re-read and retry". (codex review, High.)"""

    class _FlippedStrategy(_FakeStrategy):
        def _position_for(self, instrument_id, strategy_id, side):
            return _FakePosition(quantity=40) if side == "SHORT" else None

    strategy = _FlippedStrategy(
        cache=_FakeCache(bars_1m=_armed_bars()),
        now_ts_ns=_rth_ts_ns(10, 5),
        trade_cycles=_FakeTradeCycles([]),
    )

    async def run():
        state, detail = await _PeakWatch().apply(strategy, _row(cycle_id="cycle-1"))
        assert state == "FAILED", "a flip must not read as a clean close"
        assert "cycle" in detail

    _sync(run)


def test_an_add_after_the_trim_does_not_re_trip_the_fill_wait():
    """"Held is above the expected remainder" is not by itself an unlanded fill — a PYRAMID add or an
    upward reconciliation reads the same. The wait window is bounded ABOVE by the pre-trim size, so only
    the trim's own window counts. (codex review, Medium.)"""
    from api.engine_node import _awaiting_fill

    armed = _rth_ts_ns(10, 0)

    def row_with(**extra):
        return _row(
            params={**_row().params, "awaiting_qty": 32.0, "awaiting_from": 58.0, **extra},
            created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
        )

    clock = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + 1_000_000_000)
    assert _awaiting_fill(clock, row_with(), _FakePosition(quantity=58)) is True   # fill not landed
    assert _awaiting_fill(clock, row_with(), _FakePosition(quantity=45)) is True   # partially landed
    assert _awaiting_fill(clock, row_with(), _FakePosition(quantity=32)) is False  # landed
    assert _awaiting_fill(clock, row_with(), _FakePosition(quantity=80)) is False  # an ADD, not a lag


def test_a_flip_within_the_SAME_cycle_is_not_reported_as_closed():
    """The flat-first check falls through when something is open on the other side — and if the cycle
    guard then PASSES (same cycle, or no cycle_id at all), the downstream `pos is None` branch used to
    return APPLIED and report a flip as a clean close. The round-2 test only covered `current is None`,
    so it missed this path entirely. (codex review, round 3, High.)"""

    class _FlippedStrategy(_FakeStrategy):
        def _position_for(self, instrument_id, strategy_id, side):
            return _FakePosition(quantity=40) if side == "SHORT" else None

    # No cycle_id -> the drift guard is skipped entirely, which is the bypass.
    strategy = _FlippedStrategy(cache=_FakeCache(bars_1m=_armed_bars()), now_ts_ns=_rth_ts_ns(10, 5))

    async def run():
        state, detail = await _PeakWatch().apply(strategy, _row(cycle_id=None))
        assert state == "FAILED", "a flip is a reopen, not a completion"
        assert "flipped" in detail

    _sync(run)


def test_arming_inherits_the_trims_the_cycle_has_already_spent():
    """#266 wiring. `trim_max` bounds the POSITION, but it was only compared against a chain-local
    `trim_count` that restarted at 0 on every fresh arm — so a re-arm handed PEAK a brand new budget.

    OKTA, 2026-08-12: chain 1 spent both trims (58 -> 6); re-armed at 13:48:12 with trim_count back to 0;
    chain 2 spent two more within 34 seconds (6 -> 2), at a HIGHER price than chain 1 had sold at.

    The pure reduction is tested in `test_managers.py`; this pins that the ARM actually consults it.
    Without this, seeding could be reverted to 0 and every other test would still pass.
    """
    import api.managers as mg

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda i, sid, side: None
        s._identified_protective_stop_open = lambda i, sid, side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda i, sid, side: []
        s._submit_trailing_stop = lambda **kw: _FakeOrder(is_open=True)
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        s._reserve_attach_command = _fake_reserve_proceed()
        captured = {}

        async def fake_commit(cid, ledger_cid, **kwargs):
            captured.update(kwargs)
            return "ok", "armed"

        s._commit_attach_command = fake_commit

        original = mg.trims_spent_on_cycle

        async def two_already_spent(session, kind, instrument_id, strategy_id, cycle_id):
            return 2

        mg.trims_spent_on_cycle = two_already_spent
        try:
            status, _ = await s._handle_attach_manager_command(
                "cid-1",
                {"kind": "peak_watch", "instrument_id": "PENG.XNAS", "strategy_id": "MANUAL-001",
                 "cycle_id": "cycle-1", "params": _FULL_PEAK_ARM_PARAMS},
                "entry-1",
            )
        finally:
            mg.trims_spent_on_cycle = original

        assert status == "ok"
        assert captured["params"]["trim_count"] == 2, (
            "a re-arm must inherit the cycle's spent budget, not start a fresh ladder"
        )

    _sync(run)


def test_the_trimming_row_records_its_own_spend():
    """#266, codex review High. The successor that carries `trim_count + 1` is attached AFTER
    `chain_cancelled` is consulted — so a PEAK switched off mid-apply spends a trim that no row records.
    `max_trim_count` would miss it and a later re-arm of the same cycle would be handed back a budget it
    had already used. The row that PERFORMED the trim records it, successor or not."""
    import inspect

    from api.engine_node import _PeakWatch

    src = inspect.getsource(_PeakWatch.apply)
    trim_at = src.index("strategy._submit(trim_order)")
    # The CALL, not the word — an earlier version matched the comment above the fix and inverted this.
    chain_at = src.index("mg.chain_cancelled(", trim_at)
    record_at = src.index("mg.record_trim(", trim_at)
    assert record_at < chain_at, (
        "the spend must be recorded BEFORE the chain-cancelled check, or an OFF mid-apply loses it"
    )


# --- #255 High 4: no manager acts outside regular trading hours ---------------------------------------


def test_peak_does_not_trigger_outside_regular_hours():
    """#255 High 4. Nothing checked the session before acting.

    After the close a stale same-day fade reading can still cancel protection and submit a DAY market order
    with `extended_hours=False` — an order that cannot fill and, per #252, is not checked for acceptance
    either. So PEAK could strip a stop at 16:30 and leave the position naked overnight, having sold nothing.

    This matters more now that #239 rests backstop stops at the venue: a backstop that rests during RTH
    while PEAK cancels it after hours is not protection.

    The gate lives in `trigger_met`, deliberately, NOT in `apply`. `apply` has exactly two outcomes and
    FAILED is TERMINAL (#255 High 2, still open) — declining there would permanently disarm the manager.
    Returning False from `trigger_met` leaves the row ARMED and simply waits for the next session.
    """
    faded = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(10, i)) for i, h in enumerate([200, 190, 180, 175])]

    def _peak_strategy(now_ns: int):
        return _FakeStrategy(
            cache=_FakeCache(bars_1m=faded), now_ts_ns=now_ns,
            position=_FakePosition(quantity=100), last_price=175.0,
        )

    row = _row()

    # In hours, this fade is real and PEAK acts on it.
    assert asyncio.run(_PeakWatch().trigger_met(_peak_strategy(_rth_ts_ns(11, 0)), row)) is True

    # Outside hours the same reading must not act.
    for hour, minute, label in [(4, 0, "pre-market"), (16, 30, "after the close"), (20, 0, "post-market")]:
        strategy = _peak_strategy(_rth_ts_ns(hour, minute))
        assert asyncio.run(_PeakWatch().trigger_met(strategy, row)) is False, label


# --- #288: the ARM uses ATR-scaled widths, not the flat percentage the caller sent -------------------


def test_attach_peak_scales_the_trail_to_the_SYMBOL_not_the_callers_flat_percentage():
    """#288, and the production wiring specifically.

    Removing the scaling call at attach passed every other test in this repo — the pure derivation was
    covered and the code path that USES it was not, which is the same "tested the helper, not the seam"
    gap that let a float reach a Cython Quantity boundary earlier in this branch.

    FIG on the live book carried a PEAK trail of 2.5% against a 7.94% ATR — 0.31x ATR, firing on an
    ordinary session. The caller still sends 250bps; the engine must place the ATR-scaled width instead.
    """
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda i, st, r: None
        s._identified_protective_stop_open = lambda i, st, r: None  # widened identifier (#303)
        s._reducing_orders_open = lambda i, st, r: []
        s._last_price_for = lambda instrument_id: 26.28
        # ATR 2.087 on a 26.28 price = 7.94%; at 1.5x that is 11.91% = 1191bps.
        s._protection_atr = lambda instrument_id, lookback: 2.087
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: (submitted_trails.append(kw), _FakeOrder(is_open=True))[1]
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        s._cancel = lambda order: None
        captured = {}
        s._reserve_attach_command = _fake_reserve_proceed()

        async def fake_commit(cid, ledger_cid, **kwargs):
            captured.update(kwargs)
            return "ok", "stubbed"

        s._commit_attach_command = fake_commit

        status, _ = await s._handle_attach_manager_command(
            "cid-9",
            {"kind": "peak_watch", "instrument_id": "PENG.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-9",
        )

        assert status == "ok"
        assert len(submitted_trails) == 1
        # The ORDER carries the scaled width — 250bps sent, 1191bps placed.
        assert submitted_trails[0]["trail_bps"] == 1191
        # And the armed row records it, so every later tighten/resync reads the same number.
        assert captured["params"]["trail_wide_bps"] == 1191
        assert captured["params"]["trail_tight_bps"] < 1191

    _sync(run)


def test_attach_peak_falls_back_to_the_callers_width_when_the_ATR_cannot_be_measured():
    """Scaling is an improvement on the caller's width, never a precondition for arming.

    A missing cache, an unloaded instrument or a symbol without enough history must leave PEAK working
    exactly as before rather than refusing to arm — a stop at a defensible-but-imperfect width protects
    more than no stop at all.
    """
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100)
        s._bracket_protective_stop_open = lambda i, st, r: None
        s._identified_protective_stop_open = lambda i, st, r: None  # widened identifier (#303)
        s._reducing_orders_open = lambda i, st, r: []
        s._protection_atr = lambda instrument_id, lookback: None      # cannot measure
        s._last_price_for = lambda instrument_id: 26.28
        submitted_trails = []
        s._submit_trailing_stop = lambda **kw: (submitted_trails.append(kw), _FakeOrder(is_open=True))[1]
        
        # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
        # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
        # old safety net where a rejected trail left the previous stop untouched. Without this stub
        # the test drives the real six-second poll.
        async def _confirm_resting(coid, timeout_s=6.0):
            return True
        
        s._await_protection_resting = _confirm_resting
        s._cancel = lambda order: None
        s._reserve_attach_command = _fake_reserve_proceed()

        async def fake_commit(cid, ledger_cid, **kwargs):
            return "ok", "stubbed"

        s._commit_attach_command = fake_commit

        status, _ = await s._handle_attach_manager_command(
            "cid-10",
            {"kind": "peak_watch", "instrument_id": "PENG.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-10",
        )
        assert status == "ok"
        assert submitted_trails[0]["trail_bps"] == 250      # the caller's own value stands

    _sync(run)


# --- #303: the #239 backstop must not block arming -----------------------------------------------


def test_a_backstop_trailing_stop_is_identifiable_and_does_not_block_arming():
    """#303, a regression I introduced with #239.

    The arm guard demands zero resting exits, or exactly one that IS the identified protective stop. A
    backstop `PROT-` trailing stop is one resting exit that the old `_bracket_protective_stop_open` could
    never match — wrong order type, no `bracket:` tag — so arming was refused on every protected position
    with "cancel it manually", which would leave the position naked to satisfy a guard.

    The guard is right: a second unidentified resting order surviving an arm is how a stale exit ended up
    beside PEAK's own fresh trail. What changed is that a resting protective stop is now NORMAL rather
    than suspicious.
    """
    from api.engine_node import _is_identifiable_protective_stop

    class _O:
        def __init__(self, otype, coid, tags=None):
            from types import SimpleNamespace
            self.order_type = otype
            self.client_order_id = SimpleNamespace(value=coid)
            self.tags = tags

    from nautilus_trader.model.enums import OrderType

    backstop = _O(OrderType.TRAILING_STOP_MARKET, "PROT-SELL-AEM-XNYS-10fe8409")
    bracket = _O(OrderType.STOP_MARKET, "abc123", ["bracket:xyz"])
    peak_trail = _O(OrderType.TRAILING_STOP_MARKET, "PKW-904524631d3b4a9c9f6f")
    stranger = _O(OrderType.LIMIT, "SOMEONE-ELSES")

    assert _is_identifiable_protective_stop(backstop) is True
    assert _is_identifiable_protective_stop(bracket) is True
    # PEAK's own trail is identifiable too — re-arming over it must not be refused as "unidentified".
    assert _is_identifiable_protective_stop(peak_trail) is True
    # A take-profit limit is NOT protection and NOT ours to silently cancel.
    assert _is_identifiable_protective_stop(stranger) is False


def test_the_arm_guards_USE_the_widened_identifier_not_the_bracket_only_one():
    """#303 — the wiring, not the predicate.

    Every arm test in this file stubs the identifier, so swapping the guard back to the bracket-only
    lookup passed all 79 of them. The predicate was covered and the code path that uses it was not — the
    sixth instance of that shape this session.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._handle_attach_manager_command)
    # Both arms read the widened identifier for their "exactly one identifiable resting exit" guard.
    assert src.count("_identified_protective_stop_open") >= 2, (
        "an arm guard still uses the bracket-only lookup — it will refuse to arm on any position the "
        "#239 backstop protects, telling the operator to cancel the stop that is protecting it"
    )


def test_the_identifier_finds_a_backstop_stop_resting_on_the_position():
    """The predicate against a real cache shape, bound to a double — `cache` is a read-only Cython
    attribute so a bare strategy cannot be given one."""
    from types import SimpleNamespace

    from nautilus_trader.model.enums import OrderSide, OrderType

    from api.engine_node import UiFeedStrategy

    def _order(otype, coid, side=OrderSide.SELL, tags=None):
        return SimpleNamespace(
            instrument_id="AEM.XNYS", strategy_id="MANUAL-001", side=side,
            order_type=otype, tags=tags,
            client_order_id=SimpleNamespace(value=coid),
        )

    backstop = _order(OrderType.TRAILING_STOP_MARKET, "PROT-SELL-AEM-XNYS-10fe8409")
    # OUR coid — a bracket's take-profit leg carries one. So only the TYPE check can exclude it, which is
    # the point: a limit is not protection, and cancelling it would silently discard an operator's target.
    take_profit = _order(OrderType.LIMIT, "PK-445f5c28-a03a-411d-8")

    class _Fake:
        cache = SimpleNamespace(orders_open=lambda: [take_profit, backstop])

    found = UiFeedStrategy._identified_protective_stop_open(_Fake(), "AEM.XNYS", "MANUAL-001", OrderSide.SELL)
    assert found is backstop, "the backstop's own stop must be identifiable, or arming is refused over it"

    class _OnlyTakeProfit:
        cache = SimpleNamespace(orders_open=lambda: [take_profit])

    assert UiFeedStrategy._identified_protective_stop_open(
        _OnlyTakeProfit(), "AEM.XNYS", "MANUAL-001", OrderSide.SELL
    ) is None, "a take-profit is not protection and must never be silently cancelled by an arm"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #265 — arming PEAK over a BRACKET must work, not refuse.
#
# NBIS, 2026-08-15: 29 shares from the order dialogue, a bracket resting a stop at 260 and a target at
# 310.61. Both are reducing SELLs, so the old guard ("exactly one resting order, and it IS the identified
# stop") refused every bracketed position and told the operator to cancel by hand. Operator: "I want PEAK to
# work."
#
# The ORDERING is forced, not chosen. Alpaca reserves shares against any resting sell and frees them only
# on a CONFIRMED cancel (#245/#252), so submitting the trail first is rejected on `available: 0`. Every
# PEAK arm that ever succeeded in production had zero resting orders.
# ─────────────────────────────────────────────────────────────────────────────────────────────────


def _bracket_pair(entry_coid="entry-nbis"):
    """The two legs Alpaca rests for one bracket, as production tags them: ONE shared `bracket:<entry>`."""
    from nautilus_trader.model.enums import OrderSide, OrderType

    stop = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                          [f"bracket:{entry_coid}"])
    target = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.LIMIT,
                            [f"bracket:{entry_coid}"])
    return stop, target


class _VenueOrders:
    """The broker's own view of what rests, which production ALWAYS has and the old double did not (#269).

    The arm now asks the venue as well as the cache, because those two disagree in production: an order
    whose submit failed after Alpaca accepted it sits in the cache as REJECTED — terminal, invisible to
    `orders_open()` — while it rests at the venue holding the shares. On 2026-08-17 that let the arm guard
    conclude NBIS was bare and PEAK's own trail was then rejected `available: 0` by PEAK's own stop.

    `rows` defaults to mirroring the cache, which is the healthy case. Pass rows the cache does NOT have to
    reproduce the divergence; a double that could only ever agree with the cache could not express the bug.
    """

    #: Tickers these fixtures trade. Enumerated rather than wildcarded because Alpaca reports a position
    #: per symbol and `_venue_shares_available` matches on the symbol — a double that answered for every
    #: string would hide a real failure mode, where we ask about one instrument and the broker answers
    #: about another.
    SYMBOLS = ("NBIS", "PENG", "AEM", "OKTA", "FIG", "WDAY", "SMH", "PBF", "BETA", "WPM", "VFLO")

    def __init__(self, rows: list[dict], held: float) -> None:
        self._rows, self._held = rows, held
        self.cancelled: list[str] = []
        #: A broker that raises, so the "cannot read → refuse" branch is reachable. Production's HTTP
        #: client raises `AlpacaHttpError`; a double that could only ever succeed would leave that branch
        #: untested while every other test passed.
        self.readable = True

    async def list_orders(self, status: str = "all", limit: int = 500, paginate: bool = False) -> list[dict]:
        """MIRRORS PRODUCTION'S SIGNATURE, including `paginate` (#387).

        A double whose signature is narrower than production's does not fail where the drift is. The
        caller's `except Exception` swallows the `TypeError` and the test sees "could not read broker
        orders — assuming nothing is UNSAFE", which is a LEGITIMATE production state with its own
        branch. So the failure surfaces as an unrelated assertion about a missing log line, and 33
        tests across this file and `test_peak.py` failed pointing at the wrong thing.

        That is the mild version. The dangerous version is a test whose expectation happens to match
        the unreadable-broker branch: it would have stayed green and asserted nothing at all. Keep
        this signature identical to `AlpacaHttpClient.list_orders`. Fix the double, never loosen
        production.
        """
        if not self.readable:
            raise RuntimeError("alpaca unreachable")
        return [r for r in self._rows if str(r.get("id")) not in self.cancelled]

    async def list_positions(self) -> list[dict]:
        if not self.readable:
            raise RuntimeError("alpaca unreachable")
        # Availability is DERIVED from what is still resting, never asserted as a constant. Alpaca frees a
        # reservation on the confirmed cancel, so a double that returned a fixed number could not tell a
        # cancel that worked from one that did not — which is the only thing the wait is measuring.
        open_rows = await self.list_orders()
        out = []
        for symbol in self.SYMBOLS:
            still = sum(float(r.get("qty", 0)) for r in open_rows if r.get("symbol") == symbol)
            out.append({
                "symbol": symbol,
                "qty": str(self._held),
                "qty_available": str(max(0.0, self._held - still)),
            })
        return out

    async def cancel_order(self, venue_order_id: str) -> None:
        self.cancelled.append(str(venue_order_id))


def _venue_row(order, venue_id: str, qty: float = 29.0) -> dict:
    """One cache order as Alpaca would report it — lowercase side, string quantities, a venue id.

    The symbol comes from the order's own instrument id rather than being hard-coded: a fixture whose
    venue rows all claimed one ticker would make every cross-symbol mismatch invisible.
    """
    return {
        "id": venue_id,
        "client_order_id": str(order.client_order_id),
        "symbol": str(order.instrument_id).split(".")[0],
        "side": "sell",
        "type": "trailing_stop",
        "status": "new",
        "qty": qty,
        "filled_qty": 0,
    }


def _arm_over(resting, identified, *, clears=True, venue_rows=None, held=29.0):
    """Drive the REAL arm handler with these orders resting. Returns (status, error, order_of_events)."""
    s = _bare_strategy()
    s._orders_armed = True
    if venue_rows is None:
        # The healthy case: the venue holds exactly what the cache holds.
        venue_rows = [_venue_row(o, f"venue-{i}") for i, o in enumerate(resting)]
    if venue_rows == "unreadable":
        venue_rows = []
        s._http = _VenueOrders([], held=held)
        s._http.readable = False
    else:
        s._http = _VenueOrders(venue_rows, held=held)
    s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=29)
    s._bracket_protective_stop_open = lambda i, st, rs: identified
    s._identified_protective_stop_open = lambda i, st, rs: identified
    s._reducing_orders_open = lambda i, st, rs: list(resting)
    events: list = []

    def _submit(**kw):
        events.append(("submit", kw.get("coid")))
        return object()

    s._submit_trailing_stop = _submit
    
    # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
    # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
    # old safety net where a rejected trail left the previous stop untouched. Without this stub
    # the test drives the real six-second poll.
    async def _confirm_resting(coid, timeout_s=6.0):
        return True
    
    s._await_protection_resting = _confirm_resting

    def _cancel(order):
        events.append(("cancel", order))
        # A NATIVE cancel reaches the venue too — that is the entire point of sending it. A double that
        # recorded the call without freeing the shares leaves the reservation standing forever, so the
        # availability wait would time out on every test and report a venue failure that never happened.
        coid = str(order.client_order_id)
        for row in venue_rows:
            if str(row.get("client_order_id")) == coid:
                s._http.cancelled.append(str(row["id"]))

    s._cancel = _cancel

    async def _clear(instrument_id, strategy_id, reducing_side, timeout_s=6.0):
        events.append(("await_clear", None))
        return clears

    s._await_reducing_orders_clear = _clear

    async def _resting(coid, timeout_s=6.0):
        events.append(("confirm_resting", coid))
        return True

    s._await_protection_resting = _resting
    s._reserve_attach_command = _fake_reserve_proceed()
    s._reject_reserved_ledger_entry = _noop_async_reject

    async def fake_commit(cid, ledger_cid, **kwargs):
        events.append(("commit", None))
        return "ok", "armed"

    s._commit_attach_command = fake_commit

    async def run():
        return await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "NBIS.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-1",
        )

    holder: list = []
    _sync(lambda: _capture(run, holder))
    status, error = holder[0]
    return status, error, events


async def _capture(run, holder):
    holder.append(await run())


def test_the_fixture_really_is_two_reducing_legs_of_one_bracket():
    """The premise, asserted before anything is derived from it (a test that cannot fail says nothing).

    If the two legs ever stopped sharing a tag, every assertion below would still pass — for the wrong
    reason, because the guard would be refusing rather than clearing.
    """
    stop, target = _bracket_pair()
    stop_tags = {t for t in stop.tags if t.startswith("bracket:")}
    target_tags = {t for t in target.tags if t.startswith("bracket:")}
    assert stop_tags and stop_tags == target_tags, "the legs must share one bracket group"


def test_peak_arms_over_a_bracket_and_clears_BOTH_legs():
    """The NBIS case. Refusing here is what made PEAK unusable on every bracketed position."""
    stop, target = _bracket_pair()
    status, error, events = _arm_over([stop, target], stop)

    assert status != "error", f"PEAK still refuses to arm over a bracket: {error}"
    cancelled = [o for kind, o in events if kind == "cancel"]
    assert stop in cancelled, "the protective leg was left resting beside PEAK's own trail"
    assert target in cancelled, "the take-profit was left resting, reserving the shares"


def test_the_cancel_precedes_the_submit_because_alpaca_reserves_the_shares():
    """The ordering is the fix, not an implementation detail.

    Alpaca frees reserved shares only on a CONFIRMED cancel (#245/#252), so a trail submitted while the
    bracket still rests is rejected on `available: 0`. Place-then-cancel — the old order here — only ever
    worked because every successful arm had nothing resting. Pins cancel, then the confirmation WAIT, then
    the submit.
    """
    stop, target = _bracket_pair()
    _status, _error, events = _arm_over([stop, target], stop)

    kinds = [k for k, _ in events]
    assert "submit" in kinds, "nothing was placed at all"
    assert kinds.index("cancel") < kinds.index("submit"), "submitted into reserved shares"
    assert "await_clear" in kinds, "did not wait for the venue to confirm the cancel"
    assert kinds.index("await_clear") < kinds.index("submit"), "placed before the cancel was confirmed"


def test_an_unconfirmed_cancel_places_NOTHING_and_says_the_position_may_be_naked():
    """The cost of cancelling first, handled rather than hidden.

    If the venue does not confirm, the shares may still be reserved and a submit would be rejected — so
    this aborts. But the orders were already cancelled, so the position can be unprotected, and saying
    only "arm failed" would leave the operator believing nothing changed.
    """
    stop, target = _bracket_pair()
    status, error, events = _arm_over([stop, target], stop, clears=False)

    assert status == "error"
    assert "unprotected" in error.lower(), f"did not warn that protection was removed: {error}"
    assert [k for k, _ in events if k == "submit"] == [], "placed a trail after an unconfirmed cancel"


def test_an_unrelated_resting_sell_still_refuses():
    """The half of the guard that must NOT be widened.

    A resting sell that is neither this position's protection nor a leg of its own bracket is exactly the
    case where guessing strips something the operator wanted. It keeps refusing.
    """
    from nautilus_trader.model.enums import OrderSide, OrderType

    stop, _target = _bracket_pair()
    stranger = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.LIMIT,
                              ["bracket:some-other-entry"])
    status, error, events = _arm_over([stop, stranger], stop)

    assert status == "error"
    assert "cancel it manually" in error
    assert [k for k, _ in events if k == "cancel"] == [], "cancelled an order it could not account for"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# #269 — THE SAME GUARD, ASKED OF THE BROKER.
#
# Everything above inspects `cache.orders_open()`. On 2026-08-17 that set was EMPTY for NBIS while a
# 29-share trailing stop rested at Alpaca: PEAK's own earlier stop, recorded locally as REJECTED after
# its submit's HTTP call failed, and unrepairable because Nautilus treats REJECTED as terminal
# (`InvalidStateTrigger: REJECTED -> ACCEPTED`, 81,128 times).
#
# So the guard saw a bare position, permitted the arm, and PEAK's replacement trail was rejected by
# PEAK's own stop:
#
#     PK-519d9490-d719-4442-8  403  insufficient qty available (requested: 16, available: 0)
#
# The manager was fighting itself and no cache-based guard could ever see it.
# ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_fixture_really_hides_the_stop_from_the_cache():
    """The premise, before anything is derived from it.

    If the venue row were also visible to the cache, every assertion below would pass with the broker
    check deleted entirely — cache truth and broker truth would agree and there would be nothing for the
    new guard to catch. So pin the disagreement itself: `_reducing_orders_open` must see NOTHING while
    the broker holds a 29-share stop.
    """
    from nautilus_trader.model.enums import OrderSide

    stop, _ = _bracket_pair()
    hidden = _venue_row(stop, "venue-hidden")
    hidden["client_order_id"] = "PKW-stuck-in-rejected"

    s = _bare_strategy()
    s._http = _VenueOrders([hidden], held=29.0)
    s._reducing_orders_open = lambda i, st, rs: []  # the cache, blind to a terminal-state order

    rows = asyncio.run(s._venue_only_reducing_rows("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert [r["client_order_id"] for r in rows] == ["PKW-stuck-in-rejected"], (
        "the broker check cannot see the stop the cache is missing — there is no bug left to catch"
    )
    assert float(asyncio.run(s._http.list_positions())[0]["qty_available"]) == 0.0, (
        "with 29 held against a 29-share stop, nothing is available — this is the 403 the operator hit"
    )


def test_a_HIDDEN_stop_of_OURS_is_cleared_at_the_venue_BEFORE_the_trail_is_placed():
    """The live NBIS case, ending the way it should have.

    PEAK's own earlier trail rests at the broker holding all 29 shares and the cache cannot see it. The
    arm must not refuse — it is our own protection and replacing it is the point — but it must also not
    submit into reserved shares, which is what produced `requested: 16, available: 0` five times.
    """
    stop, _ = _bracket_pair()
    hidden = _venue_row(stop, "venue-hidden")
    hidden["client_order_id"] = "PKW-ours-but-stuck"

    status, error, events = _arm_over([], None, venue_rows=[hidden])

    assert status == "ok", f"refused to arm over its own stop: {error!r}"
    assert [k for k, _ in events if k == "submit"], "nothing was placed"


def test_the_arm_REFUSES_over_a_stop_only_the_BROKER_can_see():
    """The live NBIS case. The cache is empty, the venue holds 29 shares, and the arm must not proceed as
    if the position were bare — whatever it does next, it must not be "submit and hope"."""
    hidden = _venue_row(_bracket_pair()[0], "venue-hidden")
    hidden["client_order_id"] = "SOMEONE-ELSES-ORDER"
    status, error, _events = _arm_over([], None, venue_rows=[hidden])

    assert status == "error"
    assert "did not place" in error or "cancel them manually" in error, (
        f"the refusal must name the unaccountable order: {error!r}"
    )


def test_a_HIDDEN_order_that_IS_ours_is_cleared_rather_than_refused():
    """Our own protection is a fine thing to replace — refusing over it told the operator to "cancel it manually",
    which would leave the position naked to satisfy a guard (#303). Being invisible to the cache does not
    change whose order it is: the `PKW-` prefix is still ours."""
    stop, _ = _bracket_pair()
    hidden = _venue_row(stop, "venue-hidden")
    hidden["client_order_id"] = "PKW-ours-but-stuck"
    status, error, events = _arm_over([stop], stop, venue_rows=[_venue_row(stop, "venue-cache"), hidden])

    assert status == "ok", f"refused to arm over its own stop: {error!r}"
    assert [k for k, _ in events if k == "submit"], "nothing was placed"


def test_the_arm_REFUSES_when_the_broker_cannot_be_read():
    """Unreadable is not "nothing is resting". An arm CANCELS the position's existing protection, and
    doing that without knowing what actually rests is the move that strips a live book."""
    stop, _ = _bracket_pair()
    status, error, events = _arm_over([stop], stop)
    assert status == "ok", "precondition: this fixture arms cleanly when the broker IS readable"

    s_status, s_error, s_events = _arm_over([stop], stop, venue_rows="unreadable")
    assert s_status == "error"
    assert "refusing to arm" in s_error, f"got {s_error!r}"
    assert [k for k, _ in s_events if k == "cancel"] == [], "cancelled protection while blind"


def test_the_naked_warning_COUNTS_the_hidden_orders_too():
    """The operator is told how many exits were cleared so they can check the broker. Counting only the
    cache-visible ones under-reports exactly the orders they most need to look for."""
    stop, _ = _bracket_pair()
    hidden = _venue_row(stop, "venue-hidden")
    hidden["client_order_id"] = "PKW-ours-but-stuck"
    _status, error, _events = _arm_over(
        [stop], stop, venue_rows=[_venue_row(stop, "venue-cache"), hidden], clears=False
    )
    assert "2 resting exit order(s)" in error, f"under-counted what was cancelled: {error!r}"


# Every exit AFTER the cancel leaves the position bare. Found by auditing my own change: two of the three
# said nothing about it. The timeout path warned; the failed-submit and failed-attach paths read as
# ordinary errors, which is worse than useless — the operator concludes "the arm didn't happen" and the
# stop is gone. The failed-submit path even carried a comment asserting it could not have cancelled
# anything, true only under the place-then-cancel ordering this change replaced.


def _arm_with_failing(*, submit_raises=False, commit_fails=False):
    from nautilus_trader.model.enums import OrderSide, OrderType

    s = _bare_strategy()
    s._orders_armed = True
    s._position_for = lambda i, st, side: _FakePosition(quantity=29)
    stop = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                          ["bracket:entry-nbis"])
    s._bracket_protective_stop_open = lambda i, st, rs: stop
    s._identified_protective_stop_open = lambda i, st, rs: stop
    s._reducing_orders_open = lambda i, st, rs: [stop]
    s._cancel = lambda order: None

    def _submit(**kw):
        if submit_raises:
            raise RuntimeError("venue said no")
        return object()

    s._submit_trailing_stop = _submit

    async def _cleared(instrument_id, strategy_id, reducing_side, timeout_s=6.0):
        return True

    s._await_reducing_orders_clear = _cleared

    async def _resting(coid, timeout_s=6.0):
        return True

    s._await_protection_resting = _resting
    s._reserve_attach_command = _fake_reserve_proceed()
    s._reject_reserved_ledger_entry = _noop_async_reject

    async def fake_commit(cid, ledger_cid, **kwargs):
        return ("error", "database unavailable") if commit_fails else ("ok", "armed")

    s._commit_attach_command = fake_commit

    async def run():
        return await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "NBIS.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-1",
        )

    holder: list = []
    _sync(lambda: _capture(run, holder))
    return holder[0]


def test_a_failed_submit_after_the_cancel_says_the_position_may_be_unprotected():
    """The old stop is already gone by the time the submit runs. Silence here reads as 'nothing changed'."""
    status, error = _arm_with_failing(submit_raises=True)
    assert status == "error"
    assert "venue said no" in error, "lost the underlying cause"
    assert "UNPROTECTED" in error.upper(), f"did not warn the stop was already cancelled: {error}"


def test_a_failed_attach_rollback_says_the_position_may_be_unprotected():
    """Worst of the three: the pre-existing stop was cancelled, and the rollback cancels the replacement.
    Both are gone, and the message used to read like a clean rollback."""
    status, error = _arm_with_failing(commit_fails=True)
    assert status == "error"
    assert "UNPROTECTED" in error.upper(), f"rollback did not warn the position is bare: {error}"


def test_no_false_alarm_when_nothing_was_cancelled():
    """The counter-case. Arming a position with NOTHING resting cancels nothing, so a failed submit leaves
    it exactly as it was. Crying 'unprotected' there would train the operator to ignore the word."""
    from nautilus_trader.model.enums import OrderSide  # noqa: F401 — parity with the helper above

    s = _bare_strategy()
    s._orders_armed = True
    s._position_for = lambda i, st, side: _FakePosition(quantity=29)
    s._bracket_protective_stop_open = lambda i, st, rs: None
    s._identified_protective_stop_open = lambda i, st, rs: None
    s._reducing_orders_open = lambda i, st, rs: []

    def _submit(**kw):
        raise RuntimeError("venue said no")

    s._submit_trailing_stop = _submit
    
    # The arm CONFIRMS the replacement is resting at the venue before writing the manager row
    # (#265): `_submit_trailing_stop` is fire-and-forget, and reordering the cancel removed the
    # old safety net where a rejected trail left the previous stop untouched. Without this stub
    # the test drives the real six-second poll.
    async def _confirm_resting(coid, timeout_s=6.0):
        return True
    
    s._await_protection_resting = _confirm_resting
    s._cancel = lambda order: None
    s._reserve_attach_command = _fake_reserve_proceed()
    s._reject_reserved_ledger_entry = _noop_async_reject

    async def run():
        return await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "NBIS.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-1",
        )

    holder: list = []
    _sync(lambda: _capture(run, holder))
    status, error = holder[0]
    assert status == "error"
    assert "UNPROTECTED" not in error.upper(), f"false alarm — nothing had been cancelled: {error}"


# The two Criticals codex found after I had already "fixed" the post-cancel paths. Both are consequences
# of the SAME reordering, and neither was visible from the three exits I audited by hand.


def test_a_submit_the_venue_never_confirms_does_NOT_report_armed():
    """The worst failure this change can produce.

    `_submit_trailing_stop` is fire-and-forget. Under place-then-cancel a rejected trail was survivable —
    the pre-existing stop stayed resting. Cancelling first removed that net, so an unconfirmed submit
    means the manager row claims protection that does not exist, and the operator is told they are armed
    at the exact moment they are naked.
    """
    from nautilus_trader.model.enums import OrderSide, OrderType

    s = _bare_strategy()
    s._orders_armed = True
    s._position_for = lambda i, st, side: _FakePosition(quantity=29)
    stop = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                          ["bracket:entry-nbis"])
    s._bracket_protective_stop_open = lambda i, st, rs: stop
    s._identified_protective_stop_open = lambda i, st, rs: stop
    s._reducing_orders_open = lambda i, st, rs: [stop]
    s._cancel = lambda order: None
    s._submit_trailing_stop = lambda **kw: object()

    async def _cleared(instrument_id, strategy_id, reducing_side, timeout_s=6.0):
        return True

    s._await_reducing_orders_clear = _cleared

    async def _never_rests(coid, timeout_s=6.0):
        return False  # rejected at the venue, or simply never seen

    s._await_protection_resting = _never_rests
    s._reserve_attach_command = _fake_reserve_proceed()
    s._reject_reserved_ledger_entry = _noop_async_reject
    committed = []

    async def fake_commit(cid, ledger_cid, **kwargs):
        committed.append(cid)
        return "ok", "armed"

    s._commit_attach_command = fake_commit

    async def run():
        return await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "NBIS.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-1",
        )

    holder: list = []
    _sync(lambda: _capture(run, holder))
    status, error = holder[0]

    assert status == "error", "reported a successful arm over an unconfirmed stop"
    assert committed == [], "wrote a manager row claiming protection that never rested"
    assert "UNPROTECTED" in error.upper(), f"did not warn the position is bare: {error}"


def test_a_throw_while_clearing_still_warns_the_position_may_be_bare():
    """An uncaught exception between the first cancel and the replacement reached the generic handler,
    which reports `str(exc)` — indistinguishable from a failure that changed nothing."""
    from nautilus_trader.model.enums import OrderSide, OrderType

    s = _bare_strategy()
    s._orders_armed = True
    s._position_for = lambda i, st, side: _FakePosition(quantity=29)
    stop = _FakeOpenOrder("NBIS.XNAS", "MANUAL-001", OrderSide.SELL, OrderType.STOP_MARKET,
                          ["bracket:entry-nbis"])
    s._bracket_protective_stop_open = lambda i, st, rs: stop
    s._identified_protective_stop_open = lambda i, st, rs: stop
    s._reducing_orders_open = lambda i, st, rs: [stop]

    cancelled = []

    def _cancel(order):
        cancelled.append(order)
        raise RuntimeError("bus went away mid-cancel")

    s._cancel = _cancel
    s._submit_trailing_stop = lambda **kw: object()
    s._reserve_attach_command = _fake_reserve_proceed()
    s._reject_reserved_ledger_entry = _noop_async_reject

    async def run():
        return await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "peak_watch", "instrument_id": "NBIS.XNAS", "strategy_id": "MANUAL-001",
             "params": _FULL_PEAK_ARM_PARAMS},
            "entry-1",
        )

    holder: list = []
    _sync(lambda: _capture(run, holder))
    status, error = holder[0]

    assert status == "error"
    assert "bus went away" in error, "swallowed the underlying cause"
    assert "UNPROTECTED" in error.upper(), f"a throw mid-cancel reported as an ordinary failure: {error}"


def test_a_stop_heading_to_cancel_does_not_count_as_protection():
    """PENDING_CANCEL is not working (codex review, High).

    Driven through the REAL `_await_protection_resting`, not a stand-in, because the bug WAS the status
    set. An order already on its way out protects nothing; counting it lets the arm write a manager row
    over a stop about to vanish — the same "armed over a naked position" failure the confirmation exists
    to prevent, one step subtler.
    """
    from nautilus_trader.model.enums import OrderStatus

    from api.engine_node import UiFeedStrategy

    class _CacheWith:
        def __init__(self, status):
            self._status = status

        def order(self, coid):
            return SimpleNamespace(status=self._status)

    from types import SimpleNamespace

    class _Holder:
        pass

    async def resting_for(status):
        h = _Holder()
        h.cache = _CacheWith(status)
        return await UiFeedStrategy._await_protection_resting(h, "PKW-x", timeout_s=0.05)

    async def run():
        assert await resting_for(OrderStatus.ACCEPTED) is True, "a live stop must count"
        assert await resting_for(OrderStatus.PENDING_CANCEL) is False, "a stop being cancelled is not protection"
        assert await resting_for(OrderStatus.REJECTED) is False
        assert await resting_for(OrderStatus.PENDING_UPDATE) is True, "a modify in flight still rests"

    _sync(run)


def test_a_malformed_wait_marker_means_WAITING_not_go(caplog):
    """#651 item 6. `except (TypeError, ValueError): return False` turned an UNREADABLE `awaiting_qty`
    into "not waiting" — re-opening the exact double-trim that `awaiting_qty` was added to prevent
    (#255): a successor sizing off a quantity the broker is about to reduce. Unknown must not read as
    permission; the safe answer is WAIT, and the time bound below still frees the chain.
    """
    import logging

    import pytest as _pytest

    from api.engine_node import _AWAIT_FILL_TIMEOUT_NS, _awaiting_fill

    armed = _rth_ts_ns(10, 0)
    row = _row(
        params={**_row().params, "awaiting_qty": "fifty-eight"},
        created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
    )
    # Fixture property first: the marker IS present and IS unparseable — otherwise this test cannot
    # reach the branch it exists for.
    assert row.params["awaiting_qty"] is not None
    with _pytest.raises((TypeError, ValueError)):
        float(row.params["awaiting_qty"])

    inside = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + 30_000_000_000)
    with caplog.at_level(logging.ERROR, logger="api.engine_node"):
        assert _awaiting_fill(inside, row, _FakePosition(quantity=58)) is True, (
            "a malformed wait-marker read as 'not waiting' — the double-trim gate fails open on "
            "exactly the row it cannot read"
        )
    assert any("awaiting_qty" in r.message for r in caplog.records), (
        "the skip must be recorded — an unreadable marker that degrades silently is a fallback, "
        "not a guard"
    )

    # The TIME bound still frees the chain: a permanently malformed marker must not strand it.
    past = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + _AWAIT_FILL_TIMEOUT_NS + 1)
    assert _awaiting_fill(past, row, _FakePosition(quantity=58)) is False, (
        "waiting on a marker nobody can parse must still expire — otherwise the fix trades a "
        "double-trim for a stranded chain"
    )


def test_an_unreadable_position_quantity_also_means_WAITING():
    """The same except caught `float(pos.quantity)` — a position the code cannot read is not
    evidence the fill landed."""
    from api.engine_node import _awaiting_fill

    armed = _rth_ts_ns(10, 0)
    row = _row(
        params={**_row().params, "awaiting_qty": 32.0},
        created_at=datetime.fromtimestamp(armed / 1e9, tz=UTC),
    )
    inside = _FakeStrategy(cache=_FakeCache(), now_ts_ns=armed + 30_000_000_000)
    assert _awaiting_fill(inside, row, _FakePosition(quantity=None)) is True
