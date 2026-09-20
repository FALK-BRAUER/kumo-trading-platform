"""BEDTIME TRIM (#425) — bank half of a green position before the operator is asleep.

WHY IT EXISTS, from the fill distribution on the operator's real IBKR book (90 fills, 30 days, ET/SGT):

    ET     SGT    BUYS            SELLS
    09:xx  21:xx    4 / $17,835    16 / $33,849
    10:xx  22:xx   28 / $57,059     8 / $24,018
    11:xx  23:xx    6 / $12,981    10 / $18,897
    12:xx  00:xx    1 / $ 2,290     8 / $12,711
    14:xx  02:xx    4 / $ 6,982     1 / $ 2,297
    15:xx  03:xx    0               2 / $ 1,987
    16:xx+ 04:xx+   0               0

ZERO FILLS AFTER 16:00 ET IN NINETY DAYS. The operator works from UTC+8: the US open is 21:30 local and the
close is 04:00 SGT. The last four hours of every session — including the closing hour, where the day's
move is settled — and every overnight gap run with nobody there. That is structural, not a discipline
problem, and no amount of "check your positions" fixes a timezone.

MEASURED: +$1,082 over 282 long round trips. 17 trades flipped negative->positive and ZERO flipped
positive->negative — the only variant tested all day with no downside flips, because banking half of a
gain cannot turn a winner into a loser.

These tests are written BEFORE the module exists.
"""

from __future__ import annotations

import datetime as dt

from api.bedtime_trim import TRIM_AT, TRIM_FRACTION, bedtime_trim

AVG = 100.0


def at(h: int, m: int) -> dt.datetime:
    return dt.datetime(2026, 8, 21, h, m)


def test_the_fixture_can_express_both_a_green_and_a_red_position():
    # The fixture's own property first: if every case here were green, "red is left alone" would pass
    # against a function that trimmed unconditionally.
    assert 105.0 > AVG and 95.0 < AVG


def test_a_GREEN_position_is_half_trimmed_at_the_bedtime_boundary():
    d = bedtime_trim(avg_px=AVG, last_px=105.0, quantity=30, now_et=at(15, 45), trimmed_today=False)
    assert d is not None
    assert d.quantity == 15, "half of 30"
    assert d.reason


def test_a_RED_position_is_LEFT_ALONE():
    """The whole point is banking a GAIN before an unattended stretch. Selling a loser here would be a
    different operator with a different justification and no measurement behind it."""
    assert bedtime_trim(avg_px=AVG, last_px=95.0, quantity=30, now_et=at(15, 45),
                        trimmed_today=False) is None


def test_a_position_EXACTLY_at_cost_is_left_alone():
    # Flat is not green. Trimming here pays the spread for nothing.
    assert bedtime_trim(avg_px=AVG, last_px=AVG, quantity=30, now_et=at(15, 45),
                        trimmed_today=False) is None


def test_nothing_happens_BEFORE_the_bedtime_boundary():
    assert bedtime_trim(avg_px=AVG, last_px=105.0, quantity=30, now_et=at(15, 44),
                        trimmed_today=False) is None


def test_it_still_fires_AFTER_the_boundary_because_a_missed_tick_must_not_lose_the_session():
    """The engine polls; a single tick can be missed by a restart, a slow reconciliation, a GC pause.
    An exact-equality window would silently skip the whole day — which is the shape that lost MOMENTUM's
    08-17 and 08-18 sessions entirely."""
    d = bedtime_trim(avg_px=AVG, last_px=105.0, quantity=30, now_et=at(15, 52), trimmed_today=False)
    assert d is not None and d.quantity == 15


def test_it_does_NOT_fire_after_the_close():
    # Past 16:00 there is no regular-hours liquidity to trim into, and a market order would queue to the
    # next open — which is the gap this exists to reduce exposure to, not to trade into.
    assert bedtime_trim(avg_px=AVG, last_px=105.0, quantity=30, now_et=at(16, 1),
                        trimmed_today=False) is None


def test_ONCE_PER_DAY_and_not_once_per_poll():
    """IDEMPOTENCE IS THE LOAD-BEARING PART. The caller polls; without this the position is halved on
    every tick between 15:45 and 16:00 and is fully liquidated by the close."""
    assert bedtime_trim(avg_px=AVG, last_px=105.0, quantity=30, now_et=at(15, 45),
                        trimmed_today=True) is None


def test_an_ODD_quantity_rounds_DOWN_and_never_to_zero_shares():
    # 1 share cannot be halved. Returning a 0-quantity order would be rejected by the venue and read as
    # a failure in the journal; returning None says "nothing to do" honestly.
    assert bedtime_trim(avg_px=AVG, last_px=105.0, quantity=1, now_et=at(15, 45),
                        trimmed_today=False) is None
    d = bedtime_trim(avg_px=AVG, last_px=105.0, quantity=3, now_et=at(15, 45), trimmed_today=False)
    assert d is not None and d.quantity == 1, "3 -> 1, rounding DOWN keeps a residual position"


def test_the_measured_constants_are_pinned():
    """+$1,082 with 17 flips up and 0 down was measured at HALF, at 15:45. A different fraction or a
    different time is a different experiment and has no number behind it."""
    assert TRIM_FRACTION == 0.5
    assert TRIM_AT == dt.time(15, 45)
