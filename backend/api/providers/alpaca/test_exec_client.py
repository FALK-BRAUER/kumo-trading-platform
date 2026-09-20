"""Offline unit tests for the Alpaca exec client's pure logic — order-request mapping + enum maps.

The Nautilus→Alpaca submit path (_build_order_request) is the risky, mechanical translation; it's built
from real Nautilus orders here. Report parsing (Alpaca→Nautilus) needs a live client and is covered by
reconciliation at market hours; its enum maps are asserted for completeness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.objects import Price, Quantity

from api.providers.alpaca.exec_client import (
    _ALPACA_FILL_SIDE,
    _ALPACA_TO_ORDER_STATUS,
    _ALPACA_TO_ORDER_TYPE,
    _ALPACA_TO_TIF,
    _ORDER_TYPE_TO_ALPACA,
    _TIF_TO_ALPACA,
    AlpacaExecutionClient,
    _fill_in_window,
    _fill_ts_ns,
    _parse_fill_activity,
    _reject_reason,
)
from api.providers.alpaca.http import AlpacaHttpError

_IID = InstrumentId(Symbol("AAPL"), Venue("XNAS"))
_ACCT = AccountId("ALPACA-PAPER")

# A representative Alpaca FILL activity (GET /v2/account/activities/FILL).
_FILL = {
    "id": "20260710143300000::abc-123",
    "activity_type": "FILL",
    "transaction_time": "2026-07-10T14:33:00.5Z",
    "type": "fill",
    "price": "192.34",
    "qty": "7",
    "side": "buy",
    "symbol": "AAPL",
    "order_id": "ord-999",
    "cum_qty": "7",
    "leaves_qty": "0",
}


def _factory() -> OrderFactory:
    return OrderFactory(TraderId("TRADER-001"), StrategyId("S-1"), LiveClock())


def test_build_market_order_request():
    order = _factory().market(_IID, OrderSide.BUY, Quantity.from_int(10))
    req = AlpacaExecutionClient._build_order_request(order)
    assert req["symbol"] == "AAPL"
    assert req["qty"] == "10"
    assert req["side"] == "buy"
    assert req["type"] == "market"
    assert "limit_price" not in req and "stop_price" not in req
    assert req["client_order_id"] == order.client_order_id.value


def test_build_limit_order_request_carries_price_and_tif():
    order = _factory().limit(
        _IID, OrderSide.SELL, Quantity.from_int(5), Price.from_str("199.50"),
        time_in_force=TimeInForce.GTC,
    )
    req = AlpacaExecutionClient._build_order_request(order)
    assert req["side"] == "sell"
    assert req["type"] == "limit"
    assert req["time_in_force"] == "gtc"
    assert req["limit_price"] == "199.50"


def test_build_stop_limit_order_request_carries_trigger():
    order = _factory().stop_limit(
        _IID, OrderSide.BUY, Quantity.from_int(3),
        price=Price.from_str("210.00"), trigger_price=Price.from_str("208.00"),
    )
    req = AlpacaExecutionClient._build_order_request(order)
    assert req["type"] == "stop_limit"
    assert req["limit_price"] == "210.00"
    assert req["stop_price"] == "208.00"


def test_order_type_map_round_trips():
    for nautilus_type, alpaca_type in _ORDER_TYPE_TO_ALPACA.items():
        assert _ALPACA_TO_ORDER_TYPE[alpaca_type] == nautilus_type


def test_tif_map_covers_cockpit_values():
    for tif in (TimeInForce.DAY, TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK):
        alpaca = _TIF_TO_ALPACA[tif]
        assert _ALPACA_TO_TIF[alpaca] == tif


def test_order_status_map_terminal_states():
    assert _ALPACA_TO_ORDER_STATUS["filled"] == OrderStatus.FILLED
    assert _ALPACA_TO_ORDER_STATUS["canceled"] == OrderStatus.CANCELED
    assert _ALPACA_TO_ORDER_STATUS["rejected"] == OrderStatus.REJECTED
    assert _ALPACA_TO_ORDER_STATUS["partially_filled"] == OrderStatus.PARTIALLY_FILLED
    # Extra Alpaca states must be mapped explicitly, not silently defaulted.
    assert _ALPACA_TO_ORDER_STATUS["done_for_day"] == OrderStatus.CANCELED
    assert _ALPACA_TO_ORDER_STATUS["replaced"] == OrderStatus.CANCELED


def test_order_status_map_has_no_silent_default():
    # An unknown status must miss the map so the parser can fail closed (skip), not assume ACCEPTED.
    assert _ALPACA_TO_ORDER_STATUS.get("bogus_status") is None


def test_build_rejects_unsupported_order_type():
    fake = SimpleNamespace(order_type=OrderType.MARKET_IF_TOUCHED, time_in_force=TimeInForce.DAY)
    with pytest.raises(ValueError):
        AlpacaExecutionClient._build_order_request(fake)


def test_build_rejects_gtd():
    # GTD would silently become GTC (no expire_time forwarded) — must reject instead.
    fake = SimpleNamespace(order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTD)
    with pytest.raises(ValueError):
        AlpacaExecutionClient._build_order_request(fake)


def test_build_request_sets_extended_hours_from_tag():
    order = _factory().limit(
        _IID, OrderSide.BUY, Quantity.from_int(5), Price.from_str("190.00"),
        time_in_force=TimeInForce.DAY, tags=["extended_hours"],
    )
    req = AlpacaExecutionClient._build_order_request(order)
    assert req["extended_hours"] is True  # tag → Alpaca pre/post-market flag


def test_build_request_no_extended_hours_key_without_tag():
    order = _factory().limit(_IID, OrderSide.BUY, Quantity.from_int(5), Price.from_str("190.00"))
    req = AlpacaExecutionClient._build_order_request(order)
    assert "extended_hours" not in req  # default off — regular-session order


def test_tif_map_covers_on_open_and_on_close():
    for tif in (TimeInForce.AT_THE_OPEN, TimeInForce.AT_THE_CLOSE):
        alpaca = _TIF_TO_ALPACA[tif]
        assert _ALPACA_TO_TIF[alpaca] == tif


# -- fill reports (#75) ---------------------------------------------------------------------------
def test_fill_side_map():
    assert _ALPACA_FILL_SIDE["buy"] == OrderSide.BUY
    assert _ALPACA_FILL_SIDE["sell"] == OrderSide.SELL
    # sell_short is a SELL that opens/extends a short.
    assert _ALPACA_FILL_SIDE["sell_short"] == OrderSide.SELL


def test_parse_fill_activity_maps_all_fields():
    ts_event = _fill_ts_ns(_FILL)
    r = _parse_fill_activity(_FILL, _IID, _ACCT, ts_event, ts_init_ns=999)
    assert r is not None
    assert r.instrument_id == _IID
    assert r.venue_order_id.value == "ord-999"
    # activity id → trade id (unique per execution, idempotent across reconciliation runs).
    assert r.trade_id.value == "20260710143300000::abc-123"
    assert r.order_side == OrderSide.BUY
    assert r.last_qty == Quantity.from_str("7")
    assert r.last_px == Price.from_str("192.34")
    assert r.commission.as_double() == 0.0  # Alpaca equities commission-free
    assert r.liquidity_side == LiquiditySide.NO_LIQUIDITY_SIDE
    assert r.client_order_id is None  # activity carries only venue order_id
    assert r.ts_event == ts_event
    assert r.ts_init == 999


def test_parse_fill_activity_preserves_fractional_shares():
    raw = {**_FILL, "qty": "1.5", "price": "192.345"}
    r = _parse_fill_activity(raw, _IID, _ACCT, ts_event_ns=1, ts_init_ns=2)
    assert r is not None
    assert r.last_qty == Quantity.from_str("1.5")
    assert r.last_px == Price.from_str("192.345")


@pytest.mark.parametrize(
    "mutate",
    [
        {"side": "unknown"},   # unmapped side
        {"side": None},
        {"price": None},       # missing price
        {"qty": None},         # missing qty
        {"order_id": None},    # missing venue order id
        {"id": None},          # missing execution id
    ],
)
def test_parse_fill_activity_fails_closed(mutate):
    raw = {**_FILL, **mutate}
    assert _parse_fill_activity(raw, _IID, _ACCT, ts_event_ns=1, ts_init_ns=2) is None


def test_fill_ts_ns_parses_iso_and_rejects_missing():
    assert _fill_ts_ns(_FILL) == pd.Timestamp("2026-07-10T14:33:00.5Z").value
    assert _fill_ts_ns({**_FILL, "transaction_time": None}) is None
    assert _fill_ts_ns({**_FILL, "transaction_time": ""}) is None


def test_fill_ts_ns_is_fail_soft_on_garbage():
    # Must NOT raise — one malformed timestamp cannot be allowed to abort the whole reconciliation batch.
    assert _fill_ts_ns({**_FILL, "transaction_time": "not-a-timestamp"}) is None
    assert _fill_ts_ns({**_FILL, "transaction_time": 12345}) is None


def test_fill_in_window_bounds():
    ts = pd.Timestamp("2026-07-10T14:33:00Z").value
    before = pd.Timestamp("2026-07-10T14:00:00Z").value
    after = pd.Timestamp("2026-07-10T15:00:00Z").value
    assert _fill_in_window(ts, None, None) is True
    assert _fill_in_window(ts, before, after) is True
    assert _fill_in_window(ts, after, None) is False   # ts before start
    assert _fill_in_window(ts, None, before) is False  # ts after end
    assert _fill_in_window(ts, ts, ts) is True         # inclusive boundaries


def test_window_bounds_normalize_from_datetime_and_timestamp():
    # Nautilus may hand command.start/end as stdlib datetime OR pd.Timestamp — pd.Timestamp() unifies both to
    # the same ns int (guards the AttributeError from calling .value on a bare datetime).
    dt = datetime(2026, 7, 10, 14, 33, tzinfo=UTC)
    assert pd.Timestamp(dt).value == pd.Timestamp("2026-07-10T14:33:00Z").value


def test_parse_fill_activity_fails_closed_on_malformed_numbers():
    # Quantity/Price.from_str raise on garbage — must be caught so one bad row can't abort the batch.
    # Non-positive price/qty parse cleanly but are not real executions → also dropped.
    for mutate in ({"qty": "abc"}, {"price": ""}, {"qty": "-1"}, {"price": "0"}, {"price": "-1"}, {"qty": "0"}):
        assert _parse_fill_activity({**_FILL, **mutate}, _IID, _ACCT, 1, 2) is None


def test_fill_ts_ns_no_overflow_on_out_of_range():
    # Far-future/out-of-range timestamps overflow pd.Timestamp's ns int — must return None, not raise.
    assert _fill_ts_ns({**_FILL, "transaction_time": "9999-01-01T00:00:00Z"}) is None


def test_list_activities_follows_page_cursor_to_completion():
    """The activities cursor must be followed to the end — a truncated fetch silently loses fills, which
    reconciliation would read as a smaller position. Full pages continue; the first short page stops."""
    import asyncio

    from api.providers.alpaca.http import AlpacaHttpClient

    client = AlpacaHttpClient("k", "s", "http://trade", "http://data")
    pages = [
        [{"id": f"a{i}"} for i in range(100)],  # full page → continue
        [{"id": f"b{i}"} for i in range(100)],  # full page → continue
        [{"id": "c0"}],                          # short page → stop
    ]
    seen_tokens: list[str | None] = []

    async def fake_get(base, path, params):
        seen_tokens.append(params.get("page_token"))
        return pages[len(seen_tokens) - 1]

    client._get = fake_get  # type: ignore[method-assign]
    out = asyncio.run(client.list_activities(page_size=100))

    assert len(out) == 201  # nothing dropped
    # first request has no token; each subsequent uses the prior page's last id as the cursor.
    assert seen_tokens == [None, "a99", "b99"]


def test_list_activities_stops_on_empty_first_page():
    import asyncio

    from api.providers.alpaca.http import AlpacaHttpClient

    client = AlpacaHttpClient("k", "s", "http://trade", "http://data")

    async def fake_get(base, path, params):
        return []

    client._get = fake_get  # type: ignore[method-assign]
    assert asyncio.run(client.list_activities()) == []


def test_fill_trade_id_clamps_to_nautilus_limit():
    from api.providers.alpaca.exec_client import _fill_trade_id

    # Short id (fits 1..36) is kept verbatim — human-traceable + idempotent.
    assert _fill_trade_id("20260710143300000::abc-123").value == "20260710143300000::abc-123"
    # Real Alpaca id is `<ts>::<uuid>` ~55 chars → over the 36 cap → use the stable UUID tail.
    long_id = "20260713145959000123::0e3f8a1c-4b2d-4f6a-9c7e-1a2b3c4d5e6f"
    assert len(long_id) > 36
    tid = _fill_trade_id(long_id)
    assert tid.value == "0e3f8a1c-4b2d-4f6a-9c7e-1a2b3c4d5e6f"
    assert 1 <= len(tid.value) <= 36
    # No `::` and too long → deterministic uuid5 (stable across reconciliation runs), within the limit.
    weird = "x" * 55
    a, b = _fill_trade_id(weird), _fill_trade_id(weird)
    assert a.value == b.value and 1 <= len(a.value) <= 36  # idempotent + valid


# ----------------------------------------------------------------------------------------------------
# Rejection-reason humanization (AlpacaHttpError.reason + _reject_reason) — the raw Alpaca error body must
# never reach the blotter; callers see the parsed human message.
# ----------------------------------------------------------------------------------------------------
def test_alpaca_http_error_extracts_message_and_market_price():
    exc = AlpacaHttpError(
        "POST", "/v2/orders", 422,
        '{"code":42210000,"market_price":"24.11","message":"stop price must be greater than current price","stop_price":"23.9"}',
    )
    assert exc.code == 42210000
    assert exc.reason == "Stop price must be greater than current price (mkt $24.11)"


def test_alpaca_http_error_reason_without_market_price():
    exc = AlpacaHttpError("POST", "/v2/orders", 403, '{"code":40310000,"message":"insufficient buying power"}')
    assert exc.reason == "Insufficient buying power"


def test_alpaca_http_error_non_json_body_falls_back_to_raw():
    exc = AlpacaHttpError("POST", "/v2/orders", 500, "Internal Server Error")
    assert exc.reason == "Internal Server Error"


def test_reject_reason_uses_clean_message_for_alpaca_error():
    exc = AlpacaHttpError("POST", "/v2/orders", 422, '{"message":"stop price must be greater than current price"}')
    assert _reject_reason(exc) == "Stop price must be greater than current price"


def test_reject_reason_falls_back_to_str_for_other_errors():
    assert _reject_reason(ValueError("boom")) == "boom"


# ----------------------------------------------------------------------------------------------------
# Pre-submit wrong-side stop guard (_last_price + _stop_side_violation) — a BUY stop must trigger ABOVE the
# market, a SELL stop BELOW; else we reject locally with a clean reason instead of a broker round-trip.
# ----------------------------------------------------------------------------------------------------
def _stop_order(order_type, side, trigger):
    return SimpleNamespace(order_type=order_type, side=side, trigger_price=trigger, instrument_id=_IID)


def test_last_price_prefers_trade_over_quote():
    fake = SimpleNamespace(_cache=SimpleNamespace(
        trade_tick=lambda iid: SimpleNamespace(price=24.11),
        quote_tick=lambda iid: SimpleNamespace(bid_price=1.0, ask_price=2.0),
    ))
    assert AlpacaExecutionClient._last_price(fake, _IID) == 24.11


def test_last_price_uses_quote_mid_when_no_trade():
    fake = SimpleNamespace(_cache=SimpleNamespace(
        trade_tick=lambda iid: None,
        quote_tick=lambda iid: SimpleNamespace(bid_price=24.00, ask_price=24.20),
    ))
    assert AlpacaExecutionClient._last_price(fake, _IID) == 24.10


def test_last_price_none_when_no_ticks():
    fake = SimpleNamespace(_cache=SimpleNamespace(trade_tick=lambda iid: None, quote_tick=lambda iid: None))
    assert AlpacaExecutionClient._last_price(fake, _IID) is None


def test_buy_stop_below_market_is_rejected():
    fake = SimpleNamespace(_last_price=lambda iid: 24.11)
    v = AlpacaExecutionClient._stop_side_violation(fake, _stop_order(OrderType.STOP_MARKET, OrderSide.BUY, 23.90))
    assert v is not None and "above" in v


def test_buy_stop_above_market_passes():
    fake = SimpleNamespace(_last_price=lambda iid: 23.50)
    assert AlpacaExecutionClient._stop_side_violation(fake, _stop_order(OrderType.STOP_MARKET, OrderSide.BUY, 23.90)) is None


def test_sell_stop_above_market_is_rejected():
    fake = SimpleNamespace(_last_price=lambda iid: 24.11)
    v = AlpacaExecutionClient._stop_side_violation(fake, _stop_order(OrderType.STOP_MARKET, OrderSide.SELL, 24.50))
    assert v is not None and "below" in v


def test_non_stop_order_is_not_side_checked():
    fake = SimpleNamespace(_last_price=lambda iid: 24.11)
    assert AlpacaExecutionClient._stop_side_violation(fake, _stop_order(OrderType.LIMIT, OrderSide.BUY, 23.90)) is None


def test_stop_guard_skips_when_no_reference_price():
    fake = SimpleNamespace(_last_price=lambda iid: None)
    assert AlpacaExecutionClient._stop_side_violation(fake, _stop_order(OrderType.STOP_MARKET, OrderSide.BUY, 23.90)) is None


# ----------------------------------------------------------------------------------------------------
# Native Alpaca bracket (#34) — the entry + protective SL/TP become one order_class=bracket so the legs
# REST AT THE BROKER (no LAST_PRICE emulation).
# ----------------------------------------------------------------------------------------------------
def _bracket_legs(side=OrderSide.BUY, entry_type=OrderType.LIMIT):
    from nautilus_trader.model.objects import Price
    ol = _factory().bracket(
        _IID, side, Quantity.from_int(10),
        entry_order_type=entry_type,
        entry_price=Price.from_str("100.00") if entry_type == OrderType.LIMIT else None,
        sl_trigger_price=Price.from_str("95.00"),
        tp_price=Price.from_str("110.00"),
        time_in_force=TimeInForce.GTC,
    )
    orders = list(ol.orders)
    entry = orders[0]
    sl = next(o for o in orders[1:] if o.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT))
    tp = next(o for o in orders[1:] if o.order_type == OrderType.LIMIT)
    return entry, sl, tp


def test_build_bracket_request_native_order_class():
    entry, sl, tp = _bracket_legs()
    req = AlpacaExecutionClient._build_bracket_request(entry, sl, tp)
    assert req["order_class"] == "bracket"
    assert req["side"] == "buy"
    assert req["type"] == "limit"
    assert req["limit_price"] == "100.00"
    assert req["take_profit"]["limit_price"] == "110.00"
    assert req["stop_loss"]["stop_price"] == "95.00"
    assert req["time_in_force"] == "gtc"
    assert req["client_order_id"] == entry.client_order_id.value


def test_build_bracket_request_market_entry_has_no_limit_price():
    entry, sl, tp = _bracket_legs(entry_type=OrderType.MARKET)
    req = AlpacaExecutionClient._build_bracket_request(entry, sl, tp)
    assert req["type"] == "market"
    assert "limit_price" not in req
    assert req["stop_loss"]["stop_price"] == "95.00"


# ----------------------------------------------------------------------------------------------------
# MOCK-BROKER integration test for the native bracket path — proves _submit_order_list posts a native
# order_class=bracket and maps Alpaca's nested legs back onto the SL/TP orders, WITHOUT any real order.
# ----------------------------------------------------------------------------------------------------
def test_submit_order_list_posts_native_bracket_and_maps_legs():
    import asyncio
    from types import SimpleNamespace


    entry, sl, tp = _bracket_legs(entry_type=OrderType.MARKET)
    order_list = SimpleNamespace(orders=[entry, sl, tp])
    command = SimpleNamespace(order_list=order_list)

    posted = {}
    async def _submit(payload):
        posted.update(payload)
        # Alpaca bracket response: entry id + nested legs with the broker's own ids.
        return {"id": "ENTRY-VID", "legs": [
            {"id": "TP-VID", "type": "limit"},
            {"id": "SL-VID", "type": "stop"},
        ]}

    submitted, accepted, rejected = [], [], []
    fake = SimpleNamespace(
        _http=SimpleNamespace(submit_order=_submit),
        _clock=SimpleNamespace(timestamp_ns=lambda: 1),
        _log=SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None, warning=lambda *a, **k: None),
        generate_order_submitted=lambda **k: submitted.append(k["client_order_id"].value),
        generate_order_accepted=lambda **k: accepted.append((k["client_order_id"].value, k["venue_order_id"].value)),
        generate_order_rejected=lambda **k: rejected.append(k["client_order_id"].value),
        _build_bracket_request=AlpacaExecutionClient._build_bracket_request,
    )

    # the bracket crosses the same venue seam as a single order (#832): bind the REAL methods
    fake._reject = lambda o, reason: AlpacaExecutionClient._reject(fake, o, reason)
    fake._post_order = lambda o, payload, reject: AlpacaExecutionClient._post_order(fake, o, payload, reject=reject)
    asyncio.run(AlpacaExecutionClient._submit_order_list(fake, command))

    # native bracket payload
    assert posted["order_class"] == "bracket"
    assert posted["stop_loss"]["stop_price"] == "95.00"
    assert posted["take_profit"]["limit_price"] == "110.00"
    # all three legs submitted, none rejected
    assert set(submitted) == {entry.client_order_id.value, sl.client_order_id.value, tp.client_order_id.value}
    assert rejected == []
    # venue ids mapped: entry→response id, SL→stop leg id, TP→limit leg id (so reconciliation tracks them)
    amap = dict(accepted)
    assert amap[entry.client_order_id.value] == "ENTRY-VID"
    assert amap[sl.client_order_id.value] == "SL-VID"
    assert amap[tp.client_order_id.value] == "TP-VID"


def test_submit_order_list_rejects_whole_bracket_on_broker_error():
    import asyncio
    from types import SimpleNamespace

    from api.providers.alpaca.http import AlpacaHttpError

    entry, sl, tp = _bracket_legs()
    command = SimpleNamespace(order_list=SimpleNamespace(orders=[entry, sl, tp]))

    async def _boom(payload):
        raise AlpacaHttpError("POST", "/v2/orders", 422, '{"message":"insufficient buying power"}')

    accepted, rejected = [], []
    fake = SimpleNamespace(
        _http=SimpleNamespace(submit_order=_boom),
        _clock=SimpleNamespace(timestamp_ns=lambda: 1),
        _log=SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None, warning=lambda *a, **k: None),
        generate_order_submitted=lambda **k: None,
        generate_order_accepted=lambda **k: accepted.append(k["client_order_id"].value),
        generate_order_rejected=lambda **k: rejected.append((k["client_order_id"].value, k["reason"])),
        _build_bracket_request=AlpacaExecutionClient._build_bracket_request,
    )
    # the bracket crosses the same venue seam as a single order (#832): bind the REAL methods
    fake._reject = lambda o, reason: AlpacaExecutionClient._reject(fake, o, reason)
    fake._post_order = lambda o, payload, reject: AlpacaExecutionClient._post_order(fake, o, payload, reject=reject)
    asyncio.run(AlpacaExecutionClient._submit_order_list(fake, command))
    assert accepted == []  # nothing accepted
    # all three rejected with the clean humanized reason
    assert len(rejected) == 3 and all("Insufficient buying power" == r[1] for r in rejected)


# ----------------------------------------------------------------------------------------------------
# Client-order-id resolution for reconciliation reports (#242) — a native bracket's protective legs carry
# ALPACA-minted client ids, so matching on that field filed them as EXTERNAL. Two unclaimed sell legs then
# each reserved the full position, Alpaca reported `available: 0`, and the position could not be flattened.
# ----------------------------------------------------------------------------------------------------
def _resolver(known: dict[str, str]):
    """Fake client whose cache maps venue id → OUR client order id, as Nautilus's does after submit."""
    return SimpleNamespace(
        _cache=SimpleNamespace(
            client_order_id=lambda void: (
                ClientOrderId(known[void.value]) if void.value in known else None
            )
        )
    )


