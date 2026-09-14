"""The calendar's source is a ROLLING window, so a memoised copy of it is guaranteed to go stale (#813).

MEASURED ON staging2, 2026-09-09 06:20 UTC. Both automated lanes had been UNARMED since 09-06:

    BCTROT-004: UNARMED — the trading calendar did not answer (OutsideCalendarWindow('the venue has
    not described 2026-09-09; it told us about 2026-09-02..2026-09-04. Refusing rather than assuming,
    because a day nobody confirmed must not become a trading day.'))

320 UNARMED lines in that one boot, 306 OutsideCalendarWindow. Nothing could decide, and the refusal
was correct at every step — the calendar genuinely did not know about today. Two independent defects
put it in that state, and either one alone is enough:

  1. `_load()` returns the memoised dict whenever it is non-empty. IB's `liquidHours` is a rolling
     ~6-day window — `venue_hours.py`'s own docstring says so, and `venue_calendar.py`'s says
     `next_fire` "runs off the end of knowledge on every call". A value that rolls, cached once for
     the life of the process, ages out of validity by construction. Not an edge case: the normal path
     on any node that outlives the window.

  2. `_load()` takes "the first instrument that carries them". staging2's cache held BOTH windows:
     74 instruments stamped 20260902..20260904 (last fetched 09-01, then served from the durable
     redis cache and never re-fetched) and 259 stamped 20260908..20260910. Iteration order picked a
     stale one. The correct answer was in the cache the entire time.

WHY A SEPARATE FILE. `test_venue_calendar_adapter.py`'s only cache double yields exactly ONE
instrument, so no test there could express "the cache holds a stale one and a fresh one" — the double
could not represent the bug. That is the defect, not an inconvenience.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from kumo_strategies.runtime.calendar import OutsideCalendarWindow, TradingDay

from api.venue_calendar import VenueCalendar
from api.venue_hours import parse_sessions

_ET = ZoneInfo("US/Eastern")

#: The two real windows off staging2's book on 2026-09-09. Disjoint, which is what makes the bug
#: reachable — see the fixture-property test below.
_STALE = "20260902:0930-20260902:1600;20260903:0930-20260903:1600;20260904:0930-20260904:1600"
_FRESH = ("20260907:CLOSED;20260908:0930-20260908:1600;20260909:0930-20260909:1600;"
          "20260910:0930-20260910:1600")

_STALE_INFO = {"liquidHours": _STALE, "timeZoneId": "US/Eastern"}
_FRESH_INFO = {"liquidHours": _FRESH, "timeZoneId": "US/Eastern"}


def _cache(infos):
    """A cache holding SEVERAL instruments — the production shape.

    `instruments()` is re-read on every call rather than snapshotted, because that is what the live
    Nautilus cache does and it is the only way a test can advance the venue's window mid-life.
    """
    class _C:
        def instrument_ids(self):
            return []

        def instruments(self):
            return [SimpleNamespace(info=i) for i in infos]

    return _C()


def _mutable_cache(box):
    """One instrument whose `info` the test can swap, modelling the venue rolling its window forward."""
    class _C:
        def instrument_ids(self):
            return []

        def instruments(self):
            return [SimpleNamespace(info=box["info"])]

    return _C()


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTY FIRST. Asserting the behaviour is worthless if the fixture cannot violate it.
# ---------------------------------------------------------------------------------------------

def test_the_fixture_windows_are_DISJOINT_so_the_bug_is_reachable():
    """If the two windows overlapped on the day being asked about, every assertion below would pass
    whether the calendar re-read or not — a test that cannot fail, carrying no information."""
    stale, fresh = parse_sessions(_STALE, "US/Eastern"), parse_sessions(_FRESH, "US/Eastern")
    assert set(stale) & set(fresh) == set(), "the fixture windows overlap — the bug is unreachable"
    assert date(2026, 9, 9) in fresh and date(2026, 9, 9) not in stale
    assert date(2026, 9, 3) in stale and date(2026, 9, 3) not in fresh


def test_the_fixture_carries_a_stated_CLOSED_so_the_third_state_is_exercised():
    """2026-09-07 is Labor Day and the venue SAID closed. Without it, "closed" and "never described"
    could collapse into one and the refusal tests below would prove nothing."""
    assert parse_sessions(_FRESH, "US/Eastern")[date(2026, 9, 7)] is None


# ---------------------------------------------------------------------------------------------
# DEFECT 2 — the first instrument wins, and it was the stale one.
# ---------------------------------------------------------------------------------------------

def test_a_STALE_instrument_does_not_hide_a_FRESH_one_in_the_same_cache():
    """Both windows were in staging2's cache. The calendar answered from the stale one and refused a
    day the cache could describe."""
    td = VenueCalendar(lambda: _cache([_STALE_INFO, _FRESH_INFO])).day(date(2026, 9, 9))
    assert isinstance(td, TradingDay), (
        "the calendar refused a day the cache could describe — it read one instrument and stopped"
    )
    assert (td.open_at.hour, td.open_at.minute) == (9, 30)


def test_the_ANSWER_DOES_NOT_DEPEND_ON_ITERATION_ORDER():
    """AIMED AT THE CLASS, not the instance. "First instrument wins" is a verdict that changes when
    the cache is rebuilt in a different order — same stack, same data, different answer, and nothing
    to tell you which run you got. Whatever the resolution rule becomes, the orderings must agree."""
    a = VenueCalendar(lambda: _cache([_STALE_INFO, _FRESH_INFO]))
    b = VenueCalendar(lambda: _cache([_FRESH_INFO, _STALE_INFO]))
    for d in (date(2026, 9, 3), date(2026, 9, 7), date(2026, 9, 9)):
        assert (a.day(d) is None) == (b.day(d) is None), f"orderings disagree about {d}"
        assert a.is_trading_day(d) == b.is_trading_day(d), f"orderings disagree about {d}"


# ---------------------------------------------------------------------------------------------
# DEFECT 1 — memoised once, for the life of the process, against a value that rolls.
# ---------------------------------------------------------------------------------------------

def test_the_window_is_RE_READ_when_the_venue_ADVANCES_it():
    """THE ONE THAT COST THREE DAYS. staging2's engine answered from a window ending 09-04 while the
    venue had long since moved to 09-08..09-10 — for the whole life of the process, with the correct
    answer one re-read away."""
    box = {"info": _STALE_INFO}
    cal = VenueCalendar(lambda: _mutable_cache(box))
    assert isinstance(cal.day(date(2026, 9, 3)), TradingDay)   # warm it on the stale window
    box["info"] = _FRESH_INFO                                  # the venue rolls forward
    assert isinstance(cal.day(date(2026, 9, 9)), TradingDay), (
        "the calendar kept answering from a window the venue has moved past — it memoised a rolling "
        "value for the life of the process"
    )


def test_next_fire_ALSO_sees_the_advanced_window():
    """THE SEAM, NOT THE UNIT. `next_fire` is what momentum/QC27/QC345 actually call when they arm,
    and it reaches `_load` through `day()`. A fix proven only through `day()` leaves the arming path
    — the one that was dead on staging2 — unproven."""
    box = {"info": _STALE_INFO}
    cal = VenueCalendar(lambda: _mutable_cache(box))
    with pytest.raises(OutsideCalendarWindow):
        cal.next_fire(datetime(2026, 9, 8, 8, 0, tzinfo=_ET), offset_minutes=5)
    box["info"] = _FRESH_INFO
    session, fire = cal.next_fire(datetime(2026, 9, 8, 8, 0, tzinfo=_ET), offset_minutes=5)
    assert session == date(2026, 9, 8)
    assert (fire.hour, fire.minute) == (9, 35)


def test_trading_minutes_between_ALSO_sees_the_advanced_window():
    """The third caller. `minutes_open` walks day by day through `self.day`, so it inherits whatever
    `_load` decided — and a stale window makes it raise across a span the venue has described."""
    box = {"info": _STALE_INFO}
    cal = VenueCalendar(lambda: _mutable_cache(box))
    start = int(datetime(2026, 9, 9, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1e9)
    end = int(datetime(2026, 9, 9, 18, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1e9)
    with pytest.raises(OutsideCalendarWindow):
        cal.trading_minutes_between(start, end)
    box["info"] = _FRESH_INFO
    assert cal.trading_minutes_between(start, end) > 0


# ---------------------------------------------------------------------------------------------
# THE FIX MUST NOT TURN THE REFUSAL INTO A GUESS.
# ---------------------------------------------------------------------------------------------

def test_a_day_NO_instrument_describes_still_RAISES():
    """Three states, and the third survives. Re-reading must not become "keep looking until something
    answers yes" — a day nobody described is still not a trading day."""
    cal = VenueCalendar(lambda: _cache([_STALE_INFO, _FRESH_INFO]))
    with pytest.raises(OutsideCalendarWindow):
        cal.day(date(2026, 11, 26))


