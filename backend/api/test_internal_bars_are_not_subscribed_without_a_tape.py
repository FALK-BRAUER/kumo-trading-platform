"""The SEAM for #612: `_after_definition` must not subscribe a series the venue cannot build.

`test_internal_bars_need_trade_ticks.py` pins the SPLIT. A correct split that nothing calls is the
shape this repo keeps shipping green — a stub replacing `aggregation_plan` passed all five of those
tests. So this drives the real subscription path on a real strategy instance and asserts on what it
did to Nautilus, not on what a helper returned.

WHY IT MATTERS THAT HISTORY SURVIVES. The INTERNAL/EXTERNAL source is checked on SUBSCRIBE
(engine.pyx:1234, 1827), never on request, so the historical REST load works on a venue with no tape.
Refusing the live stream must therefore not take the chart dark — it must only stop the node from
claiming a live series exists. That is asserted here as its own case, because a fix that suppressed
both would look identical on the blank-sparkline symptom that motivated the ticket.
"""

from __future__ import annotations

import types

from api.bar_spec import GRANULARITIES
from api.engine_node import UiFeedStrategy


class _Recorder:
    """Stands in for the strategy's Nautilus surface, recording what it was asked to do."""

    def __init__(self):
        self.subscribed: list[str] = []
        self.requested: list[str] = []


def _strategy(streams_trade_ticks):
    """A UiFeedStrategy with the two calls under test replaced, and NOTHING else stubbed.

    `__new__` rather than the constructor: building one needs a live Nautilus kernel. The fields the
    path reads are set explicitly, so a field the code starts depending on raises AttributeError here
    instead of being quietly satisfied by a Mock — the double-must-reject-what-production-rejects rule.
    """
    from api.bar_spec import aggregation_plan
    from api.failed_requests import FailedRequests

    # `cache` is a read-only getset on the Cython `Actor` base and CANNOT be assigned — which is
    # itself production rejecting something a looser double would have accepted. A subclass property
    # shadows it through the MRO without loosening anything on the real class.
    class _Probe(UiFeedStrategy):
        @property
        def cache(self):
            return types.SimpleNamespace(positions_open=lambda: [])

        @property
        def clock(self):
            # Production always has one — the kernel supplies it at registration. A double that
            # cannot answer what production always answers is the bug (#618 wired a request-time
            # stamp into this path).
            return types.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)

    s = _Probe.__new__(_Probe)
    rec = _Recorder()
    s._aggregation = aggregation_plan(GRANULARITIES, streams_trade_ticks=streams_trade_ticks)
    # THE SUBSCRIPTION PATH READS THESE SINCE #812, and this probe is built with `__new__` so an
    # attribute production always has is one this double must set explicitly. The trade flag mirrors
    # the parameter under test; the quote flag is True so refusing it cannot be mistaken for the BAR
    # refusal this file is about.
    s._streams_trade_ticks = streams_trade_ticks
    s._streams_quote_ticks = True
    # Read by the subscription path since #836: on a PACED provider a live subscribe is a
    # historical request and leaves through the queue. This file counts SYNCHRONOUS subscribes,
    # so it declares the unmetered rate production gives Alpaca — the pacing itself is pinned in
    # test_boot_does_not_burst_the_venue.py.
    s._hist_rate = float("inf")
    s._failed_requests = FailedRequests()
    s._granularities = list(GRANULARITIES)
    s._bar_types = []
    s._data_client_id = None
    from api.subscription_ledger import SubscriptionLedger

    s._subscriptions = SubscriptionLedger()
    s._realtime_subscribed = set()
    s._realtime_budget_warned = set()
    s.subscribe_trade_ticks = lambda *a, **k: None
    s.subscribe_quote_ticks = lambda *a, **k: None
    s.subscribe_bars = lambda bt, *a, **k: rec.subscribed.append(str(bt))
    s._request_bars_paced = lambda bt, **k: rec.requested.append(str(bt))
    s._window = lambda i, g: (None, None)
    s._realtime_budget = lambda: float("inf")
    s._cfg = types.SimpleNamespace(data_provider="test")
    return s, rec


def _internal_granularities():
    return [g for g, (_schema, suffix) in GRANULARITIES.items() if "INTERNAL" in str(suffix)]


def test_the_fixture_can_express_the_bug():
    """Vacuity guard. If the granularity table ever stops containing an internally-aggregated entry,
    every assertion below passes by having nothing to violate it — which is how kumo-strategies'
    truncation test passed twice with the look-ahead deliberately reintroduced."""
    assert _internal_granularities(), (
        "no INTERNAL granularity is configured, so this whole file asserts nothing"
    )


