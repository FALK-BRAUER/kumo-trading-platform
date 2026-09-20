"""IB's session string, parsed into three states (#628).

REAL DATA, decoded off staging's cached SPY instrument 2026-08-28 — not from documentation:

    timeZoneId     US/Eastern
    liquidHours    20260826:0930-20260826:1600;20260827:0930-20260827:1600;
                   20260828:0930-20260828:1600;20260829:CLOSED;20260830:CLOSED;
                   20260831:0930-20260831:1600

`liquidHours` is the REGULAR session; `tradingHours` is the extended one (0400-2000). Nautilus
already puts both on `Instrument.info` via `contract_details_to_dict`, so this needs no HTTP call and
no vendor credential — which is the entire point of #628.

THE WINDOW IS THE DESIGN, NOT A CAVEAT. IB returns a ROLLING ~6-day window. `next_fire` looks 14 days
ahead, so it runs off the end of what the venue told us on EVERY call — the normal path, not an edge
case. Three states, and the third is not a variant of the other two:

    known-open       the venue said this session trades
    known-closed     the venue said CLOSED
    outside-window   the venue never described this day

A day nobody described must not be read as a trading day. That is the rule this whole week keeps
producing in different clothes — `or` reading a real 0 as absent, `nan <= 0` disarming a halt, an
empty dict invisible to the detector written for it, a rate limiter refusing by returning success:
**absence must not be readable as permission.**

Note 20260829/30 are a weekend, and CLOSED reads identically to a holiday. That is what makes the
venue's own string holiday-aware for free — and why the WINDOW BOUNDARY is the only thing here that
can lie.
"""

from __future__ import annotations

from datetime import date

import pytest

from api.venue_hours import parse_sessions

#: Verbatim from the running stack. Kept exact so a format change in IB's reply fails here loudly
#: rather than silently producing an empty calendar.
_SPY_LIQUID = (
    "20260826:0930-20260826:1600;20260827:0930-20260827:1600;20260828:0930-20260828:1600;"
    "20260829:CLOSED;20260830:CLOSED;20260831:0930-20260831:1600"
)


def test_the_fixture_contains_all_three_states():
    """FIXTURE PROPERTY FIRST. Without an open day, a CLOSED day, and a day outside the window, the
    parser could collapse two states into one and every assertion below would still pass."""
    assert "0930-" in _SPY_LIQUID and "CLOSED" in _SPY_LIQUID
    assert "20261126" not in _SPY_LIQUID, "the fixture must have a day it does NOT describe"


def test_an_OPEN_day_carries_its_real_session_times():
    sessions = parse_sessions(_SPY_LIQUID, "US/Eastern")
    d = sessions[date(2026, 8, 28)]
    assert d is not None
    assert (d.open_at.hour, d.open_at.minute) == (9, 30)
    assert (d.close_at.hour, d.close_at.minute) == (16, 0)


def test_a_CLOSED_day_is_present_and_explicitly_None():
    """Present-and-None, not absent. The venue SAID this day does not trade, which is a different
    fact from never having mentioned it — and conflating them is the whole defect."""
    sessions = parse_sessions(_SPY_LIQUID, "US/Eastern")
    assert date(2026, 8, 29) in sessions
    assert sessions[date(2026, 8, 29)] is None


def test_a_day_OUTSIDE_the_window_is_ABSENT_not_open():
    """THE ONE THAT MATTERS. US Thanksgiving 2026 is outside IB's rolling window, so the venue never
    described it. It must not appear as a trading day."""
    sessions = parse_sessions(_SPY_LIQUID, "US/Eastern")
    unknown = date(2026, 11, 26)

    # MEMBERSHIP *AND* ACCESS. Asserting only `not in` did not bite a defaultdict mutation, because
    # `in` never triggers the factory — the map looked correct while any caller reading
    # `sessions[d]` would have been handed a session for a day the venue never described. Test what
    # a CALLER gets, not what the container admits to holding.
    assert unknown not in sessions, (
        "a day the venue never described is present in the calendar — absence read as permission, "
        "which schedules a session nobody confirmed (#628)"
    )
    with pytest.raises(KeyError):
        sessions[unknown]

    # ...and the plain-dict type is itself the guarantee: anything auto-vivifying turns an unknown
    # day into a trading day at the moment it is asked about.
    assert type(sessions) is dict, f"{type(sessions).__name__} can invent a session for an unknown day"


def test_the_TIMEZONE_is_the_venues_not_the_hosts():
    """The host runs UTC in a container; the session is Eastern. A naive parse would put the open at
    09:30 UTC — 04:30 ET — and every scheduled decision would fire five hours early."""
    sessions = parse_sessions(_SPY_LIQUID, "US/Eastern")
    d = sessions[date(2026, 8, 28)]
    assert d.open_at.tzinfo is not None, "the session time is naive — it would be read as host-local"
    assert "Eastern" in str(d.open_at.tzinfo) or d.open_at.utcoffset().total_seconds() == -4 * 3600


def test_an_EMPTY_or_MALFORMED_string_yields_NOTHING_rather_than_a_guess():
    """A venue that answered with nothing must produce a calendar that knows nothing — which then
    refuses. An empty dict that reads as "no trading days" is honest; a default is not."""
    for bad in ("", "   ", "garbage", "20260826"):
        assert parse_sessions(bad, "US/Eastern") == {}


def test_a_MALFORMED_ENTRY_does_not_discard_the_good_ones(caplog):
    """One unparseable segment must cost that segment, not the session. Same discipline as dropping a
    single unresolvable symbol rather than crash-looping the node on a JEPO misread."""
    mixed = "20260826:0930-20260826:1600;WHAT;20260827:CLOSED"
    with caplog.at_level("WARNING"):
        sessions = parse_sessions(mixed, "US/Eastern")
    assert date(2026, 8, 26) in sessions and date(2026, 8, 27) in sessions
    assert "WHAT" in caplog.text, "a dropped segment was not named"