def test_the_refusal_NAMES_THE_WIDEST_window_the_cache_could_describe():
    """The message is what an operator reads when a lane will not arm. Naming one instrument's slice
    while another described more days sends them to look for a missing window that is already there —
    which is exactly the wrong-diagnosis half of the staging2 incident."""
    cal = VenueCalendar(lambda: _cache([_STALE_INFO, _FRESH_INFO]))
    with pytest.raises(OutsideCalendarWindow) as e:
        cal.day(date(2026, 11, 26))
    msg = str(e.value)
    assert "2026-11-26" in msg, "the refusal does not name the day it was asked about"
    assert "2026-09-02" in msg and "2026-09-10" in msg, (
        f"the refusal names a narrower window than the cache could describe: {msg}"
    )


def test_a_stated_CLOSED_stays_CLOSED_and_is_never_re_read_into_an_absence():
    """A day the venue called CLOSED is a FACT, and the least obvious way to break this fix is to let
    a re-read downgrade it. 2026-09-07 must answer None from either ordering, and must not raise."""
    for infos in ([_STALE_INFO, _FRESH_INFO], [_FRESH_INFO, _STALE_INFO]):
        assert VenueCalendar(lambda: _cache(infos)).day(date(2026, 9, 7)) is None


def test_an_EMPTY_cache_still_refuses_rather_than_reporting_closed():
    """Unchanged behaviour, pinned here because the fix touches the memoisation guard that produces
    it. Before connect the calendar knows nothing, and nothing is not "closed"."""
    cal = VenueCalendar(lambda: _cache([]))
    with pytest.raises(OutsideCalendarWindow):
        cal.day(date(2026, 9, 9))


