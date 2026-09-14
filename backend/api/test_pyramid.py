"""Tests for PYRAMID (#38) — progressive confirmation-scaling on a running winner, on the generic manager
framework (#55, `api.managers`).

Offline unit tests, same fake-double pattern as `test_peak.py`/`test_stop_reenter.py`. The ADX port
(`_wilder_adx`/`_adx_is_rising`/`_adx_is_falling`) was cross-language parity-verified against the real
`ui/src/lib/adx.ts` (via a one-time vitest dump + diff, this session) BEFORE these tests were written — the
hardcoded expected values below are taken directly from that verified TS output, not re-derived from the
Python port itself (which would be circular).

No `pytest-asyncio` in this repo — same `asyncio.run()`-in-a-sync-test pattern as `test_peak.py`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from api.engine_node import (
    _adx_is_falling,
    _adx_is_rising,
    _daily_range_high,
    _PyramidWatch,
    _range_high_break,
    _relative_volume,
    _validate_pyramid_params,
    _wilder_adx,
)
from api.managers import ManagerRow

_ET = ZoneInfo("America/New_York")


def _rth_dt(hour: int, minute: int, day_offset: int = 0) -> datetime:
    """The datetime BEHIND `_rth_ts_ns`, so a row's `created_at` and the bars it is measured against can be
    authored on one clock.

    `_row()` used to stamp `created_at=datetime.now()` while every bar anchors to 2026-08-05. Once the arm
    instant is actually read (#283), that lands days past the last bar, the arm-scoped window is empty, and
    an arm-scoped test would pass without the fix ever running. Fix the double, never loosen production."""
    return datetime(2026, 8, 5, hour, minute, tzinfo=_ET) + timedelta(days=day_offset)  # 2026-08-05 = Wed


def _rth_ts_ns(hour: int, minute: int, day_offset: int = 0) -> int:
    return int(_rth_dt(hour, minute, day_offset).astimezone(UTC).timestamp() * 1e9)


class _FakeBar:
    def __init__(self, high: float, close: float, ts_event: int, low: float | None = None, volume: float = 1000.0):
        self.high = high
        self.low = low if low is not None else high
        self.close = close
        self.ts_event = ts_event
        self.volume = volume


class _FakeCache:
    """Same oldest-first-authored / newest-first-returned convention as `test_peak.py`'s double."""

    def __init__(self, bars_1m: list | None = None, bars_1d: list | None = None):
        self._bars_1m = bars_1m or []
        self._bars_1d = bars_1d or []

    def bars(self, bt):
        return list(reversed(self._bars_1m if "1-MINUTE" in str(bt) else self._bars_1d))


class _FakeClock:
    def __init__(self, ts_ns: int):
        self._ts_ns = ts_ns

    def timestamp_ns(self) -> int:
        return self._ts_ns


class _FakePosition:
    def __init__(self, quantity: float = 100, account_id: str = "ACC-1", avg_px_open: float = 100.0):
        self.quantity = quantity
        self.account_id = account_id
        self.avg_px_open = avg_px_open


class _FakeOrder:
    def __init__(self, is_open: bool = True, quantity: float = 100.0):
        self.is_open = is_open
        self.quantity = quantity


class _FakeCycleDTO:
    def __init__(self, instrument_id: str, strategy_id: str, cycle_id: str):
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.cycle_id = cycle_id


class _FakeTradeCycles:
    def __init__(self, dtos: list | None = None):
        self._dtos = dtos or []

    def project(self, cache, ts_ns):
        return self._dtos


class _FakeStrategy:
    """Single shared `cache`/`_position`/`_last_price` regardless of which `instrument_id` is asked for —
    same simplification `test_peak.py`'s double makes. In practice this means the driver instrument sees the
    IDENTICAL bars/position/price as the position's own instrument in these tests: fine for verifying the
    framework correctly plumbs a driver_instrument_id through and ANDs all gates together, not a claim that
    driver-vs-position DIVERGENCE scenarios are covered (a real per-instrument cache double would be needed
    for that, out of scope here)."""

    def __init__(
        self, *, cache: _FakeCache, now_ts_ns: int, position=None, last_price=None, trade_cycles=None,
    ):
        self.cache = cache
        self.clock = _FakeClock(now_ts_ns)
        self._position = position
        self._last_price = last_price
        # DICT, keyed by strategy id — the production shape. `None` stays None so the guard's falsy
        # check is still exercised.
        self._trade_cycles = ({"MANUAL-001": trade_cycles} if trade_cycles is not None else trade_cycles)
        self.built_orders: list[dict] = []
        self.submitted: list[object] = []
        self.replaced_trailing_stops: list[dict] = []
        self.canceled: list[object] = []
        self._seen_orders: set[str] = set()
        self._orders_by_coid: dict[str, _FakeOrder] = {}


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
        kind="pyramid_watch",
        kind_version=1,
        account_id="ACC-1",
        client_id="CLIENT-1",
        instrument_id="AMAT.XNAS",
        strategy_id="MANUAL-001",
        cycle_id="cyc-1",
        leash="AUTO",
        params={
            "expected_side": "LONG",
            "qty": 100,
            "initial_qty": 100,
            "driver_instrument_id": "SMH.XNAS",
            "add_r_multiple": 0.75,
            "initial_risk_per_share": 5.0,
            "max_rungs": 3,
            "rung_count": 0,
            "trail_bps": 150,
            "current_trail_coid": "BR-init",
            "daily_lookback_days": 20,
            "min_rel_volume": 1.5,
            "sustained_bars": 2,
            "off_hod_pct_threshold": 2.0,
            "lower_high_bars": 3,
        },
        state="ARMED",
        # Armed AT THE OPEN by default, on the same clock as the bars (see `_rth_dt`). Every fixture in
        # this file prints after 09:30, so the arm-scoped window (#283) is the whole session and existing
        # expectations are unchanged; tests that care about arm scoping override this explicitly.
        created_at=_rth_dt(9, 30),
    )
    base.update(overrides)
    return ManagerRow(**base)


