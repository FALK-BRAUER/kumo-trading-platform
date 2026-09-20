"""The cockpit-side calendar, built from whatever adapter is attached (#628).

`build_calendar(require_exchange=True, calendar=...)` now accepts an injected calendar
(kumo-trading-strategies 78e634c), so an IBKR-only instance can finally satisfy the requirement instead of
being refused. This is the thing cockpit injects.

IT MUST BE LAZY, AND THAT IS THE WHOLE DESIGN. `build_node` constructs the lanes BEFORE the node runs
— `TradingNode.build()` only registers clients (`live/node.py:272-281`); instruments load on CONNECT
(`adapters/interactive_brokers/data.py:147`), which the kernel awaits at `system/kernel.py:1024`
before starting the trader at `:1039`.

So at construction the cache is EMPTY. Reading hours eagerly would produce a calendar that knows
nothing, forever — which is exactly the trap `_instrument_ids` fell into and #622 fixed. Resolve on
first use, which is after `on_start`.

THREE STATES, from `venue_hours.parse_sessions`:

    TradingDay              the venue said this day trades
    None                    the venue said CLOSED
    OutsideCalendarWindow   the venue never described this day  <- RAISES

The third is not a variant of the other two. IB returns a rolling ~6-day window and `next_fire` looks
14 days ahead, so it runs off the end of knowledge on every call. `elapsed_slots` reads "no session"
as "nothing was due", so an unknown day returned as None is a decision silently not happening.
Absence must not be readable as permission.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from kumo_strategies.runtime.calendar import OutsideCalendarWindow, TradingDay

from api.venue_calendar import VenueCalendar

_ET = ZoneInfo("US/Eastern")
_LIQUID = (
    "20260826:0930-20260826:1600;20260827:0930-20260827:1600;20260828:0930-20260828:1600;"
    "20260829:CLOSED;20260830:CLOSED;20260831:0930-20260831:1600"
)


def _cache(info: dict | None, calls: list | None = None):
    """A Cache stand-in shaped like Nautilus's — `instrument(iid)` returning an object with `.info`."""
    class _C:
        def instrument_ids(self):
            return []

        def instruments(self):
            if calls is not None:
                calls.append(1)
            return [] if info is None else [SimpleNamespace(info=info)]

    return _C()


def _cal(info: dict | None, calls: list | None = None) -> VenueCalendar:
    return VenueCalendar(lambda: _cache(info, calls))


_GOOD = {"liquidHours": _LIQUID, "timeZoneId": "US/Eastern"}


def test_the_fixture_spans_all_three_states():
    """FIXTURE PROPERTY FIRST. Without an open day, a CLOSED day and a day outside the window, two
    states could collapse into one and every assertion below would still pass."""
    assert "0930-" in _LIQUID and "CLOSED" in _LIQUID and "20261126" not in _LIQUID


def test_it_does_NOT_read_the_cache_at_construction():
    """THE TRAP #622 ALREADY FELL INTO. Lanes are built before any adapter connects, so an eager read
    caches an EMPTY calendar for the life of the process — and every day then looks undescribed."""
    calls: list = []
    _cal(_GOOD, calls)
    assert calls == [], "the calendar read the cache at construction, when it is still empty (#628)"


def test_an_OPEN_day_is_a_real_TradingDay_in_the_VENUES_timezone():
    """A container runs UTC and the session is Eastern. A naive parse puts the open at 04:30 ET and
    every scheduled decision fires five hours early."""
    td = _cal(_GOOD).day(date(2026, 8, 28))
    assert isinstance(td, TradingDay)
    assert (td.open_at.hour, td.open_at.minute) == (9, 30)
    assert (td.close_at.hour, td.close_at.minute) == (16, 0)
    assert td.open_at.utcoffset() == timedelta(hours=-4)


def test_a_day_the_venue_called_CLOSED_returns_None():
    """None means the venue SAID closed — a fact. Distinct from never having mentioned it."""
    assert _cal(_GOOD).day(date(2026, 8, 29)) is None


