"""A lane that never runs at all produces no journal rows, so nothing reports it (#378).

`judge_slot` is explicit about the hole in its own docstring: "A slot that simply did not run is the
common case and is silent." That was the right call for a predicate folding rows it can see — you
cannot judge a slot from rows that do not exist. It leaves the platform blind to the worst outcome:

    decided and did nothing   DECIDED, NEVER ATTEMPTED   (caught, #476)
    tried and lost the order  ATTEMPTED, DID NOT LAND    (caught, #476)
    errored                   ERRORS                     (caught, #476)
    NEVER RAN                 nothing. Silence.          <-- this

BCTROT missed BOTH decision slots on 2026-08-19 and journalled nothing, which is the whole of #378 and
is still open. It went unnoticed for three days (#349).

MONDAY IS EXACTLY THIS SHAPE. QC345-003 is due at open+300m and TECHIVOL-005 at open+315m — both
declared in settings, both lanes that have never successfully traded, and if either simply does not
fire, every surface stays green. "No alarms" would mean "nothing ran", and those are the two readings
an operator must never have to guess between.

The expected slots are DECLARED, so the absence is checkable: `QC345-003_SLOTS: ['open+300m']`,
`TECHIVOL-005_SLOTS: ['open+315m']`.
"""

from __future__ import annotations

import pytest

from api.slot_outcome import RTH_MINUTES, never_ran, slot_due_minutes


@pytest.mark.parametrize("slot, due", [
    ("open+5m", 5),
    ("open+300m", 300),                 # QC345-003, 14:30 ET
    ("open+315m", 315),                 # TECHIVOL-005, 14:45 ET
    ("close-20m", RTH_MINUTES - 20),    # BCTROT's second slot — the one it missed
    ("open+0m", 0),
])
def test_a_slot_name_resolves_to_minutes_after_the_open(slot, due) -> None:
    """`close-20m` is measured from the close, not the open — a naive parse would read it as 20 and
    alarm at 09:50 about a slot that has not happened yet, every single day."""
    assert slot_due_minutes(slot) == due


@pytest.mark.parametrize("slot", ["", "later", "open+", "open+xm", "close-", None])
def test_an_UNPARSEABLE_slot_is_never_alarmed_on(slot) -> None:
    """None, not 0. A slot name this cannot read is not a slot that was due at the open — guessing 0
    would fire on every poll for a name nobody recognises, which is how an alarm gets muted."""
    assert slot_due_minutes(slot) is None


def test_the_live_monday_case() -> None:
    """QC345 due at open+300m, TECHIVOL at open+315m, and neither produced a row by 16:00 ET."""
    expected = {"QC345-003": ["open+300m"], "TECHIVOL-005": ["open+315m"]}
    assert never_ran(expected, seen=set(), minutes_since_open=RTH_MINUTES) == [
        ("QC345-003", "open+300m"), ("TECHIVOL-005", "open+315m")]


def test_a_lane_that_DID_run_is_silent() -> None:
    """Or the alarm fires on the working path and is switched off before the day it matters."""
    expected = {"QC345-003": ["open+300m"], "TECHIVOL-005": ["open+315m"]}
    seen = {("QC345-003", "open+300m"), ("TECHIVOL-005", "open+315m")}
    assert never_ran(expected, seen=seen, minutes_since_open=RTH_MINUTES) == []


def test_a_slot_whose_TIME_HAS_NOT_COME_is_not_yet_missing() -> None:
    """The single most important property. QC345 is due at 14:30; alarming at 09:35 would fire every
    morning about a lane that is working perfectly, and by Monday nobody would read it."""
    expected = {"QC345-003": ["open+300m"]}
    assert never_ran(expected, seen=set(), minutes_since_open=5) == []
    # Not at the due minute either — a session takes time to write its first row.
    assert never_ran(expected, seen=set(), minutes_since_open=300) == []
    assert never_ran(expected, seen=set(), minutes_since_open=300 + 15) == [("QC345-003", "open+300m")]


def test_nothing_is_missing_when_the_MARKET_NEVER_OPENED() -> None:
    """A weekend or a holiday. `None` means "no session today", and a lane that did not run on a day
    it was never due is not a finding — this test exists because it is Sunday as I write it and the
    check must be silent right now."""
    assert never_ran({"QC345-003": ["open+300m"]}, seen=set(), minutes_since_open=None) == []


