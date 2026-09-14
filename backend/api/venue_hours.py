"""Parse a venue's own session string into three states (#628).

WHAT THIS IS FOR. staging-ibkr could not boot: `build_calendar(require_exchange=True)` refuses the
holiday-unaware fallback on any path that places orders, and ALPACA was the only thing that could
satisfy it. An IBKR-only instance had no way to supply a calendar at all.

The venue already answers it. Decoded off staging's cached SPY instrument, 2026-08-28 — real data,
not documentation:

    timeZoneId     US/Eastern
    liquidHours    20260826:0930-20260826:1600;20260827:0930-20260827:1600;
                   20260828:0930-20260828:1600;20260829:CLOSED;20260830:CLOSED;
                   20260831:0930-20260831:1600

Nautilus already puts this on `Instrument.info` via `contract_details_to_dict`, so it costs no HTTP
call and no vendor credential. Third instance of that pattern in one day — #622 (venue), #624 (asset
type), this — all reachable from `Instrument.info` the whole time.

THREE STATES, AND THE THIRD IS NOT A VARIANT OF THE OTHER TWO:

    in the map, with a session   the venue said this day trades
    in the map, None             the venue said CLOSED
    NOT in the map               the venue never described this day

IB returns a ROLLING ~6-day window, and `next_fire` looks 14 days ahead — so it runs off the end of
what the venue told us on EVERY call. That is the normal path, not an edge case. A day nobody
described must never be read as a trading day: absence must not be readable as permission.

CLOSED reads identically for a weekend and a holiday, which is what makes the venue's own string
holiday-aware for free — and why the window boundary is the only thing here that can lie.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_log = logging.getLogger("kumo.venue_hours")


class Session:
    """One trading day's real open and close, tz-aware in the VENUE's zone.

    Not the host's. A container runs UTC; the session is Eastern. A naive parse would put the open at
    09:30 UTC — 04:30 ET — and every scheduled decision would fire five hours early.
    """

    __slots__ = ("close_at", "open_at", "session")

    def __init__(self, session: date, open_at: datetime, close_at: datetime) -> None:
        self.session, self.open_at, self.close_at = session, open_at, close_at

    def __repr__(self) -> str:                                          # pragma: no cover - debug aid
        return f"Session({self.session} {self.open_at:%H:%M}-{self.close_at:%H:%M})"


def _one(segment: str, tz: ZoneInfo) -> tuple[date, Session | None] | None:
    """`20260826:0930-20260826:1600` or `20260829:CLOSED` -> (date, Session|None), or None if junk."""
    head, _, rest = segment.partition(":")
    # DTZ007 is about naive datetimes, and this one never survives: the date is taken
    # immediately and the time-of-day is placed in the VENUE's zone below.
    day = datetime.strptime(head.strip(), "%Y%m%d").date()  # noqa: DTZ007
    if rest.strip().upper() == "CLOSED":
        return day, None                       # the venue SAID closed — a fact, not an absence
    start, _, end = rest.partition("-")
    _, _, open_hm = start.partition(":") if ":" in start else ("", "", start)
    _, _, close_hm = end.rpartition(":")
    o = time(int(open_hm[:2]), int(open_hm[2:4]))
    c = time(int(close_hm[:2]), int(close_hm[2:4]))
    return day, Session(day,
                        datetime.combine(day, o, tzinfo=tz),
                        datetime.combine(day, c, tzinfo=tz))


def parse_sessions(hours: str, time_zone_id: str) -> dict[date, Session | None]:
    """IB's `tradingHours` / `liquidHours` string -> {date: Session or None}.

    ONLY the days the venue described. A caller asking about a date absent from this map is asking
    about a day the venue never mentioned, and must treat that as unknown rather than as closed —
    `elapsed_slots` reads "no session" as "nothing was due", so an unknown day read as closed is a
    decision silently not happening.

    A malformed segment costs THAT SEGMENT and is named. Same discipline as dropping one unresolvable
    symbol rather than crash-looping the node over a `JEPO` misread: one bad row costs that row.

    An unparseable or empty string yields an EMPTY map, which then knows nothing and refuses. Honest;
    a default would not be.
    """
    try:
        tz = ZoneInfo(time_zone_id)
    except Exception:                                                   # noqa: BLE001
        _log.warning("venue hours: unknown time zone %r — cannot place sessions in real time", time_zone_id)
        return {}

    out: dict[date, Session | None] = {}
    bad: list[str] = []
    for segment in (s.strip() for s in (hours or "").split(";")):
        if not segment:
            continue
        try:
            parsed = _one(segment, tz)
        except Exception:                                               # noqa: BLE001
            parsed = None
        if parsed is None:
            bad.append(segment)
            continue
        out[parsed[0]] = parsed[1]
    if bad:
        _log.warning("venue hours: %d unparseable segment(s) ignored: %s", len(bad), ", ".join(bad[:5]))
    return out