def test_a_day_OUTSIDE_the_window_RAISES_rather_than_returning_None():
    """THE ONE THAT MATTERS. Returning None here makes `elapsed_slots` report "nothing was due" for a
    day nobody described — a decision silently not happening, mid-session, permanently."""
    with pytest.raises(OutsideCalendarWindow):
        _cal(_GOOD).day(date(2026, 11, 26))


def test_next_fire_is_present_because_QC27_and_QC345_call_it_DIRECTLY():
    """The peer caught this: `next_fire` is not reached through the helpers my first double
    exercised. qc27_rotation.py:312 and qc345_rotation.py:352 call it on the calendar itself, so an
    injected calendar without it raises AttributeError the first time a lane ARMS — after boot, on a
    path that catches and retries, i.e. silently unarmed."""
    session, fire = _cal(_GOOD).next_fire(
        datetime(2026, 8, 28, 8, 0, tzinfo=_ET), offset_minutes=5)
    assert session == date(2026, 8, 28)
    assert (fire.hour, fire.minute) == (9, 35)


def test_next_fire_SKIPS_a_closed_day():
    """Friday evening must land on Monday, not Saturday — and it is the venue's CLOSED that says so,
    not a weekday rule."""
    session, _ = _cal(_GOOD).next_fire(
        datetime(2026, 8, 28, 20, 0, tzinfo=_ET), offset_minutes=5)
    assert session == date(2026, 8, 31)


def test_next_fire_RAISES_rather_than_inventing_a_day_past_the_window():
    """It searches forward and will leave the described window. It must refuse there too, or the
    refusal in `day()` is bypassed by the very caller that matters most."""
    with pytest.raises(OutsideCalendarWindow):
        _cal(_GOOD).next_fire(datetime(2026, 8, 31, 20, 0, tzinfo=_ET), offset_minutes=5)


def test_is_trading_day_agrees_with_day_on_ALL_THREE_states():
    """Two derivations of one fact drift. Open and closed must agree; and an unknown day must not be
    quietly turned into a bool by this method when `day()` refuses to answer."""
    cal = _cal(_GOOD)
    assert cal.is_trading_day(date(2026, 8, 28)) is True
    assert cal.is_trading_day(date(2026, 8, 29)) is False
    with pytest.raises(OutsideCalendarWindow):
        cal.is_trading_day(date(2026, 11, 26))


def test_an_EMPTY_cache_REFUSES_every_date_rather_than_reporting_closed():
    """Before connect, or on a venue that says nothing, the calendar knows NOTHING. Every day is
    undescribed — not closed. Reporting closed would silently cancel trading rather than say why."""
    cal = _cal(None)
    with pytest.raises(OutsideCalendarWindow):
        cal.day(date(2026, 8, 28))


def test_it_RE_READS_until_it_finds_hours_so_a_pre_connect_call_is_not_fatal():
    """A `day()` before connect must not poison the calendar for the process lifetime. The empty read
    is not cached as an answer — that is the same "fixes one boot, re-breaks the next" failure as
    resolving venues from a warm cache."""
    box = {"info": None}

    class _C:
        def instruments(self):
            return [] if box["info"] is None else [SimpleNamespace(info=box["info"])]

    cal = VenueCalendar(lambda: _C())
    with pytest.raises(OutsideCalendarWindow):
        cal.day(date(2026, 8, 28))
    box["info"] = _GOOD                                   # the adapter has now connected
    assert isinstance(cal.day(date(2026, 8, 28)), TradingDay)


def test_the_refusal_NAMES_the_window_it_actually_knows():
    """A mutation that swallowed the refusal inside `next_fire` still raised the right EXCEPTION —
    just from the end of a fruitless 14-day scan, with a message naming nothing.

    That is not a behaviour defect, so no other assertion here caught it. It is a diagnosis defect,
    and this is the message an operator reads when a lane will not arm: "the venue has not described
    2026-11-26; it told us about 2026-08-26..2026-08-31" says the window needs to advance. "no
    trading day found within 14 days" says nothing about why.
    """
    with pytest.raises(OutsideCalendarWindow) as e:
        _cal(_GOOD).day(date(2026, 11, 26))
    msg = str(e.value)
    assert "2026-11-26" in msg, "the refusal does not name the day it was asked about"
    assert "2026-08-26" in msg and "2026-08-31" in msg, (
        "the refusal does not name the window the venue actually described, so an operator cannot "
        "tell a stale window from a broken calendar"
    )


