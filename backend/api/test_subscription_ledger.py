"""Requested-versus-bound, and the three states it must keep apart (#618).

Every assertion here is aimed at the same class of defect: an answer the system cannot give being
rendered as one it can. The measured cost was 392 subscriptions out, a large subset refused, and
`status: ok` on every surface.
"""

from __future__ import annotations

import pytest

from api.subscription_ledger import (
    BOUND,
    SILENT,
    SILENT_AFTER_TRADING_MINUTES,
    UNKNOWN,
    SubscriptionLedger,
)

MIN = 60_000_000_000
T0 = 1_700_000_000_000_000_000


class _AlwaysTrading:
    """A venue that describes every span as fully open — the market-hours case."""

    def trading_minutes_between(self, start_ns, end_ns):
        return (end_ns - start_ns) / MIN


class _NeverDescribes:
    """A venue that cannot say what happened in the span.

    This is not exotic: IB's `liquidHours` is a rolling ~6-day window, so any question reaching past
    it gets this answer, and a scheduler looking 14 days ahead leaves that window on every call.
    """

    def trading_minutes_between(self, start_ns, end_ns):
        # None, NOT a raise. A venue that has not described the span answers; a venue whose calendar
        # is broken raises, and those must not be the same state — see `_trading_minutes`.
        return None


class _Closed:
    """A span the venue describes as containing no trading time at all — a weekend."""

    def trading_minutes_between(self, start_ns, end_ns):
        return 0.0


def test_the_fixture_can_express_the_bug():
    """Vacuity guard. The silence threshold must be reachable by the spans used below, or every
    assertion about SILENT passes by having nothing that could violate it."""
    assert SILENT_AFTER_TRADING_MINUTES > 0
    long_span = SILENT_AFTER_TRADING_MINUTES * 2 * MIN
    assert _AlwaysTrading().trading_minutes_between(T0, T0 + long_span) >= SILENT_AFTER_TRADING_MINUTES


def test_a_stream_that_was_NEVER_ASKED_FOR_is_not_a_state():
    """The fourth answer, and the one that keeps being collapsed. `0 of 0` is not `0 of 4`, and an
    absent row reading as a state is how a 0-allocation lane came to read as armed."""
    led = SubscriptionLedger()
    assert led.state_of("bars", "AAPL", T0, _AlwaysTrading()) is None
    assert led.summary(T0, _AlwaysTrading())["requested"] == 0


def test_a_stream_that_DELIVERED_is_bound():
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    led.bound("bars", "AAPL", T0 + MIN)
    assert led.state_of("bars", "AAPL", T0 + 999 * MIN, _AlwaysTrading()) == BOUND


def test_a_stream_that_DELIVERED_NOTHING_past_the_threshold_is_SILENT_and_NAMED():
    """The headline. Not a count — the NAMES, because "275 of 392 bound" cannot tell an operator
    which 117 are dark, and that was the entire cost of the ticket."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    now = T0 + (SILENT_AFTER_TRADING_MINUTES + 1) * MIN
    assert led.state_of("bars", "AAPL", now, _AlwaysTrading()) == SILENT
    assert "bars:AAPL" in led.summary(now, _AlwaysTrading())["silent_subjects"]


def test_a_stream_asked_for_MOMENTS_AGO_is_UNKNOWN_not_silent():
    """Otherwise every subscription is dark for the first instant of its life and the surface cries
    wolf on every boot — which is how a real alarm gets turned off."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    assert led.state_of("bars", "AAPL", T0 + MIN, _AlwaysTrading()) == UNKNOWN


def test_a_span_the_VENUE_NEVER_DESCRIBED_is_UNKNOWN_not_silent_and_not_bound():
    """The `nan <= 0` family: a comparison that cannot be evaluated must not read as permission, and
    must not read as an alarm either."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    forever = T0 + 10_000 * MIN
    assert led.state_of("bars", "AAPL", forever, _NeverDescribes()) == UNKNOWN


def test_a_WEEKEND_of_no_data_is_not_a_silent_subscription():
    """A subscription that produced nothing over a weekend produced nothing correctly. Wall clock
    would call this dark after 30 minutes and be wrong for 48 hours."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    monday = T0 + 3 * 24 * 60 * MIN
    assert led.state_of("bars", "AAPL", monday, _Closed()) == UNKNOWN


def test_RE_REQUESTING_A_BOUND_STREAM_DOES_NOT_RESET_IT():
    """The engine re-subscribes on reconnect and on the refetch heal. If that restarted the clock, a
    permanently dark subscription would never age into SILENT — it would look exactly like a healthy
    one that had just been renewed, which is this repo's own agreement-is-not-connection shape."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    now = T0 + (SILENT_AFTER_TRADING_MINUTES + 1) * MIN
    led.requested("bars", "AAPL", now)          # the heal re-asks
    assert led.state_of("bars", "AAPL", now, _AlwaysTrading()) == SILENT, (
        "a re-request restarted the silence clock, so a stream that has never delivered can never "
        "be reported dark"
    )


def test_data_for_something_NEVER_REQUESTED_is_recorded_rather_than_dropped():
    """The other half of requested-versus-bound. A venue sending what we did not ask for is a real
    disagreement, and dropping it hides exactly the mismatch the ledger exists to expose."""
    led = SubscriptionLedger()
    led.bound("quotes", "MSFT", T0)
    assert led.state_of("quotes", "MSFT", T0 + MIN, _AlwaysTrading()) == BOUND
    assert led.summary(T0 + MIN, _AlwaysTrading())["requested"] == 1


def test_the_KIND_discriminates_two_streams_on_one_symbol():
    """bars, trades and quotes are separate subscriptions with separate entitlements — `10089` on
    quotes while bars flow is precisely the half-served state. Keying on the symbol alone would let a
    bound bar stream vouch for a refused quote stream."""
    led = SubscriptionLedger()
    led.requested("bars", "AAPL", T0)
    led.requested("quotes", "AAPL", T0)
    led.bound("bars", "AAPL", T0 + MIN)
    now = T0 + (SILENT_AFTER_TRADING_MINUTES + 1) * MIN
    assert led.state_of("bars", "AAPL", now, _AlwaysTrading()) == BOUND
    assert led.state_of("quotes", "AAPL", now, _AlwaysTrading()) == SILENT


def test_the_SUMMARY_counts_add_up_to_what_was_requested():
    """A panel whose cells do not sum to its total is #596. Every requested stream must land in
    exactly one bucket."""
    led = SubscriptionLedger()
    now = T0 + (SILENT_AFTER_TRADING_MINUTES + 1) * MIN
    led.requested("bars", "A", T0)
    led.bound("bars", "A", T0 + MIN)
    led.requested("bars", "B", T0)                       # silent
    led.requested("bars", "C", now - MIN)                # too young to say
    s = led.summary(now, _AlwaysTrading())
    assert s["bound"] + s["silent"] + s["unknown"] == s["requested"] == 3
    assert (s["bound"], s["silent"], s["unknown"]) == (1, 1, 1)


@pytest.mark.parametrize("state", [BOUND, SILENT, UNKNOWN])
def test_the_three_states_are_distinct_values(state):
    """Two of them collapsing into one string would make every assertion above pass while the
    distinction they exist for was gone."""
    assert len({BOUND, SILENT, UNKNOWN}) == 3
    assert isinstance(state, str) and state