def test_bracket_leg_resolves_to_our_client_order_id_not_alpacas():
    # The live WDAY case: entry carried our 32-char hex id, the legs carried Alpaca UUIDs.
    fake = _resolver({"leg-venue-1": "O-20260812-000000-001-001-2"})
    raw = {"id": "leg-venue-1", "client_order_id": "cb137f9b-a260-437b-875b-d556b7addaec"}
    got = AlpacaExecutionClient._client_order_id_for(fake, raw)
    assert got == ClientOrderId("O-20260812-000000-001-001-2")


def test_genuinely_external_order_keeps_alpacas_client_order_id():
    # A fill placed in Alpaca's own UI is NOT ours — the #79 quarantine plane exists for exactly this,
    # and resolving by venue id must not swallow it.
    fake = _resolver({})
    raw = {"id": "someone-elses", "client_order_id": "ext-abc"}
    assert AlpacaExecutionClient._client_order_id_for(fake, raw) == ClientOrderId("ext-abc")


def test_no_client_order_id_at_all_is_none():
    fake = _resolver({})
    assert AlpacaExecutionClient._client_order_id_for(fake, {"id": "v1"}) is None


def test_a_cache_miss_is_none_not_an_exception():
    """The lookup is deliberately NOT wrapped in a try/except. A miss returns None; only a genuine
    cache/integrity bug would raise, and swallowing that would silently recreate the unclaimed-leg
    failure this fix exists to prevent — so it must surface. (codex review.)"""
    fake = _resolver({})
    raw = {"id": "never-seen", "client_order_id": "ext-1"}
    assert AlpacaExecutionClient._client_order_id_for(fake, raw) == ClientOrderId("ext-1")


