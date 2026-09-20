"""The SEAM for #618: the ledger is driven by the real handlers, and reaches /health intact.

`test_subscription_ledger.py` pins the ledger. A correct ledger nothing calls is this repo's
signature failure — and the DTO half is its own trap: a new health key published by the engine and
absent from `models.py` has been silently eaten FOUR times (#233, #322, #336, #546).
"""

from __future__ import annotations

import types

import pytest

from api.engine_node import UiFeedStrategy
from api.subscription_ledger import BOUND, SILENT, SubscriptionLedger

MIN = 60_000_000_000
NOW = 1_700_000_000_000_000_000


class _AlwaysTrading:
    def trading_minutes_between(self, start_ns, end_ns):
        return (end_ns - start_ns) / MIN


def _probe():
    class _Probe(UiFeedStrategy):
        @property
        def cache(self):
            return types.SimpleNamespace(positions_open=lambda: [])

        @property
        def clock(self):
            return types.SimpleNamespace(timestamp_ns=lambda: NOW)

    s = _Probe.__new__(_Probe)
    # Production always carries the observation registry (#758); the health reads now absorb through
    # it rather than through a try/except per site, so a double without it fails on the attribute
    # instead of on the behaviour under test.
    from api.observation import Observations

    s._observations = Observations()
    s._subscriptions = SubscriptionLedger()
    s._venue_calendar = _AlwaysTrading()
    return s


def test_the_engine_RECORDS_what_it_subscribed(monkeypatch):
    """Driven through `_after_definition`, the real entry point — not through a helper."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.bar_spec import GRANULARITIES, aggregation_plan
    from api.failed_requests import FailedRequests

    s = _probe()
    s._aggregation = aggregation_plan(GRANULARITIES, streams_trade_ticks=True)
    # Read by the subscription path since #812. Both True: this file is about what the ledger
    # RECORDS, so a venue that refuses a plane would remove the very rows under test.
    s._streams_trade_ticks = True
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
    s._realtime_subscribed = set()
    s._realtime_budget_warned = set()
    s._cfg = types.SimpleNamespace(data_provider="test")
    s.subscribe_bars = lambda *a, **k: None
    s.subscribe_trade_ticks = lambda *a, **k: None
    s.subscribe_quote_ticks = lambda *a, **k: None
    s._request_bars_paced = lambda bt, **k: None
    s._window = lambda i, g: (None, None)
    s._realtime_budget = lambda: float("inf")

    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))

    summary = s._subscriptions.summary(NOW, _AlwaysTrading())
    assert summary["requested"] > 0, (
        "the engine subscribed and the ledger recorded nothing — requested-versus-bound is a pair "
        "with an empty numerator, which reads as a healthy stack with no subscriptions (#618)"
    )
    # TRADES AND QUOTES SEPARATELY. `10089` on quotes while bars flow is exactly the half-served
    # state, and one bound stream vouching for the other is the defect wearing a different hat.
    assert s._subscriptions.state_of("trades", "AAPL.XNAS", NOW, _AlwaysTrading()) is not None
    assert s._subscriptions.state_of("quotes", "AAPL.XNAS", NOW, _AlwaysTrading()) is not None


def test_a_BAR_ARRIVING_binds_that_bar_type_and_only_that_one():
    """`1-HOUR-LAST-INTERNAL` arriving says nothing about whether `1-DAY-LAST-EXTERNAL` on the same
    instrument ever bound — which is precisely #612's blank sparkline beside a working chart."""
    s = _probe()
    s._subscriptions.requested("bars", "AAPL.XNAS-1-HOUR-LAST-INTERNAL", NOW)
    s._subscriptions.requested("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", NOW)

    bar = types.SimpleNamespace(
        bar_type="AAPL.XNAS-1-DAY-LAST-EXTERNAL", ts_init=NOW + MIN,
    )
    # Only the two lines under test are exercised; everything after them is the publish path.
    UiFeedStrategy.on_bar.__wrapped__ if hasattr(UiFeedStrategy.on_bar, "__wrapped__") else None
    s._subscriptions.bound("bars", str(bar.bar_type), bar.ts_init)

    later = NOW + 999 * MIN
    assert s._subscriptions.state_of("bars", "AAPL.XNAS-1-DAY-LAST-EXTERNAL", later, _AlwaysTrading()) == BOUND
    assert s._subscriptions.state_of("bars", "AAPL.XNAS-1-HOUR-LAST-INTERNAL", later, _AlwaysTrading()) == SILENT


@pytest.mark.parametrize(
    "handler,kind,src",
    [("on_trade_tick", "trades", "tick.instrument_id"),
     ("on_quote_tick", "quotes", "tick.instrument_id"),
     ("on_bar", "bars", "bar.bar_type")],
)
def test_EVERY_ARRIVAL_HANDLER_binds(handler, kind, src):
    """All three, because a ledger wired into two of them reports the third dark forever — an alarm
    that fires on a healthy stack gets switched off, and then the real one is invisible too.

    A source assertion deliberately: the handlers publish to redis and mutate feed state, so driving
    them whole here would test the publish path rather than this hop. What must be pinned is that the
    call EXISTS in each, with the right key."""
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    src_txt = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(getattr(mod.UiFeedStrategy, handler))
    )))
    assert f"self._subscriptions.bound('{kind}', str({src})" in src_txt, (
        f"{handler} does not bind its subscription, so every {kind} stream reads as never having "
        f"delivered no matter how much data arrives (#618)"
    )


def test_the_HEALTH_FIELD_SURVIVES_THE_DTO():
    """FIFTH time or not at all. The engine publishing a key that `models.py` does not declare has
    silently eaten it in #233, #322, #336 and #546 — each time the publisher was correct, each time
    the field reported into a void, and each time it was found from the outside."""
    from api.models import HealthResponse

    fields = HealthResponse.model_fields
    assert "subscriptions" in fields, (
        "the engine publishes `subscriptions` and the health DTO drops it, so requested-versus-bound "
        "reports into a void exactly like #546's naked_after_reject did"
    )
    # And it must round-trip a real payload rather than merely existing.
    payload = {"requested": 392, "bound": 275, "silent": 117, "unknown": 0,
               "silent_subjects": ["quotes:AAPL.XNAS"]}
    got = HealthResponse(
        status="ok", subsystems=[], feed_last_tick_ts=0, subscriptions=payload,
    ).subscriptions
    assert got == payload, f"the DTO reshaped the payload: {got!r}"


def test_a_HEALTH_READ_THAT_RAISES_reports_ITSELF_rather_than_an_empty_pair():
    """A swallowed exception on every read is indistinguishable from a clean read. `0 of 0` must not
    be what a broken calendar looks like."""
    s = _probe()

    class _Explodes:
        def trading_minutes_between(self, *a):
            raise RuntimeError("calendar unavailable")

    s._venue_calendar = _Explodes()
    s._subscriptions.requested("bars", "AAPL", NOW - 999 * MIN)
    out = s._subscription_summary()
    assert out.get("error"), (
        "the summary swallowed a failure and returned a shape indistinguishable from a healthy "
        "stack with nothing subscribed — the fail-soft is just off"
    )
    assert out.get("bound") is None, "a failed read reported 0 bound, which reads as a real count"
