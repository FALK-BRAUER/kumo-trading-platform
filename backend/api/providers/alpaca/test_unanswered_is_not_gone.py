"""#354 — an UNANSWERED venue question must never become REJECTED. Two holes beside #791's batch guard.

(B) Nautilus 1.229 `live/execution_engine.py:1426 _resolve_order_not_found_at_venue` calls
    `client.generate_order_status_report(command)`; `None` OR a raised exception both fall through to
    `create_order_rejected_event(reason="ORDER_NOT_FOUND_AT_VENUE")` for an ACCEPTED order. Our adapter's
    targeted lookup raised on a transport failure, so one timed-out GET after five "missing" cycles made
    a resting stop a corpse (#807: 16 of them, 13 fired).
(C) `_submit_order` rejected on ANY failure of `POST /v2/orders`, a timeout included — the request may
    have landed. The cache holds 8 such: `PROT-SELL-*` "Connection timeout to host …/v2/orders".

DRIVEN AT THE SEAMS: (B) through the REAL Nautilus resolver (unbound, duck-typed self) with the REAL
adapter method behind a client double, on a REAL ACCEPTED `Order` from the #807 fixture bytes; (C)
through `_place_order` with the same double shape the bracket tests use. The doubles can represent
"raised" DISTINCTLY from "returned None" — conflating those two is the entire bug (#354's own note).
"""
from __future__ import annotations

import asyncio
import types
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, InstrumentId, VenueOrderId

from api import cache_repair as cr
from api.providers.alpaca.exec_client import AlpacaExecutionClient
from api.providers.alpaca.http import AlpacaHttpError

FIXTURES = Path(__file__).parents[2] / "fixtures" / "repair_807"
PATH_CORPSE = "PROT-SELL-PATH-XNYS-b3ea77ee"
VENUE_ID = "13729305-dbb2-4a2b-a0b2-74bce6e9f0fd"


@pytest.fixture(scope="module")
def image():
    return cr.CacheImage.from_fixture(FIXTURES / "redis.json")


@pytest.fixture(scope="module")
def accepted_order(image):
    """PATH's stop as it stood BEFORE the 09-04 rejection: ACCEPTED, resting at the venue."""
    venue = cr.VenueTruth.from_fixture(FIXTURES / "venue.json")
    instrument = cr.to_obj(image.instruments["PATH.XNYS"])
    vo = venue.orders[PATH_CORPSE]
    resting = cr.VenueOrder(vo.client_order_id, "new", Decimal(0), None, None, vo.venue_order_id)
    order = cr.replay_order(cr.revive_order(image, PATH_CORPSE, resting, instrument).events)
    assert order.status == OrderStatus.ACCEPTED and str(order.venue_order_id) == VENUE_ID
    return order


RAW_ACCEPTED = {
    "id": VENUE_ID, "client_order_id": PATH_CORPSE, "symbol": "PATH", "status": "accepted", "side": "sell",
    "type": "trailing_stop", "time_in_force": "gtc", "qty": "112", "filled_qty": "0", "filled_avg_price": None,
    "limit_price": None, "stop_price": "12.875886", "trail_percent": None, "trail_price": "1.11", "hwm": "13.99",
}


def _log():
    lines: list[tuple[str, str]] = []
    return types.SimpleNamespace(
        debug=lambda m, *a, **k: lines.append(("debug", str(m))),
        info=lambda m, *a, **k: lines.append(("info", str(m))),
        warning=lambda m, *a, **k: lines.append(("warning", str(m))),
        error=lambda m, *a, **k: lines.append(("error", str(m))),
        exception=lambda m, *a, **k: lines.append(("exception", str(m))),
        lines=lines,
    )