def test_an_EMPTY_slot_list_declares_nothing_and_alarms_on_nothing() -> None:
    """BCTROT-004_SLOTS and MOMENTUM-002_SLOTS are both `[]` live — those lanes take their slots from
    their own config, not from settings. Treating an empty declaration as "due at the open" would
    alarm on two working lanes every day."""
    assert never_ran({"BCTROT-004": [], "MOMENTUM-002": []}, seen=set(),
                     minutes_since_open=RTH_MINUTES) == []


# ==================================================================================================
# THE WIRING. Everything above is pure. Seven mechanisms in this session were built, tested, and
# driven by nothing — the pure half is the half that always gets written.
# ==================================================================================================

import asyncio
import inspect
from types import SimpleNamespace


def _svc(clock):
    from api import alerts

    s = alerts.AlertsService.__new__(alerts.AlertsService)

    async def _get_clock():
        if isinstance(clock, Exception):
            raise clock
        return clock

    s._http = None if clock is None else SimpleNamespace(get_clock=_get_clock)
    return s


def test_the_check_HAS_a_production_caller_inside_the_enabled_gate() -> None:
    from api import alerts

    run_src = inspect.getsource(alerts.AlertsService.run)
    assert "_announce_never_ran" in run_src, "nothing drives the never-ran check"
    assert run_src.index("_announce_never_ran") > run_src.index("if self._enabled():"), (
        "the check runs outside the enabled gate — it queries Postgres and the venue clock"
    )


def test_a_FAILING_check_does_not_cost_the_alerts_that_already_work() -> None:
    """It runs after health, the slot scan and before nothing — but an unhandled raise still reaches
    the loop's outer except and skips `_digests` for that poll."""
    from api import alerts

    svc = alerts.AlertsService.__new__(alerts.AlertsService)
    svc._http = None                       # settings resolve will raise inside; must be swallowed
    asyncio.run(svc._announce_never_ran())  # must not raise


def test_no_venue_clock_means_SILENT_not_ASSUMED_OPEN() -> None:
    """Without the clock there is no holiday-aware answer. Guessing "open" alarms on every lane on
    Thanksgiving; guessing "closed" is silent, which is the direction that cannot cry wolf."""
    assert asyncio.run(_svc(None)._minutes_since_open()) is None
    assert asyncio.run(_svc(RuntimeError("clock down"))._minutes_since_open()) is None


def test_before_the_open_nothing_is_due_yet() -> None:
    """Closed, and today's session has not happened: `next_open` is still today."""
    got = asyncio.run(_svc({"is_open": False, "timestamp": "2026-08-24T07:00:00-04:00",
                            "next_open": "2026-08-24T09:30:00-04:00"})._minutes_since_open())
    assert got is None


def test_after_the_close_the_whole_session_counts_as_elapsed() -> None:
    """The most valuable moment to report a missed slot is after the close — that is when "it never
    ran today" stops being "it has not run YET"."""
    from api.slot_outcome import RTH_MINUTES

    got = asyncio.run(_svc_with_calendar(
        {"is_open": False, "timestamp": "2026-08-24T17:00:00-04:00",
         "next_open": "2026-08-25T09:30:00-04:00"},
        _CAL_WEEK)._minutes_since_open())
    assert got == RTH_MINUTES


def test_during_the_session_it_measures_from_0930_ET() -> None:
    """14:45 ET is open+315m — TECHIVOL-005's slot exactly."""
    got = asyncio.run(_svc({"is_open": True,
                            "timestamp": "2026-08-24T14:45:00-04:00"})._minutes_since_open())
    assert got == 315


# --- #645: a closed WEEKEND day is not an elapsed session -------------------------------------
#
# `ts[:10] != nxt[:10]` is true all Saturday and Sunday (next_open is Monday), so every closed
# non-trading day counted as a FULL session and NEVER RAN paged critically every weekend. The
# instantaneous clock cannot tell "Friday after the close" from "Saturday" — both read
# closed-with-next_open-Monday — so the discriminator has to be the venue's CALENDAR: did the
# clock's own date have a session at all?

