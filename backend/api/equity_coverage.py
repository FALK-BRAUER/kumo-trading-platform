"""Does an equity-curve series actually SPAN the window its label claims? (#653)

Measured on ibkr-paper-retired 2026-08-28: NET · 3M equalled NET · 1M to the cent across two captures,
because the account is ~2 weeks old and Alpaca clamps every longer window's base to inception. The
numbers were true — each IS the delta since inception — but the labels claimed spans the series
cannot cover, and two windows agreeing to the cent is exactly the condition under which nobody can
tell a clamped base from a quiet market (agreement is not connection).

Same rule the trend strip already applies (ui trend.ts, #612): coverage is decided by TIME against
the series' own span, with ONE BAR of slack so a session-boundary shortfall does not flip a
plainly-covered window to false. "all" is inception-to-date and covered by definition.
"""

from __future__ import annotations

#: Window length in calendar days per period key — the same vocabulary the curve publisher and the
#: UI selector share. None = inception-to-date.
WINDOW_DAYS: dict[str, int | None] = {"1D": 1, "1W": 7, "1M": 30, "3M": 90, "all": None}

_DAY_SECS = 86_400


def curve_coverage(period: str, point_ts: list[int]) -> dict:
    """{covered, covers_days} for a series of epoch-second timestamps under a period label.

    `covers_days` is the series' own span, reported even when covered — the UI says "covers 14d of
    3M" rather than dashing a number that is still a true delta-since-inception.
    """
    days = WINDOW_DAYS.get(period)
    if days is None:
        return {"covered": True, "covers_days": None}
    if len(point_ts) < 2:
        # One point spans nothing. Not covered, and 0 rather than None: the series exists and its
        # span is genuinely zero — "absent" (no series) is the caller's case, not this one.
        return {"covered": False, "covers_days": 0}
    span_days = (max(point_ts) - min(point_ts)) / _DAY_SECS
    # One bar of slack, scaled by the series' own median-free approximation: the coarsest bar the
    # publisher uses for these windows is one day, so a fixed one-day slack matches trend.ts's
    # one-bar rule at daily spacing without importing a gap estimator for three timestamps.
    return {"covered": span_days >= days - 1, "covers_days": round(span_days, 1)}
