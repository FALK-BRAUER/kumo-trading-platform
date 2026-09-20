"""Offline tests for RedisConsumer (#20) — frame routing, dedup, Node-protocol shape. No Redis needed:
we drive `_apply` directly with the exact fields the engine's UiFeedStrategy XADDs."""

from __future__ import annotations

import asyncio
import json

from api.consumer import RedisConsumer
from api.feed_config import load_feed_config


def _bar_frame(symbol: str, ts: int, close: float, *, granularity: str = "1d", historical: bool = False):
    return {
        "type": "bar",
        "payload": json.dumps(
            {
                "instrument_id": symbol,
                "granularity": granularity,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1000.0,
                "ts_event": ts,
                "historical": historical,
            }
        ),
    }


def _positions_frame(*symbols: str):
    return {
        "type": "positions",
        "payload": json.dumps(
            {
                "positions": [
                    {
                        "instrument_id": s,
                        "side": "LONG",
                        "quantity": 10.0,
                        "avg_px_open": 100.0,
                        "realized_pnl": "0.00 USD",
                        "strategy_id": "MANUAL-001",
                    }
                    for s in symbols
                ]
            }
        ),
    }


def _consumer(*, bridge_live: bool = True) -> RedisConsumer:
    """A consumer with a LIVE BRIDGE by default.

    A consumer that has never seen a health frame is a DEAD bridge, and since 2026-08-26 the trades
    plane returns nothing in that state — deliberately: it served a `seeding` frame from boot for
    fifteen minutes while the engine was publishing a full book, and the UI showed one position out of
    thirty-two.

    These tests are about FRAME HANDLING, not staleness, so they need a bridge that is up. Leaving
    `_health_at` unset made them exercise a dead-bridge consumer without saying so — the fixture was
    quietly relying on the missing guard.
    """
    import time as _t

    c = RedisConsumer(load_feed_config())
    if bridge_live:
        c._health_at = _t.monotonic()
    return c


def test_bar_routing_and_dedup():
    c = _consumer()
    c._apply(_bar_frame("AAPL.XNAS", 1000, 190.0, historical=True))
    c._apply(_bar_frame("AAPL.XNAS", 2000, 191.0, historical=False))
    c._apply(_bar_frame("AAPL.XNAS", 2000, 191.5, historical=False))  # same ts → dedup/overwrite
    bars = asyncio.run(c.bars_for("AAPL.XNAS", "1d"))
    assert [b.ts_event for b in bars] == [1000, 2000]  # deduped by ts, sorted
    assert bars[-1].close == 191.5  # latest write wins


def test_bars_default_series_across_universe():
    c = _consumer()
    c._apply(_bar_frame("AAPL.XNAS", 1000, 190.0))
    c._apply(_bar_frame("MSFT.XNAS", 1000, 400.0))
    c._apply(_bar_frame("AAPL.XNAS", 500, 189.0, granularity="1m"))  # not default granularity
    default = c.bars()
    ids = {b.instrument_id for b in default}
    assert ids == {"AAPL.XNAS", "MSFT.XNAS"}  # 1m bar excluded from the default series
    assert [b.ts_event for b in default] == sorted(b.ts_event for b in default)


def test_positions_snapshot_replaces():
    c = _consumer()
    c._apply(_positions_frame("AAPL.XNAS", "MSFT.XNAS"))
    c._apply(_positions_frame("AAPL.XNAS"))  # newer snapshot replaces
    pos = c.positions()
    assert [p.instrument_id for p in pos] == ["AAPL.XNAS"]
    assert pos[0].side == "LONG"


