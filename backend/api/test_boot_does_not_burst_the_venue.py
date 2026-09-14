"""EVERY historical call leaves through ONE paced queue, held positions first, lanes included (#836).

MEASURED on staging2 (IBKR), 2026-09-09, boot at 14:03:40 UTC — all inside ONE SECOND:

    MOMENTUM lane    105 x request_bars   180-day daily warmup, its whole pool
    BCTROT lane      105 x request_bars   the SAME 105 symbols, again
    cockpit feed     297 x subscribe_bars keepUpToDate (185 daily + 112 minute)
    total            ~507 reqHistoricalData against IB's documented 60 per 10 minutes

IB answered 39 and went silent: 66 `Request timed out`, no error code, and the daily backfill stayed
at 39 batches for the rest of the session while the pacer kept issuing 6/min into a dead pipe. The
112 minute subscriptions were never serviced — that is the UI's "feed 14h" — and seven positions
claimed that morning sat `no_atr`, unprotected, for the whole session.

THE PREMISE THIS CORRECTS. `_after_definition` said, and a test pinned, that "`subscribe_bars` is a
STREAMING subscription, not a historical request: it does not consume IB's ~60-per-10-minutes
historical allowance". On IB it is exactly a historical request: the shipped adapter's
`subscribe_historical_bars` issues `reqHistoricalData(..., keepUpToDate=True)` for every bar size
other than 5s. 297 of them in one second was most of the burst. The intent of that test — live
data must not starve behind a five-hour backfill — is kept by PRIORITY, not by exemption.

WHAT THE LANES NEED. Their warmup calls come from the pinned kumo-strategies runners
(`momentum_rotation.py:297`, `self.request_bars(bt, start)`), outside cockpit's pacer. Cockpit
constructs those objects and already passes every one through `register_strategy`, so that is where
their `request_bars` / `subscribe_bars` are wrapped into the shared queue — issued later THROUGH THE
LANE'S OWN BOUND METHOD, so the response routes back to the lane, with `join_request=True` so the
two lanes' identical requests are answered once by Nautilus rather than twice by IB.

ORDER, worst-starved first:
    0  live subscribe, HELD position     the book's price and its protection sizing
    1  live subscribe, everything else   the minute plane
    2  history, HELD position            ATR for the reconciler
    3  history, everything else          lane warmups, chart depth
"""

from __future__ import annotations

import types

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from api.bar_spec import GRANULARITIES, aggregation_plan
from api.engine_node import UiFeedStrategy
from api.failed_requests import FailedRequests
from api.observation import Observations
from api.subscription_ledger import SubscriptionLedger

_NOW = 1_700_000_000_000_000_000


class _Clock:
    def __init__(self):
        self.timers = []

    def timestamp_ns(self):
        return _NOW

    def set_timer(self, name, **kw):
        self.timers.append(name)


def _probe(*, rate: float, held: set[str] = frozenset()):
    """A real UiFeedStrategy with the Nautilus surface replaced and EVERY outbound call recorded."""
    class _Probe(UiFeedStrategy):
        @property
        def cache(self):
            pos = [types.SimpleNamespace(instrument_id=InstrumentId.from_str(h)) for h in held]
            return types.SimpleNamespace(positions_open=lambda: pos)

        @property
        def clock(self):
            return self._clk

        @property
        def id(self):
            # Production always has one — the kernel assigns it at registration. The queue keys on
            # it so the feed's and a lane's request for the same series are two entries, not one.
            return "MANUAL-001"

    s = _Probe.__new__(_Probe)
    s._clk = _Clock()
    s._hist_rate = rate
    s._bar_request_q = []          # a heap since #836 — a deque here would fail heapq, correctly
    s._bar_request_seq = 0
    s._bar_request_seen = set()
    s._aggregation = aggregation_plan(GRANULARITIES, streams_trade_ticks=False)
    s._failed_requests = FailedRequests()
    s._granularities = ["1m", "1d"]
    s._bar_types = []
    s._data_client_id = None
    s._subscriptions = SubscriptionLedger()
    s._observations = Observations()  # production always carries it (#758); the drain records through it
    s._realtime_subscribed = set()
    s._realtime_budget_warned = set()
    s._streams_trade_ticks = False
    s._streams_quote_ticks = False
    s._sibling_strategies = {}
    s._trade_cycles = {}
    s._display_hold_secs = 0.0
    s._feed_started_ns = _NOW
    s.calls: list[tuple[str, str]] = []
    s.subscribe_bars = lambda bt, **k: s.calls.append(("subscribe", str(bt)))
    s.request_bars = lambda bt, **k: s.calls.append(("request", str(bt)))
    s._window = lambda i, g: (None, None)
    s._realtime_budget = lambda: float("inf")
    s._cfg = types.SimpleNamespace(data_provider="test")
    return s


