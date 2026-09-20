"""A trading calendar built from whatever adapter Nautilus has attached (#628).

ibkr-paper-retired could not boot: `build_calendar(require_exchange=True)` refuses the holiday-unaware
fallback on any path that places orders, and ALPACA was the only thing that could satisfy it. The
refusal is right — relaxing it to obtain a boot would trade a visible outage for a session scheduled
on Thanksgiving. What was wrong is that an IBKR-only instance had no way to supply a calendar.

The venue answers it itself. `Instrument.info` carries IB's own `liquidHours` and `timeZoneId`, put
there by Nautilus's `contract_details_to_dict`, so this costs no HTTP call and no vendor credential.

LAZY BY CONSTRUCTION, AND THAT IS THE POINT. `build_node` constructs the lanes BEFORE the node runs:
`TradingNode.build()` only registers clients (`live/node.py:272-281`), and instruments load on
CONNECT (`adapters/interactive_brokers/data.py:147`), which the kernel awaits at
`system/kernel.py:1024` before starting the trader at `:1039`. Reading eagerly would capture an EMPTY
cache and know nothing forever — precisely the trap `_instrument_ids` fell into and #622 fixed. An
empty read is never cached as an answer.

THREE STATES, and the third is not a variant of the other two:

    TradingDay              the venue said this day trades
    None                    the venue said CLOSED
    OutsideCalendarWindow   the venue never described this day  -> RAISES

IB returns a rolling ~6-day window while `next_fire` looks 14 days ahead, so it runs off the end of
knowledge on every call — the normal path, not an edge case. `elapsed_slots` reads "no session" as
"nothing was due", so an unknown day returned as None is a decision silently not happening. Absence
must not be readable as permission.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from api.venue_hours import parse_sessions

_log = logging.getLogger("kumo.venue_calendar")

#: How far `next_fire` will search. Matches the strategies-side calendars so a lane behaves the same
#: whichever supplies it; the window refusal, not this bound, is what stops a runaway search.
_SEARCH_DAYS = 14


def _strategies_calendar():
    """`(OutsideCalendarWindow, TradingDay)`, imported LAZILY.

    Module-level would make `api.engine_node` — which imports this — hard-require kumo-trading-strategies at
    import time, and CI has a job whose whole purpose is proving the suite runs WITHOUT it: 153 of
    184 test files do not need it, and the 30 that do are a separate job holding the private-repo
    credential. Importing eagerly here broke that job, which is the job doing its job.

    Every other kumo-trading-strategies import in this codebase is function-local for the same reason.
    """
    from kumo_strategies.runtime.calendar import (
        OutsideCalendarWindow,
        TradingDay,
    )

    return OutsideCalendarWindow, TradingDay


class VenueCalendar:
    """`day` / `next_fire` / `is_trading_day`, sourced from the attached adapter's own sessions.

    All three are required: `build_calendar` refuses an injected calendar missing any of them,
    because `day` + `is_trading_day` alone satisfies `warm`/`next_slot_fire`/`elapsed_slots` and then
    raises `AttributeError` the first time QC27 or QC345 arms — inside arming, which catches and
    retries, i.e. silently unarmed.
    """

    def __init__(self, cache_getter) -> None:
        self._cache_getter = cache_getter
        self._sessions: dict[date, object] | None = None

    def _load(self, d: date | None = None) -> dict:
        """Every instrument's sessions, unioned, RE-READ whenever `d` is not already described (#810).

        MEMOISING THIS IS A BUG, NOT AN OPTIMISATION. The source is a ROLLING ~6-day window — both
        this module's docstring and `venue_hours`'s say so — and a rolling value cached once for the
        life of the process is guaranteed to age out. ibkr-paper booted on 2026-09-09 and answered every
        question from a window ending 2026-09-04: 320 UNARMED lines in one boot, both lanes unable to
        decide, with the correct answer one re-read away. The cache is in-memory, so a re-scan costs a
        dict walk and a parse; nothing here justifies holding a stale answer to avoid it.

        UNION, NOT "THE FIRST ONE THAT ANSWERS". The same incident had BOTH windows in the cache: 74
        instruments stamped 20260902..20260904 (last fetched 09-01, then served from the durable redis
        cache and never re-fetched) beside 259 stamped 20260908..20260910. Reading one and stopping
        made the verdict depend on cache iteration order — the same stack and the same data giving
        different answers on different boots, with nothing to say which one you got.

        FRESHEST WINS ON A CONFLICT, and the merge order is what enforces it. Instruments are merged
        oldest-window-first so a newer description overwrites an older one for a date both mention.
        That is order-independent by construction (the sort key comes from the DATA, not from the
        cache's iteration), which is the property `test_the_ANSWER_DOES_NOT_DEPEND_ON_ITERATION_ORDER`
        pins. US equity `liquidHours` agree across venues in practice, so this rule almost never
        chooses; it exists so that when it does, the choice is stated rather than incidental.

        NOT MEMOISED WHEN EMPTY. A `day()` before connect must not poison the calendar for the life
        of the process — that is the "fixes one boot, re-breaks the next" failure again.
        """
        if self._sessions and (d is None or d in self._sessions):
            return self._sessions

        found: list[tuple[date, int, dict]] = []
        for inst in (self._cache_getter().instruments() or []):
            info = getattr(inst, "info", None) or {}
            hours, tz = info.get("liquidHours"), info.get("timeZoneId")
            if not hours or not tz:
                continue
            sessions = parse_sessions(hours, tz)
            if sessions:
                found.append((max(sessions), len(sessions), sessions))
        if not found:
            return self._sessions or {}

        merged: dict[date, object] = {}
        for _, _, sessions in sorted(found, key=lambda f: (f[0], f[1])):
            merged.update(sessions)
        if merged != self._sessions:
            _log.info("venue calendar: %d sessions from %d instrument(s), %s..%s",
                      len(merged), len(found), min(merged), max(merged))
        self._sessions = merged
        return merged

    def day(self, d: date):
        OutsideCalendarWindow, TradingDay = _strategies_calendar()
        sessions = self._load(d)
        if d not in sessions:
            known = f"{min(sessions)}..{max(sessions)}" if sessions else "nothing yet"
            raise OutsideCalendarWindow(
                f"the venue has not described {d}; it told us about {known}. Refusing rather than "
                f"assuming, because a day nobody confirmed must not become a trading day."
            )
        s = sessions[d]
        return None if s is None else TradingDay(s.session, s.open_at, s.close_at)

    def is_trading_day(self, d: date) -> bool:
        """Derived from `day` rather than computed alongside it — two derivations of one fact drift,
        and an unknown day must propagate the refusal rather than collapse into a bool."""
        return self.day(d) is not None

    def trading_minutes_between(self, start_ns: int, end_ns: int) -> float:
        """Minutes the venue was OPEN between two instants (#757).

        Answers "was the market trading and we heard nothing", which is the only question that
        separates a dead feed from a weekend. A raw age cannot: 59 hours across a weekend is healthy
        and 59 hours on a Tuesday is a fault, and a fixed threshold pages every Monday until it is
        muted.

        RAISES PAST THE DESCRIBED WINDOW, like `day()` and `next_fire()` — a day the venue never
        described must not become a trading day OR a closed one. The caller reads the refusal as
        "unknown" and declines to judge, which is the honest answer and the one that does not invent
        an all-clear.
        """
        # ONE ALGORITHM, not two. `minutes_open` below is shared with every other calendar in the
        # system — the broker's `AlpacaCalendar` has `day()` and no `trading_minutes_between`, and
        # writing a second walk for it is precisely the two-derivations-drift this file's own
        # comments pay for. It needs only a `day(date)` callable, which both expose.
        return minutes_open(self.day, start_ns, end_ns)

    def next_fire(self, after: datetime, offset_minutes: int) -> tuple[date, datetime]:
        """The next session open + offset. Called DIRECTLY by QC27 and QC345, not via a helper.

        Raises past the described window rather than inventing a day — otherwise the refusal in
        `day()` is bypassed by the caller that matters most.
        """
        OutsideCalendarWindow, _ = _strategies_calendar()
        d = after.date()
        for _ in range(_SEARCH_DAYS):
            td = self.day(d)                       # propagates OutsideCalendarWindow deliberately
            if td is not None:
                fire = td.open_at + timedelta(minutes=offset_minutes)
                if fire > after.astimezone(td.open_at.tzinfo):
                    return td.session, fire
            d += timedelta(days=1)
        raise OutsideCalendarWindow(
            f"no trading day found within {_SEARCH_DAYS} days of {after.date()}")


def minutes_open(day_of, start_ns: int, end_ns: int) -> float:
    """Minutes a venue was OPEN between two instants, given a `day(date) -> session | None`.

    A FREE FUNCTION TAKING THE LOOKUP, so every calendar in the system shares one walk. The broker's
    `AlpacaCalendar` exposes `day`, `is_trading_day` and `next_fire` and NOT this — which surfaced on
    paper as `'AlpacaCalendar' object has no attribute 'trading_minutes_between'`, reported by the
    subscription ledger's own error path rather than silently becoming `0 of 0`. Writing a second walk
    for it would be the drift these modules keep paying for.

    IT DOES NOT SWALLOW. `day_of` raises past the window it describes, and that raise propagates: a
    day the venue never described must not become a trading day OR a closed one. The caller decides
    what "cannot say" means; it is not this function's to invent.
    """
    from datetime import datetime, timezone

    start = datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ns / 1e9, tz=timezone.utc)
    if end <= start:
        return 0.0

    total = 0.0
    d = start.date()
    while d <= end.date():
        session = day_of(d)          # raises OutsideCalendarWindow past the window
        if session is not None:
            lo = max(start, session.open_at)
            hi = min(end, session.close_at)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        d = date.fromordinal(d.toordinal() + 1)
    return total


def calendar_minutes(calendar, start_ns: int, end_ns: int) -> float | None:
    """Minutes open, from WHATEVER calendar this instance happens to have.

    Two implementations reach this code and they do not share an interface: the IBKR-backed
    `VenueCalendar` answers `trading_minutes_between` directly, while the broker fallback
    (`AlpacaCalendar`, which is what a paper instance gets) offers only `day`. Before this, the
    subscription ledger asked for the method by name and paper reported `requested: null` with an
    AttributeError for its whole life.

    Returns None where the calendar covers the span but says it cannot answer for it — distinct from
    a raise, which means the calendar cannot describe a venue at all.

    A calendar offering NEITHER raises, and that raise is the answer — an object that cannot describe
    a venue must not be quietly treated as one describing a closed venue. `None` for "no calendar at
    all" stays the CALLER's decision, made once, above.
    """
    fn = getattr(calendar, "trading_minutes_between", None)
    if callable(fn):
        minutes = fn(start_ns, end_ns)
        # None PASSES THROUGH AS None. A calendar answering "I do not cover this span" has ANSWERED,
        # and `float(None)` would turn that third state into a TypeError indistinguishable from a
        # calendar that is broken. Three states, and the middle one is the whole point.
        return None if minutes is None else float(minutes)
    day_of = getattr(calendar, "day", None)
    if callable(day_of):
        return minutes_open(day_of, start_ns, end_ns)
    raise AttributeError(
        f"{type(calendar).__name__} describes no venue sessions: it has neither "
        f"`trading_minutes_between` nor `day`, so nothing here can say whether the market was open"
    )