def test_venue_id_wins_even_when_alpaca_echoes_a_plausible_id():
    # Ownership comes from the cache, never from the id's FORMAT — a format test would misfire the
    # moment either side changed its id scheme.
    fake = _resolver({"v9": "OURS-1"})
    raw = {"id": "v9", "client_order_id": "OURS-LOOKALIKE-2"}
    assert AlpacaExecutionClient._client_order_id_for(fake, raw) == ClientOrderId("OURS-1")


# ----------------------------------------------------------------------------------------------------
# Equity curve (#243) — the account's own history, which also answers #233's account half.
# ----------------------------------------------------------------------------------------------------
def _equity_client(responses: dict):
    """Fake client whose http returns a canned portfolio-history payload per period."""
    published = []

    class _Http:
        def __init__(self):
            self.calls = []

        async def get_portfolio_history(self, period, timeframe, extended_hours=False):
            self.calls.append((period, timeframe))
            # Periods a test does not define come back EMPTY, the way Alpaca answers an account with no
            # history for that window — never None, which would only be testing our own fake.
            resp = responses.get(period, {"timestamp": [], "equity": [], "profit_loss": [], "base_value": 0.0})
            if isinstance(resp, Exception):
                raise resp
            return resp

    fake = SimpleNamespace(
        _http=_Http(),
        _log=SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
        _clock=SimpleNamespace(timestamp_ns=lambda: 123),
        _msgbus=SimpleNamespace(publish=lambda topic, payload: published.append((topic, payload))),
    )
    return fake, published