def _adapter(order, *, by_coid, by_id=None):
    """The REAL `generate_order_status_report` on a duck-typed adapter whose HTTP is scripted."""

    async def _by_coid(coid):
        r = by_coid(coid)
        if isinstance(r, BaseException):
            raise r
        return r

    async def _by_id(vid):
        r = (by_id or by_coid)(vid)
        if isinstance(r, BaseException):
            raise r
        return r

    self = types.SimpleNamespace(
        _http=types.SimpleNamespace(get_order_by_client_order_id=_by_coid, get_order=_by_id),
        _cache=types.SimpleNamespace(
            order=lambda coid: order if str(coid) == str(order.client_order_id) else None,
            venue_order_id=lambda coid: order.venue_order_id,
            # `_client_order_id_for` resolves OUR id by venue id first (#242) — production's cache answers this.
            client_order_id=lambda vid: order.client_order_id if str(vid) == str(order.venue_order_id) else None,
        ),
        _log=_log(),
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000),
        _symbol_to_id={"PATH": InstrumentId.from_str("PATH.XNYS")},
        account_id=order.account_id,
        _venue_unanswered_lookups=0,
    )
    # The REAL methods, bound to this self — the production code path runs verbatim.
    self._HOLDABLE = AlpacaExecutionClient._HOLDABLE
    self._client_order_id_for = lambda raw: AlpacaExecutionClient._client_order_id_for(self, raw)
    self._parse_order_report = lambda raw: AlpacaExecutionClient._parse_order_report(self, raw)
    self._hold_or_none = lambda coid, cause: AlpacaExecutionClient._hold_or_none(self, coid, cause)
    self.generate_order_status_report = lambda cmd: AlpacaExecutionClient.generate_order_status_report(self, cmd)
    return self


def _resolve(order, adapter):
    """Drive Nautilus's OWN resolver — the thing that decides REJECTED — with the adapter behind it."""
    from nautilus_trader.live.execution_engine import LiveExecutionEngine

    events: list = []
    reports: list = []
    engine = types.SimpleNamespace(
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000),
        _log=_log(),
        _cache=types.SimpleNamespace(client_id=lambda coid: ClientId("ALPACA")),
        _clients={ClientId("ALPACA"): adapter},
        _ts_last_query={},
        _order_local_activity_ns={},
        _reconcile_order_report=lambda report, trades=(): reports.append(report),
        _handle_event_with_tracking=lambda ev: events.append(ev),
        _clear_recon_tracking=lambda coid, drop_last_query=True: None,
    )
    asyncio.run(LiveExecutionEngine._resolve_order_not_found_at_venue(engine, order))
    return events, reports, adapter


# --------------------------------------------------------------------------------------------------
# (B) the targeted lookup
# --------------------------------------------------------------------------------------------------


def test_fixture_the_resolver_rejects_when_the_lookup_raises_and_the_adapter_returns_nothing(accepted_order):
    """What Nautilus does with a raise — pinned on the installed package, with a client that raises."""
    from nautilus_trader.live.execution_engine import LiveExecutionEngine

    async def _boom(cmd):
        raise TimeoutError("venue unreachable")

    events: list = []
    engine = types.SimpleNamespace(
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 1), _log=_log(),
        _cache=types.SimpleNamespace(client_id=lambda coid: ClientId("ALPACA")),
        _clients={ClientId("ALPACA"): types.SimpleNamespace(generate_order_status_report=_boom)},
        _ts_last_query={}, _order_local_activity_ns={},
        _reconcile_order_report=lambda r, trades=(): None,
        _handle_event_with_tracking=lambda ev: events.append(ev),
        _clear_recon_tracking=lambda coid, drop_last_query=True: None,
    )
    asyncio.run(LiveExecutionEngine._resolve_order_not_found_at_venue(engine, accepted_order))
    assert [type(e).__name__ for e in events] == ["OrderRejected"]
    assert events[0].reason == "ORDER_NOT_FOUND_AT_VENUE"


def test_an_UNANSWERED_lookup_holds_the_order_instead_of_rejecting_it(accepted_order):
    adapter = _adapter(accepted_order, by_coid=lambda _: TimeoutError("venue unreachable"))
    events, reports, adapter = _resolve(accepted_order, adapter)
    assert not any(isinstance(e, OrderRejected) for e in events), events
    assert len(reports) == 1
    hold = reports[0]
    assert hold.order_status == OrderStatus.ACCEPTED
    assert str(hold.client_order_id) == PATH_CORPSE and str(hold.venue_order_id) == VENUE_ID
    assert hold.quantity == accepted_order.quantity and hold.filled_qty == accepted_order.filled_qty
    assert hold.trigger_price == accepted_order.trigger_price
    assert hold.price is None  # a trailing stop has no limit price; the HOLD must not invent one
    assert adapter._venue_unanswered_lookups == 1
    assert any(lvl == "error" and "unanswered" in msg.lower() for lvl, msg in adapter._log.lines)