def test_when_TWO_instruments_describe_ONE_day_DIFFERENTLY_the_FRESHER_window_wins():
    """THE CONFLICT RULE, PINNED — otherwise it is a mechanism nobody can see working.

    The union has to choose when two instruments describe the same date and disagree. Merging
    oldest-window-first makes the newer description overwrite the older, which is order-independent
    because the sort key comes from the DATA. Without an assertion here the rule is computed and
    discarded: reversing the merge would return a byte-identical answer for every other test in this
    file, because every other fixture pair is disjoint.

    The overlap is deliberate — 2026-09-08 with a half-day close in the older description and the
    venue's real 16:00 in the newer, which is the shape of a session the venue later corrects.
    """
    older = {"liquidHours": "20260904:0930-20260904:1600;20260908:0930-20260908:1300",
             "timeZoneId": "US/Eastern"}
    newer = {"liquidHours": "20260908:0930-20260908:1600;20260909:0930-20260909:1600",
             "timeZoneId": "US/Eastern"}
    assert date(2026, 9, 8) in parse_sessions(older["liquidHours"], "US/Eastern"), (
        "fixture: the older window must actually describe the contested day"
    )
    for infos in ([older, newer], [newer, older]):
        td = VenueCalendar(lambda: _cache(infos)).day(date(2026, 9, 8))
        assert (td.close_at.hour, td.close_at.minute) == (16, 0), (
            "the stale description of 2026-09-08 won — the merge is ordered by the cache, not by "
            "which window the venue described more recently"
        )


def test_an_instrument_with_NO_hours_does_not_stop_the_scan():
    """Alpaca-shaped instruments carry no `liquidHours` at all, and on a mixed cache they must be
    skipped rather than ending the search — the same "read one and stop" shape as defect 2."""
    naked = {"timeZoneId": "US/Eastern"}
    cal = VenueCalendar(lambda: _cache([naked, {}, _FRESH_INFO]))
    assert isinstance(cal.day(date(2026, 9, 9)), TradingDay)