def _drain(s, ticks: int):
    for _ in range(ticks):
        s._drain_bar_requests()


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTIES
# ---------------------------------------------------------------------------------------------

def test_the_fixture_is_wide_enough_for_the_rate_to_BIND():
    """At 6/min and a 10s drain, one tick releases one call. Twenty instruments x (1m+1d) x
    (subscribe+history) is 80 calls. If the fixture were smaller than one tick's allowance the
    'not synchronous' assertions below could pass against an unpaced node."""
    from api.engine_node import _BAR_DRAIN_SECS
    allowance = max(1, int(6.0 * _BAR_DRAIN_SECS / 60.0))
    assert allowance < 80, "the fixture does not exceed one tick's allowance — nothing can bind"


# ---------------------------------------------------------------------------------------------
# THE BURST
# ---------------------------------------------------------------------------------------------

def test_a_PACED_provider_does_not_subscribe_synchronously_in_after_definition():
    """The 297-in-one-second half. `subscribe_bars` on IB is `reqHistoricalData(keepUpToDate=True)`
    and counts against the same allowance as history; it must leave through the queue."""
    s = _probe(rate=6.0)
    for i in range(20):
        s._after_definition(InstrumentId.from_str(f"SYM{i}.XNAS"))
    sync_subscribes = [c for c in s.calls if c[0] == "subscribe"]
    assert sync_subscribes == [], (
        f"{len(sync_subscribes)} live subscriptions went out synchronously — on IB each is a "
        f"historical request, and 297 of them in one second is the boot burst (#836)"
    )


def test_an_UNPACED_provider_is_byte_for_byte_unchanged():
    """Alpaca declares no rate. Its subscriptions are real WebSocket streams that cost nothing, and
    a queue in front of them would reintroduce the five-hour starvation #617's follow-up removed."""
    s = _probe(rate=float("inf"))
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))
    assert ("subscribe", "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL") in s.calls, "Alpaca lost its live subscription"
    assert ("subscribe", "AAPL.XNAS-1-DAY-LAST-EXTERNAL") in s.calls


def test_HELD_positions_leave_the_queue_FIRST_and_live_before_history():
    """The order is the safety property. A held position's live bar is its price and its ATR is its
    protection; both starved behind 210 lane warmups for a whole session on 2026-09-09."""
    s = _probe(rate=6.0, held={"HELD.XNAS"})
    for sym in ("AAA.XNAS", "BBB.XNAS", "HELD.XNAS", "CCC.XNAS"):
        s._after_definition(InstrumentId.from_str(sym))
    _drain(s, 4)
    first_four = s.calls[:4]
    kinds = [(k, bt.split(".")[0], bt.split("-")[1]) for k, bt in first_four]
    assert kinds[0][1] == "HELD" and kinds[0][0] == "subscribe", f"first out was {first_four[0]}"
    assert all(sym == "HELD" for _, sym, _ in kinds[:2]), (
        f"the held position's two live planes did not lead the queue: {first_four}"
    )


def test_the_LEDGER_stamps_a_subscription_when_it_is_ISSUED_not_when_it_is_queued():
    """`state_of` turns UNKNOWN into SILENT after 30 trading minutes from `requested_ns`. Stamping at
    enqueue time would call a subscription silent while it was still waiting its turn — the pacer
    manufacturing the alarm it exists to prevent."""
    s = _probe(rate=6.0)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))
    cal = types.SimpleNamespace(day=lambda d: None)
    assert s._subscriptions.state_of("bars", "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL", _NOW, cal) is None, (
        "recorded as requested before anything was sent"
    )
    _drain(s, 4)
    assert s._subscriptions.state_of("bars", "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL", _NOW, cal) is not None


