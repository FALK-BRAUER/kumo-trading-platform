"""When the book gets captured, and the three states of "not now" (#734 step 4).

THE SCHEDULE MUST COME FROM THE VENUE. A fixed 16:00 ET cron is wrong on every half day, and the
operator's clock is SGT — a locally-stamped date puts a 04:00 SGT close on the wrong calendar day.
`VenueCalendar` already answers both, and already refuses a day it was never told about.

THE REFUSAL IS THE POINT. `next_fire` looks 14 days ahead while IB's `liquidHours` is a rolling ~6-day
window, so leaving that window is NORMAL, not exceptional. A scheduler that read "unknown day" as
"not a trading day" would skip every capture on a stale calendar and never say so — absence readable
as permission, and the failure would be invisible for exactly as long as nobody looked at the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from api.eod_capture import CAPTURE_OFFSET_MINUTES, capture_enabled, should_capture

ET = timezone(timedelta(hours=-4))          # the venue's own offset in the fixture window


@dataclass
class _Day:
    """Shaped from `kumo_strategies`' `TradingDay`, which `VenueCalendar.day()` returns:
    `session`, `open_at`, `close_at`."""

    session: str
    open_at: datetime
    close_at: datetime


class _Calendar:
    """`day(d)` returns a TradingDay, None for a non-trading day, or RAISES for a day the venue never
    described. All three are real and they mean different things — a double that could not produce
    the third could not test the state this module exists to handle."""

    def __init__(self, days: dict, unknown=()):
        self._days = days
        self._unknown = set(unknown)

    def day(self, d):
        if d in self._unknown:
            raise RuntimeError(f"the venue has not described {d}")
        return self._days.get(d)


_CLOSE = datetime(2026, 8, 28, 16, 0, tzinfo=ET)
_TRADING = _Calendar({_CLOSE.date(): _Day("2026-08-28", _CLOSE.replace(hour=9, minute=30), _CLOSE)})


# ==================================================================================================
# THE GATE — off unless someone says otherwise
# ==================================================================================================
def test_the_capture_is_OFF_unless_explicitly_enabled():
    """Every new automation gate in this repo defaults False. A capture job that began writing the
    moment it deployed would be a behaviour change nobody asked for."""
    assert capture_enabled({}) is False
    assert capture_enabled({"KUMO_EOD_CAPTURE": ""}) is False
    assert capture_enabled({"KUMO_EOD_CAPTURE": "maybe"}) is False
    assert capture_enabled({"KUMO_EOD_CAPTURE": "true"}) is True
    assert capture_enabled({"KUMO_EOD_CAPTURE": "1"}) is True


# ==================================================================================================
# THE THIRD STATE
# ==================================================================================================
def test_a_day_the_venue_never_DESCRIBED_is_REFUSED_and_not_treated_as_a_holiday():
    """The whole reason this returns a decision object rather than a bool.

    `next_fire` looks 14 days ahead while IB's `liquidHours` is a rolling ~6-day window, so leaving
    the described window is NORMAL. Reading it as "not a trading day" would skip every capture on a
    stale calendar, silently, for as long as nobody checked the table."""
    cal = _Calendar({}, unknown={_CLOSE.date()})
    d = should_capture(_CLOSE + timedelta(hours=1), cal)
    assert d.action == "refuse"
    assert d.should_fire is False
    assert "has not described" in d.reason


def test_a_genuine_NON_trading_day_is_a_WAIT_and_says_which_it_is():
    """Distinguishable from the refusal above. A holiday is a known answer; an undescribed day is
    not, and collapsing them would hide a broken calendar behind a plausible one."""
    d = should_capture(_CLOSE, _Calendar({_CLOSE.date(): None}))
    assert d.action == "wait"
    assert "not a trading day" in d.reason


def test_a_trading_day_with_NO_CLOSE_TIME_is_REFUSED_rather_than_guessed():
    """A calendar row that exists but cannot say when the session ends is not a licence to pick
    16:00. Half days are exactly the case where a guess is wrong."""
    day = _Day("2026-08-28", _CLOSE.replace(hour=9, minute=30), None)
    d = should_capture(_CLOSE + timedelta(hours=1), _Calendar({_CLOSE.date(): day}))
    assert d.action == "refuse"
    assert "no close time" in d.reason


def test_a_MIXED_awareness_comparison_is_REFUSED_rather_than_assumed():
    """Comparing an aware and a naive datetime raises in Python; catching that and guessing a zone
    would be a whole-day error on a stack whose operator is in SGT and whose venue is in ET.

    MIXED is the case that must refuse — review caught the first version refusing BOTH-naive too,
    with the message "one side is timezone-naive", which is simply false for that case. A wrong
    reason attached to a conservative outcome is how the next reader fixes the wrong thing."""
    naive_close = datetime(2026, 8, 28, 16, 0)
    day = _Day("2026-08-28", naive_close, naive_close)
    d = should_capture(_CLOSE + timedelta(hours=1), _Calendar({_CLOSE.date(): day}))
    assert d.action == "refuse"
    assert "now is aware" in d.reason and "close is naive" in d.reason


def test_a_BOTH_NAIVE_comparison_is_COMPARABLE_and_fires_normally():
    """The half the first version got wrong. Two naive datetimes compare fine; refusing them was a
    conservative outcome reached for a false reason, and it would have made an all-naive calendar
    silently never capture."""
    naive_close = datetime(2026, 8, 28, 16, 0)
    day = _Day("2026-08-28", naive_close, naive_close)
    d = should_capture(naive_close + timedelta(minutes=CAPTURE_OFFSET_MINUTES),
                       _Calendar({naive_close.date(): day}))
    assert d.should_fire, f"both-naive is comparable; got {d.action}: {d.reason}"


# ==================================================================================================
# THE TIMING
# ==================================================================================================
def test_it_does_not_fire_AT_the_close_but_after_the_book_has_settled():
    """In-flight fills make engine-vs-venue mismatches normal, and continuous reconciliation runs on
    5s/10s loops. Capturing into that produces rows whose disagreement means nothing — and an alarm
    that cries wolf gets muted, which is the notifier-death pattern already paid for here."""
    assert should_capture(_CLOSE, _TRADING).action == "wait"
    assert should_capture(_CLOSE + timedelta(minutes=CAPTURE_OFFSET_MINUTES - 1), _TRADING).action == "wait"


def test_it_FIRES_once_the_offset_has_passed_and_names_the_session():
    d = should_capture(_CLOSE + timedelta(minutes=CAPTURE_OFFSET_MINUTES), _TRADING)
    assert d.should_fire
    # The session date comes from the CALENDAR, never from the local clock — that is the whole
    # defence against an SGT operator stamping a 04:00 local date on an ET session.
    assert d.session_date == "2026-08-28"


def test_it_does_not_fire_TWICE_for_one_session():
    """The DATABASE is the real guard (`uq_eod_observation_base` refuses a duplicate base row); this
    is the cheap check in front of it. Belt and braces in that order — if this were the only guard,
    a process restart would re-capture."""
    now = _CLOSE + timedelta(minutes=CAPTURE_OFFSET_MINUTES + 5)
    assert should_capture(now, _TRADING).should_fire
    d = should_capture(now, _TRADING, already_captured={"2026-08-28"})
    assert d.action == "wait"
    assert "already captured" in d.reason


def test_the_fixture_can_express_every_state_it_claims_to_test():
    """FIXTURE PROPERTY. A calendar double that could only return a trading day would make three of
    the tests above pass without exercising anything — the refusal path is the one most likely to be
    wrong and the least likely to be noticed."""
    cal = _Calendar({_CLOSE.date(): None}, unknown={_CLOSE.date() + timedelta(days=1)})
    assert cal.day(_CLOSE.date()) is None                      # holiday
    with pytest.raises(RuntimeError):                          # undescribed
        cal.day(_CLOSE.date() + timedelta(days=1))
    assert _TRADING.day(_CLOSE.date()) is not None             # trading


def test_a_calendar_row_that_cannot_NAME_its_session_is_REFUSED_not_stamped_with_the_LOCAL_date():
    """F7. This was `getattr(day, "session", "") or local.date().isoformat()` — a fallback writing
    the OPERATOR's date onto the row, and this module's docstring names that exact hazard two
    paragraphs up. The operator is in SGT and the venue in ET, so a capture running after 12:00 SGT
    files an ET session under the FOLLOWING day, silently and forever.

    A calendar row that cannot name its own session is a broken calendar, not permission to guess."""
    day = _Day(session="", open_at=_CLOSE.replace(hour=9, minute=30), close_at=_CLOSE)
    d = should_capture(_CLOSE + timedelta(hours=1), _Calendar({_CLOSE.date(): day}))
    assert d.action == "refuse"
    assert d.session_date is None
    assert "does not name its session" in d.reason