# The venue calendar for the week of 2026-08-24, as Alpaca's /v2/calendar returns it: rows exist
# ONLY for trading days. Saturday and Sunday are not rows that say "closed" — they are absent.
_CAL_WEEK = [
    {"date": "2026-08-24", "open": "09:30", "close": "16:00"},
    {"date": "2026-08-25", "open": "09:30", "close": "16:00"},
    {"date": "2026-08-26", "open": "09:30", "close": "16:00"},
    {"date": "2026-08-27", "open": "09:30", "close": "16:00"},
    {"date": "2026-08-28", "open": "09:30", "close": "16:00"},
    {"date": "2026-08-31", "open": "09:30", "close": "16:00"},
]


def _svc_with_calendar(clock, calendar):
    s = _svc(clock)

    async def _get_calendar(start: str, end: str):
        if isinstance(calendar, Exception):
            raise calendar
        return [r for r in calendar if start <= r["date"] <= end]

    s._http.get_calendar = _get_calendar
    return s


def test_the_fixture_weekend_IS_a_weekend_the_venue_confirms_closed() -> None:
    """Fixture-property first: if the fixture cannot express 'Saturday with no session', the tests
    below are vacuous. 2026-08-29 must be a real Saturday AND absent from the venue's own calendar
    while both its trading neighbours (Friday, Monday) are present."""
    from datetime import date

    days = {r["date"] for r in _CAL_WEEK}
    assert date(2026, 8, 29).weekday() == 5, "the fixture date is not a Saturday"
    assert "2026-08-28" in days and "2026-08-31" in days, "the surrounding trading days are missing"
    assert "2026-08-29" not in days and "2026-08-30" not in days, (
        "the fixture calendar claims the weekend traded")


def test_a_SATURDAY_is_not_an_elapsed_session() -> None:
    """The #645 shape exactly: closed, timestamp Saturday, next_open Monday. Before the fix this
    returned RTH_MINUTES — a full session assumed on a day that had none — and NEVER RAN paged
    critically for every declared lane, every weekend, until the channel got muted."""
    got = asyncio.run(_svc_with_calendar(
        {"is_open": False, "timestamp": "2026-08-29T12:00:00-04:00",
         "next_open": "2026-08-31T09:30:00-04:00"},
        _CAL_WEEK)._minutes_since_open())
    assert got is None, f"a Saturday counted as {got} elapsed session minutes"


def test_a_TRADING_day_after_the_close_STILL_counts_as_elapsed() -> None:
    """The other direction: the weekend fix must not disarm the detector. Friday 17:00 ET is a
    trading day whose close has passed — a Monday-shaped never-ran must still page."""
    from api.slot_outcome import RTH_MINUTES

    got = asyncio.run(_svc_with_calendar(
        {"is_open": False, "timestamp": "2026-08-28T17:00:00-04:00",
         "next_open": "2026-08-31T09:30:00-04:00"},
        _CAL_WEEK)._minutes_since_open())
    assert got == RTH_MINUTES


def test_a_calendar_that_answers_NOTHING_refuses_rather_than_guessing() -> None:
    """Absence must not be readable as permission — in either direction. 'Closed' is inferred from
    a day being absent from the venue's enumeration, so an EMPTY enumeration (a broken read, a
    malformed response) is indistinguishable from 'every day is closed' and would silently disarm
    the detector on a real Monday. Any window wide enough around a US-equity date contains trading
    days; zero rows is a broken answer and must RAISE into the broken-check channel."""
    svc = _svc_with_calendar(
        {"is_open": False, "timestamp": "2026-08-29T12:00:00-04:00",
         "next_open": "2026-08-31T09:30:00-04:00"},
        [])
    with pytest.raises(Exception):
        asyncio.run(svc._minutes_since_open())


def test_a_calendar_read_FAILURE_raises_into_the_broken_check_channel() -> None:
    """A failed read is 'never told us', not 'closed': swallowing it to None would make a weekend
    of venue 503s look identical to a healthy quiet Saturday forever. The raise is caught by
    `_announce_never_ran`, counted by `_check_failed`, and self-reports after BROKEN_CHECK_POLLS."""
    svc = _svc_with_calendar(
        {"is_open": False, "timestamp": "2026-08-29T12:00:00-04:00",
         "next_open": "2026-08-31T09:30:00-04:00"},
        RuntimeError("calendar down"))
    with pytest.raises(Exception):
        asyncio.run(svc._minutes_since_open())