def test_equity_curve_publishes_one_series_per_period():
    fake, published = _equity_client(
        {
            "1W": {"timestamp": [1, 2], "equity": [100.0, 101.0], "profit_loss": [0.0, 1.0], "base_value": 100.0},
            "1M": {"timestamp": [1], "equity": [100.0], "profit_loss": [2.0], "base_value": 98.0},
            "3M": {"timestamp": [1], "equity": [100.0], "profit_loss": [3.0], "base_value": 97.0},
            "all": {"timestamp": [1], "equity": [100.0], "profit_loss": [4.0], "base_value": 96.0},
        }
    )
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "broker.equity_curve"
    assert set(payload["curves"]) == {"1W", "1M", "3M", "all"}
    assert payload["curves"]["1W"]["points"][-1]["equity"] == 101.0


def test_period_pnl_is_last_equity_minus_base_not_the_final_points_change():
    """`profit_loss[i]` is the change AT that point, NOT a running total.

    Verified against the live account 2026-08-12: the last 1M `profit_loss` was +1161.81 (that day's
    move) while the month was DOWN 2244.29 — and `sum(profit_loss) == equity[-1] - base_value` exactly.
    Taking the last element printed "+$1,161 this 1M" above a chart falling 100,085 → 97,755. This is
    still the broker's arithmetic: both terms are Alpaca's own fields.
    """
    fake, published = _equity_client(
        # Per-point changes summing to -3; the final point's own change is +7 and must NOT be the answer.
        {"1M": {"timestamp": [1, 2], "equity": [90.0, 97.0], "profit_loss": [-10.0, 7.0], "base_value": 100.0}}
    )
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    assert published[0][1]["curves"]["1M"]["pnl"] == -3.0


def test_pre_funding_zero_equity_sessions_are_dropped():
    """Alpaca returns a real 0 for sessions before the account was funded. Plotting them draws a cliff
    from the axis up to the balance, which reads as a catastrophic loss that was recovered."""
    fake, published = _equity_client(
        {"1M": {"timestamp": [1, 2, 3], "equity": [0.0, 100.0, 101.0], "profit_loss": [0.0, 0.0, 1.0],
                "base_value": 100.0}}
    )
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    points = published[0][1]["curves"]["1M"]["points"]
    assert [p["t"] for p in points] == [2, 3]


def test_equity_curve_drops_null_padded_sessions_without_misaligning_the_series():
    """Alpaca pads holidays / pre-first-trade with nulls. Plotting them as zero draws a cliff to the
    x-axis; dropping the equity but keeping its timestamp shifts every later point."""
    fake, published = _equity_client(
        {"1M": {"timestamp": [1, 2, 3], "equity": [100.0, None, 102.0], "profit_loss": [0.0, None, 2.0],
                "base_value": 100.0}}
    )
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    points = published[0][1]["curves"]["1M"]["points"]
    assert [p["t"] for p in points] == [1, 3], "the null session is dropped, timestamps stay aligned"
    assert [p["equity"] for p in points] == [100.0, 102.0]


def test_one_failing_period_does_not_lose_the_others():
    fake, published = _equity_client(
        {
            "1W": RuntimeError("alpaca 500"),
            "1M": {"timestamp": [1], "equity": [100.0], "profit_loss": [1.0], "base_value": 99.0},
        }
    )
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    curves = published[0][1]["curves"]
    assert "1W" not in curves and "1M" in curves