def _sync(run) -> None:
    asyncio.run(run())


# --- _validate_pyramid_params ------------------------------------------------------------------------------


def test_validate_rejects_missing_field():
    params = _row().params.copy()
    del params["max_rungs"]
    assert "max_rungs" in _validate_pyramid_params(params)


def test_validate_accepts_well_formed_params():
    assert _validate_pyramid_params(_row().params) is None


def test_validate_rejects_short_v1_signals_are_long_only():
    params = {**_row().params, "expected_side": "SHORT"}
    reason = _validate_pyramid_params(params)
    assert reason is not None
    assert "LONG-only" in reason


def test_validate_rejects_empty_driver_instrument_id():
    params = {**_row().params, "driver_instrument_id": "  "}
    reason = _validate_pyramid_params(params)
    assert reason is not None
    assert "driver_instrument_id" in reason


# --- _daily_range_high / _relative_volume / _range_high_break — the PENG 3-way discrimination ------------


def _daily_bars_fixture(highs: list[float]) -> list[_FakeBar]:
    """Authored OLDEST-FIRST (`highs[0]` = furthest back), matching `_FakeCache`'s own convention — and
    NONE of these date to TODAY (the most recent, `highs[-1]`, is dated yesterday), matching `_daily_bars`'s
    now-explicit exclusion of today's still-forming daily bar (codex review, High)."""
    n = len(highs)
    return [
        _FakeBar(high=h, close=h, ts_event=_rth_ts_ns(16, 0, day_offset=-(n - i))) for i, h in enumerate(highs)
    ]


def test_daily_range_high_max_over_lookback():
    bars_1d = _daily_bars_fixture([60, 65, 72, 68, 61])  # oldest-first as authored, none dated today
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 0))
    assert _daily_range_high(strategy, "PENG.XNAS", lookback_days=5) == 72


def test_daily_range_high_none_with_insufficient_history():
    bars_1d = _daily_bars_fixture([60, 65])
    strategy = _FakeStrategy(cache=_FakeCache(bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 0))
    assert _daily_range_high(strategy, "PENG.XNAS", lookback_days=5) is None


def test_relative_volume_ratio_of_latest_bar_to_trailing_average():
    bars_1m = [_FakeBar(high=10, close=10, ts_event=_rth_ts_ns(10, i), volume=100.0) for i in range(5)]
    bars_1m.append(_FakeBar(high=10, close=10, ts_event=_rth_ts_ns(10, 5), volume=250.0))  # 2.5x
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 6))
    assert _relative_volume(strategy, "PENG.XNAS", lookback_bars=5) == 2.5


def test_relative_volume_none_with_insufficient_history():
    bars_1m = [_FakeBar(high=10, close=10, ts_event=_rth_ts_ns(10, 0), volume=100.0)]
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 1))
    assert _relative_volume(strategy, "PENG.XNAS", lookback_bars=20) is None


def _peng_range_break_bars(*, breaks_range: bool, thin_volume: bool) -> tuple[list, list]:
    """Shared fixture builder for the PENG 3-way test — a prior 60-72 daily range, then a session that
    either breaks it (76.78, real volume) or doesn't (chase case: thin, mid-air, no base to break)."""
    bars_1d = _daily_bars_fixture([60, 72, 65, 68, 61, 63, 66, 70, 64, 62, 67, 69, 61, 60, 71, 65, 63, 68, 66, 64])
    baseline_vol = 100.0
    sustained_vol = 60.0 if thin_volume else 200.0  # 0.6x (thin) vs 2.0x (real) the 100 baseline
    session_high = 65.0 if not breaks_range else 76.78  # 65 stays inside the 60-72 range; 76.78 clears it
    bars_1m = [_FakeBar(high=60, close=59.5, ts_event=_rth_ts_ns(10, i), volume=baseline_vol) for i in range(20)]
    bars_1m += [
        _FakeBar(high=session_high, close=session_high - 0.2, ts_event=_rth_ts_ns(10, 20 + i), volume=sustained_vol)
        for i in range(2)
    ]
    return bars_1d, bars_1m


def test_range_high_break_fires_on_real_breakout_with_volume():
    """The add we MISSED (PENG's 72 breakout) — must fire."""
    bars_1d, bars_1m = _peng_range_break_bars(breaks_range=True, thin_volume=False)
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25))
    assert _range_high_break(
        strategy, "PENG.XNAS", daily_lookback_days=20, min_rel_volume=1.5, sustained_bars=2
    ) is True


def test_range_high_break_vetoes_thin_chase_no_base():
    """The chase we correctly BLOCKED (09:30 buy-stop, no base, thin volume) — must NOT fire, on two
    independent grounds (no range actually broken AND volume too thin)."""
    bars_1d, bars_1m = _peng_range_break_bars(breaks_range=False, thin_volume=True)
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25))
    assert _range_high_break(
        strategy, "PENG.XNAS", daily_lookback_days=20, min_rel_volume=1.5, sustained_bars=2
    ) is False


def test_range_high_break_vetoes_extension_with_no_range_to_break():
    """A late, extended print with real volume but NOTHING left to break (already through the range) —
    isolates the "sustained_bars volume-only" case from the "clears the range" case; still requires BOTH."""
    bars_1d, bars_1m = _peng_range_break_bars(breaks_range=False, thin_volume=False)  # real volume, in-range
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25))
    assert _range_high_break(
        strategy, "PENG.XNAS", daily_lookback_days=20, min_rel_volume=1.5, sustained_bars=2
    ) is False


