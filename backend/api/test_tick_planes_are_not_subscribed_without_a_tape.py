"""`_after_definition` must not ask a venue for a tick plane it declares it does not serve (#812).

MEASURED on ibkr-paper, boot of 2026-09-09 05:36 UTC (engine jsonl, 7986 lines):

    112 instruments, each subscribed to BOTH planes  = 224 tick-by-tick requests
    135  x 10189  No market data permissions (NYSE / ISLAND / AMEX STK)
    199  x 10190  Max number of tick-by-tick requests has been reached
     67  x   300  Can't find EId with tickerId
      0         ticks delivered

`api/providers/ibkr.py` ALREADY declares `streams_trade_ticks=False`, with the measurement in its own
comment ("IB serves market data to ONE session per user and the live session holds it"). That
declaration reaches exactly one consumer — `aggregation_plan` at `engine_node.py:1057`, which
correctly keeps the INTERNAL bar granularities out. It does not reach the subscription path.
A declared capability, computed and half-discarded.

`_after_definition` subscribes both planes on the belief that a provider without a stream no-ops.
Its own comments say so:

    # Providers without a live trade stream simply emit nothing (the client no-ops)
    # Providers without a live quote stream no-op — last-price still serves

The IBKR adapter does not no-op. It issues `reqTickByTickData` and IB rejects it. IB's tick-by-tick
concurrency limit is small and shared, so 224 unservable requests consume it — the 199 `10190` ARE
that cap being hit, which means a symbol that could have been served was denied a slot.

TWO PLANES, TWO FACTS. Trades and quotes are separate: Alpaca serves both, this IBKR entitlement
serves neither, and a single flag cannot honestly represent both. `DataClientSpec` has no quote
declaration at all, so even a correct reading of the trade flag could not answer the quote question.

WHY NOTHING CAUGHT IT. `test_internal_bars_are_not_subscribed_without_a_tape.py` drives this exact
seam — and stubs both tick subscriptions to `lambda *a, **k: None`, discarding the calls. The double
could not represent the bug. This file records them instead.
"""

from __future__ import annotations

import types

from nautilus_trader.model.identifiers import InstrumentId

from api.bar_spec import GRANULARITIES, aggregation_plan
from api.engine_node import UiFeedStrategy
from api.failed_requests import FailedRequests
from api.subscription_ledger import SubscriptionLedger

#: A REAL `InstrumentId`, not a string. Production reaches `_after_definition` only through
#: `_load(instrument_id)` (engine_node.py:1473), itself fed by `InstrumentId.from_str(...)`
#: (engine_node.py:2566) — so a probe passing a bare `str` is looser than the surface it claims to
#: protect and would accept a fix that only works on strings. (codex, test-coverage review)
_IID = InstrumentId.from_str("AAPL.XNAS")


class _AlwaysOpenCalendar:
    """`state_of` walks trading minutes to decide SILENT vs UNKNOWN. Neither matters here — the
    assertions are about `None` (never asked) versus not-None — but a calendar that RAISED would make
    every state read UNKNOWN and hide a wrongly-recorded subscription behind a third answer."""

    def day(self, d):
        from datetime import datetime, timezone
        return types.SimpleNamespace(
            session=d,
            open_at=datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc),
            close_at=datetime(d.year, d.month, d.day, 23, 59, tzinfo=timezone.utc),
        )


class _Recorder:
    def __init__(self):
        self.trade_ticks: list[str] = []
        self.quote_ticks: list[str] = []
        self.bars: list[str] = []