def test_a_429_or_5xx_is_unanswered_too(accepted_order):
    for status in (429, 500, 503):
        adapter = _adapter(accepted_order, by_coid=lambda _, s=status: AlpacaHttpError("GET", "/v2/orders:by_client_order_id", s, "{}"))
        events, reports, _ = _resolve(accepted_order, adapter)
        assert not any(isinstance(e, OrderRejected) for e in events), status
        assert len(reports) == 1 and reports[0].order_status == OrderStatus.ACCEPTED


def test_an_ANSWERED_not_found_still_rejects(accepted_order):
    """404 is an answer: the venue says there is no such order. REJECT is then correct."""
    adapter = _adapter(accepted_order, by_coid=lambda _: AlpacaHttpError("GET", "/v2/orders:by_client_order_id", 404, '{"message":"order not found"}'))
    events, reports, adapter = _resolve(accepted_order, adapter)
    assert [type(e).__name__ for e in events] == ["OrderRejected"]
    assert reports == [] and adapter._venue_unanswered_lookups == 0


def test_an_ANSWERED_found_reconciles_the_venues_report(accepted_order):
    adapter = _adapter(accepted_order, by_coid=lambda _: RAW_ACCEPTED)
    events, reports, _ = _resolve(accepted_order, adapter)
    assert events == [] and len(reports) == 1
    assert reports[0].order_status == OrderStatus.ACCEPTED and str(reports[0].venue_order_id) == VENUE_ID


def test_our_id_is_asked_first_and_the_venue_id_only_when_ours_is_unknown_there(accepted_order):
    """Alpaca's `orders:by_client_order_id` answers with the CURRENT order for our id, so a replace is
    followed; a cached venue id after a replace names the OLD row ("replaced" → a CANCELED on a live
    order, review §5). The venue id is the fallback for a bracket leg Alpaca named itself (#242)."""
    asked: list[str] = []

    def by_coid(c):
        asked.append(f"coid:{c}")
        return RAW_ACCEPTED

    def by_id(v):
        asked.append(f"id:{v}")
        return RAW_ACCEPTED

    adapter = _adapter(accepted_order, by_coid=by_coid, by_id=by_id)
    _resolve(accepted_order, adapter)
    assert asked == [f"coid:{PATH_CORPSE}"]

    asked.clear()

    def coid_unknown(c):
        asked.append(f"coid:{c}")
        return AlpacaHttpError("GET", "/x", 404, "{}")

    adapter = _adapter(accepted_order, by_coid=coid_unknown, by_id=by_id)
    events, reports, _ = _resolve(accepted_order, adapter)
    assert asked == [f"coid:{PATH_CORPSE}", f"id:{VENUE_ID}"]
    assert events == [] and len(reports) == 1  # found under Alpaca's id: a bracket leg, not a corpse


def test_both_ids_unknown_is_None(accepted_order):
    from nautilus_trader.execution.messages import GenerateOrderStatusReport
    from nautilus_trader.core.uuid import UUID4

    adapter = _adapter(accepted_order, by_coid=lambda _: RAW_ACCEPTED)
    adapter._cache.venue_order_id = lambda coid: None
    cmd = GenerateOrderStatusReport(instrument_id=accepted_order.instrument_id, client_order_id=None, venue_order_id=None,
                                    command_id=UUID4(), ts_init=1)
    assert asyncio.run(adapter.generate_order_status_report(cmd)) is None


def test_an_unanswered_lookup_for_a_SUBMITTED_order_returns_None_not_a_hold(image):
    """A HOLD for a SUBMITTED order would make Nautilus mint an OrderAccepted with no venue id
    (`live/execution_engine.py:3265`, codex). None → Nautilus's inflight retries run on and resolve
    to its own "UNKNOWN" — that is Nautilus's decision, kept."""
    events = image.orders["kumo-8b68fc87e100dbcb6e07"][:2]  # Initialized, Submitted
    submitted = cr.replay_order(events)
    assert submitted.status == OrderStatus.SUBMITTED
    adapter = _adapter(submitted, by_coid=lambda _: TimeoutError("venue unreachable"))
    from nautilus_trader.execution.messages import GenerateOrderStatusReport
    from nautilus_trader.core.uuid import UUID4

    cmd = GenerateOrderStatusReport(instrument_id=submitted.instrument_id, client_order_id=submitted.client_order_id,
                                    venue_order_id=None, command_id=UUID4(), ts_init=1)
    report = asyncio.run(adapter.generate_order_status_report(cmd))
    assert report is None
    assert adapter._venue_unanswered_lookups == 1