def test_range_high_break_vetoes_breakout_with_thin_volume():
    """Clears the range, but volume never confirms — a real level broken on no conviction, still vetoed."""
    bars_1d, bars_1m = _peng_range_break_bars(breaks_range=True, thin_volume=True)
    strategy = _FakeStrategy(cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25))
    assert _range_high_break(
        strategy, "PENG.XNAS", daily_lookback_days=20, min_rel_volume=1.5, sustained_bars=2
    ) is False


# --- _wilder_adx / _adx_is_rising / _adx_is_falling — parity-verified against ui/src/lib/adx.ts -----------


def _uptrend_bars(n: int) -> list[_FakeBar]:
    """Mirrors `ui/src/lib/adx.test.ts`'s own `uptrend(n)` fixture exactly, so results are directly
    comparable to that file's own assertions and to the hardcoded parity values below."""
    out = []
    for i in range(n):
        base = 100 + i * 1.5
        out.append(_FakeBar(high=base + 1, low=base - 1, close=base + 0.5, ts_event=_rth_ts_ns(16, 0, -i)))
    return out


def _flat_bars(n: int) -> list[_FakeBar]:
    return [_FakeBar(high=100.2, low=99.8, close=100.0, ts_event=_rth_ts_ns(16, 0, -i)) for i in range(n)]


def _gradually_strengthening_uptrend_bars(n: int = 30) -> list[_FakeBar]:
    """A trend that STARTS choppy (mixed up/down closes, weak/low ADX) and gradually smooths into a clean
    uptrend (ADX rising through the window, not yet saturated at 100 by the end) — a pure, ZERO-down-bar
    uptrend (like `_uptrend_bars`) saturates ADX to 100 almost immediately (a mathematical property of
    Wilder's DX: -DM stays exactly 0 the whole way, so DX≈100 regardless of how gentle the slope is), which
    is useless for testing an add-gate that specifically requires `_adx_is_rising` (still climbing, not yet
    flat at the ceiling). Params (decaying sine amplitude + linear drift) tuned empirically against the real
    port to land in a genuinely rising, non-saturated ADX range at n=30 — verified via
    `test_gradually_strengthening_uptrend_bars_fixture_is_actually_rising_adx` below, which would fail loudly
    if a future edit broke that property. Authored oldest-first, matching `_FakeCache`'s convention — and
    NONE dated today (the most recent, index n-1, is dated yesterday), matching `_daily_bars`'s exclusion of
    today's still-forming bar (codex review, High)."""
    import math as _math

    closes = []
    for i in range(n):
        amp = 5.0 * max(0.0, 1 - i / (n * 0.8))
        closes.append(100 + 0.5 * i + amp * _math.sin(i * 1.3))
    return [
        _FakeBar(high=c + 1.0, low=c - 1.0, close=c, ts_event=_rth_ts_ns(16, 0, -(n - i)))
        for i, c in enumerate(closes)
    ]


def test_wilder_adx_empty_bars():
    assert _wilder_adx([]) == {"adx": [], "plus_di": [], "minus_di": []}


def test_wilder_adx_arrays_1to1_with_bars_bar0_is_null():
    result = _wilder_adx(_uptrend_bars(5))  # already chronological (oldest-first, matches adx.test.ts)
    assert len(result["adx"]) == 5
    assert len(result["plus_di"]) == 5
    assert len(result["minus_di"]) == 5
    assert result["adx"][0] is None


def test_wilder_adx_zero_range_series_is_all_none():
    dead_flat = [_FakeBar(high=100, low=100, close=100, ts_event=_rth_ts_ns(16, 0, -i)) for i in range(20)]
    result = _wilder_adx(dead_flat)
    assert all(v is None for v in result["adx"])
    assert all(v is None for v in result["plus_di"])
    assert all(v is None for v in result["minus_di"])


def test_wilder_adx_parity_against_real_ts_port_uptrend():
    """Hardcoded from a one-time cross-language check this session: ran the REAL `ui/src/lib/adx.ts`
    `computeAdx` (via vitest) on this exact 15-bar uptrend fixture and dumped its output; these are that
    output's tail values, bit-parity-matched (< 1e-9) against this Python port at the time this test was
    written. Not re-derived from the Python port itself — a genuine cross-language regression guard."""
    bars = _uptrend_bars(15)  # already chronological (oldest-first, matches adx.test.ts's array order)
    result = _wilder_adx(bars, period=9)
    assert result["adx"][0] is None
    assert result["adx"][-3:] == [100, 100, 100]
    assert result["minus_di"][-3:] == [0, 0, 0]
    for got, want in zip(result["plus_di"][-3:], [56.7513393980777, 58.778968353846835, 60.58130520341941]):
        assert abs(got - want) < 1e-9


def test_gradually_strengthening_uptrend_bars_fixture_is_actually_rising_adx():
    """Self-check for the fixture `_apply()`'s add-gate tests below depend on — would fail loudly if a
    future edit to the tuning params broke the "still rising, not yet saturated" property."""
    adx = _wilder_adx(_gradually_strengthening_uptrend_bars(30), period=9)["adx"]
    assert _adx_is_rising(adx, lookback=3) is True
    assert adx[-1] is not None and adx[-1] < 95  # not saturated at the ceiling


def test_adx_is_rising_true_when_latest_exceeds_lookback_slot():
    assert _adx_is_rising([10, 12, 14, 16], lookback=3) is True
    assert _adx_is_rising([16, 14, 12, 10], lookback=3) is False


def test_adx_is_rising_false_when_series_too_short_or_none():
    assert _adx_is_rising([], lookback=3) is False
    assert _adx_is_rising([1, 2, 3], lookback=3) is False  # length == lookback
    assert _adx_is_rising([None, 12, 14, 16], lookback=3) is False
    assert _adx_is_rising([10, 12, 14, None], lookback=3) is False


def test_adx_is_falling_mirrors_rising():
    assert _adx_is_falling([16, 14, 12, 10], lookback=3) is True
    assert _adx_is_falling([10, 12, 14, 16], lookback=3) is False
    assert _adx_is_falling([10, 10, 10, 10], lookback=3) is False  # flat is NOT falling