def _trade_frame(cycle_id: str, state: str, instrument: str = "AAPL.XNAS"):
    return {
        "type": "trades",
        "payload": json.dumps(
            {
                "trades": [
                    {
                        "account_id": "ALPACA-PAPER",
                        "client_id": "ALPACA",
                        "instrument_id": instrument,
                        "strategy_id": "MANUAL-001",
                        "cycle_id": cycle_id,
                        "manager_id": None,
                        "state": state,
                        "side": "LONG" if state == "HELD" else "FLAT",
                        "quantity": 100.0 if state == "HELD" else 0.0,
                        "is_capital_deployed": state == "HELD",
                        "is_engaged": state != "CLOSED",
                        "avg_px_open": 90.0 if state == "HELD" else None,
                        "realized_pnl": "0.00 USD",
                        "leg_count": 1,
                        "opened_ts": 1,
                        "closed_ts": None,
                        "last_event_ts": 5,
                        "working_orders": [],
                    }
                ],
                "ts": 10,
            }
        ),
    }


def test_trades_snapshot_replaces_and_sorts_held_first():
    c = _consumer()
    c._apply(_trade_frame("cyc-1", "CLOSED", "MSFT.XNAS"))
    c._apply(_trade_frame("cyc-2", "HELD", "AAPL.XNAS"))  # newer snapshot replaces the whole plane
    trades = c.trades()
    assert [t.cycle_id for t in trades] == ["cyc-2"]
    assert trades[0].state == "HELD"
    assert trades[0].client_id == "ALPACA"  # exec client threaded through


def test_bad_payload_ignored():
    c = _consumer()
    c._apply({"type": "bar", "payload": "not json"})
    c._apply({"type": "unknown", "payload": "{}"})
    assert c.bars() == []
    assert c.positions() == []


