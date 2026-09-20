"""Per-lane NET INVESTED per ET session, from the cache's own filled orders (#699 option a).

THE IDENTITY the lane cell will render: `net(W) = ΔMV − invested(W)` — the lane's change in market
value over the window minus the cash it put in (buys +, sells −). No basis rule anywhere: the
FIFO-vs-average-cost counter-example that made `realized(W) + Δunrealized(W)` wrong
(`books.ts::cellHeadline`) nets to exactly zero under it. Same identity as the account's DELTA NET
(equity change net of flows), so the lane cells compose like the headline above them.

THE SOURCE IS THE CACHE'S ORDERS, on every venue. `cache.orders()` survives restarts (durable Redis,
nothing purged — engine_node's CacheConfig says why) and every order carries its `strategy_id` and
its `OrderFilled` events with `last_qty`, `last_px`, `order_side`, `ts_event`. INTERNAL_TRANSFER and
RECONCILIATION fills are orders too and are COUNTED: within the cache's own world a transfer moves
MV between lanes at a price, and a reconciliation leg is minted with a basis — counting their flows is
what keeps each lane's identity closed. Fees are not flows (Nautilus models none but commission,
which the account headline carries and this does not — stated, not hidden).

Fixture doubles are REAL Nautilus orders with REAL `OrderFilled` events applied, because the sign and
the timestamp both come off the event and a hand-built dict cannot be wrong the way production can.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")
_SEQ = 0


def _ns(iso: str) -> int:
    """An ET wall-clock ISO string → engine ns."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=_ET).timestamp() * 1e9)


def _filled(strategy_id: str, side: str, qty: int, px: str, at: str, symbol: str = "PLTR", *,
            tags: list[str] | None = None, coid: str | None = None, reconciliation: bool = False,
            commission: str | None = None):
    """A REAL order that REALLY filled: the event is applied, so `order.events` is production's shape."""
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId, StrategyId
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    global _SEQ
    _SEQ += 1
    instrument = TestInstrumentProvider.equity(symbol=symbol)
    # The stub's factory takes no tags; production's transfer legs carry `INTERNAL_TRANSFER` in
    # `order.tags`, so the REAL order is built through the same factory production uses.
    order = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
        quantity=Quantity.from_int(qty),
        strategy_id=StrategyId(strategy_id),
        client_order_id=ClientOrderId(coid or f"O-699-{_SEQ}"),
    ) if tags is None else _tagged_market_order(instrument, side, qty, strategy_id, coid or f"O-699-{_SEQ}", tags)
    order.apply(TestEventStubs.order_submitted(order))
    order.apply(TestEventStubs.order_accepted(order))
    filled = TestEventStubs.order_filled(
        order=order, instrument=instrument, strategy_id=StrategyId(strategy_id),
        last_qty=Quantity.from_int(qty), last_px=Price.from_str(px), ts_event=_ns(at),
        commission=Money.from_str(f"{commission} USD") if commission is not None else None,
    )
    if reconciliation:
        # `OrderFilled.reconciliation` is what the kernel stamps on a fill it synthesised from a venue
        # report (`from_dict` is the only constructor path that sets it).
        d = type(filled).to_dict(filled)
        d["reconciliation"] = True
        filled = type(filled).from_dict(d)
    order.apply(filled)
    return order


def _tagged_market_order(instrument, side, qty, strategy_id, coid, tags):
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import ClientOrderId, StrategyId, TraderId
    from nautilus_trader.model.objects import Quantity
    from nautilus_trader.model.orders import MarketOrder
    from nautilus_trader.core.uuid import UUID4

    return MarketOrder(
        trader_id=TraderId("TESTER-000"), strategy_id=StrategyId(strategy_id), instrument_id=instrument.id,
        client_order_id=ClientOrderId(coid), order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
        quantity=Quantity.from_int(qty), init_id=UUID4(), ts_init=0, tags=tags,
    )


NOW = _ns("2026-09-14T06:00:00")


def test_FIXTURE_the_real_order_carries_a_real_fill_with_the_sign_and_the_timestamp_on_the_event():
    o = _filled("MOMENTUM-002", "SELL", 50, "20.00", "2026-09-10T10:30:00")
    fills = [e for e in o.events if type(e).__name__ == "OrderFilled"]
    assert len(fills) == 1 and fills[0].is_sell and float(fills[0].last_px) == 20.0
    assert fills[0].ts_event == _ns("2026-09-10T10:30:00")
    assert str(o.strategy_id) == "MOMENTUM-002"