def test_a_venue_with_NO_TAPE_does_not_subscribe_the_internal_series():
    from nautilus_trader.model.identifiers import InstrumentId

    s, rec = _strategy(False)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    # Assert on the SOURCE tag Nautilus itself renders, not on our granularity names: the bar type
    # string is what the DataEngine routes on, so it is the thing that decides tape or no tape.
    assert not [b for b in rec.subscribed if "INTERNAL" in b], (
        f"subscribed an internally-aggregated series on a venue that streams no trade ticks: "
        f"{[b for b in rec.subscribed if 'INTERNAL' in b]} — the subscription is accepted and then "
        f"sits empty forever, which is indistinguishable from a quiet market (#612)"
    )


def test_the_SAME_path_still_subscribes_them_when_the_venue_HAS_a_tape():
    """The other direction. A guard that refuses everything would pass the test above."""
    from nautilus_trader.model.identifiers import InstrumentId

    s, rec = _strategy(True)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    assert [b for b in rec.subscribed if "INTERNAL" in b], (
        "no internally-aggregated series was subscribed on a venue that DOES stream trade ticks — "
        "the refusal is unconditional, which takes Alpaca's working charts dark to fix IBKR's"
    )


def test_the_EXTERNAL_series_survive_a_venue_with_no_tape():
    from nautilus_trader.model.identifiers import InstrumentId

    s, rec = _strategy(False)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    assert [b for b in rec.subscribed if "EXTERNAL" in b], (
        "a venue with no trade tape subscribed NO live bars at all — m1/d1 are venue-streamed and "
        "are what the rotation strategies trade off, so this would take the stack dark"
    )


def test_HISTORY_is_still_requested_for_the_refused_granularities():
    """The refusal is about the live stream only. History is a REST request the aggregation source
    never reaches, so a fix that suppressed both would leave the chart just as blank."""
    from nautilus_trader.model.identifiers import InstrumentId

    s, rec = _strategy(False)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    assert [b for b in rec.requested if "INTERNAL" in b], (
        "no historical bars were requested for the internally-aggregated granularities — refusing "
        "the live subscribe must not also cancel the backfill that still works"
    )


def test_the_REFUSAL_REACHES_HEALTH_rather_than_being_silence():
    """The entire defect was silence. An operator seeing a blank sparkline must be able to read WHY
    from the same place every other failing request is reported."""
    from nautilus_trader.model.identifiers import InstrumentId

    s, rec = _strategy(False)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    rows = s._failed_requests.as_rows()
    assert rows, "the node refused a granularity and reported nothing anywhere"
    kinds = {r.get("kind") for r in rows}
    assert "aggregation" in kinds, f"refusals not recorded under their own kind: {kinds}"
    notes = " ".join(str(r.get("note", "")) for r in rows)
    assert "trade tick" in notes, (
        f"the recorded reason does not name the missing tape, so the row says a granularity failed "
        f"without saying what would fix it: {notes!r}"
    )


def test_the_PROVIDERS_DECLARATION_is_what_the_plan_is_BUILT_FROM():
    """The hop the behavioural tests above cannot reach, and it was measurably open.

    Those tests set `_aggregation` directly, because constructing a `UiFeedStrategy` needs a live
    Nautilus kernel. So replacing the constructor's `streams_trade_ticks=streams_trade_ticks` with a
    hardcoded `True` — the exact defect, every venue assumed to have a tape — left all six GREEN.
    Verified by mutation, not assumed.

    This is deliberately a source assertion. The thing being pinned is that an ARGUMENT TRAVELS, and
    a constant substituted for it is invisible to any test that supplies the downstream value itself.
    Same family as the `daily_bars_cover` forwarding check in
    `test_daily_bar_semantics_is_declared.py`, and for the same reason.
    """
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.UiFeedStrategy.__init__)))
    fn = tree.body[0]
    param_names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "streams_trade_ticks" in param_names, (
        "UiFeedStrategy takes no `streams_trade_ticks`, so the provider's declaration cannot reach "
        "the aggregation split no matter what build_node passes (#612)"
    )

    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "aggregation_plan"
    ]
    assert len(calls) == 1, f"expected exactly one aggregation_plan call in __init__, found {len(calls)}"
    kw = {k.arg: k.value for k in calls[0].keywords}
    arg = kw.get("streams_trade_ticks")
    assert isinstance(arg, ast.Name) and arg.id == "streams_trade_ticks", (
        f"the aggregation split is built from {ast.dump(arg) if arg else 'nothing'} rather than from "
        f"the constructor's own parameter — the provider declares, and the node ignores it"
    )


def test_BUILD_NODE_forwards_the_spec_field_to_the_strategy():
    """And the hop before that one. The spec REQUIRES the field, so every provider answers — but a
    required field nobody forwards is the shape of #581, where compose declared two variables into
    a container that read neither."""
    import ast
    import inspect

    import api.engine_node as mod

    src = ast.unparse(ast.parse(inspect.getsource(mod.build_node)))
    assert "streams_trade_ticks=spec.streams_trade_ticks" in src, (
        "build_node constructs the strategy without forwarding the provider's trade-tape "
        "declaration, so the constructor default ('never told us') wins on every real node and "
        "Alpaca's internal granularities go dark alongside IBKR's"
    )