def test_the_CAPABILITY_reaches_the_strategy_and_the_lanes():
    """THE WIRING, and it shipped GREEN and broke staging without this.

    My edit script anchored on a field that lives on an unmerged branch, so every replace silently
    no-op'd: `supplies_trading_calendar` existed nowhere, the lanes asked for a calendar nothing
    provided, and the engine crash-looped on

        RuntimeError: no APCA credentials and no calendar supplied

    CI passed because every test here constructs `VenueCalendar` DIRECTLY. Not one of them asked
    whether the provider declares the capability, whether `build_node` forwards it, or whether the
    lanes receive it. The unit was right and the chain was absent — this repo's signature failure,
    and the third time today.
    """
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod
    from api.providers.base import DataClientSpec
    from api.providers.ibkr import build_data

    assert "supplies_trading_calendar" in DataClientSpec.__dataclass_fields__, (
        "the provider contract cannot declare a calendar capability"
    )
    assert build_data({}).supplies_trading_calendar is True, (
        "IBKR does not declare that it supplies a calendar, so an IBKR node still cannot boot (#628)"
    )
    # daily_bars_cover is REQUIRED since #616 — unrelated to the calendar capability under test.
    assert DataClientSpec(
        client_id="X", config=None, factory=None, daily_bars_cover="rth",
        # required since #612 / #812, and orthogonal to the calendar capability under test
        streams_trade_ticks=True, streams_quote_ticks=True,
        price_adjustments=frozenset({"raw"}),  # required since #1124, orthogonal here
    ).supplies_trading_calendar is False, (
        "a provider that says nothing is treated as supplying one — Alpaca's info carries no hours, "
        "so every day would read as undescribed and the trading tenant would stop arming"
    )

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.build_node)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    assert "supplies_trading_calendar=spec.supplies_trading_calendar" in ast.unparse(fn), (
        "build_node does not forward the capability, so the constructor default wins and no lane "
        "ever receives a calendar"
    )


def test_EVERY_lane_passes_the_calendar_it_was_given():
    """AIMED AT THE CLASS. Three lanes call `build_calendar(require_exchange=True)`; one that forgets
    `calendar=` crash-loops the whole node on an IBKR instance, because the refusal is at BUILD."""
    import ast
    import pathlib as _pl

    root = _pl.Path(__file__).parent.parent / "strategies"
    missing = []
    for lane in ("momentum.py", "qc27.py", "qc345.py"):
        tree = ast.parse((root / lane).read_text())
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        if "build_calendar(" in code and "_venue_calendar" not in code:
            missing.append(lane)
    assert not missing, (
        f"these lanes call build_calendar without passing the venue calendar: {missing}. On an "
        f"IBKR instance that raises at BUILD and takes the whole node down (#628)"
    )


def test_this_module_does_NOT_hard_require_kumo_strategies_at_import():
    """CI HAS A JOB WHOSE ENTIRE PURPOSE IS THIS, and I broke it.

    153 of 184 test files do not import kumo-trading-strategies; the 30 that do are a separate job holding
    the private-repo credential. `api.engine_node` imports this module, so a module-level
    kumo-trading-strategies import here makes the whole engine unimportable without it — and the `backend`
    job failed with ModuleNotFoundError while `backend-full` passed, which is exactly the pair of
    signals that job exists to produce.

    Every other kumo-trading-strategies import in this codebase is function-local for the same reason. This
    pins it so the next one is caught before CI rather than by it.
    """
    import ast
    import pathlib as _pl

    src = (_pl.Path(__file__).parent / "venue_calendar.py").read_text()
    offenders = [
        (getattr(n, "module", "") or "")
        for n in ast.parse(src).body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and "kumo_strategies" in ((getattr(n, "module", "") or "")
                                  + "".join(a.name for a in getattr(n, "names", [])))
    ]
    assert not offenders, (
        f"venue_calendar imports kumo_strategies at module level ({offenders}); api.engine_node "
        f"imports this file, so the engine becomes unimportable without the private dependency"
    )