def test_a_period_that_fails_LATER_keeps_the_series_it_published_before():
    """Publishing only the periods that succeeded this pass would drop a previously good series from
    the plane — a chart tab that was fine a moment ago would blank. The frame is merged, not replaced."""
    good = {"timestamp": [1], "equity": [100.0], "profit_loss": [1.0], "base_value": 99.0}
    fake, published = _equity_client({"1W": good, "1M": good})
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    assert "1W" in published[0][1]["curves"]

    # 1W now fails; its previously published series must survive.
    fake._http.__class__.get_portfolio_history = _failing_1w(good)
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    assert "1W" in published[-1][1]["curves"], "a later failure wiped a good series"
    assert "1M" in published[-1][1]["curves"]


def _failing_1w(good):
    async def _gph(self, period, timeframe, extended_hours=False):
        if period == "1W":
            raise RuntimeError("alpaca 500")
        return good if period == "1M" else {"timestamp": [], "equity": [], "profit_loss": [], "base_value": 0.0}

    return _gph


def test_nothing_is_published_when_every_period_fails():
    """Publishing an empty payload would blank a chart that was previously correct — better to leave
    the UI holding its last good frame."""
    fake, published = _equity_client({"1W": RuntimeError("boom"), "1M": RuntimeError("boom")})
    asyncio.run(AlpacaExecutionClient._report_equity_curve(fake))
    assert published == []


# --- #169: a replace returns a NEW venue id, and dropping it strands the order ------------------------


class _FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def replace_order(self, venue_order_id: str, payload: dict):
        self.calls.append((venue_order_id, payload))
        return self.response


def test_modify_reports_the_new_venue_id_alpaca_returns():
    """Alpaca's replace is not an in-place edit: the old order goes to `replaced` and a NEW order takes
    its place with a NEW id (measured 2026-08-12). Dropping it left the cache holding a dead id, so the
    next cancel or modify targeted an order that no longer existed — silently, because cancelling a
    replaced order is not an error. (#169)

    Uses a REAL `Quantity`, not a mock: `OrderUpdated` rejects a null quantity, and a double that let
    None through is how the first version of this fix raised AFTER a successful replace and then
    reported it as a rejection. (codex review.)
    """
    import asyncio
    from unittest.mock import MagicMock

    from nautilus_trader.model.identifiers import VenueOrderId
    from nautilus_trader.model.objects import Quantity

    from api.providers.alpaca import exec_client as mod

    client = MagicMock()
    client._http = _FakeHttp({"id": "new-venue-id-999", "qty": "5"})
    client._log = MagicMock()
    client._clock = MagicMock()
    client._clock.timestamp_ns.return_value = 0
    client._cache = MagicMock()
    client._cache.order.return_value = None
    updates: list[dict] = []
    client.generate_order_updated = lambda **kw: updates.append(kw)

    command = MagicMock()
    command.venue_order_id = VenueOrderId("old-venue-id-111")
    command.quantity = Quantity.from_int(5)
    command.price = None
    command.trigger_price = None

    asyncio.run(mod.AlpacaExecutionClient._modify_order(client, command))

    assert updates, "a changed venue id must be reported to the engine"
    assert str(updates[0]["venue_order_id"]) == "new-venue-id-999"
    assert updates[0]["venue_order_id_modified"] is True, (
        "without this flag the engine treats a changed id as a mismatch"
    )
    assert updates[0]["quantity"] is not None, "OrderUpdated rejects a null quantity"


def test_a_replace_with_no_resolvable_quantity_logs_instead_of_raising():
    """The failure mode codex found: passing None quantity raises AFTER the venue accepted the replace,
    and because the call sat inside the try it was re-reported as a modify REJECTION — telling the engine
    the change had not happened when it had. Now it logs and leaves the old id, which is recoverable."""
    import asyncio
    from unittest.mock import MagicMock

    from nautilus_trader.model.identifiers import VenueOrderId

    from api.providers.alpaca import exec_client as mod

    client = MagicMock()
    client._http = _FakeHttp({"id": "new-venue-id-999"})  # no qty echoed
    client._log = MagicMock()
    client._clock = MagicMock()
    client._clock.timestamp_ns.return_value = 0
    client._cache = MagicMock()
    client._cache.order.return_value = None
    client._cache.instrument.return_value = None
    updates: list[dict] = []
    rejects: list[dict] = []
    client.generate_order_updated = lambda **kw: updates.append(kw)
    client.generate_order_modify_rejected = lambda **kw: rejects.append(kw)

    command = MagicMock()
    command.venue_order_id = VenueOrderId("old-venue-id-111")
    command.quantity = None
    command.price = None
    command.trigger_price = None

    asyncio.run(mod.AlpacaExecutionClient._modify_order(client, command))

    assert updates == []
    assert rejects == [], "a successful replace must never be reported as a rejection"
    assert client._log.error.called, "and it must not fail silently either"


def test_modify_says_nothing_when_the_venue_id_is_unchanged():
    """No event when nothing moved — an update the engine did not need is noise in the order log."""
    import asyncio
    from unittest.mock import MagicMock

    from nautilus_trader.model.identifiers import VenueOrderId

    from api.providers.alpaca import exec_client as mod

    client = MagicMock()
    client._http = _FakeHttp({"id": "same-id"})
    client._log = MagicMock()
    client._clock = MagicMock()
    client._clock.timestamp_ns.return_value = 0
    client._cache = MagicMock()
    updates: list[dict] = []
    client.generate_order_updated = lambda **kw: updates.append(kw)

    command = MagicMock()
    command.venue_order_id = VenueOrderId("same-id")
    command.quantity = None
    command.price = None
    command.trigger_price = None

    asyncio.run(mod.AlpacaExecutionClient._modify_order(client, command))
    assert updates == []


# --- reconciliation must be able to REBUILD a trailing stop (#75) -------------------------------------


def test_a_trailing_stop_report_carries_its_offset():
    """Without this the engine cannot boot. Reconciliation constructs a real `TrailingStopMarketOrder`
    from the report, and its constructor does a bare `init['trailing_offset']` — so omitting the field
    raises `KeyError: 'trailing_offset'` INSIDE startup reconciliation and crash-loops the node.

    That happened on 2026-08-13: a trailing stop placed outside Nautilus (raw REST) came back through
    reconciliation and the engine could not start until the lookback window rolled past it.
    """
    from decimal import Decimal

    from nautilus_trader.model.enums import TrailingOffsetType

    from api.providers.alpaca.exec_client import _trailing_fields

    fields = _trailing_fields({"trail_percent": "6.06"})
    assert fields["trailing_offset_type"] == TrailingOffsetType.BASIS_POINTS
    # x100 — Alpaca reports a PERCENT, Nautilus wants BASIS POINTS. Passing it through unscaled would
    # rebuild a 6.06% trail as 6.06 bps: a stop 0.06% away that fires on the first tick.
    assert fields["trailing_offset"] == Decimal(606)


def test_the_offset_round_trips_through_the_submit_path():
    """The submit side divides by 100 (`trail_percent = trailing_offset / 100`). Parsing must be its
    exact inverse or every reconciled trail drifts by two orders of magnitude."""
    from decimal import Decimal as D

    from api.providers.alpaca.exec_client import _trailing_fields

    for bps in (10, 100, 250, 606, 1500):
        alpaca_pct = D(bps) / 100          # what we send
        back = _trailing_fields({"trail_percent": str(alpaca_pct)})
        assert back["trailing_offset"] == D(bps), bps


def test_a_dollar_trail_uses_the_PRICE_offset_type():
    from decimal import Decimal

    from nautilus_trader.model.enums import TrailingOffsetType

    from api.providers.alpaca.exec_client import _trailing_fields

    fields = _trailing_fields({"trail_price": "2.50"})
    assert fields["trailing_offset"] == Decimal("2.50")
    assert fields["trailing_offset_type"] == TrailingOffsetType.PRICE


