"""A book the engine cannot price must SAY SO. Measured defects, pinned before any fix.

MEASURED 2026-08-31, both stacks deployed, reviewed and green:

    staging   22 held   11 with a price   11 WITHOUT   /health status = "ok"
    paper     34 held   34 with a price    0 WITHOUT   /health status = "ok"
    staging   feed_last_tick_ts = Friday's close, 58.9h old   /health status = "ok"

Half of staging's book could not be priced and every automated surface reported healthy. The UI's
own banner showed `feed 59h` — so the NUMBER reached the screen — while `/health` ignored it in the
verdict and carried no field about price coverage at all.

WHY THIS WENT UNCAUGHT THROUGH REVIEWS AND 3139 TESTS: every test asked whether a MECHANISM works —
does the reconciler place a stop, does the fold sum correctly, does the guard refuse. None asked
whether the system TELLS US when it cannot do its job. A price is an input to protection sizing, exit
sizing and every displayed P&L, and its absence was a log line.

Operator: "how can this be. seems we need even more tests or what? write tests for all the problems
before fixing them."

Each test below fails today. None of them has a fix in this commit, deliberately.

HOW THESE ARE MARKED, and why it is not a way of hiding them. Each is `xfail(strict=True)`, so it is
LISTED on every run and FAILS THE SUITE the moment the behaviour changes without the marker being
removed — a fix cannot land silently, and neither can a regression be confused with these. The count
below is the number of measured, reported, unfixed defects; it must go DOWN.

    0 known-unfixed defects, 2026-08-31 (was 8).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _status_for(*, unpriced=(), feed_stale=None) -> str:
    """The REAL /health endpoint's verdict, for a given engine frame.

    Drives `api.app.health` against a node double so the assertion is on the status a client would
    actually receive, not on the shape of the source.
    """
    import api.app as _app

    observed = {
        "unpriced_positions": list(unpriced),
        "feed_stale": feed_stale,
        "last_tick_ts": 1,
        "reconcile_drift": [], "protection_divergence": [], "naked_after_reject": [],
        "failed_requests": [], "subsystems": [],
    }
    node = SimpleNamespace(health=lambda: observed, positions=lambda: [], trades=lambda: [])
    _app.app.state.node = node

    async def _ok(_observed):
        # The REAL model, not a namespace: the endpoint validates its response, so a double the
        # model rejects fails on the type rather than on the verdict under test.
        from api.models import SubsystemHealth

        return [SubsystemHealth(name="engine", ok=True)]

    async def _none(_observed):
        return []

    old_probe, old_inert = _app._probe_subsystems, _app._inert_contradictions
    _app._probe_subsystems, _app._inert_contradictions = _ok, _none
    try:
        return asyncio.run(_app.health()).status
    finally:
        _app._probe_subsystems, _app._inert_contradictions = old_probe, old_inert


# ==================================================================================================
# 1. /health reports OK while the book cannot be priced
# ==================================================================================================
def test_health_reports_HOW_MANY_HELD_POSITIONS_CANNOT_BE_PRICED():
    """There is no field for it. An operator reading `/health` sees `status: ok` with 11 of 22
    positions unpriceable, and nothing in the payload can contradict that.

    This is the number that would have made the staging feed problem visible on day one instead of
    four days later from a phone screenshot."""
    import ast
    import inspect
    import pathlib as _p

    from api import engine_node

    src = _p.Path(inspect.getfile(engine_node)).read_text()
    assert '"unpriced_positions"' in src, "the health payload carries no unpriced count"
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_unpriced_positions"), None)
    assert fn is not None, "the derivation is gone; the key would publish a stale constant"


def test_a_book_that_cannot_be_PRICED_is_not_status_OK():
    """`status` is the one field an alert or a dashboard reads. With half the book unpriceable the
    engine cannot size a protective stop, cannot value a position, and cannot answer what a lane is
    worth — that is not `ok`, and calling it `ok` is the failure mode this repo names most often:
    a degraded state reported as the healthy one."""
    # BEHAVIOURAL, not structural. The first version asserted that the name `_unpriced` appeared in
    # the health function — and it still appears in the FORWARDING line, so a mutation removing it
    # from the STATUS expression survived. Drive the endpoint and read the verdict.
    assert _status_for(unpriced=["CGAU.XNYS"]) == "degraded", (
        "a book the engine cannot price reported ok — half of staging's book was unpriceable and "
        "every automated surface said healthy"
    )
    assert _status_for(unpriced=[]) == "ok", "a healthy book must not be degraded"


# ==================================================================================================
# 2. /health ignores feed staleness in its verdict
# ==================================================================================================
def test_a_STALE_FEED_changes_the_health_VERDICT_not_only_a_timestamp():
    """`feed_last_tick_ts` is published and the UI renders `feed 59h` in red — so the raw fact is
    already there. `status` never reads it.

    The distinction that matters: 59h across a WEEKEND is correct and healthy; 59h on a Tuesday
    afternoon is a dead feed. A verdict must be computed against the venue calendar, which the engine
    already has (`broker_calendar_or_none`), not against a fixed threshold."""
    import ast
    import inspect
    import pathlib as _p

    from api import app as _app

    assert _status_for(feed_stale=True) == "degraded", "an open-market silent feed reported ok"
    assert _status_for(feed_stale=False) == "ok", "a healthy feed must not be degraded"
    assert _status_for(feed_stale=None) == "ok", (
        "UNKNOWN staleness must not degrade — a boot before the open has told us nothing, and "
        "paging on that is how a real alarm gets muted"
    )


def test_feed_staleness_is_judged_against_the_VENUE_CALENDAR_not_a_fixed_age():
    """A fixed threshold would page every Monday morning and be muted by the second weekend. The
    engine has the calendar; this must ask it whether the market was OPEN during the gap."""
    from api.feed_staleness import feed_is_stale

    _MIN = 60_000_000_000

    class _Cal:
        def __init__(self, m):
            self._m = m

        def trading_minutes_between(self, a, b):
            return self._m

    # Same 59-hour age, opposite verdicts — which is the whole point.
    assert feed_is_stale(1, 1 + 59 * 60 * _MIN, _Cal(0)) is False, "a weekend must not be stale"
    assert feed_is_stale(1, 1 + 59 * 60 * _MIN, _Cal(59 * 60)) is True, "open-market silence is stale"
    assert feed_is_stale(1, 1 + 99 * 60 * _MIN, None) is None, "no calendar must be UNKNOWN"


# ==================================================================================================
# 3. A position that cannot be priced is silently unprotectable
# ==================================================================================================
def test_a_NO_PRICE_refusal_becomes_STANDING_STATE_not_only_a_log_line():
    """`plan_protection` refuses a position it cannot price — `Refusal(iid, "no_price", notional)` —
    and the reconciler logs a warning. That is the correct DECISION and an invisible one.

    On staging that is 11 positions which can never be protected while the feed is refused, and the
    only trace is a line in a log nobody reads on a stack whose alerting may not even be armed. The
    refusal already carries the uncovered notional; it must reach `/health` and page."""
    import ast
    import inspect
    import pathlib as _p
    import textwrap

    from api.engine_node import UiFeedStrategy

    src = _p.Path(inspect.getfile(UiFeedStrategy)).read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reconcile_protection_inner"),
              None)
    body = textwrap.dedent(ast.unparse(fn))
    assert "_failed_requests.record" in body, (
        "a refusal to rest a stop is still only a log line — 11 unpriceable positions on staging "
        "could never be protected and no surface said so"
    )
    assert "clear_kind" in body, (
        "refusals are never cleared, so the surface becomes a history of everything that ever "
        "failed rather than what is true now"
    )


def test_the_UNPROTECTED_count_distinguishes_CANNOT_from_NOT_YET():
    """The UI shows `9 of 38 unprotected` on paper and `Unprotected 22` on staging, as if they were
    the same condition. They are not: paper's are positions protection has not covered YET, staging's
    are positions it CAN NEVER cover while there is no price.

    Three states, not two: protected / unprotected / unprotectable. Collapsing the third into the
    second is what let 22 unprotectable positions read as an ordinary backlog."""
    # THE BACKEND HALF: the engine must publish which held instruments it could not price, because
    # only the engine knows — it knows because it TRIED. The UI half (`securedValue`'s third counter,
    # and the copy that names it) is pinned in ui/src/tiles/managed-portfolio/protectability.wired.test.ts;
    # the split lives across the seam and each side asserts its own half.
    from api.models import HealthResponse

    assert "unpriced_positions" in HealthResponse.model_fields, (
        "the engine's unpriceable list does not survive the health DTO, so the UI cannot tell a "
        "backlog that drains from one that never can"
    )
    got = HealthResponse(
        status="ok", subsystems=[], feed_last_tick_ts=0, unpriced_positions=["PENG.XNAS"],
    ).unpriced_positions
    assert got == ["PENG.XNAS"], f"the DTO reshaped the list: {got!r}"


# ==================================================================================================
# 4. INTERNAL bar aggregation is dead on a venue that serves no trade ticks (#612, FIXED)
# ==================================================================================================
def test_a_provider_serving_NO_TRADE_TICKS_cannot_be_configured_for_INTERNAL_bars():
    """`bar_spec.py` makes h1/w1 INTERNAL, and Nautilus builds those by subscribing to TRADE TICKS.
    That reasoning is written entirely about Alpaca's WebSocket. IBKR serves this account no trade
    ticks at all (`10197: No market data during competing live session` — the live session holds the
    single entitlement), so INTERNAL aggregation produces nothing and the series is silently empty.

    Measured: paper subscribes `1-HOUR-LAST-INTERNAL` and `1-WEEK-LAST-INTERNAL`; staging subscribes
    NEITHER. Same code, same config, one venue.

    A provider must DECLARE whether it streams trade ticks, and a granularity whose aggregation
    depends on them must be refused — loudly — where they do not exist. Silence here is a blank
    sparkline and an unpriceable position."""
    from api.bar_spec import GRANULARITIES, aggregation_plan
    from api.providers import ibkr

    # The DECLARATION exists and IBKR's is False — measured from `10197`, not assumed.
    spec = ibkr.build_data({})
    assert spec.streams_trade_ticks is False, (
        "IBKR declares it streams trade ticks, but this account gets `10197: No market data during "
        "competing live session` — the live session holds the single entitlement"
    )

    # And a granularity that DEPENDS on that tape is refused, with a reason, rather than assumed.
    plan = aggregation_plan(GRANULARITIES, streams_trade_ticks=spec.streams_trade_ticks)
    assert plan.refused, "no granularity is refused on a venue with no trade tape"
    assert plan.usable, (
        "every granularity was refused — m1/d1 are venue-streamed and must survive, or the fix for a "
        "blank sparkline takes the whole stack dark"
    )
    for g in plan.refused:
        assert "trade tick" in plan.reason_for(g), (
            f"{g} is refused without naming the missing tape, which is the silence this ticket is about"
        )


# ==================================================================================================
# 5. Nothing reconciles what was REQUESTED against what the venue actually BOUND (#618, FIXED)
# ==================================================================================================
def test_the_engine_reports_SUBSCRIPTIONS_REQUESTED_versus_ACTUALLY_BOUND():
    """392 subscriptions go out, the venue refuses a subset — 275 x `10089 requires additional
    subscription`, 136 x `10189 no permissions` — and nothing anywhere holds both numbers.

    #618 designed exactly this ("the adapter answers with what it actually bound... throttling
    downgrades the tier and SAYS SO rather than dropping the need silently") and was closed with a
    `docs(plan)` commit and one test file. There is no tier code in the backend.

    Until requested-vs-bound is a reported pair, a venue refusing half the book is indistinguishable
    from a quiet market."""
    from api.models import HealthResponse
    from api.subscription_ledger import BOUND, SILENT, SubscriptionLedger

    MIN = 60_000_000_000
    T0 = 1_700_000_000_000_000_000

    class _Open:
        def trading_minutes_between(self, a, b):
            return (b - a) / MIN

    led = SubscriptionLedger()
    led.requested("quotes", "AAPL.XNAS", T0)   # the venue refuses this one
    led.requested("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", T0)
    led.bound("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", T0 + MIN)
    now = T0 + 999 * MIN

    # BOTH NUMBERS, HELD TOGETHER — which is the thing that did not exist.
    summary = led.summary(now, _Open())
    assert (summary["requested"], summary["bound"], summary["silent"]) == (2, 1, 1)
    # And the NAMES. "275 of 392 bound" cannot tell an operator which are dark, and the cost of the
    # ticket was precisely that the missing ones could not be identified.
    assert summary["silent_subjects"] == ["quotes:AAPL.XNAS"]
    assert led.state_of("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", now, _Open()) == BOUND
    assert led.state_of("quotes", "AAPL.XNAS", now, _Open()) == SILENT

    # And it survives to the surface a human reads, rather than into a void like #546.
    assert "subscriptions" in HealthResponse.model_fields
