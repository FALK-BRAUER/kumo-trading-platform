"""Wide-stop-on-open (#425). The numbers pinned here are measured, not chosen — see the module docstring."""

from __future__ import annotations

import datetime as dt

import pytest

from api.open_window_stop import RESTORE_AT, SESSION_OPEN, WIDE_MULTIPLE, open_window_stop

#: WHD on 2026-08-21, the position that survived the whole day: bought 28 @ 70.57 with a protective stop
#: that would have rested a leash below it. Real numbers so the arithmetic below is checkable against a
#: real position rather than a round one chosen to make the maths tidy.
ENTRY = 70.57
STOP = 67.14          # the PROT-SELL trigger actually observed on WHD.XNYS, 2026-08-21
LEASH = ENTRY - STOP  # 3.43


def at(h: int, m: int) -> dt.datetime:
    return dt.datetime(2026, 8, 21, h, m)


def test_the_fixture_is_a_real_protective_stop_below_a_real_entry():
    # The fixture's own property first. If the stop were not below the entry every assertion below would
    # be about a leash that does not exist, and `open_window_stop` would correctly refuse them all.
    assert STOP < ENTRY
    assert LEASH == pytest.approx(3.43, abs=0.001)


def test_inside_the_opening_window_the_stop_sits_a_MULTIPLE_of_the_leash_below_entry():
    d = open_window_stop(entry_price=ENTRY, original_stop=STOP, now_et=at(9, 31))
    assert d is not None and d.widened
    # 70.57 - (3.43 * 3) = 60.28. Deliberately spelled out: a reader who changes WIDE_MULTIPLE should see
    # this number move and have to think about why.
    assert d.price == pytest.approx(70.57 - 3.43 * 3, abs=1e-9)
    assert d.price < STOP, "the widened stop must sit BELOW the original or it is not a widening"


def test_after_the_restore_time_the_ORIGINAL_stop_rests_again():
    d = open_window_stop(entry_price=ENTRY, original_stop=STOP, now_et=at(10, 0))
    assert d is not None and not d.widened
    assert d.price == STOP, "10:00 is the RESTORE boundary — the window is [09:30, 10:00)"


def test_before_the_open_the_original_stop_rests():
    # Pre-market has no auction to be swept by. Widening there would loosen protection through hours
    # nobody measured, for no reason the evidence supports.
    d = open_window_stop(entry_price=ENTRY, original_stop=STOP, now_et=at(8, 45))
    assert d is not None and not d.widened and d.price == STOP


def test_the_window_is_half_open_at_BOTH_ends():
    # Boundary behaviour stated explicitly because an off-by-one here is invisible in production: it
    # would widen or restore one tick early and nothing would look wrong.
    assert open_window_stop(entry_price=ENTRY, original_stop=STOP,
                            now_et=dt.datetime.combine(dt.date(2026, 8, 21), SESSION_OPEN)).widened
    assert not open_window_stop(entry_price=ENTRY, original_stop=STOP,
                                now_et=dt.datetime.combine(dt.date(2026, 8, 21), RESTORE_AT)).widened


def test_a_stop_AT_OR_ABOVE_entry_is_refused_rather_than_widened():
    """THE SILENCING DIRECTION, and the one that must fail closed.

    A stop at or above entry is not a protective stop for a long. Widening it would push a nonsense
    level further away and hand back a number the caller would rest at the venue. None says "decide
    nothing", which the caller can act on; a float cannot be distinguished from a real answer.
    """
    assert open_window_stop(entry_price=70.0, original_stop=70.0, now_et=at(9, 31)) is None
    assert open_window_stop(entry_price=70.0, original_stop=71.0, now_et=at(9, 31)) is None


def test_a_multiple_below_one_is_refused_because_it_TIGHTENS_into_the_auction():
    # k < 1 would move the stop UP, into the opening print — manufacturing the exact sweep this exists
    # to avoid. Refused rather than clamped: a caller asking for it has a bug worth surfacing.
    assert open_window_stop(entry_price=ENTRY, original_stop=STOP,
                            now_et=at(9, 31), wide_multiple=0.5) is None


def test_the_measured_default_is_3x_and_the_restore_is_10_00():
    """Pins the two constants the measurement chose.

    3.0x scored +$2,894 against suspending the stop entirely at +$2,901 — within $7 — while still
    electing 4 times in 11. The curve is flat from 1.0x to suspension, so this constant is chosen for
    RETAINED PROTECTION, not for P&L, and a future edit that treats it as a free parameter should have
    to read that.
    """
    assert WIDE_MULTIPLE == 3.0
    assert RESTORE_AT == dt.time(10, 0)