# --- _PyramidWatch.trigger_met / apply ----------------------------------------------------------------------


def _riding_bars(session_high: float = 65.0) -> tuple[list, list]:
    """A calm, non-fading, non-breaking session on both position and driver — nothing should fire."""
    bars_1d = _daily_bars_fixture([70] * 20)  # already above anything the session can reach -> no break
    bars_1m = [
        _FakeBar(high=session_high, close=session_high - 0.1, ts_event=_rth_ts_ns(10, i), volume=100.0)
        for i in range(5)
    ]
    return bars_1d, bars_1m


def test_trigger_met_FIRES_when_flat_so_the_row_can_terminalize():
    """Was `test_trigger_met_false_when_flat`, and it pinned the defect rather than the contract.

    Returning False when flat means `apply()` is never called, so the row stays ARMED forever after its
    position closes by any route — "is this manager alive?" cannot then be answered from state alone. That
    is #255 High 1, fixed in PEAK by #274 and never applied to PYRAMID (codex review, High).

    `apply()` returns a terminal FAILED on flat, so firing is precisely what retires the row.
    """
    bars_1d, bars_1m = _riding_bars()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 6), position=None,
    )

    async def run():
        assert await _PyramidWatch().trigger_met(strategy, _row()) is True

    _sync(run)


def test_trigger_met_false_while_calmly_riding():
    bars_1d, bars_1m = _riding_bars()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 6),
        position=_FakePosition(), last_price=64.5,
    )

    async def run():
        assert await _PyramidWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def _fully_confirmed_add_fixture():
    """Position AND driver both break their range with sustained volume and a rising ADX — every add gate
    passes. Shared by the apply()-add tests below."""
    bars_1d, bars_1m = _peng_range_break_bars(breaks_range=True, thin_volume=False)
    # Rising ADX needs a genuinely still-climbing trend on daily closes (a pure monotonic uptrend saturates
    # ADX to 100 almost immediately — see `_gradually_strengthening_uptrend_bars`'s own docstring) — long
    # enough (>= 9*3 per _PYRAMID_ADX_PERIOD*3) for the port to produce a non-null, rising tail. Already
    # authored oldest-first, matching `_FakeCache`'s `bars_1d` constructor convention.
    bars_1d = _gradually_strengthening_uptrend_bars(30)  # overrides the plain daily_range fixture; also the range-high source
    # Rebuild bars_1m against this same daily series' scale so _range_high_break still fires.
    prior_high = max(float(b.high) for b in bars_1d[-20:])
    baseline = [_FakeBar(high=prior_high - 5, close=prior_high - 5.5, ts_event=_rth_ts_ns(10, i), volume=100.0) for i in range(20)]
    breakout = [
        _FakeBar(high=prior_high + 5, close=prior_high + 4.8, ts_event=_rth_ts_ns(10, 20 + i), volume=250.0)
        for i in range(2)
    ]
    bars_1m = baseline + breakout
    return bars_1d, bars_1m, prior_high


def test_apply_adds_a_tranche_without_touching_the_trail():
    """codex review (Critical) — the trail must NOT be resized in the same call as the add: sizing a SELL
    stop above what's currently held (an assumed post-add total) risks an oversell/short if it triggers
    before the BUY fill lands, which this fire-and-forget engine has no way to confirm synchronously. The
    BUY gets the tracked coid instead; the trail is left exactly as it was."""
    from unittest.mock import AsyncMock, patch

    bars_1d, bars_1m, prior_high = _fully_confirmed_add_fixture()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25),
        position=_FakePosition(quantity=100), last_price=prior_high + 5,
    )
    strategy._orders_by_coid["BR-init"] = _FakeOrder(is_open=True, quantity=100)  # matches live -> no resync
    row = _row()  # initial_qty=100, add_r_multiple=0.75 -> add_qty=75

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
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PyramidWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert strategy.replaced_trailing_stops == []  # trail untouched — resync's job, not the add's
        assert len(strategy.built_orders) == 1
        assert strategy.built_orders[0]["side"] == "BUY"
        assert strategy.built_orders[0]["quantity"] == 75
        # the BUY (not a trail replace) now carries the tracked, crash-recovery-visible coid
        tracked_coid = _PyramidWatch().client_order_id_for(row.manager_id)
        assert strategy.built_orders[0]["client_order_id"] == tracked_coid
        assert captured["params"]["rung_count"] == 1
        # qty/current_trail_coid deliberately NOT advanced here -- the resync step (next tick) owns that,
        # once pos.quantity genuinely reflects the fill.
        assert captured["params"]["qty"] == row.params["qty"]
        assert captured["params"]["current_trail_coid"] == row.params["current_trail_coid"]

    _sync(run)


def test_apply_resyncs_the_trail_once_a_prior_add_fill_has_landed():
    """codex review (Critical) — the day AFTER an add fills, the resting trail (still sized for the
    pre-add quantity) no longer matches the live position; this must be caught and corrected to the
    VERIFIED live quantity BEFORE any new add is considered."""
    from unittest.mock import AsyncMock, patch

    bars_1d, bars_1m = _riding_bars()  # calm — isolates resync from add/exit
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 6),
        position=_FakePosition(quantity=175), last_price=64.5,  # live qty grew since the trail was set
    )
    strategy._orders_by_coid["BR-init"] = _FakeOrder(is_open=True, quantity=100)  # stale -- still pre-add size
    row = _row()

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
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PyramidWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert "resync" in detail
        assert strategy.built_orders == []  # no new order placed -- pure trail resize
        assert len(strategy.replaced_trailing_stops) == 1
        assert strategy.replaced_trailing_stops[0]["quantity"] == 175  # VERIFIED live qty, not an assumption
        tracked_coid = _PyramidWatch().client_order_id_for(row.manager_id)
        assert strategy.replaced_trailing_stops[0]["new_coid"] == tracked_coid
        assert captured["params"]["qty"] == 175
        assert captured["params"]["current_trail_coid"] == tracked_coid

    _sync(run)