def test_net_invested_is_bucketed_per_lane_per_ET_session_buys_positive_sells_negative():
    """The counter-example's flows: 50@10 + 50@20 bought on 09-08, 50 sold @20 on 09-10."""
    from api.realized import lane_flows_by_day

    orders = [
        _filled("MOMENTUM-002", "BUY", 50, "10.00", "2026-09-08T10:00:00"),
        _filled("MOMENTUM-002", "BUY", 50, "20.00", "2026-09-08T15:59:00"),
        _filled("MOMENTUM-002", "SELL", 50, "20.00", "2026-09-10T10:30:00"),
        _filled("TECHIVOL-005", "BUY", 10, "100.00", "2026-09-10T10:31:00", symbol="CRM"),
    ]
    out = lane_flows_by_day(orders, now_ns=NOW)
    assert out["by_day"] == {
        "MOMENTUM-002": {"2026-09-08": 1500.0, "2026-09-10": -1000.0},
        "TECHIVOL-005": {"2026-09-10": 1000.0},
    }
    assert out["error"] is None
    # The stubs' orders are initialised at ts 0 (1970): a cache OLDER than the horizon vouches for the
    # whole horizon, so `earliest` is the horizon floor — 100 days before NOW.
    assert out["earliest"] == "2026-06-06", "the date the cache vouches from, for the reader to compare with a base date"


def test_the_COUNTER_EXAMPLE_nets_to_ZERO_under_the_identity():
    """Buy 50@10 + 50@20 (avg 15), base mark 20 → MV_base 2000; sell 50@20 inside W, mark flat →
    MV_now 1000, invested(W) = −1000. net = (1000 − 2000) − (−1000) = 0. realized(W)+Δunrealized
    said 250 — the exact defect `cellHeadline` refused to ship."""
    from api.realized import lane_flows_by_day, net_of_flows

    orders = [_filled("MOMENTUM-002", "SELL", 50, "20.00", "2026-09-10T10:30:00")]
    flows = lane_flows_by_day(orders, now_ns=NOW)["by_day"]
    invested = net_of_flows(flows, "MOMENTUM-002", after="2026-09-08")
    assert invested == -1000.0
    assert (1000.0 - 2000.0) - invested == 0.0


def test_flows_ON_the_base_date_are_EXCLUDED_and_flows_after_it_are_summed():
    """The base is a CLOSE: everything that day is already inside MV_base. Strictly after."""
    from api.realized import lane_flows_by_day, net_of_flows

    orders = [
        _filled("MOMENTUM-002", "BUY", 10, "10.00", "2026-09-04T15:00:00"),   # on the base day: in MV_base
        _filled("MOMENTUM-002", "BUY", 10, "10.00", "2026-09-08T10:00:00"),
        _filled("MOMENTUM-002", "SELL", 5, "12.00", "2026-09-11T10:00:00"),
    ]
    flows = lane_flows_by_day(orders, now_ns=NOW)["by_day"]
    assert net_of_flows(flows, "MOMENTUM-002", after="2026-09-04") == 100.0 - 60.0
    assert net_of_flows(flows, "NOBODY-009", after="2026-09-04") == 0.0, "a lane with no fills invested nothing"