# --------------------------------------------------------------------------------------------------
# (C) a submit the venue did not answer
# --------------------------------------------------------------------------------------------------


def _place(order, *, submit, lookup, cached_after_submit=None):
    async def _submit(payload):
        if isinstance(submit, BaseException):
            raise submit
        return submit

    calls = {"n": 0}

    async def _lookup(coid):
        calls["n"] += 1
        r = lookup(coid) if lookup.__code__.co_argcount == 1 else lookup(coid, calls["n"])
        if isinstance(r, BaseException):
            raise r
        return r

    accepted, rejected = [], []
    self = types.SimpleNamespace(
        _http=types.SimpleNamespace(submit_order=_submit, get_order_by_client_order_id=_lookup),
        _cache=types.SimpleNamespace(order=lambda coid: cached_after_submit),
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 1),
        _log=_log(),
        generate_order_accepted=lambda **k: accepted.append(k["venue_order_id"].value),
        generate_order_rejected=lambda **k: rejected.append(k["reason"]),
        _build_order_request=lambda o: {"symbol": "PATH"},
        _venue_unanswered_lookups=0,
        _SUBMIT_LOOKUP_ATTEMPTS=2,
        _SUBMIT_LOOKUP_DELAY_S=0.0,
        _SUBMIT_LOOKUP_TIMEOUT_S=1.0,
        _LANDED_STATUSES=AlpacaExecutionClient._LANDED_STATUSES,
        calls=calls,
    )
    _bind_submit_seam(self)
    asyncio.run(AlpacaExecutionClient._place_order(self, order))
    return accepted, rejected, self


def _bind_submit_seam(self):
    """The REAL venue seam, bound to a duck-typed self — every submit path crosses `_post_order`."""
    self._reject = lambda o, reason: AlpacaExecutionClient._reject(self, o, reason)
    self._lookup_after_unanswered_submit = (
        lambda o, cause, reject=None: AlpacaExecutionClient._lookup_after_unanswered_submit(self, o, cause, reject=reject))
    self._post_order = lambda o, payload, reject: AlpacaExecutionClient._post_order(self, o, payload, reject=reject)


# -- #832: a bracket is ONE payload and THREE orders; the venue's answer applies to all of them ----

RAW_BRACKET = {"id": "E-1", "status": "accepted",
               "legs": [{"id": "TP-1", "type": "limit"}, {"id": "SL-1", "type": "stop"}]}


def _bracket(*, submit, lookup):
    from nautilus_trader.model.enums import OrderType

    def _o(coid, order_type):
        # A REAL ClientOrderId: the lookup reads `.value`, and a bare string double passed the whole
        # bracket path while the lookup raised AttributeError on every ask — "unanswered" by accident.
        return types.SimpleNamespace(client_order_id=ClientOrderId(coid), strategy_id="MANUAL-001",
                                     instrument_id="PATH.XNYS", order_type=order_type)

    entry, sl, tp = _o("E", OrderType.MARKET), _o("SL", OrderType.STOP_MARKET), _o("TP", OrderType.LIMIT)
    command = types.SimpleNamespace(order_list=types.SimpleNamespace(orders=[entry, sl, tp]))

    async def _submit(payload):
        if isinstance(submit, BaseException):
            raise submit
        return submit

    calls = {"n": 0}

    async def _lookup(coid):
        calls["n"] += 1
        r = lookup(coid)
        if isinstance(r, BaseException):
            raise r
        return r

    async def _budget(o):
        return True, "", {}

    accepted, rejected = [], []
    self = types.SimpleNamespace(
        _http=types.SimpleNamespace(submit_order=_submit, get_order_by_client_order_id=_lookup),
        _cache=types.SimpleNamespace(order=lambda coid: None),
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 1),
        _log=_log(),
        _budget_allows=_budget,
        generate_order_submitted=lambda **k: None,
        generate_order_accepted=lambda **k: accepted.append((k["client_order_id"].value, k["venue_order_id"].value)),
        generate_order_rejected=lambda **k: rejected.append((k["client_order_id"].value, k["reason"])),
        _build_bracket_request=lambda e, s_, t: {"symbol": "PATH", "order_class": "bracket"},
        _venue_unanswered_lookups=0,
        _SUBMIT_LOOKUP_ATTEMPTS=2,
        _SUBMIT_LOOKUP_DELAY_S=0.0,
        _SUBMIT_LOOKUP_TIMEOUT_S=1.0,
        _LANDED_STATUSES=AlpacaExecutionClient._LANDED_STATUSES,
        calls=calls,
    )
    _bind_submit_seam(self)
    asyncio.run(AlpacaExecutionClient._submit_order_list(self, command))
    return accepted, rejected, self