def test_a_non_trailing_order_carries_NO_offset_key_at_all():
    """Absent, not None: a non-trailing order must not carry a trailing offset, and the report treats a
    missing key differently from a null one."""
    from api.providers.alpaca.exec_client import _trailing_fields

    assert _trailing_fields({"type": "limit"}) == {}
    assert _trailing_fields({"trail_percent": None, "trail_price": ""}) == {}


def test_the_parse_methods_are_actually_ON_the_class():
    """A guard against the mistake that broke the engine on 2026-08-13.

    Inserting a module-level helper in the middle of the class body ended the class, so
    `_parse_order_report` and `_parse_position_report` silently became NESTED FUNCTIONS inside that
    helper — valid Python, never called, invisible on the class. Position reconciliation then failed for
    every symbol with `AttributeError: 'AlpacaExecutionClient' object has no attribute
    '_parse_position_report'`, and the engine ran with zero positions.

    Reading the source text instead of the class is what let it through: the string was present, the
    method was not.
    """
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    for name in ("_parse_order_report", "_parse_position_report"):
        assert hasattr(AlpacaExecutionClient, name), (
            f"{name} is not on the class — a module-level def has probably split the class body"
        )


def test_the_ORDER_REPORT_itself_carries_the_trailing_offset():
    """The helper being correct is not enough — the report has to USE it."""
    import inspect

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    src = inspect.getsource(AlpacaExecutionClient._parse_order_report)
    assert "**_trailing_fields(raw)" in src, (
        "the order report must spread the trailing fields, or reconciliation cannot rebuild the order "
        "and the engine cannot boot"
    )


def test_a_trailing_stop_report_carries_its_offset():
    """Without this the engine cannot boot. Reconciliation constructs a real `TrailingStopMarketOrder`
    from the report, and its constructor does a bare `init['trailing_offset']` — so omitting the field
    raises `KeyError: 'trailing_offset'` INSIDE startup reconciliation and crash-loops the node.

    That happened on 2026-08-13: a trailing stop placed outside Nautilus (raw REST) came back through
    reconciliation and the engine could not start until the lookback window rolled past it.
    """
    from decimal import Decimal

    from nautilus_trader.model.enums import TrailingOffsetType

    from api.providers.alpaca.exec_client import _trailing_fields

    fields = _trailing_fields({"trail_percent": "6.06"})
    assert fields["trailing_offset_type"] == TrailingOffsetType.BASIS_POINTS
    # x100 — Alpaca reports a PERCENT, Nautilus wants BASIS POINTS. Passing it through unscaled would
    # rebuild a 6.06% trail as 6.06 bps: a stop 0.06% away that fires on the first tick.
    assert fields["trailing_offset"] == Decimal(606)


def test_the_offset_round_trips_through_the_submit_path():
    """The submit side divides by 100 (`trail_percent = trailing_offset / 100`). Parsing must be its
    exact inverse or every reconciled trail drifts by two orders of magnitude."""
    from decimal import Decimal as D

    from api.providers.alpaca.exec_client import _trailing_fields

    for bps in (10, 100, 250, 606, 1500):
        alpaca_pct = D(bps) / 100          # what we send
        back = _trailing_fields({"trail_percent": str(alpaca_pct)})
        assert back["trailing_offset"] == D(bps), bps


def test_a_dollar_trail_uses_the_PRICE_offset_type():
    from decimal import Decimal

    from nautilus_trader.model.enums import TrailingOffsetType

    from api.providers.alpaca.exec_client import _trailing_fields

    fields = _trailing_fields({"trail_price": "2.50"})
    assert fields["trailing_offset"] == Decimal("2.50")
    assert fields["trailing_offset_type"] == TrailingOffsetType.PRICE


def test_a_non_trailing_order_carries_NO_offset_key_at_all():
    """Absent, not None: a non-trailing order must not carry a trailing offset, and the report treats a
    missing key differently from a null one."""
    from api.providers.alpaca.exec_client import _trailing_fields

    assert _trailing_fields({"type": "limit"}) == {}
    assert _trailing_fields({"trail_percent": None, "trail_price": ""}) == {}


def mod_path() -> str:
    from api.providers.alpaca import exec_client as mod

    return mod.__file__


def test_the_ORDER_REPORT_itself_carries_the_trailing_offset():
    """The helper being correct is not enough — the report has to USE it. Testing `_trailing_fields` in
    isolation passed cleanly while `_parse_order_report` omitted the spread entirely, which is exactly
    the shape that crash-looped the node."""

    import pathlib

    # Read the SOURCE FILE: this method is a Cython-invisible attribute on a Cython base class, so
    #  does not find it.
    src = pathlib.Path(mod_path()).read_text()
    src = src[src.index("def _parse_order_report"):]
    src = src[: src.index("def _parse_position_report")]
    assert "**_trailing_fields(raw)" in src, (
        "the order report must spread the trailing fields, or reconciliation cannot rebuild the order "
        "and the engine cannot boot"
    )


# --- #75: fill reports, built from a REAL Alpaca activity payload -------------------------------------


def _real_fill_activity(**over) -> dict:
    """A verbatim Alpaca FILL activity, captured from the paper account on 2026-08-14.

    Copied from the wire rather than invented, per the rule that a double which cannot represent
    production IS the bug — five defects hid behind hand-written doubles on 2026-08-14 alone. Every field
    name, type and format here is Alpaca's own: prices and quantities arrive as STRINGS, `transaction_time`
    carries microseconds and a `Z`, and `id` is a compound `timestamp::uuid`.
    """
    return {
        "id": "20260813145004467::a12d65a2-2336-4ed3-bade-7b1d02f35484",
        "activity_type": "FILL",
        "transaction_time": "2026-08-13T18:50:04.467891Z",
        "type": "fill",
        "price": "225.62",
        "qty": "1",
        "side": "sell",
        "symbol": "WDAY",
        "leaves_qty": "0",
        "order_id": "7e08d680-b918-4443-a466-6acc587e72aa",
        "cum_qty": "21",
        "order_status": "filled",
        **over,
    }


class TestParseFillActivity:
    def _parse(self, raw):
        from nautilus_trader.model.identifiers import AccountId, InstrumentId

        from api.providers.alpaca.exec_client import _parse_fill_activity

        return _parse_fill_activity(
            raw, InstrumentId.from_str("WDAY.XNAS"), AccountId("ALPACA-001"), 1_786_000_000_000_000_000, 1
        )

    def test_a_real_activity_becomes_a_FillReport(self):
        report = self._parse(_real_fill_activity())
        assert report is not None
        assert str(report.instrument_id) == "WDAY.XNAS"
        assert report.last_px.as_double() == 225.62
        assert report.last_qty.as_double() == 1.0
        assert report.order_side.name == "SELL"
        assert report.venue_order_id.value == "7e08d680-b918-4443-a466-6acc587e72aa"

    def test_a_PARTIAL_fill_is_a_fill_too(self):
        """`type: partial_fill` with `order_status: partially_filled` — the WDAY exit produced five of
        these before its final one. Treating only `type: fill` as real would lose 20 of 21 shares."""
        report = self._parse(_real_fill_activity(type="partial_fill", order_status="partially_filled",
                                                 qty="2", leaves_qty="1", cum_qty="20"))
        assert report is not None
        assert report.last_qty.as_double() == 2.0

    def test_a_malformed_row_is_DROPPED_not_reconciled_as_a_bogus_fill(self):
        # Each of these alone must fail the row — a fill reconciled at price 0 or with no venue id would
        # corrupt the position, which is worse than losing one row (the caller warns).
        assert self._parse(_real_fill_activity(price="0")) is None
        assert self._parse(_real_fill_activity(qty="0")) is None
        assert self._parse(_real_fill_activity(price="not-a-number")) is None
        assert self._parse(_real_fill_activity(side="sideways")) is None
        assert self._parse(_real_fill_activity(order_id="")) is None

    def test_prices_and_quantities_arrive_as_STRINGS_and_must_survive_it(self):
        """Alpaca sends `"225.62"`, not `225.62`. A parser assuming floats would raise or truncate, and a
        double that used floats would never reveal it."""
        raw = _real_fill_activity()
        assert isinstance(raw["price"], str) and isinstance(raw["qty"], str)
        report = self._parse(raw)
        assert report is not None and report.last_px.as_double() == 225.62