def test_trigger_met_false_when_trail_already_matches_live_qty():
    """No false-positive resync trigger when the trail is already correctly sized."""
    bars_1d, bars_1m = _riding_bars()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 6),
        position=_FakePosition(quantity=100), last_price=64.5,
    )
    strategy._orders_by_coid["BR-init"] = _FakeOrder(is_open=True, quantity=100)  # matches live
    row = _row()

    async def run():
        assert await _PyramidWatch().trigger_met(strategy, row) is False

    _sync(run)


def test_apply_refuses_add_when_rung_cap_reached():
    bars_1d, bars_1m, prior_high = _fully_confirmed_add_fixture()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25),
        position=_FakePosition(quantity=100), last_price=prior_high + 5,
    )
    row = _row(params={**_row().params, "rung_count": 3, "max_rungs": 3})  # already at cap

    async def run():
        state, detail = await _PyramidWatch().apply(strategy, row)
        assert state == "FAILED"
        assert strategy.built_orders == []
        assert strategy.replaced_trailing_stops == []

    _sync(run)


def test_apply_exits_on_own_sustained_fade():
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0), volume=100.0),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1), volume=100.0),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2), volume=100.0),
    ]
    bars_1d = _daily_bars_fixture([90] * 20)  # no breakout possible -> isolates the exit branch
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(quantity=175), last_price=87.0,
    )
    strategy._orders_by_coid["BR-init"] = _FakeOrder(is_open=True)
    row = _row()

    async def run():
        state, detail = await _PyramidWatch().apply(strategy, row)
        assert state == "APPLIED"
        assert "full exit" in detail
        assert len(strategy.built_orders) == 1
        assert strategy.built_orders[0]["side"] == "SELL"
        assert strategy.built_orders[0]["quantity"] == 175
        assert strategy.built_orders[0]["client_order_id"] == _PyramidWatch().client_order_id_for(row.manager_id)
        assert len(strategy.canceled) == 1  # old trail canceled
        assert strategy.replaced_trailing_stops == []  # exit takes priority, add branch never reached

    _sync(run)


def test_apply_refuses_when_a_different_cycle_is_now_open():
    bars_1m = [
        _FakeBar(high=89.5, close=89, ts_event=_rth_ts_ns(10, 0), volume=100.0),
        _FakeBar(high=88.5, close=88, ts_event=_rth_ts_ns(10, 1), volume=100.0),
        _FakeBar(high=87.5, close=87, ts_event=_rth_ts_ns(10, 2), volume=100.0),
    ]
    drifted = _FakeTradeCycles([_FakeCycleDTO("AMAT.XNAS", "MANUAL-001", "cyc-DIFFERENT")])
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m), now_ts_ns=_rth_ts_ns(10, 5),
        position=_FakePosition(), last_price=87.0, trade_cycles=drifted,
    )

    async def run():
        state, detail = await _PyramidWatch().apply(strategy, _row())  # _row()'s cycle_id is "cyc-1"
        assert state == "FAILED"
        assert "cycle" in detail
        assert strategy.built_orders == []
        assert strategy.replaced_trailing_stops == []

    _sync(run)


# --- _handle_attach_manager_command's PYRAMID-specific arm step ---------------------------------------------


def _bare_strategy():
    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    return UiFeedStrategy(load_feed_config(), ClientId("DATABENTO"), "test-key")


class _FakeBracketStop:
    def __init__(self, trigger_price: float | None, has_trigger_price: bool = True, coid: str = "BR-1"):
        self.trigger_price = trigger_price
        self.has_trigger_price = has_trigger_price
        self.client_order_id = coid


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


_FULL_PYRAMID_ARM_PARAMS = {
    "expected_side": "LONG",
    "qty": 1,  # overridden by the live position inside _handle_attach_manager_command
    "driver_instrument_id": "SMH.XNAS",
    "add_r_multiple": 0.75,
    "max_rungs": 3,
    "trail_bps": 150,
    "daily_lookback_days": 20,
    "min_rel_volume": 1.5,
    "sustained_bars": 2,
    "off_hod_pct_threshold": 2.0,
    "lower_high_bars": 3,
}


def test_attach_pyramid_computes_r_from_bracket_stop_and_arms():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)
        s._last_price_for = lambda instrument_id: 76.0  # driver has live data
        stop = _FakeBracketStop(trigger_price=70.0, coid="BR-9")
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [stop]
        s._reserve_attach_command = _fake_reserve_proceed()
        captured = {}

        async def fake_commit(cid, ledger_cid, **kwargs):
            captured.update(kwargs)
            return "ok", "stubbed"

        s._commit_attach_command = fake_commit

        status, _ = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PYRAMID_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "ok"
        assert captured["params"]["initial_qty"] == 100.0
        assert captured["params"]["rung_count"] == 0
        assert captured["params"]["current_trail_coid"] == "BR-9"
        assert captured["params"]["initial_risk_per_share"] == 74.06 - 70.0

    _sync(run)