def test_a_timed_out_bracket_that_LANDED_is_accepted_with_every_legs_venue_id():
    """Fixture property first: the lookup answers with the bracket AND its legs — the shape Alpaca
    returns for `orders:by_client_order_id` on a bracket entry. Before #832 this was three
    OrderRejected("Connection timeout") on three orders resting at the venue."""
    accepted, rejected, _ = _bracket(submit=TimeoutError("Connection timeout"), lookup=lambda _: RAW_BRACKET)
    assert rejected == [], rejected
    assert sorted(accepted) == [("E", "E-1"), ("SL", "SL-1"), ("TP", "TP-1")]


def test_a_timed_out_bracket_the_venue_never_saw_rejects_all_three_with_the_cause():
    accepted, rejected, self = _bracket(
        submit=TimeoutError("Connection timeout"),
        lookup=lambda _: AlpacaHttpError("GET", "/v2/orders:by_client_order_id", 404, '{"message":"order not found"}'))
    assert accepted == []
    assert sorted(rejected) == [("E", "Connection timeout"), ("SL", "Connection timeout"), ("TP", "Connection timeout")]
    assert self.calls["n"] == 2, "a single 404 is not proof; the window must be exhausted"


def test_an_ANSWERED_bracket_rejection_rejects_all_three_with_the_venues_word():
    accepted, rejected, _ = _bracket(
        submit=AlpacaHttpError("POST", "/v2/orders", 422, '{"message":"insufficient buying power"}'), lookup=lambda _: RAW_BRACKET)
    assert accepted == [] and {r.lower() for _, r in rejected} == {"insufficient buying power"} and len(rejected) == 3


def test_a_still_unanswered_bracket_is_left_SUBMITTED_not_rejected():
    accepted, rejected, self = _bracket(submit=TimeoutError("Connection timeout"), lookup=lambda _: TimeoutError("still down"))
    assert accepted == [] and rejected == []
    assert self._venue_unanswered_lookups == 1


def test_a_timed_out_submit_that_LANDED_is_accepted_not_rejected(accepted_order):
    accepted, rejected, _ = _place(accepted_order, submit=TimeoutError("Connection timeout"), lookup=lambda _: RAW_ACCEPTED)
    assert accepted == [VENUE_ID] and rejected == []


def test_a_timed_out_submit_the_venue_never_saw_is_rejected_with_the_cause(accepted_order):
    """404 on EVERY ask across the window — only then is 'never landed' an answer."""
    accepted, rejected, self = _place(accepted_order, submit=TimeoutError("Connection timeout"),
                                      lookup=lambda _: AlpacaHttpError("GET", "/v2/orders:by_client_order_id", 404, '{"message":"order not found"}'))
    assert accepted == [] and rejected == ["Connection timeout"]
    assert self.calls["n"] == 2, "a single 404 is not proof; the window must be exhausted"


def test_a_first_ask_404_that_turns_into_found_is_accepted(accepted_order):
    """Read-your-writes on `orders:by_client_order_id` is unmeasured (review H1): a 404 milliseconds
    after a timed-out POST must not reject an order that then shows up."""
    def lookup(coid, n):
        return AlpacaHttpError("GET", "/x", 404, "{}") if n == 1 else RAW_ACCEPTED
    accepted, rejected, _ = _place(accepted_order, submit=TimeoutError("Connection timeout"), lookup=lookup)
    assert accepted == [VENUE_ID] and rejected == []


def test_a_landed_order_the_venue_reports_DEAD_is_rejected_with_the_venues_word(accepted_order):
    dead = dict(RAW_ACCEPTED, status="rejected")
    accepted, rejected, _ = _place(accepted_order, submit=TimeoutError("Connection timeout"), lookup=lambda _: dead)
    assert accepted == [] and rejected == ["venue reports rejected"]


def test_no_second_acceptance_when_nautilus_already_resolved_the_order(accepted_order):
    """Nautilus's inflight check runs on its own clock; an order it already moved past SUBMITTED must
    not get a contradictory acceptance from a late lookup (review H2)."""
    accepted, rejected, _ = _place(accepted_order, submit=TimeoutError("Connection timeout"), lookup=lambda _: RAW_ACCEPTED,
                                   cached_after_submit=accepted_order)  # cache says ACCEPTED already
    assert accepted == [] and rejected == []


