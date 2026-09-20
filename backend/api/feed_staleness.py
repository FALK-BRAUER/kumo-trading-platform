"""Is a quiet feed a dead feed? (#757)

Measured 2026-08-31: staging's `feed_last_tick_ts` was 58.9 hours old and `/health` said `ok`. I
called it a dead feed. It was Friday's close plus a weekend — the correct and healthy answer.

BOTH READINGS WERE WRONG IN THE SAME WAY. `status` ignored the timestamp entirely, so a genuinely
dead feed would also read ok; and a fixed age threshold would have paged every Monday morning and
been muted by the second weekend. Neither a raw age nor a constant can answer this.

THE QUESTION IS NOT "HOW OLD", IT IS "WAS THE MARKET OPEN AND WE HEARD NOTHING". The engine already
carries the venue calendar it schedules the EOD capture off (`broker_calendar_or_none`), so the
answer is available without guessing: count only the time the venue was actually trading.

Three states, as ever: fresh / stale while the market was open / quiet because the market was shut.
The third is not a degraded feed and must never page — that is the false alarm that gets a real one
scrolled past.
"""

from __future__ import annotations

#: Trading minutes of silence before a feed is stale. A venue that is open and sending nothing for
#: this long is not quiet, it is not answering. Generous on purpose: a thin symbol can genuinely go
#: minutes between prints, and this is a whole-feed verdict, not a per-symbol one.
STALE_AFTER_TRADING_MINUTES = 15


def trading_minutes_between(start_ns: int, end_ns: int, calendar) -> float | None:
    """Minutes the venue was OPEN between two instants, or None if the calendar cannot say.

    None is a third answer, not zero: a calendar that does not cover the window has not told us the
    market was shut, and treating silence from it as "closed, therefore healthy" is the shape that
    reads a refusal as an all-clear.
    """
    if calendar is None or not start_ns or not end_ns or end_ns <= start_ns:
        return None
    try:
        from api.venue_calendar import calendar_minutes

        # Same adapter as the subscription ledger — ONE rule for "how do I ask a calendar", because
        # two derivations of it drift, which is the subject of half the comments in these files.
        return float(calendar_minutes(calendar, start_ns, end_ns))
    except Exception:  # noqa: BLE001 — a calendar that cannot answer says so by returning None
        return None


def feed_is_stale(last_tick_ns: int, now_ns: int, calendar) -> bool | None:
    """True if the venue was open and we heard nothing for too long. None when unknowable.

    NEVER TRUE FROM AGE ALONE. A weekend, an overnight and a holiday all produce a large age and a
    healthy feed; only open-market silence is a fault.
    """
    if not last_tick_ns:
        # Never a tick in this process. Not stale — it is "never told us", and a boot before the open
        # would otherwise page every morning.
        return None
    open_minutes = trading_minutes_between(last_tick_ns, now_ns, calendar)
    if open_minutes is None:
        return None
    return open_minutes > STALE_AFTER_TRADING_MINUTES