def test_an_ET_session_not_a_UTC_day_a_1930_ET_fill_is_that_days_session():
    """20:30 ET on 09-10 is 00:30 UTC on 09-11. The observation table is ET; so is this."""
    from api.realized import lane_flows_by_day

    orders = [_filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-09-10T19:30:00")]
    assert list(lane_flows_by_day(orders, now_ns=NOW)["by_day"]["MOMENTUM-002"]) == ["2026-09-10"]


def test_INTERNAL_TRANSFER_and_RECONCILIATION_fills_are_COUNTED_so_each_lanes_identity_stays_closed():
    """A transfer at price p moves MV p×q from A to B with a −p×q / +p×q flow: net 0 on both sides.
    Excluding them would book the moved MV as P&L on both lanes, opposite signs. The fixture cannot
    tag orders (the stub takes no tags), so this pins that the function reads NO tag at all —
    `test_the_function_reads_no_tags` — and that internal legs are ordinary fills to it."""
    from api.realized import lane_flows_by_day

    orders = [
        _filled("TECHIVOL-005", "SELL", 10, "15.00", "2026-09-09T12:00:00"),
        _filled("EXTERNAL", "BUY", 10, "15.00", "2026-09-09T12:00:00"),
        _filled("EXTERNAL", "BUY", 98, "15.78", "2026-09-08T17:06:00"),
    ]
    out = lane_flows_by_day(orders, now_ns=NOW)["by_day"]
    assert out["TECHIVOL-005"] == {"2026-09-09": -150.0}
    assert out["EXTERNAL"] == {"2026-09-08": pytest.approx(98 * 15.78), "2026-09-09": 150.0}


def test_unfilled_and_partially_filled_orders_contribute_ONLY_their_fills():
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.model.objects import Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    from api.realized import lane_flows_by_day

    instrument = TestInstrumentProvider.equity(symbol="PLTR")
    never = TestExecStubs.market_order(instrument=instrument, order_side=OrderSide.BUY,
                                       quantity=Quantity.from_int(5), strategy_id=StrategyId("MOMENTUM-002"))
    out = lane_flows_by_day([never, _filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-09-10T10:00:00")],
                            now_ns=NOW)
    assert out["by_day"] == {"MOMENTUM-002": {"2026-09-10": 10.0}}


def test_fills_older_than_the_horizon_are_dropped_and_the_horizon_is_named():
    from api.realized import lane_flows_by_day

    orders = [
        _filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-05-01T10:00:00"),   # 136 days back
        _filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-09-10T10:00:00"),
    ]
    out = lane_flows_by_day(orders, now_ns=NOW, days=100)
    assert out["by_day"] == {"MOMENTUM-002": {"2026-09-10": 10.0}}
    assert out["earliest"] == "2026-06-06"
    assert out["horizon_days"] == 100
    assert lane_flows_by_day([], now_ns=NOW)["earliest"] is None, "an empty cache vouches for nothing"


def test_a_broken_order_object_costs_the_field_LOUDLY_never_a_partial_map_wearing_a_complete_label():
    """One unreadable order must not yield a map missing one lane's flows — that is a wrong number,
    not a missing one. The whole field reports its error."""
    from api.realized import lane_flows_by_day

    class _Broken:
        strategy_id = "MOMENTUM-002"

        @property
        def events(self):
            raise RuntimeError("cache row unreadable")

    out = lane_flows_by_day([_filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-09-10T10:00:00"), _Broken()],
                            now_ns=NOW)
    assert out["by_day"] == {} and out["error"]


def test_EVERY_fill_class_is_COUNTED_and_the_NON_COCKPIT_ones_are_NAMED_by_ORIGIN_never_by_the_flag():
    """PAPER READBACK 2026-09-14 02:45Z: `OrderFilled.reconciliation` is True on EVERY polled Alpaca
    fill, so classifying by the flag named ordinary trading "reconciliation (19)" and caught nothing.
    Origin is the coid: cockpit prefixes (`cache_repair.COCKPIT_PREFIXES`) are ours; a fill under a
    strategy lane on any other coid is INFERRED — the kernel attributed it (MOMENTUM LAND 3×BUY 429
    on uuid coids, 09-08); an EXTERNAL fill tagged RECONCILIATION is the kernel's minted leg. All are
    COUNTED (the cache-world identity) and NAMED; the naming is what lets the reader refuse."""
    from api.realized import lane_flows_by_day

    transfer = _filled("TECHIVOL-005", "SELL", 10, "15.00", "2026-09-09T12:00:00", tags=["INTERNAL_TRANSFER", "x", "SRC"], coid="TR-abc-S")
    repair = _filled("EXTERNAL", "SELL", 10, "203.40", "2026-09-13T06:31:00", coid="RPR-fbed990e-EXTERNAL")
    recon = _filled("EXTERNAL", "BUY", 98, "15.78", "2026-09-08T17:06:00", tags=["RECONCILIATION"], coid="737d1848-3fe0-4a6f")
    venue = _filled("EXTERNAL", "SELL", 5, "10.00", "2026-09-08T17:07:00", tags=["VENUE"], coid="0b6ff4a9-ce7f-41bf")
    inferred = _filled("MOMENTUM-002", "BUY", 429, "9.44", "2026-09-08T11:57:00", coid="1f77773c-ba73-447c", reconciliation=True)
    ours = _filled("MOMENTUM-002", "SELL", 10, "196.38", "2026-09-10T09:35:00", coid="kumo-bcf99badc66bfd4164b1", reconciliation=True)
    stop = _filled("MOMENTUM-002", "SELL", 217, "9.62", "2026-09-08T11:58:00", coid="PROT-SELL-LAND-XNAS-50", reconciliation=True)
    # ibkr-paper, measured 2026-09-14: a cockpit FLATTEN is `close_position(pos, tags=["flatten:FL-…"])`
    # — Nautilus mints the `O-` coid and the cockpit's identity rides in the TAG. Ours, not inferred.
    flatten = _filled("BCTROT-004", "SELL", 5, "50.00", "2026-09-11T15:59:00", coid="O-20260911-155900-001-001-7",
                      tags=["flatten:FL-b64c2a00f65f1c9d8a9e"])
    out = lane_flows_by_day([transfer, repair, recon, venue, inferred, ours, stop, flatten], now_ns=NOW)
    assert "BCTROT-004" not in out["internal"], "a tagged flatten on a Nautilus coid is cockpit-world"
    assert out["by_day"]["MOMENTUM-002"] == {"2026-09-08": pytest.approx(429 * 9.44 - 217 * 9.62), "2026-09-10": -1963.8}
    assert out["internal"] == {
        "TECHIVOL-005": {"2026-09-09": {"transfer": 1}},
        "EXTERNAL": {"2026-09-08": {"reconciliation": 1, "venue": 1}, "2026-09-13": {"repair": 1}},
        "MOMENTUM-002": {"2026-09-08": {"inferred": 1}},
    }, "ours and PROT- fills are NOT named whatever their flag says; VENUE-tagged EXTERNAL is the kernel's mirror leg (#1072 b)"


def test_the_origin_prefixes_are_cache_repairs_ONE_list():
    """Two lists of what a cockpit coid looks like will drift; `cache_repair.COCKPIT_PREFIXES` is it."""
    import ast
    import inspect
    import textwrap

    from api import realized

    src = textwrap.dedent(inspect.getsource(realized._fill_class))
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "COCKPIT_PREFIXES" in names


def test_COMMISSIONS_are_INVESTED_so_the_lanes_sum_to_the_account_headline():
    """The account headline is equity, which paid the commissions. A cost is money put in: a buy
    of 10 @ 10 with 1.25 commission invested 101.25; a sell of 10 @ 10 with 1.25 commission
    returned 98.75 (invested −98.75). Without this the lanes drift from DELTA NET by Σcommissions."""
    from api.realized import lane_flows_by_day

    orders = [_filled("MOMENTUM-002", "BUY", 10, "10.00", "2026-09-10T10:00:00", commission="1.25"),
              _filled("MOMENTUM-002", "SELL", 10, "10.00", "2026-09-11T10:00:00", commission="1.25")]
    out = lane_flows_by_day(orders, now_ns=NOW)["by_day"]["MOMENTUM-002"]
    assert out == {"2026-09-10": 101.25, "2026-09-11": -98.75}


def test_a_fill_TODAY_before_the_read_is_INCLUDED():
    """`day > after` with no upper bound: the window runs to NOW, and a fill an hour ago is in it."""
    from api.realized import lane_flows_by_day, net_of_flows

    orders = [_filled("MOMENTUM-002", "BUY", 1, "10.00", "2026-09-14T05:00:00")]   # NOW is 06:00 ET
    flows = lane_flows_by_day(orders, now_ns=NOW)["by_day"]
    assert net_of_flows(flows, "MOMENTUM-002", after="2026-09-11") == 10.0


# -- the frame (seam) ------------------------------------------------------------------------------------

def test_the_trades_frame_CARRIES_lane_flows_from_the_caches_own_orders():
    """`_publish_trades` (real, bound to the realized-legs double) reads `cache.orders()` and publishes
    `lane_flows` beside `realized_periods`. A helper nothing calls proves nothing (#846's lesson)."""
    from nautilus_trader.cache.cache import Cache

    from api.test_realized_legs import _engine, _frame

    cache = Cache()
    for o in (_filled("MOMENTUM-002", "BUY", 10, "10.00", "2026-09-10T10:00:00"),
              _filled("TECHIVOL-005", "SELL", 3, "50.00", "2026-09-11T10:00:00", symbol="CRM")):
        cache.add_order(o)
    assert len(cache.orders()) == 2, "fixture: the cache holds the two filled orders"
    eng = _engine(cache)

    eng._publish_trades()

    flows = _frame(eng)["lane_flows"]
    assert flows["error"] is None
    assert flows["by_day"] == {"MOMENTUM-002": {"2026-09-10": 100.0}, "TECHIVOL-005": {"2026-09-11": -150.0}}
    from api.realized import FLOWS_HORIZON_DAYS, _et_session_date
    from api.test_realized_legs import NOW_NS
    assert flows["earliest"] == _et_session_date(NOW_NS - FLOWS_HORIZON_DAYS * 86_400 * 10**9), \
        "vouches from the horizon floor of the ENGINE's clock, the one production reads"


def test_the_REST_boundary_and_the_consumer_carry_lane_flows_not_drop_them():
    """Pydantic DTOs have eaten published fields three times (#233/#322/#336). The consumer keeps the
    LAST frame that carried the key; `TradesResponse` declares it; `/trades` passes it explicitly."""
    import inspect

    from api import app as app_module
    from api.consumer import RedisConsumer
    from api.models import TradesResponse

    assert "lane_flows" in TradesResponse.model_fields
    node = RedisConsumer.__new__(RedisConsumer)
    node._trades = []
    node._trades_flows = None
    node._apply({"type": "trades", "payload": '{"trades": [], "lane_flows": {"by_day": {"A": {"2026-09-10": 1.0}}, "earliest": "2026-09-10", "horizon_days": 100, "error": null}}'})
    assert node.trades_flows()["by_day"] == {"A": {"2026-09-10": 1.0}}
    node._apply({"type": "trades", "payload": '{"trades": []}'})
    assert node.trades_flows()["by_day"] == {"A": {"2026-09-10": 1.0}}, "a frame without the key says nothing, it does not blank"
    src = inspect.getsource(app_module.get_trades)
    assert "lane_flows=node.trades_flows()" in src, "/trades builds the response field-by-field; an undeclared pass is a silent None"


# -- #1072: instrument granularity for the vouch decision ------------------------------------------

def test_the_flows_frame_CARRIES_every_kernel_fill_with_its_instrument_side_qty_and_the_instruments_each_lane_touched():
    """Per-instrument scoping (#1072) needs two things the counts cannot give: each DIRTY fill with
    its instrument/side/qty (so boot pairs can be matched and unpaired ones can be placed on an
    instrument), and which instruments each lane's fills touched (so a lane is refused only when a
    dirty fill lands on something it held or traded)."""
    from api.realized import lane_flows_by_day

    inferred_sell = _filled("MOMENTUM-002", "SELL", 93, "133.22", "2026-09-11T12:08:00", symbol="CF", coid="33db1024-9fb0-46c8")
    inferred_buy = _filled("MOMENTUM-002", "BUY", 93, "133.23", "2026-09-11T12:08:00", symbol="CF", coid="CF.XNYS")
    recon = _filled("EXTERNAL", "SELL", 14, "162.45", "2026-09-08T09:31:00", symbol="GWRE", tags=["RECONCILIATION"], coid="737d1848-3fe0")
    ours = _filled("TECHIVOL-005", "BUY", 24, "206.25", "2026-09-11T12:00:00", symbol="CRWD", coid="kumo-36bfefe8589d29ca21e")
    out = lane_flows_by_day([inferred_sell, inferred_buy, recon, ours], now_ns=NOW)
    assert out["kernel_fills"] == [
        {"lane": "EXTERNAL", "day": "2026-09-08", "instrument": "GWRE.XNAS", "side": "SELL", "qty": 14.0, "px": 162.45, "kind": "reconciliation", "ts_ns": _ns("2026-09-08T09:31:00")},
        {"lane": "MOMENTUM-002", "day": "2026-09-11", "instrument": "CF.XNAS", "side": "SELL", "qty": 93.0, "px": 133.22, "kind": "inferred", "ts_ns": _ns("2026-09-11T12:08:00")},
        {"lane": "MOMENTUM-002", "day": "2026-09-11", "instrument": "CF.XNAS", "side": "BUY", "qty": 93.0, "px": 133.23, "kind": "inferred", "ts_ns": _ns("2026-09-11T12:08:00")},
    ], "sorted by (day, lane, instrument), insertion order within — and the fill's own ts, for the pair gate"
    assert out["touched"] == {"EXTERNAL": {"2026-09-08": ["GWRE.XNAS"]},
                              "MOMENTUM-002": {"2026-09-11": ["CF.XNAS"]},
                              "TECHIVOL-005": {"2026-09-11": ["CRWD.XNAS"]}}


def test_the_flows_frame_CARRIES_each_lanes_NET_FILL_QTY_per_instrument_per_day_and_classes_EXTERNAL_VENUE_fills_as_kernel():
    """The qty invariant (#1072 b): a lane's position may only change by its OWN fills. That needs the
    signed net qty each lane filled per instrument per session. And on ibkr-paper the kernel's boot
    mirror is an EXTERNAL BUY tagged VENUE (coid == instrument id) beside a same-second RECONCILIATION
    SELL — the VENUE leg must be a kernel fill too, or the pair cannot be matched."""
    from api.realized import lane_flows_by_day

    buy = _filled("TECHIVOL-005", "BUY", 24, "206.25", "2026-09-11T12:00:00", symbol="CRWD", coid="kumo-36bfefe8589d29ca21e")
    mirror_buy = _filled("EXTERNAL", "BUY", 24, "206.30", "2026-09-11T12:08:17", symbol="CRWD", tags=["VENUE"], coid="CRWD.XNAS")
    mirror_sell = _filled("EXTERNAL", "SELL", 24, "206.25", "2026-09-11T12:08:17", symbol="CRWD", tags=["RECONCILIATION"], coid="85e6d615-df69-4f03")
    trim = _filled("TECHIVOL-005", "SELL", 4, "210.00", "2026-09-11T15:40:00", symbol="CRWD", coid="kumo-trim")
    out = lane_flows_by_day([buy, mirror_buy, mirror_sell, trim], now_ns=NOW)
    assert out["net_qty"] == {"TECHIVOL-005": {"2026-09-11": {"CRWD.XNAS": 20.0}},
                              "EXTERNAL": {"2026-09-11": {"CRWD.XNAS": 0.0}}}
    kinds = [(f["lane"], f["side"], f["kind"]) for f in out["kernel_fills"]]
    assert kinds == [("EXTERNAL", "BUY", "venue"), ("EXTERNAL", "SELL", "reconciliation")]


def test_the_flows_frame_CARRIES_qty_now_from_the_SAME_pass_so_the_invariant_has_no_read_skew():
    """Review (2): `qty_now` from the api's 2 s positions plane against `net_qty` from the engine
    frame lets a fill between the two reads fail the invariant for one poll. Both terms come off ONE
    engine pass: `lane_flows_by_day(orders, positions=…)` reports the open positions' signed qty per
    lane and instrument beside the fills it walked."""
    from nautilus_trader.cache.cache import Cache

    from api.realized import lane_flows_by_day
    from api.test_realized_legs import _engine, _frame

    cache = Cache()
    o = _filled("MOMENTUM-002", "BUY", 10, "10.00", "2026-09-10T10:00:00")
    cache.add_order(o)
    class _Pos:
        strategy_id = "MOMENTUM-002"
        instrument_id = "PLTR.XNAS"
        signed_qty = -7.0        # a short, signed as Nautilus signs it
    out = lane_flows_by_day([o], now_ns=NOW, positions=[_Pos()])
    assert out["qty_now"] == {"MOMENTUM-002": {"PLTR.XNAS": -7.0}}
    assert lane_flows_by_day([o], now_ns=NOW)["qty_now"] is None, "not asked → None, never an empty book"
    eng = _engine(cache)
    eng._publish_trades()
    assert _frame(eng)["lane_flows"]["qty_now"] == {}, "asked (the engine passed positions_open(), an empty book) — not None"
