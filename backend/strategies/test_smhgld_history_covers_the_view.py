"""SMHGLD's history request covers its declared market view, in SESSIONS, on any boot date (#1049).

THE DEFECT. `build_smhgld_strategy` passed `history_days=30` — ~21 trading sessions — sized to the
lane's own warmup (`_need` = 3) when that was the only consumer of its daily bars. ks#212 makes the
lane declare `INDEX_VS_MA / window 50`, and a 50-session average needs 51 sessions of the lane's own
bars: measured on an Alpaca paper instance 2026-09-12, a boot at 10:03:56Z requested `start=2026-08-13` for both legs,
and TECHIVOL's identical view says in its own reason line what that costs — "1 sessions of history,
51 needed for a 50-session average with dwell 1". The view would read UNKNOWN until late October and
`hook_unknown_twice` would page on every restart in between.

THE REQUIREMENT IS DERIVED, NEVER TYPED. It is `max(view.window + view.dwell, warmup_bars_needed(cfg))`
when the lane DECLARES a view and `warmup_bars_needed(cfg)` when it does not — `market_state` needs
`window + dwell` rows (market_view.py:371), and a number read off an undeclared view is one the lane
has not declared. Read off the INSTALLED kumo-trading-strategies config, so a change to either side of the
seam moves the number here.

NECESSARY, NOT SUFFICIENT ON ITS OWN. The adapter keeps its bars in `deque(maxlen=...)`; on b575db1
that is `_need + 5` = 8 per leg, so a 51-row average can never compute whatever this request asks
for. ks#212 sizes it to the view. The readback below pins the built adapter's container against the
same requirement, so a cockpit deploy against an older ks pin fails here rather than reading UNKNOWN
for six weeks with every surface green. The SUPPLY is a LOWER BOUND on NYSE sessions inside the requested calendar window: weekdays
counted exactly over the real date range, minus an upper bound on full-day holidays. No offline
holiday table exists in either repo, so a bound is the honest derivation — it under-counts by a few
sessions (holidays are ~9-10 a year, not 10 in every window) and never over-counts. Evaluated on
every boot date of the coming year, because a December window is the one that dips.

Every test here was seen red on 04036f6 (history_days=30) for the reason its docstring names.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest

from strategies.test_crsi_short_builder import _Feed, _settings, _stub_store_and_calendar

#: NYSE full-day closures per year, upper bound. 2024: 9 (incl. the Carter day of mourning), 2025: 10
#: (incl. Jan 9, Carter), 2026: 9 scheduled. 10 pro-rated over the window, plus one for alignment.
_NYSE_HOLIDAYS_PER_YEAR_UPPER = 10


def _upstream_or_skip():
    """Skip in a venv without the sleeve — but FAIL under the merge gate's pin run, where
    `MERGE_GATE_EXPECT_KS_TREE` names the tree the suite must import from: a skip there is a green
    that measured nothing (the ks#146 shape)."""
    import os
    try:
        from kumo_strategies.runtime.nautilus.smhgld_sleeve import SmhGldSleeveStrategy, warmup_bars_needed
        from kumo_strategies.strategies.smhgld_sleeve import live_config
    except ImportError as exc:
        if os.environ.get("MERGE_GATE_EXPECT_KS_TREE"):
            pytest.fail(f"the pin run cannot import the SMHGLD sleeve adapter: {exc}")
        pytest.skip("installed kumo-trading-strategies predates the SMHGLD sleeve adapter (ks#177)")
    return SmhGldSleeveStrategy, warmup_bars_needed, live_config


def sessions_lower_bound(boot: dt.date, history_days: int) -> int:
    """Sessions the venue serves in `[boot - history_days, boot)`, bounded from BELOW.

    Weekdays are exact for the actual range; holidays are bounded at `_NYSE_HOLIDAYS_PER_YEAR_UPPER`
    pro-rated to the window, rounded up, plus one so a window that starts or ends on a holiday
    weekend cannot be over-counted. A bound that can only under-count is the right side to be wrong
    on: it can refuse a sufficient window, never accept an insufficient one.
    """
    start = boot - dt.timedelta(days=history_days)
    weekdays = sum(1 for i in range(history_days) if (start + dt.timedelta(days=i)).weekday() < 5)
    holidays_upper = math.ceil(_NYSE_HOLIDAYS_PER_YEAR_UPPER * history_days / 365) + 1
    return weekdays - holidays_upper


def sessions_required(cfg, warmup_bars_needed) -> int:
    """`max(view.window + view.dwell, _need)` for a DECLARED view, `_need` alone otherwise. Both
    consumers read the same per-leg bar container, so the larger need governs. `window + dwell` is
    `market_state`'s own requirement (market_view.py:371: "51 needed for a 50-session average with
    dwell 1" is `50 + 1`) — the first draft wrote `+ 1` and would have passed a dwell-3 lane that
    reads UNKNOWN. A NONE view declares nothing, so its window is not read."""
    from kumo_strategies.strategies.market_view import MarketSignal
    view = getattr(cfg, "market_view", None)
    need = int(warmup_bars_needed(cfg))
    if view is None or view.signal is MarketSignal.NONE:
        return need
    return max(int(view.window) + int(view.dwell), need)


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_bound_counts_weekdays_exactly_and_holidays_from_above():
    """30 days ending on Monday 2026-09-14 = the 20 weekdays of Aug 17 - Sep 11, minus
    ceil(10*30/365)+1 = 2 → 18; 90 days ending 2027-01-04 (the holiday-heavy window: Thanksgiving,
    Christmas, New Year) = 64 weekdays minus ceil(10*90/365)+1 = 4 → 60; 75 days ending the same
    day = 53 - 4 → 49. Computed, not hand-counted — the first draft of this docstring said 22 and 65.
    And the bound must never exceed the weekday count."""
    assert sessions_lower_bound(dt.date(2026, 9, 14), 30) == 18
    assert sessions_lower_bound(dt.date(2027, 1, 4), 90) == 60
    assert sessions_lower_bound(dt.date(2027, 1, 4), 75) == 49
    for days in (30, 60, 75, 90):
        assert sessions_lower_bound(dt.date(2026, 9, 14), days) < sum(
            1 for i in range(days) if (dt.date(2026, 9, 14) - dt.timedelta(days=days) + dt.timedelta(days=i)).weekday() < 5)


def test_FIXTURE_the_requirement_follows_the_DECLARED_view_and_the_dwell():
    """Three shapes, three numbers, none typed into the assertion below: an undeclared view costs
    only the lane's warmup (3); the 50/1 view costs 51; a 50/3 view costs 53 (the `+ 1` draft would
    have said 51 and passed a lane that reads UNKNOWN)."""
    _, warmup_bars_needed, live_config = _upstream_or_skip()
    from kumo_strategies.strategies.market_view import MarketAction, MarketSignal, MarketViewConfig
    cfg = live_config()
    assert warmup_bars_needed(cfg) == 3, "the lane's own warmup moved; re-derive the docstring numbers"
    undeclared = dataclasses.replace(cfg, market_view=MarketViewConfig())
    d1 = dataclasses.replace(cfg, market_view=MarketViewConfig(signal=MarketSignal.INDEX_VS_MA, window=50, dwell=1, action=MarketAction.EXIT_ONLY))
    d3 = dataclasses.replace(cfg, market_view=MarketViewConfig(signal=MarketSignal.INDEX_VS_MA, window=50, dwell=3, action=MarketAction.EXIT_ONLY))
    assert sessions_required(undeclared, warmup_bars_needed) == 3
    assert sessions_required(d1, warmup_bars_needed) == 51
    assert sessions_required(d3, warmup_bars_needed) == 53


def _declared_50_1(live_config):
    """The view ks#212 declares. Constructed here so this file is red on b575db1 (where the live
    config still says NONE) for the HISTORY reason and not for ks#212's — and stays correct once
    `live_config()` carries it, because the two are then equal."""
    from kumo_strategies.strategies.market_view import MarketAction, MarketSignal, MarketViewConfig
    return dataclasses.replace(live_config(), market_view=MarketViewConfig(
        signal=MarketSignal.INDEX_VS_MA, window=50, dwell=1, action=MarketAction.EXIT_ONLY))


def test_FIXTURE_75_days_is_NOT_enough_in_the_holiday_window_which_is_why_the_lead_ruled_90():
    """The lead's reason, measured rather than asserted: a 75-day window ending in early January
    bounds to fewer than 51 sessions; 90 clears it on every boot date."""
    _, warmup_bars_needed, live_config = _upstream_or_skip()
    required = sessions_required(_declared_50_1(live_config), warmup_bars_needed)
    assert sessions_lower_bound(dt.date(2027, 1, 4), 75) < required
    assert sessions_lower_bound(dt.date(2027, 1, 4), 90) >= required


# ------------------------------------------------------------------ the seam -------------------

def _built_history_days(monkeypatch) -> int:
    """What the REAL builder hands the REAL adapter — read back off the constructed strategy, the
    same harness `test_smhgld_builder` uses (network, database and credentials stubbed; nothing else)."""
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    strategy = smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    days = getattr(strategy, "_history_days", None)
    assert isinstance(days, int) and days > 0, f"the builder passed no history_days ({days!r}) — the lane cannot warm at all (ks#200)"
    return days


def test_the_builder_requests_enough_history_for_the_view_on_EVERY_boot_date_of_the_coming_year(monkeypatch):
    """THE ASSERTION. Supply (lower bound, from the calendar) ≥ requirement (from the installed
    config), for each of the next 366 boot dates — so the holiday-heavy window is judged, not only
    today's. Red on 04036f6: 30 days bounds to 18-20 sessions against 51."""
    _, warmup_bars_needed, live_config = _upstream_or_skip()
    required = sessions_required(_declared_50_1(live_config), warmup_bars_needed)
    days = _built_history_days(monkeypatch)
    today = dt.date(2026, 9, 14)
    short = [(today + dt.timedelta(days=i), sessions_lower_bound(today + dt.timedelta(days=i), days))
             for i in range(366)]
    short = [(d, n) for d, n in short if n < required]
    assert not short, (
        f"history_days={days} bounds to fewer than the {required} sessions the declared view needs on "
        f"{len(short)} boot date(s), first {short[0][0]} ({short[0][1]} sessions); the view reads UNKNOWN "
        f"until enough sessions accrue live, and hook_unknown_twice pages on every restart until then (#1049)")


def test_the_history_the_builder_passes_reaches_the_adapters_own_attribute(monkeypatch):
    """The kwarg travels: `_history_days` on the constructed adapter is what `on_start` reads to
    size `request_bars`. A builder that computed the right number and passed it under the wrong
    name would satisfy the literal test and request nothing."""
    SmhGldSleeveStrategy, _, _ = _upstream_or_skip()
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    strategy = smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    assert isinstance(strategy, SmhGldSleeveStrategy)
    assert strategy._history_days == _built_history_days(monkeypatch)


def test_the_built_adapter_KEEPS_enough_bars_for_the_view_it_declares(monkeypatch):
    """NECESSARY AND SUFFICIENT, together. The request above fills `_bars`, and `_bars` is a bounded
    deque: on b575db1 `maxlen = _need + 5 = 8`, so the 51 rows the view needs are discarded on
    arrival and UNKNOWN is permanent at any `history_days`. ks#212 sizes the container to the view.
    Judged against the DECLARED view on the installed config — so on a ks pin that predates ks#212
    (view NONE, container 8) this passes on the lane's own 3, and the moment the pin declares 50/1
    it demands 51. A cockpit deploy that bumps this repo's pin without ks#212's fails HERE."""
    SmhGldSleeveStrategy, warmup_bars_needed, live_config = _upstream_or_skip()
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    strategy = smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    assert isinstance(strategy, SmhGldSleeveStrategy)
    required = sessions_required(strategy._cfg, warmup_bars_needed)
    for leg in ("SMH", "GLD"):
        container = strategy._bars[leg]
        maxlen = getattr(container, "maxlen", None)
        assert maxlen is None or maxlen >= required, (
            f"{leg}: the adapter keeps at most {maxlen} bars and its declared view needs {required} — "
            f"the history requested above is discarded on arrival and the view reads UNKNOWN forever "
            f"(ks#212 sizes the container; this cockpit pin needs that ks pin)")