def test_attach_pyramid_refuses_when_bracket_stop_rests_alongside_another_order():
    """codex review (High) — same guard PEAK's arm step has: a SECOND, unidentified resting reducing order
    alongside the bracket stop must refuse arming, not silently ignore it."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)
        s._last_price_for = lambda instrument_id: 76.0
        stop = _FakeBracketStop(trigger_price=70.0, coid="BR-9")
        other = object()
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [stop, other]
        s._reserve_attach_command = _fake_reserve_proceed()
        s._reject_reserved_ledger_entry = _noop_async_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PYRAMID_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "isn't identifiable" in error

    _sync(run)


def test_attach_pyramid_refuses_without_an_identifiable_bracket_stop():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)
        s._last_price_for = lambda instrument_id: 76.0
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: None  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: []
        s._reserve_attach_command = _fake_reserve_proceed()
        s._reject_reserved_ledger_entry = _noop_async_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PYRAMID_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "no identifiable" in error

    _sync(run)


def test_attach_pyramid_refuses_when_driver_has_no_live_price():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)
        s._last_price_for = lambda instrument_id: None  # driver not subscribed / no data yet
        s._reserve_attach_command = _fake_reserve_proceed()
        s._reject_reserved_ledger_entry = _noop_async_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PYRAMID_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "SMH.XNAS" in error
        assert "no live price" in error

    _sync(run)


def test_attach_pyramid_refuses_non_positive_r():
    """A bracket stop ABOVE the entry price (or at it) would compute R <= 0 — nonsensical, refuse rather
    than arm with a broken risk unit."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)
        s._last_price_for = lambda instrument_id: 76.0
        stop = _FakeBracketStop(trigger_price=80.0)  # above entry -> negative R
        s._bracket_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop
        s._identified_protective_stop_open = lambda instrument_id, strategy_id, reducing_side: stop  # widened identifier (#303)
        s._reducing_orders_open = lambda instrument_id, strategy_id, reducing_side: [stop]
        s._reserve_attach_command = _fake_reserve_proceed()
        s._reject_reserved_ledger_entry = _noop_async_reject

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": _FULL_PYRAMID_ARM_PARAMS,
            },
            "entry-1",
        )

        assert status == "error"
        assert "non-positive" in error

    _sync(run)


def test_attach_pyramid_refuses_short():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=100, avg_px_open=74.06)

        def must_not_be_called(*args, **kwargs):
            raise AssertionError("should refuse SHORT before checking price/bracket stop")

        s._last_price_for = must_not_be_called
        s._bracket_protective_stop_open = must_not_be_called
        s._identified_protective_stop_open = must_not_be_called  # widened identifier (#303)

        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "pyramid_watch",
                "instrument_id": "AMAT.XNAS",
                "strategy_id": "MANUAL-001",
                "params": {**_FULL_PYRAMID_ARM_PARAMS, "expected_side": "SHORT"},
            },
            "entry-1",
        )

        assert status == "error"
        assert "LONG-only" in error

    _sync(run)


def test_apply_does_not_hand_off_when_the_operator_turned_pyramid_off():
    """PYRAMID chains too — `apply()` attaches a fresh successor to keep watching after an add. Without
    consulting the cancel intent, OFF cancels the one row the UI named while the successor is already
    (or about to be) attached, and the toggle springs back to ON."""
    from unittest.mock import AsyncMock, patch

    bars_1d, bars_1m, prior_high = _fully_confirmed_add_fixture()
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25),
        position=_FakePosition(quantity=100), last_price=prior_high + 5,
    )
    strategy._orders_by_coid["BR-init"] = _FakeOrder(is_open=True, quantity=100)
    attached: list = []

    async def fake_attach(session, **kwargs):
        attached.append(kwargs)

    class _FakeSessionCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    async def run():
        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=True)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _PyramidWatch().apply(strategy, _row())

        assert state == "APPLIED"
        assert attached == [], "a cancelled chain must not attach a successor"
        assert "turned off" in detail

    _sync(run)


def test_a_bare_ticker_driver_explains_itself_instead_of_leaking_the_constructor_error():
    """the operator typed `SMH` into the driver field on 2026-08-13 and got:

        invalid `InstrumentId` value 'SMH': missing '.' separator between symbol and venue components

    That is Nautilus's constructor talking, and it tells the operator nothing about what to do. Nobody
    knows a symbol's MIC by heart — the venue is a lookup, not a decision. The UI now offers a picker, but
    the attach command takes arbitrary params, so a bad one still has to explain itself.
    """
    from api.engine_node import _validate_pyramid_params

    params = {**_FULL_PYRAMID_ARM_PARAMS, "driver_instrument_id": "SMH"}
    err = _validate_pyramid_params(params)
    assert err is not None
    assert "SMH.XNAS" in err, "the message must show the shape that works"
    assert "InstrumentId" not in err, "and must not leak the constructor error"


def test_a_full_instrument_id_driver_is_accepted():
    from api.engine_node import _validate_pyramid_params

    assert _validate_pyramid_params({**_FULL_PYRAMID_ARM_PARAMS, "driver_instrument_id": "SMH.XNAS"}) is None


def test_an_empty_driver_is_still_refused():
    from api.engine_node import _validate_pyramid_params

    err = _validate_pyramid_params({**_FULL_PYRAMID_ARM_PARAMS, "driver_instrument_id": "  "})
    assert err is not None and "empty" in err


def test_a_dotted_TICKER_is_not_mistaken_for_an_instrument_id():
    """"Contains a dot" was too weak (codex review, High). `BRK.B` is a real ticker with a dot and NO
    venue, `SMH.` has an empty one — both would pass that test and reach `InstrumentId.from_str`, where
    the raw constructor error leaks through the generic command catch. Which is the exact error this
    validation exists to replace."""
    from api.engine_node import _validate_pyramid_params

    for bad in ("BRK.B", "SMH.", ".XNAS", "SMH.X1"):
        err = _validate_pyramid_params({**_FULL_PYRAMID_ARM_PARAMS, "driver_instrument_id": bad})
        assert err is not None, bad
        assert "InstrumentId" not in err, bad

    # And a symbol that legitimately contains a dot still works WITH its venue.
    assert _validate_pyramid_params(
        {**_FULL_PYRAMID_ARM_PARAMS, "driver_instrument_id": "BRK.B.XNYS"}
    ) is None


# --- #283: PYRAMID's fade window is scoped to THIS manager's arm ---------------------------------------
#
# The same defect #253 fixed in PEAK, sitting untouched in `_PyramidWatch`. Four `_session_high` calls —
# the position's and the driver's, in both `_add_condition` and `_exit_condition` — measured the high
# against the whole session rather than against the arm.


