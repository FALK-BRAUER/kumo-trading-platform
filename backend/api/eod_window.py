"""Which stored day a window subtracts from (#699 read path).

A per-lane window delta is `unrealized_now − unrealized_at_the_window's_start`. The tile already
computes the "now" half per lane; this decides the other date.

IT DECIDES IT ONCE. A period whose boundary is computed in two places is how the realized figure and
the unrealized figure end up describing different weeks inside one cell — and that cell is the thing
this whole chain exists to make trustworthy.

THE VOCABULARY IS `realized_broker.PERIOD_DAYS`, imported rather than copied. That map is already
what the selector offers and what the sweep uses; a second list is a second definition of "1W", and
the first divergence would be silent.
"""

from __future__ import annotations

from datetime import date, timedelta

from api.realized_broker import PERIOD_DAYS


def window_base_date(period: str, *, today: date) -> str | None:
    """The session whose close this window subtracts from, or None when the window has no base.

    `all` IS UNBOUNDED, so there is no earlier close and the honest answer is None. Picking the
    oldest row instead would silently mean "since we started capturing" and would drift every day
    the table grows — a number whose meaning changes under a fixed label.

    AN UNKNOWN PERIOD IS REFUSED. Defaulting it to 1D produces a real number under the wrong label,
    which is worse than an error, because nothing downstream can tell.

    A CALENDAR DATE, NOT A TRADING DAY — deliberate, and a stated limit. Seven days back may be a
    weekend or a holiday with no close row, and the caller then renders UNKNOWN rather than reaching
    further back for the nearest row it can find. Reaching back would make "1W" span a different
    length depending on where the holidays fell, and a window whose length depends on the calendar is
    not the window its label claims. Resolving through the venue calendar is a real alternative and
    belongs with the capture that knows the sessions, not here.
    """
    if period not in PERIOD_DAYS:
        raise ValueError(
            f"unknown period {period!r}; the vocabulary is {sorted(PERIOD_DAYS)} and it comes from "
            f"`realized_broker.PERIOD_DAYS` so the selector, the sweep and this cannot disagree"
        )
    days = PERIOD_DAYS[period]
    if days is None:
        return None
    # `1D` is 0 days, meaning "since the last close" — and the last close is YESTERDAY's row. A base
    # of today would make every 1D delta exactly zero, the window subtracting from itself.
    return (today - timedelta(days=days or 1)).isoformat()