def test_the_lookup_window_is_bounded_inside_nautilus_inflight_window():
    """3 asks x (5 s box + 2 s pause) < the ~25 s after which Nautilus resolves SUBMITTED itself."""
    c = AlpacaExecutionClient
    assert c._SUBMIT_LOOKUP_ATTEMPTS * (c._SUBMIT_LOOKUP_TIMEOUT_S + c._SUBMIT_LOOKUP_DELAY_S) < 25.0


def test_a_hung_lookup_is_time_boxed(accepted_order):
    async def _hang(coid):
        await asyncio.sleep(30)

    accepted, rejected = [], []
    self = types.SimpleNamespace(
        _http=types.SimpleNamespace(get_order_by_client_order_id=_hang),
        _log=_log(), _clock=types.SimpleNamespace(timestamp_ns=lambda: 1),
        generate_order_rejected=lambda **k: rejected.append(k["reason"]),
        _venue_unanswered_lookups=0, _SUBMIT_LOOKUP_ATTEMPTS=1, _SUBMIT_LOOKUP_DELAY_S=0.0, _SUBMIT_LOOKUP_TIMEOUT_S=0.05,
    )
    self._reject = lambda o, reason: AlpacaExecutionClient._reject(self, o, reason)
    out = asyncio.run(AlpacaExecutionClient._lookup_after_unanswered_submit(self, accepted_order, TimeoutError("x")))
    assert out is None and rejected == [] and self._venue_unanswered_lookups == 1


def test_a_timed_out_submit_still_unanswered_is_left_SUBMITTED_and_said_out_loud(accepted_order):
    accepted, rejected, self = _place(accepted_order, submit=TimeoutError("Connection timeout"), lookup=lambda _: TimeoutError("still down"))
    assert accepted == [] and rejected == []
    assert self._venue_unanswered_lookups >= 1
    assert any(lvl == "error" and "SUBMITTED" in msg for lvl, msg in self._log.lines)


def test_an_ANSWERED_submit_rejection_is_still_a_rejection(accepted_order):
    accepted, rejected, _ = _place(accepted_order, submit=AlpacaHttpError("POST", "/v2/orders", 422, '{"message":"insufficient buying power"}'),
                                   lookup=lambda _: RAW_ACCEPTED)
    assert accepted == [] and rejected == ["Insufficient buying power"]


def test_submit_order_places_through_the_one_seam():
    import inspect

    src = inspect.getsource(AlpacaExecutionClient._submit_order)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "await self._place_order(order)" in code
    assert "self._http.submit_order(" not in code  # the venue call lives in _place_order, nowhere else


# --------------------------------------------------------------------------------------------------
# The count reaches the operator — three states, threaded (the Pydantic-drop class)
# --------------------------------------------------------------------------------------------------


def test_the_unanswered_count_rides_the_reconcile_snapshot_and_reaches_health():
    from api.engine_node import UiFeedStrategy
    from api.models import HealthResponse

    node = types.SimpleNamespace(_reconcile_drift=[], _broker_qty=None, _venue_unanswered_lookups=None)
    UiFeedStrategy._on_broker_reconcile(node, {"drift": [], "broker_qty": {}, "unanswered_lookups": 3, "ts": 1})
    assert node._venue_unanswered_lookups == 3
    UiFeedStrategy._on_broker_reconcile(node, {"drift": [], "ts": 2})  # an older producer: not told
    assert node._venue_unanswered_lookups is None
    dto = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0, venue_unanswered_lookups=3)
    assert dto.model_dump()["venue_unanswered_lookups"] == 3
    assert HealthResponse.model_fields["venue_unanswered_lookups"].default is None


def test_the_reconcile_snapshot_carries_the_count():
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    published = []

    async def list_positions():
        return []

    client = types.SimpleNamespace(
        _http=types.SimpleNamespace(list_positions=list_positions),
        _cache=types.SimpleNamespace(positions_open=lambda: []),
        _msgbus=types.SimpleNamespace(publish=lambda topic, msg: published.append(msg)),
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 7),
        _unrealized_totals={},
        _venue_unanswered_lookups=2,
    )
    asyncio.run(AlpacaExecutionClient._report_reconcile_drift(client))
    assert published[0]["unanswered_lookups"] == 2