def _faded_before_arm_then_flat() -> list[_FakeBar]:
    """A session that peaks and rolls over BEFORE the arm, then goes quiet at its new level.

    Session-wide the high is the 200 peak and the price now sits 25% below it, so `_sustained_fade` is
    satisfied on its off-HoD branch. Scoped to the arm the window is three flat bars sitting at their own
    high, and there is nothing to fade from.
    """
    before = [_FakeBar(high=h, close=h, ts_event=_rth_ts_ns(9, 31 + i)) for i, h in enumerate([180, 200, 170, 160])]
    after = [_FakeBar(high=150, close=150, ts_event=_rth_ts_ns(11, i)) for i in range(3)]
    return before + after


def test_exit_does_not_fire_on_a_fade_that_completed_before_the_manager_armed():
    """Arm PYRAMID after a name has already peaked and rolled over, and the exit fires on the first
    dispatch tick — the manager sells a position it was armed to scale INTO.

    Fixture: peak of 200 at 09:32, decline to 160, arm at 10:55, then three flat bars at 150.

    PEAK did exactly this to OKTA on 2026-08-12, trimming against a three-minute-old high seconds after
    arming. PYRAMID is gated on a protective stop no position carries (#257) so it has never armed in
    production, which is the only reason this has not cost anything yet.
    """
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=_faded_before_arm_then_flat(), bars_1d=_uptrend_bars(40)),
        now_ts_ns=_rth_ts_ns(11, 3),
    )
    row = _row(created_at=_rth_dt(10, 55))

    assert asyncio.run(_PyramidWatch()._exit_condition(strategy, row)) is False


def test_exit_still_fires_on_a_fade_that_developed_AFTER_the_arm():
    """The other side of the same scoping — arm-scoping must not blind the exit to a real fade.

    Without this the fix could be 'never exit' and the test above would still pass. Same bars, but the arm
    sits BEFORE the roll-over, so the descent is inside the window and the exit must fire.
    """
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=_faded_before_arm_then_flat(), bars_1d=_uptrend_bars(40)),
        now_ts_ns=_rth_ts_ns(11, 3),
    )
    row = _row(created_at=_rth_dt(9, 30))

    assert asyncio.run(_PyramidWatch()._exit_condition(strategy, row)) is True


def test_every_session_high_in_both_conditions_is_arm_scoped_including_the_DRIVER_leg():
    """Four call sites, not one — and the driver's two are the ones a behavioural test here cannot reach.

    `_FakeStrategy` returns identical bars for every instrument by design (see its docstring), so the
    driver leg cannot be made to diverge from the position's in this file. Pinning it structurally is the
    honest alternative to leaving half the defect uncovered: a test that only exercised the position's own
    high would report success with two of the four sites still measuring the whole session.
    """
    import inspect

    for fn in (_PyramidWatch._add_condition, _PyramidWatch._exit_condition):
        src = inspect.getsource(fn)
        assert "_session_high(strategy, instrument_id)" not in src, f"{fn.__name__}: position leg unscoped"
        assert "_session_high(strategy, driver_id)" not in src, f"{fn.__name__}: DRIVER leg unscoped"
        assert src.count("since_ns=armed_ns") == 4, (
            f"{fn.__name__} must pass the arm instant to both `_session_high` calls AND both "
            f"`_sustained_fade` calls — scoping only the high leaves the fade's own lower-high branch "
            f"reading pre-arm bars (the codex High that #253 caught in PEAK)"
        )


def test_both_conditions_derive_the_arm_instant_exactly_once():
    """Two derivations of one fact will disagree — already bitten leash validation, the manager-armed
    check, and percent/bps rounding across the JS/Python seam.

    Deriving `_armed_at_ns(row)` separately per call site is four chances for the position leg and the
    driver leg to disagree about when this manager armed. One local, used four times.
    """
    import inspect

    for fn in (_PyramidWatch._add_condition, _PyramidWatch._exit_condition):
        src = inspect.getsource(fn)
        assert src.count("_armed_at_ns(row)") == 1, f"{fn.__name__} must derive the arm instant once"


def test_add_fires_only_when_the_high_it_measures_against_is_its_OWN():
    """The ADD path, behaviourally — codex flagged that only the exit leg had a real test.

    This is the quieter half of #283 and the reason PYRAMID would have looked merely useless rather than
    broken: measured session-wide, a name that spiked early and pulled back is permanently "faded", so
    `_add_condition` returns False forever and the manager never scales into anything. No error, no log —
    it just never fires.

    ONE fixture, TWO arms, differing only in when the row armed:

        09:31-09:33   spike to 135.5, rolling over          <- pre-arm
        10:00-10:19   base at 95.5
        10:20-10:21   breaks the 115.5 daily range at 120.5 on 2x volume

    Armed 09:45 (after the spike) the window starts at the base and the breakout IS the high — the add
    fires. Armed 09:30 (before it) the session high is 135.5, the breakout sits 11% below it, the fade is
    satisfied and the add is refused. Same bars, same gates, opposite answers.
    """
    bars_1d = _gradually_strengthening_uptrend_bars(30)
    range_high = _daily_range_high(
        _FakeStrategy(cache=_FakeCache(bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25)),
        "AMAT.XNAS", lookback_days=20,
    )
    spike = range_high + 20
    bars_1m = (
        [_FakeBar(high=h, close=h, low=h - 0.5, ts_event=_rth_ts_ns(9, 31 + i), volume=100.0)
         for i, h in enumerate([spike, spike - 4, spike - 8])]
        + [_FakeBar(high=range_high - 20, close=range_high - 20.5, low=range_high - 21,
                    ts_event=_rth_ts_ns(10, i), volume=100.0) for i in range(20)]
        + [_FakeBar(high=range_high + 5, close=range_high + 4.8, low=range_high + 4,
                    ts_event=_rth_ts_ns(10, 20 + i), volume=200.0) for i in range(2)]
    )
    strategy = _FakeStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 25)
    )

    armed_after = _row(created_at=_rth_dt(9, 45))
    armed_before = _row(created_at=_rth_dt(9, 30))

    assert asyncio.run(_PyramidWatch()._add_condition(strategy, armed_after)) is True
    # The pre-#283 behaviour, still reachable by arming before the spike — and the reason this test
    # discriminates: with the bug restored BOTH arms return False, because both measure against 135.5.
    assert asyncio.run(_PyramidWatch()._add_condition(strategy, armed_before)) is False