def _strategy(*, streams_trade_ticks: bool, streams_quote_ticks: bool,
              held: bool = False, budget: float = float("inf")):
    """A real `UiFeedStrategy` with only the Nautilus surface replaced — and the tick calls RECORDED.

    `__new__` rather than the constructor: building one needs a live kernel. Fields are set
    explicitly so a field the code starts depending on raises AttributeError here rather than being
    quietly satisfied by a Mock.

    `budget` is a parameter because `allow_realtime = is_position or under_budget`. With the budget
    at `inf` the `is_position` half is dead and a `held=True` test proves nothing about it — that is
    exactly the vacuity codex caught in the first draft of this file. The held case below sets
    `budget=0` so the position branch is the ONLY thing that can open the gate.
    """
    class _Probe(UiFeedStrategy):
        @property
        def cache(self):
            pos = [types.SimpleNamespace(instrument_id=_IID)] if held else []
            return types.SimpleNamespace(positions_open=lambda: pos)

        @property
        def clock(self):
            return types.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)

    s = _Probe.__new__(_Probe)
    rec = _Recorder()
    s._aggregation = aggregation_plan(GRANULARITIES, streams_trade_ticks=streams_trade_ticks)
    s._failed_requests = FailedRequests()
    s._granularities = list(GRANULARITIES)
    s._bar_types = []
    s._data_client_id = None
    s._subscriptions = SubscriptionLedger()
    s._realtime_subscribed = set()
    s._realtime_budget_warned = set()
    s._streams_trade_ticks = streams_trade_ticks
    s._streams_quote_ticks = streams_quote_ticks
    # Read by the subscription path since #836: on a PACED provider a live subscribe is a
    # historical request and leaves through the queue. This file counts SYNCHRONOUS subscribes,
    # so it declares the unmetered rate production gives Alpaca — the pacing itself is pinned in
    # test_boot_does_not_burst_the_venue.py.
    s._hist_rate = float("inf")
    s.subscribe_trade_ticks = lambda iid, **k: rec.trade_ticks.append(str(iid))
    s.subscribe_quote_ticks = lambda iid, **k: rec.quote_ticks.append(str(iid))
    s.subscribe_bars = lambda bt, *a, **k: rec.bars.append(str(bt))
    s._request_bars_paced = lambda bt, **k: None
    s._window = lambda i, g: (None, None)
    s._realtime_budget = lambda: budget
    s._cfg = types.SimpleNamespace(data_provider="test")
    return s, rec


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTY FIRST. The double must be able to SEE the calls, or every assertion is vacuous.
# ---------------------------------------------------------------------------------------------

def test_the_probe_actually_records_tick_subscriptions():
    """The reason this defect survived: the existing seam test stubs both tick calls to no-ops, so a
    node subscribing 224 unservable planes and a node subscribing none look identical to it. Prove
    this double can tell them apart before asserting anything about which it saw."""
    s, rec = _strategy(streams_trade_ticks=True, streams_quote_ticks=True)
    s._after_definition(_IID)
    assert rec.trade_ticks == ["AAPL.XNAS"], "the probe cannot see a trade-tick subscription"
    assert rec.quote_ticks == ["AAPL.XNAS"], "the probe cannot see a quote-tick subscription"


def test_the_probe_reaches_the_bar_path_too():
    """The fix must not take the chart dark, so the assertion that bars survive has to be reachable.
    A probe that subscribed no bars at all would make that guarantee untestable."""
    s, rec = _strategy(streams_trade_ticks=True, streams_quote_ticks=True)
    s._after_definition(_IID)
    assert rec.bars, "the probe never reaches the bar subscription path"


# ---------------------------------------------------------------------------------------------
# THE RULE — one declaration per plane, and each gates only its own.
# ---------------------------------------------------------------------------------------------

def test_a_provider_with_NO_TAPE_is_not_asked_for_trade_ticks():
    """ibkr-paper's case. 112 requests IB answers with 10189/10190 and never serves."""
    s, rec = _strategy(streams_trade_ticks=False, streams_quote_ticks=False)
    s._after_definition(_IID)
    assert rec.trade_ticks == [], (
        "trade ticks were requested from a provider that declares it streams none — this is the "
        "224-subscription storm on every IBKR boot"
    )


def test_a_provider_with_NO_QUOTES_is_not_asked_for_quote_ticks():
    s, rec = _strategy(streams_trade_ticks=False, streams_quote_ticks=False)
    s._after_definition(_IID)
    assert rec.quote_ticks == [], (
        "quote ticks were requested from a provider that declares it streams none"
    )


def test_THE_TWO_PLANES_ARE_INDEPENDENT():
    """AIMED AT THE CLASS. Gating both on `streams_trade_ticks` would pass every other test in this
    file and be wrong: trades and quotes are different facts. A venue serving NBBO but no trade tape
    must still get its quote subscription, and vice versa — otherwise the fix silently deletes a
    working plane on the next provider that has only one of them."""
    s, rec = _strategy(streams_trade_ticks=False, streams_quote_ticks=True)
    s._after_definition(_IID)
    assert rec.trade_ticks == [], "a venue with no trade tape was asked for trade ticks"
    assert rec.quote_ticks == ["AAPL.XNAS"], (
        "a venue that DOES serve quotes was denied them — one flag is gating both planes"
    )

    s2, rec2 = _strategy(streams_trade_ticks=True, streams_quote_ticks=False)
    s2._after_definition(_IID)
    assert rec2.trade_ticks == ["AAPL.XNAS"], "a venue that DOES serve trades was denied them"
    assert rec2.quote_ticks == [], "a venue with no quote stream was asked for quotes"