def test_a_PARTIALLY_FILLED_hold_carries_the_fills_and_the_average(image):
    """The other resting state a corpse can be in. A HOLD that dropped the fills would make Nautilus's
    fill-mismatch helper (`live/execution_engine.py:3094`) see venue < cache and start inventing."""
    venue = cr.VenueTruth.from_fixture(FIXTURES / "venue.json")
    instrument = cr.to_obj(image.instruments["PATH.XNYS"])
    vo = venue.orders[PATH_CORPSE]
    partial = cr.VenueOrder(vo.client_order_id, "partially_filled", Decimal(80), Decimal("15.78"), vo.filled_at_ns, vo.venue_order_id)
    order = cr.replay_order(cr.revive_order(image, PATH_CORPSE, partial, instrument).events)
    assert order.status == OrderStatus.PARTIALLY_FILLED and str(order.filled_qty) == "80"  # fixture property
    adapter = _adapter(order, by_coid=lambda _: TimeoutError("venue unreachable"))
    events, reports, _ = _resolve(order, adapter)
    assert not any(isinstance(e, OrderRejected) for e in events)
    hold = reports[0]
    assert hold.order_status == OrderStatus.PARTIALLY_FILLED
    assert hold.filled_qty == order.filled_qty and hold.quantity == order.quantity
    assert hold.avg_px == Decimal(str(order.avg_px))


# --------------------------------------------------------------------------------------------------
# The HOLD through Nautilus's REAL reconcile path — the safety argument, asserted (review §1)
# --------------------------------------------------------------------------------------------------


_INSTRUMENT = cr.to_obj(cr.CacheImage.from_fixture(FIXTURES / "redis.json").instruments["PATH.XNYS"])


def _reconcile_through_nautilus(order, report):
    """`LiveExecutionEngine._reconcile_order_report` with its OWN helpers bound; only the event
    generators are recorders. If any of them is called, the HOLD was not a no-op."""
    from nautilus_trader.live.execution_engine import LiveExecutionEngine as E

    emitted: list[str] = []
    rec = lambda name: (lambda *a, **k: emitted.append(name))  # noqa: E731
    eng = types.SimpleNamespace(
        _is_shutting_down=False,
        _log=_log(),
        _cache=types.SimpleNamespace(
            order=lambda coid: order,
            # THE REAL INSTRUMENT. With None the reconciler returns early ("filtered instrument not
            # loaded", :3050-3055) BEFORE the status branch — and every no-op test below asserts nothing.
            # The DIFFERS fixture-property test is what caught that.
            instrument=lambda iid: _INSTRUMENT,
            client_order_id=lambda vid: order.client_order_id,
        ),
        manage_own_order_books=False,
        _clear_recon_tracking=lambda coid, drop_last_query=True: None,
        _ensure_venue_order_id_indexed=lambda *a, **k: None,
        _resolve_client_order_id=lambda r: order.client_order_id,
        _inferred_fill_ts={},
        _handle_event_with_tracking=rec("event"),
        _generate_order=rec("generate_order"),
        _generate_order_accepted=rec("accepted"), _generate_order_rejected=rec("rejected"),
        _generate_order_updated=rec("updated"), _generate_order_triggered=rec("triggered"),
        _generate_order_canceled=rec("canceled"), _generate_order_expired=rec("expired"),
        _generate_inferred_fill=rec("inferred_fill"), _reconcile_fill_report=rec("fill_report"),
        _add_own_book_order=rec("own_book"),
    )
    eng._handle_order_status_transitions = lambda o, r, trades, instrument: E._handle_order_status_transitions(eng, o, r, trades, instrument)
    eng._should_update = lambda o, r: E._should_update(eng, o, r)
    eng._handle_fill_quantity_mismatch = lambda *a, **k: E._handle_fill_quantity_mismatch(eng, *a, **k)
    ok = E._reconcile_order_report(eng, report, trades=[])
    return ok, emitted


def _hold_for(order):
    adapter = _adapter(order, by_coid=lambda _: TimeoutError("venue unreachable"))
    return adapter._hold_or_none(order.client_order_id, "test")


def test_an_ACCEPTED_hold_is_a_no_op_through_the_real_reconciler(accepted_order):
    ok, emitted = _reconcile_through_nautilus(accepted_order, _hold_for(accepted_order))
    assert ok is True and emitted == [], emitted


