"""One way to ask a calendar, because there are two calendars and they do not share an interface.

MEASURED ON AN ALPACA PAPER INSTANCE, 2026-08-31, from the running engine's own health frame:

    "subscriptions": {"requested": null, ..., "error":
       "'AlpacaCalendar' object has no attribute 'trading_minutes_between'"}

The subscription ledger asked for that method BY NAME. `VenueCalendar` (IBKR-backed, #628) has it;
the broker fallback a paper instance gets (`AlpacaCalendar`, from kumo-trading-strategies) exposes
`day`/`is_trading_day`/`next_fire` and not that. So requested-versus-bound was dead on paper from the
moment it shipped, while working on staging — the same code, one venue apart, for the second time
today.

IT WAS CAUGHT ONLY BECAUSE THE SUMMARY REPORTS ITS OWN FAILURES. Had `_subscription_summary` returned
an empty dict on exception, paper would have shown `0 of 0` and read as a stack with nothing
subscribed. That is the entire argument for degrading loudly, and this is the first time it has paid.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.venue_calendar import calendar_minutes, minutes_open

MIN = 60_000_000_000
DAY = date(2026, 8, 31)
OPEN_UTC = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)   # 09:30 ET
CLOSE_UTC = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)   # 16:00 ET


def _session(d):
    return SimpleNamespace(open_at=OPEN_UTC, close_at=CLOSE_UTC) if d == DAY else None


class _DayOnly:
    """The broker fallback's shape: `day` and no `trading_minutes_between`."""

    def day(self, d):
        return _session(d)


class _MinutesOnly:
    """The IBKR-backed shape."""

    def trading_minutes_between(self, start_ns, end_ns):
        return 42.0


class _Neither:
    """An object that describes no venue at all."""


def _ns(dt):
    return int(dt.timestamp() * 1e9)


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the day-only calendar must genuinely lack the method, or this file is asserting
    that a working thing works."""
    assert not hasattr(_DayOnly(), "trading_minutes_between"), (
        "the double supplies the very method whose absence is the defect"
    )
    assert callable(_DayOnly().day)


def test_a_calendar_with_ONLY_day_still_answers():
    """The defect. Paper's calendar is this shape and got an AttributeError instead of a number."""
    got = calendar_minutes(_DayOnly(), _ns(OPEN_UTC), _ns(CLOSE_UTC))
    assert got == pytest.approx(390.0), (
        f"a day-only calendar could not be asked how long the venue was open: got {got!r}. This is "
        f"the shape a PAPER instance has, so requested-versus-bound is dead there."
    )


def test_the_OTHER_shape_is_untouched():
    """And the direct method still wins where it exists — a fix for one venue must not reroute the
    other through a second code path."""
    assert calendar_minutes(_MinutesOnly(), 0, 1) == 42.0


def test_BOTH_SHAPES_AGREE_on_the_same_span():
    """The point of sharing one walk. Two implementations of this disagreeing is how a 1.69% gap in
    an exit simulator exposed a population bias — here it would silently make one venue's staleness
    threshold mean something different from the other's."""
    span = (_ns(OPEN_UTC), _ns(CLOSE_UTC))

    class _Delegating:
        def trading_minutes_between(self, a, b):
            return minutes_open(_session, a, b)

    assert calendar_minutes(_DayOnly(), *span) == calendar_minutes(_Delegating(), *span)


def test_a_calendar_that_ANSWERS_NONE_passes_None_through():
    """`None` means "I do not cover this span" — an ANSWER, and a different one from a raise. Turning
    it into `float(None)` would collapse the third state into a crash."""

    class _CannotSay:
        def trading_minutes_between(self, a, b):
            return None

    assert calendar_minutes(_CannotSay(), 0, 1) is None


def test_an_object_that_is_NOT_A_CALENDAR_raises_rather_than_reading_as_closed():
    """A fallback here would be a silent wrong answer: zero open minutes is what a weekend looks
    like, and every staleness and silence check would read a missing calendar as a quiet market."""
    with pytest.raises(AttributeError, match="describes no venue sessions"):
        calendar_minutes(_Neither(), 0, 1)


def test_a_span_the_calendar_REFUSES_still_propagates():
    """`day()` raises past the window it describes, and that must not be swallowed on the way through
    the adapter — a day the venue never described must not become a trading day or a closed one."""

    class _Refuses:
        def day(self, d):
            raise RuntimeError("OutsideCalendarWindow")

    with pytest.raises(RuntimeError):
        calendar_minutes(_Refuses(), _ns(OPEN_UTC), _ns(CLOSE_UTC))


def test_the_LEDGER_now_measures_on_a_day_only_calendar():
    """The seam. The adapter being right is not the claim — the claim is that the ledger uses it."""
    from api.subscription_ledger import SILENT, SILENT_AFTER_TRADING_MINUTES, SubscriptionLedger

    led = SubscriptionLedger()
    led.requested("bars", "AAPL", _ns(OPEN_UTC))
    later = _ns(OPEN_UTC + timedelta(minutes=SILENT_AFTER_TRADING_MINUTES + 5))
    assert led.state_of("bars", "AAPL", later, _DayOnly()) == SILENT, (
        "the ledger still cannot measure with a day-only calendar, so paper reports nothing"
    )