def test_the_held_position_fixture_ACTUALLY_drives_the_position_branch():
    """FIXTURE PROPERTY for the test below, and the reason it needed one.

    `allow_realtime = is_position or under_budget`. The first draft of the held test left the budget
    at `inf`, so `under_budget` was already True and `held=True` changed no branch — it asserted
    nothing about the position path and would have passed against a fix that ignored it entirely
    (codex, test-coverage review). With `budget=0` the position branch is the ONLY thing that can
    open the gate; prove that here on a provider that DOES serve both planes, so the next assertion
    is about the capability rather than about whether the gate opened at all.
    """
    held, rec_held = _strategy(streams_trade_ticks=True, streams_quote_ticks=True,
                               held=True, budget=0)
    held._after_definition(_IID)
    assert rec_held.trade_ticks == ["AAPL.XNAS"], (
        "budget=0 with a held position did not reach the subscription — the position branch is not "
        "being exercised and the capability test below would be vacuous"
    )

    unheld, rec_unheld = _strategy(streams_trade_ticks=True, streams_quote_ticks=True,
                                   held=False, budget=0)
    unheld._after_definition(_IID)
    assert rec_unheld.trade_ticks == [], (
        "budget=0 WITHOUT a held position still subscribed — the budget is not binding, so `held` "
        "is not what opened the gate above"
    )


def test_a_HELD_POSITION_does_not_bypass_the_capability():
    """A held position deliberately bypasses the BUDGET, because real capital outranks a plan limit.
    It must not thereby bypass the CAPABILITY: the venue cannot serve the plane whoever is asking,
    and asking anyway spends a concurrency slot a servable symbol could have used.

    `budget=0` so the position branch is the only route to `allow_realtime` — see the fixture test
    directly above, which proves that route is live in both directions.
    """
    s, rec = _strategy(streams_trade_ticks=False, streams_quote_ticks=False,
                       held=True, budget=0)
    s._after_definition(_IID)
    assert rec.trade_ticks == [] and rec.quote_ticks == [], (
        "a held position resurrected the tick subscriptions on a venue that serves neither plane"
    )


def test_the_BARS_still_flow_when_both_tick_planes_are_refused():
    """THE THING THAT MUST NOT BREAK. ibkr-paper trades off 1-MINUTE-LAST-EXTERNAL and
    1-DAY-LAST-EXTERNAL — the two the venue serves directly. A fix that suppressed the bar path along
    with the tick planes would look identical on the "no live data" symptom and would take the whole
    instance dark."""
    s, rec = _strategy(streams_trade_ticks=False, streams_quote_ticks=False)
    s._after_definition(_IID)
    assert rec.bars, "refusing the tick planes also stopped the bars — the instance would go dark"


# ---------------------------------------------------------------------------------------------
# THREE STATES. "never asked" is not "asked and refused".
# ---------------------------------------------------------------------------------------------

def test_the_LEDGER_does_not_record_a_request_that_was_never_made():
    """`requested: 224, bound: 0` reads as a venue failing to serve us. It was not: we never asked
    for something askable. Recording a request nobody made turns a correct refusal into a phantom
    outage on the subscription surface — absence-readable-as-permission, pointed the other way.

    ASSERTED THROUGH `state_of`, WHICH ALREADY HAS THE FOURTH ANSWER. Its docstring: "None ... where
    nothing was ever asked for ... 'we never asked' and 'we asked and cannot yet say' are different
    facts". The first draft of this test matched over a `repr` with an `or`, which would have passed
    if quotes were wrongly recorded, if the kind were renamed, or if the subject were normalised
    differently (codex, test-coverage review). Ask the ledger the question it exists to answer.
    """
    s, _ = _strategy(streams_trade_ticks=False, streams_quote_ticks=False)
    s._after_definition(_IID)

    now = 1_700_000_000_000_000_000
    cal = _AlwaysOpenCalendar()
    for kind in ("trades", "quotes"):
        assert s._subscriptions.state_of(kind, "AAPL.XNAS", now, cal) is None, (
            f"the ledger holds a {kind!r} subscription that was never issued — the surface will "
            f"report it as SILENT or UNKNOWN, i.e. as a venue fault, for a request nobody made"
        )
    # And the bars, which WERE asked for, must still be recorded — otherwise this test would pass
    # against a fix that simply stopped recording everything.
    assert s._subscriptions.state_of("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", now, cal) is not None, (
        "the bar subscription vanished from the ledger too — this assertion would then be vacuous"
    )


def test_the_LEDGER_still_records_a_plane_that_IS_asked_for():
    """The other direction. A fix that made `state_of` return None for everything would satisfy the
    test above and destroy the subscription surface."""
    s, _ = _strategy(streams_trade_ticks=True, streams_quote_ticks=True)
    s._after_definition(_IID)
    now, cal = 1_700_000_000_000_000_000, _AlwaysOpenCalendar()
    for kind in ("trades", "quotes"):
        assert s._subscriptions.state_of(kind, "AAPL.XNAS", now, cal) is not None, (
            f"a {kind!r} plane that WAS subscribed is absent from the ledger"
        )