def test_pyramid_does_not_act_outside_regular_hours():
    """Same class as #255 High 4 in PEAK, aimed at the class rather than the instance.

    PYRAMID's exit cancels a resting trail and submits a market order exactly as PEAK's does, so the same
    after-hours reading could strip protection and leave the position naked overnight having sold nothing.
    Fixing only the manager the ticket named would leave the sibling defect in place — which is how the
    fade-window bug (#253/#283) came to exist in two managers at once.
    """
    strategy_in = _FakeStrategy(
        cache=_FakeCache(bars_1m=_faded_before_arm_then_flat(), bars_1d=_uptrend_bars(40)),
        now_ts_ns=_rth_ts_ns(11, 3),
    )
    row = _row(created_at=_rth_dt(9, 30))
    assert asyncio.run(_PyramidWatch()._exit_condition(strategy_in, row)) is True

    # In hours, with a live position and price, the same fade DOES trigger — without this the closed-hours
    # assertions below pass on a `trigger_met` that returns False for an unrelated reason (a fake with no
    # position short-circuits at `pos is None`, which is how the first version of this test proved nothing).
    def _live(now_ts_ns):
        return _FakeStrategy(
            cache=_FakeCache(bars_1m=_faded_before_arm_then_flat(), bars_1d=_uptrend_bars(40)),
            now_ts_ns=now_ts_ns, position=_FakePosition(quantity=100), last_price=150.0,
        )

    assert asyncio.run(_PyramidWatch().trigger_met(_live(_rth_ts_ns(11, 3)), row)) is True

    for hour, minute, label in [(4, 0, "pre-market"), (16, 30, "after the close")]:
        assert asyncio.run(_PyramidWatch().trigger_met(_live(_rth_ts_ns(hour, minute)), row)) is False, label


def test_a_pyramid_row_whose_position_closed_terminalizes_even_though_the_market_is_shut():
    """codex, High — a zombie path I introduced with the RTH gate, on top of one already there.

    Two bugs stacked. PYRAMID returned False when flat, so `apply()` was never called and the row sat ARMED
    forever — the same defect #255 High 1 fixed in PEAK and never applied here. Then the RTH gate went in
    ABOVE that check, so even at the next open the flat row returned False first and could never recover.

    A position closing while the market is shut is the ordinary case, not an exotic one. `apply()` on flat
    returns a terminal FAILED, so firing is what lets the row retire; waiting for a session it will never
    care about is what leaves a zombie. Trail resync can wait for RTH — stale flat state cannot.
    """
    bars = _FakeCache(bars_1m=_faded_before_arm_then_flat(), bars_1d=_uptrend_bars(40))
    row = _row(created_at=_rth_dt(9, 30))

    # Flat, market shut: must still fire so the row can terminalize.
    flat_closed = _FakeStrategy(cache=bars, now_ts_ns=_rth_ts_ns(16, 30), position=None, last_price=150.0)
    assert asyncio.run(_PyramidWatch().trigger_met(flat_closed, row)) is True

    # Flat, market open: same.
    flat_open = _FakeStrategy(cache=bars, now_ts_ns=_rth_ts_ns(11, 3), position=None, last_price=150.0)
    assert asyncio.run(_PyramidWatch().trigger_met(flat_open, row)) is True

    # HELD and market shut: must NOT act — this is what the RTH gate is for, and it still holds.
    held_closed = _FakeStrategy(
        cache=bars, now_ts_ns=_rth_ts_ns(16, 30), position=_FakePosition(quantity=100), last_price=150.0,
    )
    assert asyncio.run(_PyramidWatch().trigger_met(held_closed, row)) is False


def test_a_FLIPPED_position_is_not_treated_as_a_closed_one():
    """codex, High. `_position_for(expected_side)` returns None both when the position CLOSED and when it
    FLIPPED to the other side — the two are indistinguishable to it, and PYRAMID treated both as flat.

    A flip is not a close. The manager was armed to scale into a LONG; if the book is now SHORT the
    premise is void and the row must retire, but retiring it with "position already flat" is a false
    account of what happened, and any later reader of the action log is misled about whether the name was
    exited or reversed.

    `_any_position_open` exists precisely for this and PEAK has used it since #274. Applying it here is the
    same aim-at-the-class rule that put the RTH gate and the fade window into both managers.
    """
    bars_1d, bars_1m = _riding_bars()
    row = _row()

    class _FlippedStrategy(_FakeStrategy):
        """LONG is gone, SHORT is open — what a reversal actually looks like to the manager."""

        def _position_for(self, instrument_id, strategy_id, side):
            return _FakePosition(quantity=100) if side == "SHORT" else None

    flipped = _FlippedStrategy(
        cache=_FakeCache(bars_1m=bars_1m, bars_1d=bars_1d), now_ts_ns=_rth_ts_ns(10, 6), last_price=150.0,
    )
    assert asyncio.run(_PyramidWatch().trigger_met(flipped, row)) is True

    state, reason = asyncio.run(_PyramidWatch().apply(flipped, row))
    assert state == "FAILED"
    assert "flip" in reason.lower(), reason
    # And emphatically NOT reported as a clean exit.
    assert "already flat" not in reason.lower()