# ---------------------------------------------------------------------------------------------
# THE LANES
# ---------------------------------------------------------------------------------------------

class _Lane:
    """A kumo-strategies runner as cockpit sees it: an object with its own bound request methods."""

    def __init__(self, sid):
        self.id = sid
        self.issued: list[tuple] = []

    def request_bars(self, bar_type, start=None, end=None, **kw):
        self.issued.append(("request", str(bar_type), kw.get("join_request")))

    def subscribe_bars(self, bar_type, **kw):
        self.issued.append(("subscribe", str(bar_type)))


def test_a_REGISTERED_lane_has_its_warmup_routed_through_the_shared_queue():
    """The 210-in-one-second half. The lanes are built by cockpit and already pass through
    `register_strategy`; that is where their outbound calls are captured."""
    s = _probe(rate=6.0)
    lane = _Lane("MOMENTUM-002")
    s.register_strategy(lane.id, lane)
    for i in range(10):
        lane.request_bars(f"S{i}.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    assert lane.issued == [], f"the lane's warmup went straight to the venue: {lane.issued[:3]}"
    _drain(s, 3)
    assert len(lane.issued) == 3, f"expected one lane request per drain tick, got {lane.issued}"


def test_the_lanes_request_is_issued_THROUGH_ITS_OWN_METHOD_so_the_answer_routes_back():
    """Nautilus routes a request's response to the actor that issued it. The feed must not issue the
    lane's request itself — the bars would land in the feed's `on_historical_data` and the lane
    would never clear warmup."""
    s = _probe(rate=6.0)
    lane = _Lane("BCTROT-004")
    s.register_strategy(lane.id, lane)
    lane.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    _drain(s, 1)
    assert lane.issued and lane.issued[0][0] == "request"
    assert not any(k == "request" and "X.XNAS" in bt for k, bt in s.calls), (
        "the FEED issued the lane's request — the response will route to the wrong actor"
    )


def test_lane_requests_carry_NO_join_request_because_nautilus_parks_joined_legs():
    """MEASURED on staging2, boot 15:45Z 2026-09-09: 173 lane warmup requests issued through the queue
    with join_request=True, ZERO reached the data client, zero bars delivered to the lanes in 18 min,
    no error anywhere. In NautilusTrader 1.229 `join_request=True` marks a request as a LEG of a
    `RequestJoin` and the DataEngine PARKS it (`engine.pyx` `_handle_request`: `if state.join_request:
    self._requests[request.id] = request; return`) until a join arrives — which nothing here sends.
    It is not "join identical in-flight requests"; that premise was mine, and it was wrong.

    So the wrapper forwards the lane's call exactly as the lane made it."""
    s = _probe(rate=6.0)
    lane = _Lane("MOMENTUM-002")
    s.register_strategy(lane.id, lane)
    lane.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    _drain(s, 1)
    assert lane.issued == [("request", "X.XNAS-1-DAY-LAST-EXTERNAL", None)], (
        f"a lane request left with a join_request the DataEngine would park: {lane.issued}"
    )


def test_lane_warmups_rank_BEHIND_a_held_positions_live_subscription():
    s = _probe(rate=6.0, held={"HELD.XNAS"})
    lane = _Lane("MOMENTUM-002")
    s.register_strategy(lane.id, lane)
    lane.request_bars("W1.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    lane.request_bars("W2.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    s._after_definition(InstrumentId.from_str("HELD.XNAS"))
    _drain(s, 1)
    assert lane.issued == [], "a lane warmup left before the held position's live subscription"
    assert s.calls and s.calls[0][0] == "subscribe" and "HELD" in s.calls[0][1], s.calls[:2]


def test_registering_a_lane_on_an_UNPACED_provider_wraps_nothing():
    """Alpaca again: no rate, no queue, the lane's calls go out exactly as before."""
    s = _probe(rate=float("inf"))
    lane = _Lane("MOMENTUM-002")
    s.register_strategy(lane.id, lane)
    lane.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    assert lane.issued and lane.issued[0][0] == "request"


def test_a_HELD_positions_HISTORY_leaves_before_any_UNHELD_live_subscription():
    """Protection is history: ATR needs 14 daily bars, and a position without it is REAL CAPITAL
    with no stop. On 2026-09-09 seven positions sat `no_atr` for the whole session.

    At 6/min, ranking every live subscription (297 that boot) ahead of held-position history would
    leave those seven unprotected for ~50 minutes. Behind only the held position's OWN live planes
    it is under a minute — so the order is: held live, held history, lanes, display."""
    s = _probe(rate=6.0, held={"HELD.XNAS"})
    for sym in ("AAA.XNAS", "BBB.XNAS", "HELD.XNAS", "CCC.XNAS"):
        s._after_definition(InstrumentId.from_str(sym))
    keys = [entry[5] for entry in s._bar_request_q]
    held = [k for k in keys if "HELD" in k[2]]
    # FIXTURE PROPERTY: the held symbol has BOTH a live plane and a history request queued, and
    # unheld live subscriptions exist to jump the queue — otherwise the order cannot be violated.
    assert any(k[0] == "request" for k in held), keys
    assert any(k[0] == "subscribe" for k in held), keys
    assert any(k[0] == "subscribe" and "HELD" not in k[2] for k in keys), keys
    _drain(s, len(held))
    assert all("HELD" in bt for _, bt in s.calls), (
        f"an unheld live subscription left before the held position's history: {s.calls}"
    )
    assert any(kind == "request" for kind, _ in s.calls), s.calls


def test_a_venue_call_that_RAISES_is_RECORDED_and_the_drain_continues():
    """The drain runs on the live path. A call that raises must neither stop the queue nor vanish
    into a log line — it is recorded on the observation registry (CLAUDE.md 2026-08-31: observation
    is a mechanism, not a try/except), where /health reads it."""
    s = _probe(rate=60.0)  # allowance 10 per tick: both entries leave on one drain

    def _boom(bt, **k):
        raise RuntimeError("venue said no")

    s._enqueue_paced(3, _boom, ("BOOM.XNAS-1-DAY-LAST-EXTERNAL",), {}, key=("request", "t", "BOOM"))
    s._enqueue_paced(3, s.request_bars, ("OK.XNAS-1-DAY-LAST-EXTERNAL",), {}, key=("request", "t", "OK"))
    _drain(s, 1)
    assert s.calls == [("request", "OK.XNAS-1-DAY-LAST-EXTERNAL")], "the drain stopped at the raise"
    summary = s._observations.summary()
    names = {row["observer"] for row in summary["rows"]}
    assert summary["failing"] == 1 and "paced_venue_call" in names, summary


def test_the_DISPLAY_hold_does_not_park_held_positions_or_lane_warmups():
    """KUMO_DISPLAY_BACKFILL_HOLD_SECS was written for a display-only queue (#618 step 2). Since
    #836 the same queue carries a held position's live plane, its ATR history and the lanes'
    warmups. MEASURED on staging2 at the 14:47Z boot on 2026-09-09: hold 900, and NOTHING left
    the queue — 348 instruments loaded, 0 requests, 15 positions `no_price`, for the whole hold.

    The hold parks DISPLAY (priority 3) only. The heap is priority-ordered, so the drain issues
    until the first priority-3 entry and stops there while the hold stands."""
    s = _probe(rate=60.0, held={"HELD.XNAS"})  # allowance 10 per tick: everything could leave
    s._display_hold_secs = 900.0
    s._feed_started_ns = _NOW - 10 * 10**9  # 10 s into a 900 s hold
    s._after_definition(InstrumentId.from_str("HELD.XNAS"))
    s._after_definition(InstrumentId.from_str("DISP.XNAS"))
    lane = _Lane("MOMENTUM-002")
    s.register_strategy(lane.id, lane)
    lane.request_bars("W1.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    prios = sorted({e[0] for e in s._bar_request_q})
    assert prios == [0, 1, 2, 4, 5], f"fixture must hold held/lane/display tiers for the hold to discriminate: {prios}"
    _drain(s, 1)
    left = [bt for _, bt in s.calls]
    assert any("HELD" in bt for bt in left), f"the hold parked the held position: {s.calls}"
    assert lane.issued == [("request", "W1.XNAS-1-DAY-LAST-EXTERNAL", None)], (
        f"the hold parked the lane's warmup: {lane.issued}"
    )
    assert not any("DISP" in bt for _, bt in s.calls), f"display work left during the hold: {s.calls}"
    assert {e[0] for e in s._bar_request_q} == {4, 5}, "display entries were dropped, not held"


def test_lane_WARMUPS_leave_before_any_DISPLAY_subscription():
    """MEASURED 2026-09-09, 15:18Z boot: after the held tier, the queue spent its 6/min on display and
    lane LIVE subscriptions (~300 of them, ~50 min) before a single lane warmup request left. BCTROT-004
    decides at 17:05Z off 180 days of daily history; a lane that is not warm by its slot refuses — the
    display plane (#618: "the display plane can wait; the lanes cannot") had jumped the queue.

    Order: held live → held history → lane warmups → lane live subs → display live → display history."""
    s = _probe(rate=6.0)
    for sym in ("AAA.XNAS", "BBB.XNAS"):
        s._after_definition(InstrumentId.from_str(sym))          # display: 2 subs + 2 requests each
    lane = _Lane("BCTROT-004")
    s.register_strategy(lane.id, lane)
    lane.request_bars("W1.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    lane.subscribe_bars("W1.XNAS-1-DAY-LAST-EXTERNAL")
    assert s.calls == [] and lane.issued == [], "fixture: everything must be queued, nothing issued"
    _drain(s, 2)
    assert lane.issued[:2] == [("request", "W1.XNAS-1-DAY-LAST-EXTERNAL", None),
                               ("subscribe", "W1.XNAS-1-DAY-LAST-EXTERNAL")], (
        f"lane work did not lead the queue: lane={lane.issued} feed={s.calls}"
    )
    assert s.calls == [], f"display work left before the lane was warm: {s.calls}"


def test_two_lanes_IDENTICAL_requests_take_TWO_slots_because_each_is_a_venue_call():
    """Without a RequestJoin there is nothing that collapses two lanes' identical requests: each is
    its own reqHistoricalData and must take its own 10 s slot, or the pacer issues 2 per step and the
    burst is back at half size. Two lanes sharing a 105-symbol pool cost 210 slots — 35 minutes."""
    s = _probe(rate=6.0)   # allowance 1 per tick
    a, b = _Lane("MOMENTUM-002"), _Lane("BCTROT-004")
    s.register_strategy(a.id, a)
    s.register_strategy(b.id, b)
    a.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    b.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", start=_NOW - 10**15)
    assert len(s._bar_request_q) == 2, "fixture: two distinct entries for one series"
    _drain(s, 1)
    assert len(a.issued) + len(b.issued) == 1, f"two venue calls left in one step: {a.issued} {b.issued}"
    _drain(s, 1)
    assert len(a.issued) + len(b.issued) == 2


def test_the_lane_wrapper_forwards_EVERY_positional_argument_nautilus_accepts():
    """codex on 5f15fdd (BLOCKER): the wrapper named only `bar_type, start, end`, so a lane calling
    `request_bars(bt, start, end, 100)` — `limit` positional, which `Actor.request_bars` accepts —
    would have `100` land in the slot reserved for the bound method: a TypeError recorded on the
    drain, no venue call, no warmup. The pinned lanes pass only `start` today, so it passed by accident.
    The wrapper forwards the call exactly as made: every positional, every keyword."""
    s = _probe(rate=6.0)
    lane = _Lane("MOMENTUM-002")
    lane.calls: list[tuple] = []
    lane.request_bars = lambda *a, **k: lane.calls.append(("request", a, k))     # type: ignore[method-assign]
    lane.subscribe_bars = lambda *a, **k: lane.calls.append(("subscribe", a, k))  # type: ignore[method-assign]
    s.register_strategy(lane.id, lane)
    lane.request_bars("X.XNAS-1-DAY-LAST-EXTERNAL", _NOW - 10**15, None, 100)
    lane.subscribe_bars("Y.XNAS-1-DAY-LAST-EXTERNAL", None, False, {"k": 1})
    _drain(s, 2)
    assert lane.calls == [
        ("request", ("X.XNAS-1-DAY-LAST-EXTERNAL", _NOW - 10**15, None, 100), {}),
        ("subscribe", ("Y.XNAS-1-DAY-LAST-EXTERNAL", None, False, {"k": 1}), {}),
    ], f"the wrapper reshaped the lane's call: {lane.calls}; observations={s._observations.summary()['rows']}"