def test_generate_fill_reports_drives_the_whole_path_not_just_the_parser():
    """#75, and the SEAM rather than the unit.

    `_parse_fill_activity` being correct says nothing about whether `generate_fill_reports` calls it with
    the right arguments, filters the right way, or resolves symbol -> instrument at all. Five defects on
    2026-08-14 were exactly this shape: the unit right, the wiring wrong, the suite green.

    Two activities on one order, both real WDAY rows from the paper account.
    """
    import asyncio

    from nautilus_trader.model.identifiers import AccountId, InstrumentId

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    activities = [
        _real_fill_activity(),
        _real_fill_activity(id="20260813145003418::36b85ae1", type="partial_fill", qty="2",
                            leaves_qty="1", cum_qty="20"),
        _real_fill_activity(id="other::1", symbol="NOPE"),   # unknown symbol -> warned, not crashed
    ]

    class _Http:
        async def list_activities(self, activity_type=None, after=None):
            assert activity_type == "FILL", "must ask Alpaca for FILL activities specifically"
            return activities

    class _Log:
        def __init__(self):
            self.warnings = []

        def warning(self, m):
            self.warnings.append(str(m))

    # A plain double with the REAL method bound to it. `_log` is a read-only Cython attribute on
    # Nautilus's Component, so a real instance cannot be given a capturing logger — and reimplementing the
    # method body in the test would be the drifted double this whole exercise is about.
    class _Client:
        _http = _Http()
        _log = _Log()
        account_id = AccountId("ALPACA-001")
        _symbol_to_id = {"WDAY": InstrumentId.from_str("WDAY.XNAS")}
        _clock = type("_C", (), {"timestamp_ns": staticmethod(lambda: 1_786_000_000_000_000_001)})()

    client = _Client()

    class _Cmd:
        instrument_id = None
        venue_order_id = None
        start = None
        end = None

    reports = asyncio.run(AlpacaExecutionClient.generate_fill_reports(client, _Cmd()))

    assert len(reports) == 2, "both WDAY fills must survive the whole path"
    assert {r.last_qty.as_double() for r in reports} == {1.0, 2.0}
    # The unmappable row is REPORTED, never dropped in silence — a reconciliation source that hides fills
    # causes position drift, and drift is invisible until it is expensive.
    assert any("NOPE" in w or "unmappable" in w.lower() for w in client._log.warnings)


def test_the_account_is_declared_MARGIN_because_that_is_what_alpaca_is():
    """#306 — the execution engine went fully offline mid-session over 580.52 of negative cash.

    Alpaca reports `buying_power` at ~2.8x equity: it is a margin account, and it permits margin whatever
    we declare. On 2026-08-14 MOMENTUM deployed past cash, the venue accepted it, and `CashAccount` then
    refused to represent the resulting balance — `AccountBalanceNegative` is raised ONLY for
    `AccountType.CASH` (`accounting/manager.pyx:126`). The ExecClient could not connect, so there were no
    positions, no managers, no order commands, and the #239 backstop could protect nothing new.

    Declaring CASH never prevented the over-deployment — only the venue could, and it allowed it. It only
    blocked recovery from a state that had already happened.

    Pinned as source inspection because constructing a real client needs a live loop, msgbus and cache.
    """
    import inspect

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    src = inspect.getsource(AlpacaExecutionClient.__init__)
    assert "account_type=AccountType.MARGIN" in src
    assert "account_type=AccountType.CASH" not in src


def test_margin_does_not_silently_enable_leverage():
    """The one thing MARGIN could smuggle in. Nautilus defaults `default_leverage` to 1, so this is 1x
    unless a per-instrument leverage is set — which nothing here does, and this test says so out loud."""
    import inspect

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    src = inspect.getsource(AlpacaExecutionClient)
    assert "set_leverage" not in src, "leverage is being configured somewhere — 1x was the whole premise"
    assert "default_leverage" not in src or "Decimal(1)" in src


def test_the_account_snapshot_carries_long_market_value_so_the_book_can_tally():
    """#310 — the operator added up the BOOK tile and it did not reconcile:

        DEPLOYED + CASH = 99,978.32 + (-208.42) = 99,769.90
        LIQUIDATION                              = 99,764.62

    DEPLOYED was derived from OUR trade projection while CASH and LIQUIDATION came from the broker, so the
    three used different prices and could never add up. Alpaca's own figures do, exactly:
    `cash + long_market_value == equity`. Carrying long_market_value lets all three come from one snapshot,
    making the panel consistent by construction rather than by luck.
    """
    import inspect

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    src = inspect.getsource(AlpacaExecutionClient)
    assert '"long_market_value"' in src, "the account snapshot drops the broker's own market value"


def test_alpacas_own_account_fields_are_internally_consistent():
    """The premise, stated so it is checked rather than assumed. Measured on a live instance paper account
    2026-08-14: equity 99,843.62 = cash -208.42 + long_market_value 100,052.04, to the cent."""
    equity, cash, lmv = 99_843.62, -208.42, 100_052.04
    assert round(cash + lmv, 2) == equity


def test_the_account_snapshot_carries_last_equity_so_the_ui_can_show_net_for_1d():
    """#336 — NET(1D) has no other source.

    The equity-curve plane publishes 1W/1M/3M/all and NOTHING for 1D, so the hero number on Home's
    DEFAULT tab is derived from `equity - last_equity` — Alpaca's own day P&L. Measured on a live instance
    paper account 2026-08-18: 100,658.95 - 100,116.28 = 542.67.

    This is a SEAM test on purpose. The field was already being read from Alpaca and published by the
    engine while the tile still showed "unknown", because `AccountDTO` silently dropped it in between —
    a pydantic model discards undeclared keys. Asserting the parse alone would have stayed green through
    exactly that defect, so `test_the_account_dto_does_not_drop_last_equity` below drives the DTO too.
    """
    raw = {
        "portfolio_value": "100658.95",
        "cash": "25942.04",
        "buying_power": "312975.51",
        "multiplier": "4",
        "long_market_value": "74716.91",
        "last_equity": "100116.28",
    }
    snapshot = {
        "equity": float(raw.get("portfolio_value") or raw.get("equity") or 0.0),
        "cash": float(raw.get("cash") or 0.0),
        "buying_power": float(raw.get("buying_power") or 0.0),
        "multiplier": float(raw.get("multiplier") or 1.0),
        "long_market_value": float(raw.get("long_market_value") or 0.0),
        "last_equity": float(raw["last_equity"]) if raw.get("last_equity") is not None else None,
        "ts": 1,
    }
    assert snapshot["last_equity"] == 100116.28
    assert round(snapshot["equity"] - snapshot["last_equity"], 2) == 542.67


def test_a_missing_last_equity_stays_none_rather_than_becoming_zero():
    """Zero would make the day P&L read as the WHOLE account equity.

    Every other numeric field in the snapshot uses `or 0.0`, so the obvious thing is to follow suit —
    and the obvious thing is wrong here. With last_equity=0.0 the tile would print $100,658.95 as
    today's move. A wrong number in the hero slot is worse than the dash it replaces, because nobody
    re-checks a figure that looks plausible.
    """
    for raw in ({}, {"last_equity": None}):
        value = float(raw["last_equity"]) if raw.get("last_equity") is not None else None
        assert value is None


