"""Which stored day a window SUBTRACTS FROM (#699 read path).

A per-lane window delta is `unrealized_now − unrealized_at_the_window's_start`. The tile already
computes the "now" half per lane. This decides the other date, and it must decide it ONCE — a period
whose boundary is computed in two places is how the realized figure and the unrealized figure end up
describing different weeks while sitting in the same cell.

THE VOCABULARY IS `realized_broker.PERIOD_DAYS`, not a copy of it. That map is already the one the
selector offers and the sweep uses; a second list is a second definition of "1W".
"""

from __future__ import annotations

from datetime import date

import pytest

from api.eod_window import window_base_date


def test_it_uses_the_SAME_period_vocabulary_OBJECT_the_realized_sweep_uses():
    """MUTATION SURVIVOR, and the reason is the rule: replacing the import with a local copy of the
    same dict passed every assertion, because the two AGREED. Agreement is exactly the condition
    under which a severed connection is invisible.

    So this asserts IDENTITY, which a copy cannot fake. The contents test below still matters, but it
    can only see a fork after the fork has already diverged — by which time the selector offers a
    period whose base is an em dash while the realized half of the same cell shows a number.
    """
    from api import eod_window
    from api.realized_broker import PERIOD_DAYS

    assert eod_window.PERIOD_DAYS is PERIOD_DAYS, (
        "eod_window holds its own copy of the period vocabulary. Two definitions of '1W' cannot be "
        "kept equal by anyone remembering to; import the one the sweep uses."
    )
    for period in PERIOD_DAYS:
        window_base_date(period, today=date(2026, 8, 30))


def test_a_window_subtracts_from_the_day_it_STARTED():
    """1W is seven days back. The base is the close BEFORE the window, which is what makes
    `now − base` the move DURING it."""
    assert window_base_date("1W", today=date(2026, 8, 30)) == "2026-08-23"
    assert window_base_date("1M", today=date(2026, 8, 30)) == "2026-07-31"
    assert window_base_date("3M", today=date(2026, 8, 30)) == "2026-06-01"


def test_1D_subtracts_from_YESTERDAY_not_from_today():
    """A base of TODAY would make every 1D delta exactly zero — the window would subtract from
    itself. `PERIOD_DAYS["1D"] == 0` means "since the last close", and the last close is yesterday's
    row."""
    assert window_base_date("1D", today=date(2026, 8, 30)) == "2026-08-29"


def test_ALL_has_NO_base_date_and_says_so_rather_than_picking_inception():
    """`all` is unbounded, so there is no earlier close to subtract from — the honest answer is that
    this window has no base, not a guessed one. A caller must render an em dash rather than compute
    against whatever the oldest row happens to be, which would silently mean 'since we started
    capturing' and drift every day the table grows."""
    assert window_base_date("all", today=date(2026, 8, 30)) is None


def test_an_UNKNOWN_period_is_REFUSED_rather_than_defaulted():
    """A period nobody defined must not quietly become 1D. Refusing names it; defaulting produces a
    number under the wrong label."""
    with pytest.raises(ValueError, match="unknown period"):
        window_base_date("6M", today=date(2026, 8, 30))


def test_the_base_is_a_CALENDAR_date_not_a_trading_day():
    """DELIBERATE, AND A LIMIT WORTH STATING. Seven calendar days back may be a weekend or a holiday,
    which has no close row — the caller then finds nothing and renders UNKNOWN rather than silently
    reaching further back for the nearest row it can find.

    Reaching back would make "1W" mean a different span depending on where the holidays fell, and a
    window whose length depends on the calendar is not the window the label claims. The alternative —
    resolving through the venue calendar — is a real option, and it belongs with the capture that
    knows the sessions, not here. Stated so the em dash is understood as correct rather than broken.
    """
    # A Sunday: 1W back from 2026-08-30 is 2026-08-23, also a Sunday, which has no close.
    assert window_base_date("1W", today=date(2026, 8, 30)) == "2026-08-23"