def test_a_PARTIALLY_FILLED_hold_is_a_no_op_through_the_real_reconciler(image):
    venue = cr.VenueTruth.from_fixture(FIXTURES / "venue.json")
    instrument = cr.to_obj(image.instruments["PATH.XNYS"])
    vo = venue.orders[PATH_CORPSE]
    partial = cr.VenueOrder(vo.client_order_id, "partially_filled", Decimal(80), Decimal("15.78"), vo.filled_at_ns, vo.venue_order_id)
    order = cr.replay_order(cr.revive_order(image, PATH_CORPSE, partial, instrument).events)
    ok, emitted = _reconcile_through_nautilus(order, _hold_for(order))
    assert ok is True and emitted == [], emitted


def test_PENDING_CANCEL_and_PENDING_UPDATE_get_None_so_nautilus_can_resolve_them(image):
    """A HOLD for an inflight state reconciles "successfully" and resets Nautilus's retry tracking every
    cycle (`live/execution_engine.py:3048`), so a persistent 5xx would pin a pending cancel forever
    (review §5). None → `_resolve_inflight_order` cancels it after its retries, Nautilus's own call."""
    from nautilus_trader.model.events import OrderPendingCancel, OrderPendingUpdate
    from nautilus_trader.core.uuid import UUID4

    for kind in (OrderPendingCancel, OrderPendingUpdate):
        order = cr.replay_order(cr.revive_order(image, PATH_CORPSE, cr.VenueOrder(PATH_CORPSE, "new", Decimal(0), None, None, VENUE_ID), cr.to_obj(image.instruments["PATH.XNYS"])).events)
        order.apply(kind(trader_id=order.trader_id, strategy_id=order.strategy_id, instrument_id=order.instrument_id,
                         client_order_id=order.client_order_id, venue_order_id=order.venue_order_id, account_id=order.account_id,
                         event_id=UUID4(), ts_event=2, ts_init=2))
        assert order.status in (OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_UPDATE)
        adapter = _adapter(order, by_coid=lambda _: TimeoutError("venue unreachable"))
        assert adapter._hold_or_none(order.client_order_id, "test") is None, order.status_string()


def test_a_fixture_property__a_report_that_DIFFERS_does_emit_through_the_same_harness(accepted_order):
    """The harness can see an event, or the no-op tests above are asserting nothing."""
    hold = _hold_for(accepted_order)
    from nautilus_trader.execution.reports import OrderStatusReport
    from nautilus_trader.core.uuid import UUID4
    d = {k: getattr(hold, k) for k in ("account_id", "instrument_id", "client_order_id", "venue_order_id", "order_side", "order_type",
                                       "time_in_force", "order_status", "quantity", "filled_qty", "avg_px", "price", "trigger_price",
                                       "trigger_type", "ts_accepted", "ts_last", "ts_init")}
    d["report_id"] = UUID4()
    from nautilus_trader.model.objects import Quantity
    d["quantity"] = Quantity.from_str("113")  # one share more than the cache
    different = OrderStatusReport(**d)
    _, emitted = _reconcile_through_nautilus(accepted_order, different)
    assert "updated" in emitted


# --------------------------------------------------------------------------------------------------
# Sibling cases (review §4)
# --------------------------------------------------------------------------------------------------


def test_an_order_the_cache_does_not_know_gets_None_on_an_unanswered_lookup(accepted_order):
    adapter = _adapter(accepted_order, by_coid=lambda _: TimeoutError("venue unreachable"))
    adapter._cache.order = lambda coid: None
    assert adapter._hold_or_none(accepted_order.client_order_id, "test") is None
    assert adapter._venue_unanswered_lookups == 1


def test_a_venue_id_lookup_that_answers_with_a_foreign_client_id_resolves_OURS(accepted_order):
    """A bracket leg carries Alpaca's own client id (#242); the cache maps the venue id back to ours."""
    foreign = dict(RAW_ACCEPTED, client_order_id="1d128cff-alpaca-minted")
    adapter = _adapter(accepted_order, by_coid=lambda _: foreign, by_id=lambda _: foreign)
    events, reports, _ = _resolve(accepted_order, adapter)
    assert events == [] and str(reports[0].client_order_id) == PATH_CORPSE


def test_the_real_client_starts_its_counter_at_zero():
    import inspect

    src = inspect.getsource(AlpacaExecutionClient.__init__)
    assert "self._venue_unanswered_lookups = 0" in src