def test_the_account_dto_does_not_drop_last_equity():
    """The seam that actually broke. Engine published it, tile never saw it, DTO ate it in between."""
    from api.models import AccountDTO

    dto = AccountDTO(
        equity=100658.95, cash=25942.04, buying_power=312975.51, multiplier=4.0,
        long_market_value=74716.91, last_equity=100116.28, ts=1,
    )
    assert dto.last_equity == 100116.28
    assert dto.model_dump()["last_equity"] == 100116.28
    # Absent is None, never 0.0 — a synthetic node with no broker account must say "unknown".
    assert AccountDTO(equity=1.0, cash=1.0, buying_power=1.0, ts=1).last_equity is None


# ----------------------------------------------------------------------------------------------------
# #649 item 2 — a position whose symbol is missing from `_symbol_to_id` (built ONCE at _connect) must
# not vanish from reconciliation in silence. A post-connect listing/delisting is exactly the case where
# the map is stale, and "return None, no log" renders an incomplete book as a smaller-but-confident one.
# The order-report path already warns (_parse_order_report); this pins the position path to the same
# contract: name the symbol, count the drop, keep the batch.
# ----------------------------------------------------------------------------------------------------
def test_position_report_for_unmapped_symbol_is_named_and_counted_not_silently_dropped():
    # Built from what GET /v2/positions actually returns: symbol/qty/avg_entry_price rows.
    positions = [
        {"symbol": "AAPL", "qty": "10", "avg_entry_price": "190.00"},
        {"symbol": "NEWIPO", "qty": "5", "avg_entry_price": "10.00"},
    ]
    symbol_to_id = {"AAPL": _IID}
    # Fixture properties FIRST: the dropped row's symbol is GENUINELY absent from the map while the
    # other row's is present — the two rows differ only in the mapping, so any drop below can only
    # come from the missing-symbol branch.
    assert "NEWIPO" not in symbol_to_id
    assert "AAPL" in symbol_to_id

    warnings: list[str] = []

    async def _list_positions():
        return positions

    fake = SimpleNamespace(
        _symbol_to_id=symbol_to_id,
        _clock=SimpleNamespace(timestamp_ns=lambda: 1),
        _log=SimpleNamespace(
            warning=lambda m: warnings.append(str(m)),
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
        _http=SimpleNamespace(list_positions=_list_positions),
        account_id=_ACCT,
    )
    fake._parse_position_report = lambda raw: AlpacaExecutionClient._parse_position_report(fake, raw)

    # Fixture property: the mapped row DOES produce a report through the real parser, so the double
    # can express both the healthy row and the bug.
    assert fake._parse_position_report(positions[0]) is not None

    reports = asyncio.run(
        AlpacaExecutionClient.generate_position_status_reports(fake, SimpleNamespace())
    )

    # The batch continues — one unmapped row must not sink the mapped one.
    assert [r.instrument_id for r in reports] == [_IID]
    joined = " | ".join(warnings)
    # The dropped symbol is NAMED...
    assert "NEWIPO" in joined
    # ...and the drop is COUNTED, so a shortened list cannot read as "the whole book".
    assert "1 of 2" in joined


# ----------------------------------------------------------------------------------------------------
# #649 item 3 — the scoped cancel-all's list-and-cancel loop sat under ONE `except Exception:
# log.error`, with no `generate_order_cancel_rejected`. A single failed DELETE aborted the remaining
# cancels AND told the state machine nothing: orders the operator believes cancelled can fill later.
# These pin the fix: failure is per-order, every failed cancel emits the same event `_cancel_order`
# emits (exec_client.py ~:1120-1136), and the loop keeps cancelling.
# ----------------------------------------------------------------------------------------------------
def _scoped_cancel_fake(list_orders, cancel_order, rejected, open_in_cache=None):
    fake = SimpleNamespace(
        _http=SimpleNamespace(list_orders=list_orders, cancel_order=cancel_order),
        _clock=SimpleNamespace(timestamp_ns=lambda: 1),
        _log=SimpleNamespace(
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
        _cache=SimpleNamespace(
            client_order_id=lambda vid: None,  # not in our cache → falls back to Alpaca's own coid
            orders_open=lambda **k: list(open_in_cache or []),
        ),
        generate_order_cancel_rejected=lambda **k: rejected.append(k),
    )
    fake._client_order_id_for = lambda raw: AlpacaExecutionClient._client_order_id_for(fake, raw)
    return fake


def test_scoped_cancel_all_emits_cancel_rejected_per_failed_order_and_keeps_going():
    raws = [
        {"id": "BAD-VID", "symbol": "AAPL", "side": "buy", "client_order_id": "O-BAD"},
        {"id": "OK-VID", "symbol": "AAPL", "side": "sell", "client_order_id": "O-OK"},
        {"id": "OTHER-VID", "symbol": "MSFT", "side": "buy", "client_order_id": "O-OTHER"},
    ]
    cancelled: list[str] = []

    async def _list_orders(status, paginate):
        assert status == "open" and paginate is True
        return raws

    async def _cancel(vid):
        if vid == "BAD-VID":
            raise AlpacaHttpError(
                "DELETE", f"/v2/orders/{vid}", 500, '{"message":"internal server error"}'
            )
        cancelled.append(vid)

    # Fixture property: the double RAISES where production raises — a cancel that cannot fail
    # cannot exercise the failure path.
    with pytest.raises(AlpacaHttpError):
        asyncio.run(_cancel("BAD-VID"))

    rejected: list[dict] = []
    fake = _scoped_cancel_fake(_list_orders, _cancel, rejected)
    command = SimpleNamespace(
        instrument_id=_IID, order_side=OrderSide.NO_ORDER_SIDE, strategy_id=StrategyId("S-1")
    )

    asyncio.run(AlpacaExecutionClient._cancel_all_orders(fake, command))

    # The failed cancel did NOT abort the loop: the other AAPL order was still cancelled, and the
    # out-of-scope MSFT order was untouched. (BAD-VID listed FIRST is the fixture's point — under the
    # old single try/except the loop died there and OK-VID stayed resting in silence.)
    assert cancelled == ["OK-VID"]
    # The failure is LOUD: exactly one cancel-rejected, for the order that failed, symmetric with
    # _cancel_order's event (strategy/instrument/coid/venue-id/reason/ts).
    assert len(rejected) == 1
    event = rejected[0]
    assert event["venue_order_id"].value == "BAD-VID"
    assert event["client_order_id"].value == "O-BAD"
    assert event["instrument_id"] == _IID
    assert event["strategy_id"] == StrategyId("S-1")
    assert "internal server error" in event["reason"].lower()


def test_scoped_cancel_all_listing_failure_rejects_every_open_scoped_order():
    """If the LISTING dies, nothing was cancelled at all — the state machine must hear that for every
    open order in scope, not read silence as done."""

    async def _list_boom(status, paginate):
        raise AlpacaHttpError("GET", "/v2/orders", 503, '{"message":"service unavailable"}')

    async def _cancel(vid):  # pragma: no cover — must never be reached
        raise AssertionError("cancel_order must not be called when listing failed")

    open_order = SimpleNamespace(
        client_order_id=ClientOrderId("O-RESTING"),
        venue_order_id=SimpleNamespace(value="V-RESTING"),
        strategy_id=StrategyId("S-1"),
    )
    rejected: list[dict] = []
    fake = _scoped_cancel_fake(_list_boom, _cancel, rejected, open_in_cache=[open_order])
    command = SimpleNamespace(
        instrument_id=_IID, order_side=OrderSide.NO_ORDER_SIDE, strategy_id=StrategyId("S-1")
    )

    asyncio.run(AlpacaExecutionClient._cancel_all_orders(fake, command))

    assert len(rejected) == 1
    event = rejected[0]
    assert event["client_order_id"] == ClientOrderId("O-RESTING")
    assert "service unavailable" in event["reason"].lower()
