"""INTERNAL bar aggregation builds from TRADE TICKS — so a venue that sends none cannot use it (#612).

`bar_spec.py` makes h1/w1 INTERNAL, and Nautilus builds an internally-aggregated bar by subscribing
to the instrument's trade ticks (`_subscribe_bar_aggregator` issues `SubscribeTradeTicks`). That
file's own reasoning is written entirely about Alpaca's WebSocket.

IBKR sends this account NO trade ticks — `10197: No market data during competing live session`, the
live session holds the single entitlement — so INTERNAL aggregation produces nothing and the series
is silently empty.

MEASURED 2026-08-31, same code, same config, one venue apart:

    paper     subscribes  AEM.XNYS-1-HOUR-LAST-INTERNAL, AEM.XNYS-1-WEEK-LAST-INTERNAL
    staging   subscribes  NEITHER

Consequence on staging: four of six sparklines blank, and 11 of 22 held positions with no price —
`_last_price_for` falls back to bars, and there were none.

#612 is still OPEN and its title says exactly this. The Alpaca half was fixed on 2026-08-28; nothing
declared that the fix depended on a venue property, so the IBKR half failed silently.

A PROVIDER MUST DECLARE WHETHER IT STREAMS TRADE TICKS, and a granularity whose aggregation depends
on them must be refused — loudly — where they do not. Absence of a bar is not evidence of a quiet
market.
"""

from __future__ import annotations

import pytest

from api.bar_spec import GRANULARITIES, aggregation_plan


def test_a_venue_that_STREAMS_trade_ticks_may_aggregate_internally():
    """Alpaca: the case the current split was designed for, and it must not regress."""
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=True)
    assert plan.usable, "a tick-streaming venue must keep its INTERNAL granularities"
    assert not plan.refused


def test_a_venue_that_STREAMS_NONE_cannot_use_INTERNAL_granularities():
    """IBKR here. The INTERNAL series would subscribe to a tape the venue never sends and sit empty
    forever, which is indistinguishable from a quiet market."""
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=False)
    assert plan.refused, "no granularity was refused on a venue that sends no trade ticks"
    for g in plan.refused:
        assert "trade tick" in plan.reason_for(g).lower()


def test_the_EXTERNAL_granularities_SURVIVE_a_venue_with_no_ticks():
    """This is what keeps staging usable rather than dark: m1 and d1 are venue-streamed and
    backfilled, so they work with no tape at all. Refusing them too would take the daily bars the
    rotation strategies actually trade off."""
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=False)
    assert plan.usable, "a venue with no ticks still serves EXTERNAL bars — do not blank it entirely"


def test_the_REFUSAL_IS_NAMED_not_silent():
    """The whole defect was silence. A refused granularity must be reportable, with the reason, so it
    reaches `failed_requests` rather than being an empty chart nobody can explain."""
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=False)
    g = plan.refused[0]
    assert plan.reason_for(g), "a refused granularity carries no reason"


def test_UNKNOWN_tick_availability_is_not_assumed_to_work():
    """A provider that has not declared is the shape that produced this: `bar_spec` assumed ticks
    because Alpaca had them. Unknown must refuse the dependent granularities, not assume them."""
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=None)
    assert plan.refused, "unknown tick availability silently kept INTERNAL granularities"