def test_consumer_stores_fill_frame():
    import json

    from api.consumer import RedisConsumer
    c = RedisConsumer.__new__(RedisConsumer)
    c._fills = []
    c._MAX_FILLS = 500
    fill = {"instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 5.0, "price": 100.0, "ts_event": 1, "order_type": "MARKET", "strategy_id": "MANUAL"}
    c._apply({"type": "fill", "payload": json.dumps(fill)})
    fills = c.fills()
    assert len(fills) == 1 and fills[0].instrument_id == "AAPL.XNAS" and fills[0].side == "BUY"


# --- Orders blotter (#33) — event upsert + snapshot reconcile + terminal pruning -----------------
def _order_dict(coid: str, *, status: str = "ACCEPTED", ts: int = 1000, symbol: str = "AAPL.XNAS"):
    return {
        "client_order_id": coid,
        "venue_order_id": None,
        "instrument_id": symbol,
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10.0,
        "filled_qty": 0.0,
        "leaves_qty": 10.0,
        "price": 190.0,
        "trigger_price": None,
        "time_in_force": "DAY",
        "status": status,
        "avg_px": None,
        "ts_last": ts,
        "strategy_id": "MANUAL",
        "tags": [],
    }


def _order_event(coid: str, **over):
    return {"type": "order", "payload": json.dumps(_order_dict(coid, **over))}


def _orders_snapshot(*dicts, ts=None):
    payload = {"orders": list(dicts)}
    if ts is not None:
        payload["ts"] = ts
    return {"type": "orders", "payload": json.dumps(payload)}


def test_order_event_upserts_and_updates_status():
    c = _consumer()
    c._apply(_order_event("c1", status="ACCEPTED", ts=1000))
    c._apply(_order_event("c1", status="FILLED", ts=2000))  # same order, later event → replace
    orders = c.orders()
    assert len(orders) == 1
    assert orders[0].client_order_id == "c1" and orders[0].status == "FILLED"


def test_orders_sort_working_before_terminal_then_newest():
    c = _consumer()
    c._apply(_order_event("working_old", status="ACCEPTED", ts=1000))
    c._apply(_order_event("working_new", status="SUBMITTED", ts=3000))
    c._apply(_order_event("done", status="CANCELED", ts=5000))
    ids = [o.client_order_id for o in c.orders()]
    # working first (newest working ahead of older working), terminal last even though it's newest overall
    assert ids == ["working_new", "working_old", "done"]


def test_orders_snapshot_reconciles_open_set():
    c = _consumer()
    c._apply(_orders_snapshot(_order_dict("a", ts=1000), _order_dict("b", ts=1000)))
    c._apply(_orders_snapshot(_order_dict("a", ts=2000, status="ACCEPTED")))  # b absent, a refreshed
    a = {o.client_order_id: o for o in c.orders()}
    assert a["a"].ts_last == 2000  # snapshot refreshed a's fields


def test_snapshot_drops_stale_working_order_missed_terminal():
    c = _consumer()
    # b is working, then a later snapshot no longer lists it (its CANCELED/FILLED event was missed).
    c._apply(_orders_snapshot(_order_dict("a", status="ACCEPTED"), _order_dict("b", status="ACCEPTED")))
    c._apply(_orders_snapshot(_order_dict("a", status="ACCEPTED")))  # b gone from open set
    ids = {o.client_order_id for o in c.orders()}
    assert ids == {"a"}  # phantom working order dropped — no leak, no ghost row


def test_snapshot_keeps_recorded_terminal_orders():
    c = _consumer()
    c._apply(_order_event("done", status="FILLED", ts=1))  # terminal, recorded via event
    c._apply(_orders_snapshot(_order_dict("a", status="ACCEPTED")))  # snapshot has only the working one
    ids = {o.client_order_id for o in c.orders()}
    assert ids == {"a", "done"}  # terminal order retained (absent from open snapshot, but not 'working')


def test_terminal_orders_pruned_working_kept():
    c = _consumer()
    c._apply(_order_event("keep_working", status="ACCEPTED", ts=1))
    for i in range(c._MAX_TERMINAL_ORDERS + 20):
        c._apply(_order_event(f"t{i}", status="FILLED", ts=i + 10))
    orders = {o.client_order_id: o for o in c.orders()}
    terminal = [o for o in orders.values() if o.status in c._TERMINAL_STATUS]
    assert len(terminal) == c._MAX_TERMINAL_ORDERS  # bounded
    assert "keep_working" in orders  # working never pruned


# --- quote plane (#40) ---------------------------------------------------------------------------
def test_quote_routing_latest_per_symbol():
    c = _consumer()
    def qframe(sym, bid, ask, ts):
        return {"type": "quote", "payload": json.dumps(
            {"instrument_id": sym, "bid": bid, "ask": ask, "bid_size": 100.0, "ask_size": 200.0, "ts_event": ts})}
    c._apply(qframe("AAPL.XNAS", 189.99, 190.01, 1))
    c._apply(qframe("AAPL.XNAS", 190.10, 190.12, 2))  # newer quote overwrites
    c._apply(qframe("MSFT.XNAS", 410.00, 410.05, 1))
    quotes = {q.instrument_id: q for q in c.quotes()}
    assert quotes["AAPL.XNAS"].bid == 190.10 and quotes["AAPL.XNAS"].ask == 190.12
    assert quotes["MSFT.XNAS"].ask == 410.05
    assert len(c.quotes()) == 2


# --- vwap plane (#182 follow-up, watchlist KPI Phase 2) -------------------------------------------
def test_vwap_routing_latest_per_symbol():
    c = _consumer()
    def vframe(sym, vwap, ts, session_date="2026-07-27"):
        return {"type": "vwap", "payload": json.dumps(
            {"instrument_id": sym, "vwap": vwap, "session_date": session_date, "ts_event": ts})}
    c._apply(vframe("AAPL.XNAS", 190.00, 1))
    c._apply(vframe("AAPL.XNAS", 190.25, 2))  # newer vwap overwrites
    c._apply(vframe("MSFT.XNAS", 410.00, 1))
    vwaps = {v.instrument_id: v for v in c.vwaps()}
    assert vwaps["AAPL.XNAS"].vwap == 190.25
    assert vwaps["MSFT.XNAS"].vwap == 410.00
    assert len(c.vwaps()) == 2


def test_vwap_malformed_frame_is_skipped_not_fatal():
    c = _consumer()
    c._apply({"type": "vwap", "payload": json.dumps({"vwap": 1.0})})  # missing instrument_id
    assert c.vwaps() == []


def test_vwap_null_frame_tombstones_the_symbol():
    # The engine publishes `vwap: null` at the exact moment a new session starts (codex review, Phase 2)
    # — a prior session's value must not linger past its own session boundary.
    c = _consumer()
    c._apply({"type": "vwap", "payload": json.dumps(
        {"instrument_id": "AAPL.XNAS", "vwap": 190.00, "session_date": "2026-07-27", "ts_event": 1})})
    assert {v.instrument_id for v in c.vwaps()} == {"AAPL.XNAS"}
    c._apply({"type": "vwap", "payload": json.dumps(
        {"instrument_id": "AAPL.XNAS", "vwap": None, "session_date": "2026-07-28", "ts_event": 2})})
    assert c.vwaps() == []  # tombstoned — no stale prior-session value survives


def test_vwap_null_frame_for_unknown_symbol_is_a_harmless_noop():
    c = _consumer()
    c._apply({"type": "vwap", "payload": json.dumps(
        {"instrument_id": "MSFT.XNAS", "vwap": None, "session_date": "2026-07-28", "ts_event": 1})})
    assert c.vwaps() == []


# --- today_range plane (#182 follow-up, watchlist KPI Phase 3) ------------------------------------
def test_today_range_routing_latest_per_symbol():
    c = _consumer()
    def rframe(sym, high, low, prev_close, ts):
        return {"type": "today_range", "payload": json.dumps(
            {"instrument_id": sym, "high": high, "low": low, "prev_close": prev_close, "ts_event": ts})}
    c._apply(rframe("AAPL.XNAS", 172.34, 169.80, 169.75, 1))
    c._apply(rframe("AAPL.XNAS", 173.00, 169.80, 169.75, 2))  # newer snapshot overwrites
    c._apply(rframe("MSFT.XNAS", 410.00, 405.00, 404.50, 1))
    ranges = {r.instrument_id: r for r in c.today_ranges()}
    assert ranges["AAPL.XNAS"].high == 173.00
    assert ranges["MSFT.XNAS"].prev_close == 404.50
    assert len(c.today_ranges()) == 2


def test_today_range_malformed_frame_is_skipped_not_fatal():
    c = _consumer()
    c._apply({"type": "today_range", "payload": json.dumps({"high": 1.0})})  # missing instrument_id
    assert c.today_ranges() == []


def test_today_range_null_frame_tombstones_the_symbol():
    # The engine publishes `high: null` when a previously-published value's dailyBar has gone stale
    # (codex review, Phase 3) — a prior day's range must not linger past its own day.
    c = _consumer()
    c._apply({"type": "today_range", "payload": json.dumps(
        {"instrument_id": "AAPL.XNAS", "high": 172.34, "low": 169.80, "prev_close": 169.75, "ts_event": 1})})
    assert {r.instrument_id for r in c.today_ranges()} == {"AAPL.XNAS"}
    c._apply({"type": "today_range", "payload": json.dumps(
        {"instrument_id": "AAPL.XNAS", "high": None, "low": None, "prev_close": None, "ts_event": 2})})
    assert c.today_ranges() == []


def test_today_range_null_frame_for_unknown_symbol_is_a_harmless_noop():
    c = _consumer()
    c._apply({"type": "today_range", "payload": json.dumps(
        {"instrument_id": "MSFT.XNAS", "high": None, "low": None, "prev_close": None, "ts_event": 1})})
    assert c.today_ranges() == []


# --- fundamentals plane (#182 follow-up, watchlist KPI Phase 2) -----------------------------------
def test_fundamentals_routing_latest_per_symbol():
    c = _consumer()
    def ffrm(sym, market_cap, ts):
        return {"type": "fundamentals", "payload": json.dumps({
            "instrument_id": sym, "market_cap": market_cap, "beta": 1.0, "eps": 2.0, "pe": 3.0,
            "dividend_amount": 0.5, "as_of": "2026-07-27", "ts_event": ts,
        })}
    c._apply(ffrm("AAPL.XNAS", 1_000_000, 1))
    c._apply(ffrm("AAPL.XNAS", 2_000_000, 2))  # newer fetch overwrites
    c._apply(ffrm("MSFT.XNAS", 3_000_000, 1))
    fundamentals = {f.instrument_id: f for f in c.fundamentals()}
    assert fundamentals["AAPL.XNAS"].market_cap == 2_000_000
    assert fundamentals["MSFT.XNAS"].market_cap == 3_000_000
    assert len(c.fundamentals()) == 2


def test_fundamentals_no_tombstone_prior_value_persists_through_a_failed_cycle():
    # Unlike vwap/today_range, fundamentals never tombstone — the engine simply doesn't publish anything
    # for a symbol on a failed fetch cycle, and the consumer's prior value correctly just sits there.
    c = _consumer()
    c._apply({"type": "fundamentals", "payload": json.dumps({
        "instrument_id": "AAPL.XNAS", "market_cap": 1.0, "beta": 1.0, "eps": 1.0, "pe": 1.0,
        "dividend_amount": 1.0, "as_of": "2026-07-27", "ts_event": 1,
    })})
    assert len(c.fundamentals()) == 1
    # No second frame arrives (simulating a failed engine-side fetch cycle) — nothing to _apply.
    assert len(c.fundamentals()) == 1
    assert c.fundamentals()[0].market_cap == 1.0


def test_fundamentals_malformed_frame_is_skipped_not_fatal():
    c = _consumer()
    c._apply({"type": "fundamentals", "payload": json.dumps({"market_cap": 1.0})})  # missing instrument_id
    assert c.fundamentals() == []


# --- account plane (#41) -------------------------------------------------------------------------
def test_account_routing_latest_snapshot():
    c = _consumer()
    assert c.account() is None  # none until the first frame
    def aframe(equity, cash, bp, mult, ts):
        return {"type": "account", "payload": json.dumps(
            {"equity": equity, "cash": cash, "buying_power": bp, "multiplier": mult, "ts": ts})}
    c._apply(aframe(100000.0, 40000.0, 80000.0, 2.0, 1))
    c._apply(aframe(101500.0, 39000.0, 78000.0, 2.0, 2))  # newer snapshot overwrites
    acc = c.account()
    assert acc is not None and acc.equity == 101500.0 and acc.cash == 39000.0
    assert acc.buying_power == 78000.0 and acc.multiplier == 2.0  # margin: bp ≈ 2 × equity


# --- latest-state planes on Redis keys (#56) -----------------------------------------------------
class _FakeRedis:
    """Minimal async redis stub — only GET, backed by a dict of key→JSON-string."""

    def __init__(self, keys: dict[str, str]):
        self._keys = keys

    async def get(self, key: str):
        return self._keys.get(key)


def test_state_poll_reads_keys_and_applies():
    """The state planes now arrive via `ui:state:*` KEYS, not the stream — _poll_state_once GETs each and
    feeds the SAME _apply handlers, so positions/account/health land exactly as a stream frame would."""
    c = _consumer()
    c._redis = _FakeRedis({
        "ui:state:positions": _positions_frame("AAPL.XNAS")["payload"],
        "ui:state:account": json.dumps(
            {"equity": 100000.0, "cash": 40000.0, "buying_power": 80000.0, "multiplier": 2.0, "ts": 1}),
        "ui:state:health": json.dumps({"engine_ok": True, "last_tick_ts": 5000, "ts": 1}),
        # ui:state:orders intentionally absent → GET returns None → skipped, no crash
    })
    asyncio.run(c._poll_state_once())
    assert [p.instrument_id for p in c.positions()] == ["AAPL.XNAS"]
    assert c.account() is not None and c.account().equity == 100000.0
    assert c.health()["engine_ok"] is True and c.health()["last_tick_ts"] == 5000


def test_stale_snapshot_does_not_drop_a_fresher_streamed_order():
    """#56 causal-ordering fix: the orders snapshot is now polled off a 2s-lagged key while `order` events
    still stream immediately. A snapshot taken BEFORE a just-placed order must not drop that working order."""
    c = _consumer()
    # A fresh working order arrives via the stream at ts=5000 (e.g. the trader just clicked Buy).
    c._apply(_order_event("fresh", status="ACCEPTED", ts=5000))
    # A STALE snapshot (taken at ts=1000, before the order existed) is then polled — order absent from it.
    c._apply(_orders_snapshot(_order_dict("old", status="ACCEPTED", ts=900), ts=1000))
    ids = {o.client_order_id for o in c.orders()}
    assert "fresh" in ids  # NOT dropped — the snapshot simply predates it
    # A snapshot taken AFTER the order (ts=6000) that omits it → it really closed → drop.
    c._apply(_orders_snapshot(_order_dict("old", status="ACCEPTED", ts=900), ts=6000))
    assert "fresh" not in {o.client_order_id for o in c.orders()}


def test_stale_snapshot_does_not_revert_a_fresher_streamed_status():
    """A stale snapshot carrying an OLD status for an order must not overwrite a newer streamed event.

    THE SNAPSHOT IS OLDER THAN THE EVENT (ts 4000 < 5000) — that is the #56 lag this guards. A snapshot
    the engine took AFTER emitting FILLED cannot list the order as open unless its truth was rewritten,
    and that case is #816's, below."""
    c = _consumer()
    c._apply(_order_event("x", status="FILLED", ts=5000))  # newest truth: filled
    c._apply(_orders_snapshot(_order_dict("x", status="ACCEPTED", ts=1000), ts=4000))  # lagged ACCEPTED
    order = next(o for o in c.orders() if o.client_order_id == "x")
    assert order.status == "FILLED"  # not reverted to the stale ACCEPTED


def test_a_newer_snapshot_that_reopens_a_terminal_order_is_the_engines_rewritten_truth():
    """#816 — measured 2026-09-09: three stops REJECTED (09-04) in the api, revived to ACCEPTED in the
    engine's cache by the #807 repair; their newest event is the original OrderAccepted (08-31), older
    than the REJECTED the api held, so the row-ts gate kept REJECTED for four hours. The engine's
    snapshot is the authority on what is OPEN: a snapshot taken AFTER the terminal event that lists
    the order open means the engine's truth changed under us — take it."""
    c = _consumer()
    c._apply(_order_event("corpse", status="REJECTED", ts=5000))        # 09-04 08:46 in the api's memory
    c._apply(_orders_snapshot(_order_dict("corpse", status="ACCEPTED", ts=1000), ts=6000))  # engine, after repair
    order = next(o for o in c.orders() if o.client_order_id == "corpse")
    assert order.status == "ACCEPTED"


def test_the_reopen_rule_does_not_fire_on_a_lagged_snapshot():
    """Same shape, snapshot OLDER than the terminal event: that is lag, not a rewrite. Terminal stands."""
    c = _consumer()
    c._apply(_order_event("x", status="REJECTED", ts=5000))
    c._apply(_orders_snapshot(_order_dict("x", status="ACCEPTED", ts=1000), ts=4000))
    assert next(o for o in c.orders() if o.client_order_id == "x").status == "REJECTED"


def test_health_liveness_not_refreshed_by_stale_key():
    """A dead engine leaves the health KEY frozen; the api keeps polling and GETs the SAME value forever.
    Liveness (_health_at) must advance ONLY when the engine's ts advances — else a dead engine looks alive."""
    c = _consumer()
    def hframe(ts):
        return {"type": "health", "payload": json.dumps(
            {"engine_ok": True, "last_tick_ts": 5000, "ts": ts})}
    c._apply(hframe(1000))
    first = c._health_at
    assert first > 0
    c._apply(hframe(1000))  # same ts (stale key re-read) → liveness clock must NOT move
    assert c._health_at == first
    c._apply(hframe(2000))  # engine advanced → refresh
    assert c._health_at >= first


# --- command ack correlation (#39) ---------------------------------------------------------------
import pytest


def test_command_ack_stored_and_status():
    c = _consumer()
    c._apply({"type": "command_ack", "payload": json.dumps(
        {"id": "c1", "type": "submit_order", "status": "ok", "error": ""})})
    assert c.command_status("c1")["status"] == "ok"
    assert c.command_status("missing") is None  # unseen → None (caller reports pending, never false reject)


def test_command_ack_bounded_newest_kept():
    c = _consumer()
    c._MAX_ACKS = 3  # instance shadow of the class cap
    for i in range(6):
        c._apply({"type": "command_ack", "payload": json.dumps({"id": f"c{i}", "status": "ok"})})
    assert len(c._acks) == 3
    assert c.command_status("c0") is None  # oldest evicted
    assert c.command_status("c5") is not None  # newest kept


def test_send_command_raises_without_bus():
    c = _consumer()  # not started → _redis is None
    with pytest.raises(RuntimeError):
        asyncio.run(c.send_command("submit_order", {}))


def _external_frame(strategy: str, instrument: str = "AAPL.XNAS"):
    return {
        "type": "external_activity",
        "payload": json.dumps({
            "external": [{
                "account_id": "ACC", "client_id": "ALPACA", "instrument_id": instrument,
                "source": "POSITION", "strategy_id": strategy, "origin": "VENUE",
                "status": "QUARANTINED", "side": "LONG", "quantity": 100.0,
                "realized_pnl": "0.00 USD", "client_order_id": None, "order_status": None, "ts_last": 5,
            }],
            "ts": 10,
        }),
    }


def test_external_activity_snapshot_replaces():
    c = _consumer()
    c._apply(_external_frame("EXTERNAL", "MSFT.XNAS"))
    c._apply(_external_frame("EXTERNAL", "AAPL.XNAS"))  # newer snapshot replaces the whole plane
    ext = c.external()
    assert [e.instrument_id for e in ext] == ["AAPL.XNAS"]
    assert ext[0].origin == "VENUE" and ext[0].status == "QUARANTINED"


def test_the_trades_frames_HEALTH_survives_the_hop_to_the_api():
    """#298 — the rows and the health travel together, and only the rows used to survive.

    The engine reports `status`/`error` on its trades frame. The consumer rebuilt the frame from
    `payload["trades"]` alone, so a FAILED projection reached the UI indistinguishable from a flat book —
    which is precisely the state that showed an empty tile while eight positions were held.

    This is the seam, not the unit: the engine publishing status and the UI rendering it both worked, and
    the hop between them dropped it.
    """
    import time as _t

    c = RedisConsumer.__new__(RedisConsumer)
    # A LIVE BRIDGE. Hand-building with `__new__` leaves `_health_at` unset, which since 2026-08-26
    # means a DEAD bridge — and the trades plane deliberately reports `stale` there rather than the
    # frame's own status. This test is about the frame surviving the hop, so the bridge must be up.
    c._health_at = _t.monotonic()
    c._trades = []
    c._trades_status = None
    c._trades_error = None

    import json as _json
    RedisConsumer._apply(c, {"type": "trades", "payload": _json.dumps({"trades": [], "status": "failed", "error": "NameError: cache"})})
    assert c.trades_health() == ("failed", "NameError: cache")

    RedisConsumer._apply(c, {"type": "trades", "payload": _json.dumps({"trades": [], "status": "seeding", "error": None})})
    assert c.trades_health() == ("seeding", None)


def test_the_trades_plane_EMPTIES_when_the_bridge_goes_stale():
    """The guard that took fifteen minutes to notice on 2026-08-26 (see
    `test_trades_plane_goes_stale_like_every_other.py` for the incident).

    Driven here rather than asserted structurally: a live bridge serves the book, a dead one serves
    nothing and says `stale`."""
    c = _consumer()
    c._apply(_trade_frame("cyc-1", "HELD", "AAPL.XNAS"))
    assert [t.cycle_id for t in c.trades()] == ["cyc-1"]
    assert c.trades_health()[0] != "stale"

    c._health_at = None                     # the engine stopped publishing
    assert c.trades() == [], "a stale bridge still served the last known book"
    assert c.trades_health()[0] == "stale", (
        "a stale bridge still asserted its old status — which is what told the UI an empty book was "
        "legitimate and kept 'Reconciling with the broker…' on screen")
