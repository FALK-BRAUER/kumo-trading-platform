"""Alpaca execution client — a pure-Python Nautilus `LiveExecutionClient` (equities, paper/live).

Scope: US equities on the MANUAL lane (discretionary clicks). Order submit/cancel/modify over the Alpaca
trading REST API, plus reconciliation reports (orders + positions + account) that the ExecEngine polls.
Real-time fills arrive via reconciliation for now; a live account trade-updates WebSocket is a later
increment. Options/crypto/margin/multi-leg from upstream PR #3375 are intentionally dropped.

Venue model: one client serves every US-equity MIC (routing.default=True). Instrument ids are canonical
`TICKER.MIC` — the Alpaca REST responses carry only `symbol`, so reports resolve symbol→id via the
instrument provider (which loaded each symbol with its listing MIC), matching the data provider's ids.

Safety: this client only *acts on* commands Nautilus routes to it (a MANUAL-lane SubmitOrder from an
explicit UI click). It never originates orders. Paper is the default account.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from decimal import Decimal, InvalidOperation

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import RoutingConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.execution.reports import (
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Money, Price, Quantity

from api.bus_topics import ACCOUNT_TOPIC, EQUITY_TOPIC, RECONCILE_TOPIC, UNRECONCILED_TOPIC
from api.providers.alpaca.config import AlpacaExecClientConfig
from api.providers.alpaca.data_client import _KEY_ENV, _SECRET_ENV, ALPACA
from api.providers.alpaca.http import AlpacaHttpClient, AlpacaHttpError
from api.providers.alpaca.providers import AlpacaInstrumentProvider
from api.providers.base import ExecClientSpec

_log = logging.getLogger("kumo.alpaca_exec")

# In-process MessageBus topic for the broker's own account snapshot (#41) — carries Alpaca's equity /
# buying_power / multiplier (which Nautilus AccountState doesn't model). The engine (api/engine_node.py)
# imports THIS constant to subscribe — single source of truth, so the pub/sub pair can't drift.

#: Both unknown. NOT zero — "this venue does not report it" and "the mark did not move" are
#: different claims, and only one of them is safe on a hero panel.
_UNREALIZED_UNKNOWN = {"unrealized_standing_total": None, "unrealized_intraday_total": None}


def _unrealized_totals(positions: list[dict]) -> dict:
    """Σ the broker's own per-position unrealized fields, or None if the book cannot be summed.

    UNKNOWN PROPAGATES (codex, scope review). A total built from the positions that HAPPEN to carry
    the field would report a partial book as the whole one — the quiet wrong answer this panel keeps
    producing. One missing or non-finite value makes the whole total unknown.

    IBKR publishes no intraday field at all, so its intraday total is None by construction rather
    than by accident, and the panel renders an em dash instead of borrowing Alpaca's semantics.

    An EMPTY book sums to 0.0 for both, and that is a fact: nothing held has an unrealized of zero.
    """
    out: dict = {}
    for key, field in (
        ("unrealized_standing_total", "unrealized_pl"),
        ("unrealized_intraday_total", "unrealized_intraday_pl"),
    ):
        total = 0.0
        for pos in positions or []:
            raw = pos.get(field)
            if raw is None:
                total = None
                break
            try:
                value = float(raw)
            except (TypeError, ValueError):
                total = None
                break
            # NaN/Infinity reach here as real floats through some JSON encoders, and `nan` survives
            # every comparison written for numbers — the defect family that disarmed a daily-loss
            # halt in kumo-trading-strategies 43c6d3e.
            if not math.isfinite(value):
                total = None
                break
            total += value
        out[key] = total
    return out

# The topic NAMES live in api/bus_topics.py — the neutral contract the engine subscribes to
# without importing this connector (#608). The aliases keep this module's vocabulary.
_ACCOUNT_TOPIC = ACCOUNT_TOPIC
# Broker-vs-cache position drift (#26 reconciliation banner) — the exec client is the only component that
# sees BOTH the broker (REST) and the Nautilus cache, so it computes drift and the engine surfaces it.
_RECONCILE_TOPIC = RECONCILE_TOPIC
# Orders reconciliation had to SKIP because no OrderStatusReport can represent them (#643) — e.g. a
# dollar-based (notional) order whose `qty` is null. Published EVERY batch, empty included: [] states
# known-clean, non-empty names the offenders, and a bus that never saw the topic is "never asked".
_UNRECONCILED_TOPIC = UNRECONCILED_TOPIC
_ACCOUNT_REFRESH_SECS = 3.0  # re-fetch the account so equity/buying-power stay live (not stuck at connect)
# Equity curve (#243) — its own topic and its own MUCH slower cadence. A curve is not tick data; the 1D
# series moves once every 5 minutes at best, and four period fetches every 3s would hammer a rate-limited
# broker for nothing.
_EQUITY_TOPIC = EQUITY_TOPIC
_EQUITY_REFRESH_SECS = 120.0
# (period, timeframe, extended_hours) per Alpaca's vocabulary.
#
# 1D IS FETCHED, NOT DERIVED, AND THAT IS THE CHANGE (#536). There was no 1D entry here, so the default
# tab built one by slicing the 1W/HOURLY series to the session (`sessionCurve.ts`, #345 item 4). Ten
# minutes into a session that is one or two points, and the chart was a straight diagonal.
#
# Measured on a live instance paper account, `period=1D`, 2026-08-25 09:45 ET:
#
#     timeframe   market_hours   extended_hours
#     1Min              94             424
#     5Min              19              85
#     1H                 2               8      <- what we drew
#
# `1Min` over `5Min` because 424 points is a trivial payload for a real curve. It also fixes a second
# thing the same measurement found: the hourly series' LAST BUCKET IS NOT CLOSED, so it lagged
# (1H 103,890.31 vs 5Min 104,008.74) and the header disagreed with its own chart by ~$100.
#
# EXTENDED HOURS ONLY ON 1D. It is what starts the curve at the PRE-MARKET open rather than 09:30 —
# the lanes decide at open+5m and open+150m, and #302 puts ~6 points of MOMENTUM's backtest/live gap
# on fill timing, so that segment is not decoration. On a DAILY series it would move session
# boundaries for no benefit, and on 1W it would silently change what the derived curve has sliced
# since #345.
#
# NO `pnl_reset`, and for 1D that needs no exception: the window IS the session, so `base_value` is
# already the prior close and `profit_loss` accumulates from there. `get_portfolio_history`'s
# docstring calls `pnl_reset` "right for a single intraday view but wrong for every period here" —
# with `period=1D` the two agree, so the default stays correct for all five.
#
# The longer windows stay daily: a month at 1Min is ~30k points nobody reads.
from api.equity_coverage import curve_coverage

_EQUITY_PERIODS = (
    ("1D", "1Min", True),
    ("1W", "1H", False),
    ("1M", "1D", False),
    ("3M", "1D", False),
    ("all", "1D", False),
)


def _reject_reason(exc: Exception) -> str:
    """UI-ready rejection reason. An Alpaca HTTP error carries a parsed human `message`; anything else falls
    back to its string form. Keeps raw JSON envelopes out of the order blotter."""
    if isinstance(exc, AlpacaHttpError):
        return exc.reason
    return str(exc)


# Nautilus order type → Alpaca `type`. Cockpit uses market/limit/stop; trailing kept for completeness.
_ORDER_TYPE_TO_ALPACA: dict[OrderType, str] = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP_MARKET: "stop",
    OrderType.STOP_LIMIT: "stop_limit",
    OrderType.TRAILING_STOP_MARKET: "trailing_stop",
}
# Nautilus time-in-force → Alpaca `time_in_force` (GTD maps to GTC + expire_time; cockpit uses DAY/GTC).
_TIF_TO_ALPACA: dict[TimeInForce, str] = {
    TimeInForce.DAY: "day",
    TimeInForce.GTC: "gtc",
    TimeInForce.IOC: "ioc",
    TimeInForce.FOK: "fok",
    TimeInForce.GTD: "gtc",
    TimeInForce.AT_THE_OPEN: "opg",
    TimeInForce.AT_THE_CLOSE: "cls",
}
# Reverse maps for reconciliation reports (Alpaca → Nautilus).
_ALPACA_TO_ORDER_TYPE: dict[str, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP_MARKET,
    "stop_limit": OrderType.STOP_LIMIT,
    "trailing_stop": OrderType.TRAILING_STOP_MARKET,
}
_ALPACA_TO_TIF: dict[str, TimeInForce] = {
    "day": TimeInForce.DAY,
    "gtc": TimeInForce.GTC,
    "ioc": TimeInForce.IOC,
    "fok": TimeInForce.FOK,
    "opg": TimeInForce.AT_THE_OPEN,
    "cls": TimeInForce.AT_THE_CLOSE,
}
# Full Alpaca order-status vocabulary → Nautilus OrderStatus. Unknown statuses are NOT assumed ACCEPTED
# (a wrong reconciliation state is worse than a skipped report) — the parser drops them with a warning.
_ALPACA_TO_ORDER_STATUS: dict[str, OrderStatus] = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.PENDING_CANCEL,
    "pending_replace": OrderStatus.PENDING_UPDATE,
    "replaced": OrderStatus.CANCELED,  # the replaced order is closed; the replacement is a new order
    "canceled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
    "stopped": OrderStatus.ACCEPTED,
    "suspended": OrderStatus.ACCEPTED,
    # POST-COMPLETION, not live. Alpaca documents `calculated` as "completed for the day (either
    # filled or done for day), but remaining settlement calculations are still pending" — the sibling
    # of `done_for_day` above, which maps to CANCELED for the same reason.
    #
    # ACCEPTED made a finished stop read as RESTING through the typed path, so the protection
    # reconciler counted it as coverage and did not arm a replacement. The raw path never had this
    # bug: `is_resting`'s own docstring records `calculated` being excluded in #387, and calls
    # counting it as protection "the silencing direction, which is the worse one". The mapping is a
    # second derivation of that same fact and disagreed with it.
    "calculated": OrderStatus.CANCELED,
}

# Alpaca fill-activity `side` → Nautilus OrderSide. `sell_short` is a SELL that opens/extends a short.
_ALPACA_FILL_SIDE: dict[str, OrderSide] = {
    "buy": OrderSide.BUY,
    "sell": OrderSide.SELL,
    "sell_short": OrderSide.SELL,
}

# GET /v2/orders `side` → Nautilus OrderSide. An order's side is only ever buy/sell; anything else is
# skipped fail-closed by `_parse_order_report` (#652 item 8a) — the old bare ternary reconciled every
# unknown/absent side as SELL, minting a phantom reducing order out of absence.
_ALPACA_ORDER_SIDE: dict[str, OrderSide] = {
    "buy": OrderSide.BUY,
    "sell": OrderSide.SELL,
}


def _fill_ts_ns(raw: dict) -> int | None:
    """Fill event time (ns) from an activity's ISO8601 `transaction_time`. None if missing/unparseable — the
    caller warns and drops the fill rather than stamp it with `now` (a wrong ts_event corrupts the trade
    timeline). Genuinely non-raising: a malformed timestamp must not abort the whole reconciliation batch."""
    ts = raw.get("transaction_time")
    # transaction_time is documented as an ISO8601 string; reject any other type (a bare int would be
    # mis-parsed by pandas as epoch-nanoseconds → a bogus 1970 timestamp).
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = pd.Timestamp(ts)
        if parsed is pd.NaT:
            return None
        value = parsed.value  # can OverflowError on an out-of-range timestamp
    except (ValueError, TypeError, OverflowError):
        return None
    return value if value > 0 else None


def _fill_in_window(ts_event_ns: int, start_ns: int | None, end_ns: int | None) -> bool:
    """Client-side reconciliation window on the fill's `transaction_time` (execution time) — the authoritative
    bound (the server `after` param filters activity-creation time, a different clock). Bounds are ns ints so
    the caller normalizes `command.start`/`end` once (Nautilus may hand them as `datetime` OR `pd.Timestamp`)."""
    if start_ns is not None and ts_event_ns < start_ns:
        return False
    return not (end_ns is not None and ts_event_ns > end_ns)


def _fill_trade_id(fill_id: str) -> TradeId:
    """Alpaca's activity `id` is unique + stable per execution → an idempotent trade id. But its
    `<timestamp>::<uuid>` form can exceed Nautilus's TradeId limit (1..36 chars), which would raise and
    ABORT the whole reconciliation batch (seen live: 55-char id → engine stuck 'offline'). Keep the id when
    it fits; else use the stable UUID tail (still unique per execution, human-traceable); else a deterministic
    uuid5 of the full id (stable across reconciliation runs, so the trade id stays idempotent)."""
    s = str(fill_id)
    if 1 <= len(s) <= 36:
        return TradeId(s)
    tail = s.rsplit("::", 1)[-1]
    if 1 <= len(tail) <= 36:
        return TradeId(tail)
    return TradeId(str(uuid.uuid5(uuid.NAMESPACE_OID, s)))


def _parse_fill_activity(
    raw: dict, instrument_id: InstrumentId, account_id: AccountId, ts_event_ns: int, ts_init_ns: int
) -> FillReport | None:
    """Map one Alpaca FILL activity → Nautilus `FillReport`. Pure: the caller resolves symbol→instrument_id and
    the timestamps. Returns None (fail-closed) on an unmapped side or missing price/qty, so a malformed activity
    is skipped rather than reconciled as a bogus fill.

    commission=0/USD (Alpaca equities are commission-free; the activity carries no fee) and
    liquidity=NO_LIQUIDITY_SIDE (Alpaca reports no maker/taker). client_order_id is omitted — the activity
    carries only the venue `order_id`; Nautilus attributes the fill to its order/strategy via that."""
    side = _ALPACA_FILL_SIDE.get(raw.get("side", ""))
    order_id = raw.get("order_id")
    fill_id = raw.get("id")
    price = raw.get("price")
    qty = raw.get("qty")
    if side is None or not order_id or not fill_id or price is None or qty is None:
        return None
    try:
        last_qty = Quantity.from_str(str(qty))
        last_px = Price.from_str(str(price))
    except (ValueError, InvalidOperation):
        # Malformed numeric on a required field → drop this one row (caller warns); do NOT let it raise and
        # abort the whole reconciliation batch (warn-and-continue posture).
        return None
    # Price.from_str accepts 0 / negative; a fill with non-positive price or qty is malformed, not a real
    # execution — drop it rather than reconcile a bogus report.
    if last_px.as_double() <= 0 or last_qty.as_double() <= 0:
        return None
    return FillReport(
        account_id=account_id,
        instrument_id=instrument_id,
        venue_order_id=VenueOrderId(str(order_id)),
        # Alpaca's activity `id` is unique per execution + stable across reconciliation runs → idempotent trade
        # id, clamped to Nautilus's 1..36-char TradeId limit (Alpaca ids can be ~55 chars) — see _fill_trade_id.
        trade_id=_fill_trade_id(fill_id),
        order_side=side,
        last_qty=last_qty,
        last_px=last_px,
        commission=Money(0, USD),
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        report_id=UUID4(),
        ts_event=ts_event_ns,
        ts_init=ts_init_ns,
    )


def _trailing_fields(raw: dict) -> dict:
    """`trailing_offset` + `trailing_offset_type` for a trailing stop, or empty for anything else.

    Empty rather than None-valued: `OrderStatusReport` treats an absent key and a null differently, and a
    non-trailing order must not carry a trailing offset at all.
    """
    pct, price = raw.get("trail_percent"), raw.get("trail_price")
    if pct not in (None, ""):
        # x100, the exact inverse of the submit path (`trail_percent = trailing_offset / 100`, line ~645).
        # Alpaca's `trail_percent` is a PERCENT; Nautilus's BASIS_POINTS is basis points. Passing the
        # percent straight through would rebuild a 6.06% trail as 6.06 bps — a stop 0.06% away, which
        # fires on the first tick. Getting this backwards is a 100x error in the direction that sells.
        return {
            "trailing_offset": Decimal(str(pct)) * 100,
            "trailing_offset_type": TrailingOffsetType.BASIS_POINTS,
        }
    if price not in (None, ""):
        return {
            "trailing_offset": Decimal(str(price)),
            "trailing_offset_type": TrailingOffsetType.PRICE,
        }
    return {}


class AlpacaExecutionClient(LiveExecutionClient):
    """Execution client for Alpaca US equities (REST orders + reconciliation reports)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: AlpacaInstrumentProvider,
        http_client: AlpacaHttpClient,
        config: AlpacaExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(ALPACA),
            venue=None,  # multi-venue (XNAS/XNYS/…): routing.default catches every MIC
            oms_type=OmsType.NETTING,
            # MARGIN, because that is what the Alpaca account IS (#306). It reports buying_power at ~2.8x
            # equity, so the venue permits margin whatever we declare — and on 2026-08-14 MOMENTUM
            # deployed 580.52 past cash, which the venue accepted and CashAccount then refused to
            # represent: `AccountBalanceNegative` is raised ONLY for AccountType.CASH
            # (accounting/manager.pyx:126), so the ExecClient could not connect and the whole execution
            # side went offline mid-session — no positions, no managers, no order commands, and the #239
            # backstop unable to protect anything new.
            #
            # Declaring CASH never prevented the over-deployment; only the venue could, and it allowed it.
            # All the declaration did was block RECOVERY from a state that had already happened. A guard
            # that cannot stop the bad thing but does stop the fix is worse than no guard.
            #
            # This does NOT enable leverage: MarginAccount defaults to `default_leverage = Decimal(1)`
            # (accounts/margin.pyx:83), so 1x unless a per-instrument leverage is set, which we never set.
            account_type=AccountType.MARGIN,
            base_currency=USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._http = http_client
        self._provider = instrument_provider
        # symbol ("AAPL") → canonical InstrumentId ("AAPL.XNAS"); Alpaca REST carries only the symbol.
        self._symbol_to_id: dict[str, InstrumentId] = {}
        #: The Δ half of NET (#596), refreshed by the drift cycle. Declared HERE rather than left to
        #: a getattr default so the lifecycle is stated: unknown until a position fetch succeeds,
        #: and back to unknown whenever one fails.
        self._unrealized_totals: dict = dict(_UNREALIZED_UNKNOWN)
        self._venue_unanswered_lookups = 0  # targeted reads / post-submit lookups the venue did not answer (#354)
        self._set_account_id(AccountId(f"{ALPACA}-master"))

    # -- lifecycle --------------------------------------------------------------------------------
    async def _connect(self) -> None:
        await self._http.connect()
        await self._provider.load_all_async()
        self._symbol_to_id = {
            iid.symbol.value: iid for iid in self._provider.get_all()
        }
        # REFUSING A SNAPSHOT MUST NOT REFUSE THE NODE (#588). `_report_account_state` now raises
        # rather than reporting cash as equity, and this call site is NOT inside the refresh loop's
        # try/except — an unusable account payload at boot would propagate out of `_connect` and stop
        # the exec client connecting at all. A node that will not start is a far worse outcome than a
        # chart that is briefly empty, and it is the same shape as the reconciliation failure that
        # already made a node inert once.
        #
        # DEGRADED LOUDLY, NOT SILENTLY: this is an ERROR, not a warning, and the refresh loop retries
        # every `_ACCOUNT_REFRESH_SECS`. What must never happen is publishing cash as equity.
        try:
            await self._report_account_state()
        except Exception as exc:  # noqa: BLE001 — see above; the loop below retries
            self._log.error(
                f"account state unavailable at connect, publishing NOTHING rather than a wrong "
                f"equity (#588): {exc!r}. The refresh loop will retry."
            )
        # Keep equity/buying-power live: Alpaca's own fields drift with fills + marks, so re-fetch on a loop
        # (the connect-only fetch would leave the UI showing stale account values forever).
        self._account_task = self._loop.create_task(self._account_refresh_loop())
        self._equity_task = self._loop.create_task(self._equity_refresh_loop())
        self._log.info("Alpaca execution client connected")

    async def _disconnect(self) -> None:
        for attr in ("_account_task", "_equity_task"):
            task = getattr(self, attr, None)
            if task is None:
                continue
            task.cancel()
            # AWAIT the cancellation before closing the HTTP session below — otherwise a loop mid-request
            # can wake to a closed session and log a spurious failure on the way down. (codex review, Low.)
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown must not raise
                pass
            setattr(self, attr, None)
        await self._http.close()

    async def _account_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(_ACCOUNT_REFRESH_SECS)
            try:
                await self._report_account_state()
            except Exception as exc:  # noqa: BLE001 — a transient account fetch failure must not kill exec
                self._log.warning(f"account refresh failed: {exc!r}")
            try:
                await self._report_reconcile_drift()
            except Exception as exc:  # noqa: BLE001 — best-effort drift signal; skip this cycle on failure
                self._log.warning(f"reconcile-drift check failed: {exc!r}")

    async def _equity_refresh_loop(self) -> None:
        """Own loop, own cadence — see `_EQUITY_REFRESH_SECS`. Publishes once at connect so the chart is
        populated before the first interval elapses."""
        try:
            await self._report_equity_curve()
        except Exception as exc:  # noqa: BLE001 — never let the curve break exec startup
            self._log.warning(f"initial equity curve failed: {exc!r}")
        while True:
            await asyncio.sleep(_EQUITY_REFRESH_SECS)
            try:
                await self._report_equity_curve()
            except Exception as exc:  # noqa: BLE001
                self._log.warning(f"equity curve refresh failed: {exc!r}")

    async def _report_reconcile_drift(self) -> None:
        """Publish broker-vs-cache position DRIFT for the UI reconciliation banner (#26). The broker is the
        hard anchor: any symbol the broker holds whose quantity the cockpit's cache doesn't match — a missed
        reconciliation, a lost-cache restart, external/manual activity — is surfaced so the cockpit can NEVER
        silently show a flat/empty book while positions are actually held. Emits the full drift list every
        cycle (empty = in sync); the engine folds it into its health frame."""
        try:
            broker_positions = await self._http.list_positions()
        except Exception:
            # A FAILED FETCH MUST NOT LEAVE A STALE TOTAL BEHIND. The caller catches and retries in
            # 3s, so without this the account frame would keep republishing the LAST good unrealized
            # figure indefinitely while the book moved underneath it — a number that looks live and
            # is not, which is exactly the fallback CLAUDE.md forbids. Unknown, then re-raise.
            self._unrealized_totals = dict(_UNREALIZED_UNKNOWN)
            raise
        # THE BROKER'S OWN UNREALIZED TOTALS (#596), computed HERE because this call already fetches
        # the positions every cycle. Doing it in `_report_account_state` would double the position
        # requests against a shared rate limit, which is what cost staging two and a half hours of
        # bars in #572.
        self._unrealized_totals = _unrealized_totals(broker_positions)
        cache_qty: dict[str, Decimal] = {}
        for pos in self._cache.positions_open():
            sym = pos.instrument_id.symbol.value
            cache_qty[sym] = cache_qty.get(sym, Decimal(0)) + Decimal(str(pos.signed_qty))
        drift: list[dict] = []
        seen: set[str] = set()
        # THE BROKER'S OWN MAP, symbol -> signed qty, published beside the drift (#807 item 3) so a
        # row the broker holds none of can say so. Signed: Alpaca reports `qty` unsigned with `side`.
        broker_map: dict[str, float] = {}
        for raw in broker_positions:
            sym = raw.get("symbol", "")
            seen.add(sym)
            broker_qty = Decimal(raw.get("qty", "0"))
            if str(raw.get("side", "long")).lower() == "short":
                broker_qty = -abs(broker_qty)
            broker_map[sym] = float(broker_qty)
            platform_qty = cache_qty.get(sym, Decimal(0))
            if broker_qty != platform_qty:
                drift.append({"symbol": sym, "broker_qty": float(broker_qty), "platform_qty": float(platform_qty)})
        # A symbol the cockpit thinks it holds but the broker does not (phantom) is drift too.
        for sym, platform_qty in cache_qty.items():
            if sym not in seen and platform_qty != 0:
                drift.append({"symbol": sym, "broker_qty": 0.0, "platform_qty": float(platform_qty)})
        self._msgbus.publish(
            _RECONCILE_TOPIC,
            {
                "drift": drift,
                "broker_qty": broker_map,
                # Lookups the venue did not answer (#354): a count that only ever grows in this process.
                "unanswered_lookups": int(getattr(self, "_venue_unanswered_lookups", 0)),
                "ts": self._clock.timestamp_ns(),
            },
        )

    async def _report_account_state(self) -> None:
        """Fetch the Alpaca account → (1) a Nautilus AccountState for the cash ledger, and (2) a broker
        snapshot on the msgbus carrying Alpaca's OWN equity/buying_power/multiplier (#41). `AccountBalance.total`
        carries NET LIQUIDATION (#588), matching Nautilus's IBKR adapter; the order ticket sizes on
        equity + gates on buying_power, and those come from the broker snapshot below — a local cash+positions estimate diverges (margin, pending, marks)."""
        account = await self._http.get_account()
        # Alpaca id becomes the account id so reports/positions attribute to the real account.
        self._set_account_id(AccountId(f"{ALPACA}-{account.get('id', 'master')}"))
        # TOTAL IS NET LIQUIDATION, NOT CASH (#588). This field is what the cockpit reads back as
        # equity — `engine_node.py` takes `account.balances_total()` and `account_curve.py` reads
        # `state.balances_total` — so putting cash here made the Home curve plot the cash balance:
        # 44,121.93 against a real 104,989.31 on 2026-08-27, and NET-1D read $72,372.19 on an
        # account that had moved +$669.33.
        #
        # NAUTILUS'S OWN IBKR ADAPTER IS THE CONVENTION, not an alternative to it:
        # `interactive_brokers/execution.py:1667` writes NetLiquidation to `total`,
        # FullAvailableFunds to `free`, `total - free` to `locked`, and TotalCashValue into `info`.
        # Ours was the outlier, and the docstring above ("AccountState only models cash") was simply
        # untrue for a margin account.
        #
        # NO FALLBACK TO CASH. Reporting cash as equity IS the defect, so a missing figure raises
        # rather than silently degrading — the #382 rule: a number known to be wrong must not render.
        # PARSE, THEN VALIDATE — an `or` chain cannot tell "absent" from "unparseable" from "zero",
        # and all three used to slip through differently. Measured on the first implementation:
        #   portfolio_value="0"   -> total 0.00, free 0.00   (ZERO equity published, silently)
        #   portfolio_value=""    -> fell through to `equity`, which is fine
        #   both ""               -> Decimal("") raised InvalidOperation, a decimal error rather than
        #                            a statement about which input was missing
        # A zero equity is not a degraded reading, it is a wrong one: the account is worth something.
        raw_equity = None
        for key in ("portfolio_value", "equity"):
            candidate = account.get(key)
            if candidate is None or str(candidate).strip() == "":
                continue
            try:
                parsed = Decimal(str(candidate))
            except (InvalidOperation, ValueError):
                continue
            # NaN PARSES AND THEN POISONS THE COMPARISON. `Decimal("NaN")` is a perfectly good
            # Decimal, so it escapes the except above — and `Decimal("NaN") > 0` RAISES
            # InvalidOperation, outside the try, from a line that looks like a plain comparison.
            # This is the third time a non-finite number has walked through a guard in this stack:
            # kumo-trading-strategies 43c6d3e had `broker_equity` return NaN, where `nan <= 0` is False so
            # the daily-loss halt could never fire. Check finiteness, never just the sign.
            if not parsed.is_finite():
                continue
            if parsed > 0:
                raw_equity = parsed
                break
        if raw_equity is None:
            raise ValueError(
                "Alpaca account carried no usable equity — portfolio_value="
                f"{account.get('portfolio_value')!r}, equity={account.get('equity')!r}. Refusing to "
                "report cash as equity (#588); a wrong number must not render as a number (#382)."
            )
        try:
            cash_decimal = Decimal(str(account.get("cash", "0") or "0"))
        except (InvalidOperation, ValueError):
            cash_decimal = Decimal(0)
        if not cash_decimal.is_finite():
            cash_decimal = Decimal(0)
        cash = Money(cash_decimal, USD)
        total = Money(raw_equity, USD)
        # FREE STAYS CASH, DELIBERATELY (#590). `risk/engine.pyx:696` admits orders against
        # `balance_free`. Cash 44,121.93 vs buying_power 346,916.39 — aligning this with IBKR's
        # available-funds concept would loosen the live order gate 7.86x. That is its own decision.
        # NAUTILUS ENFORCES `total - locked == free` EXACTLY (`correctness.pyx`, raised on
        # construction), so a negative `locked` cannot simply be clamped to zero — the identity has
        # to keep holding. When cash EXCEEDS net-liq (a short, or a debit balance) the only way to
        # satisfy it is to clamp FREE down to total, which is also the conservative direction for
        # the order gate: `risk/engine.pyx:696` then admits against the smaller number, not the
        # larger. Measured, not assumed: the first version clamped `locked` and Nautilus rejected it
        # with "`total` (100000.00 USD) - `locked` (0.00 USD) != `free` (120000.00 USD)".
        # COMPARED AS DECIMAL, NOT AS FLOAT. This branch decides `free`, which is the number
        # `risk/engine.pyx:696` admits live orders against — the wrong boundary primitive here is a
        # wrong order gate, not a rounding nit (codex, implementation review).
        # ONE derivation of cash, used by every consumer below. It was briefly two — one raising on
        # an empty string, one coercing it to zero — which is the drift this file exists to prevent.
        free = cash if cash_decimal <= raw_equity else total
        locked = total - free
        locked_raw = total - cash
        balance = AccountBalance(total=total, locked=locked, free=free)
        self.generate_account_state(
            balances=[balance],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
            info={
                # NAMES THE SEMANTICS so the curve can tell a net-liq row from a pre-#588 cash row.
                # Rows written before this deploy carry cash in `total` and no marker; without this
                # the 1W/1M/3M curves would step by the whole book at cutover and read as a gain.
                "BalanceTotalSemantics": "NET_LIQUIDATION",
                # Cash has no other home on this plane once `total` stops being it. IBKR uses this
                # exact key, so the two connectors stay readable by one consumer.
                "TotalCashValue": float(cash.as_double()),   # float to match IBKR's own info
                "RawLockedCashValue": float(locked_raw.as_double()),
            },
        )
        # Broker snapshot for UI sizing: portfolio_value = net-liq (risk base); buying_power = affordability
        # (multiplier × equity on margin); multiplier tells cash (1) vs margin (2/4).
        try:
            snapshot = {
                "equity": float(account.get("portfolio_value") or account.get("equity") or 0.0),
                "cash": float(account.get("cash") or 0.0),
                "buying_power": float(account.get("buying_power") or 0.0),
                "multiplier": float(account.get("multiplier") or 1.0),
                # Carried so the BOOK tile can source DEPLOYED from the BROKER rather than from our own
                # marks (#310). Alpaca's own figures satisfy `cash + long_market_value == equity` exactly;
                # deriving one of the three locally made the panel unable to add up.
                "long_market_value": float(account.get("long_market_value") or 0.0),
                # PREVIOUS SESSION'S CLOSING equity. Carried so the BOOK tile can show NET for 1D (#336):
                # the equity-curve plane publishes 1W/1M/3M/all and nothing for 1D, so without this the
                # hero number on Home's DEFAULT tab has no source and renders a dash.
                #
                # `equity - last_equity` is Alpaca's own day P&L — the number its UI shows — so NET(1D)
                # stays broker-sourced rather than becoming a locally-derived figure beside the statement.
                #
                # `or None`, NOT `or 0.0`, unlike every field above. A missing last_equity must stay
                # missing: defaulting it to zero would make the day P&L read as the ENTIRE account equity
                # (100,658.95 instead of 542.67) — a wrong number in the hero slot, which is worse than
                # the dash it replaces. The UI treats None as "unknown" and shows nothing.
                "last_equity": (
                    float(account["last_equity"]) if account.get("last_equity") is not None else None
                ),
                "ts": self._clock.timestamp_ns(),
                # NET = realized + Δunrealized (#596), and these are the Δ half. Sourced from the
                # drift cycle's position fetch, so they lag the account by one 3s tick — which is
                # far cheaper than a second positions call and well inside what a panel needs.
                #
                # NAMED EXPLICITLY, not splatted. A splat sits last and would silently overwrite any
                # earlier key if this dict ever grew one; naming them means a stray key is inert
                # rather than destructive (codex, implementation review).
                "unrealized_standing_total": self._unrealized_totals.get("unrealized_standing_total"),
                "unrealized_intraday_total": self._unrealized_totals.get("unrealized_intraday_total"),
            }
            self._msgbus.publish(_ACCOUNT_TOPIC, snapshot)
        except Exception as exc:  # noqa: BLE001 — snapshot is best-effort; the cash ledger above still posts
            self._log.warning(f"account snapshot publish failed: {exc!r}")

    async def _report_equity_curve(self) -> None:
        """Publish the account's equity curve per period (#243) — the Home chart, and #233's account half.

        One fetch per period. They are separate Alpaca calls with no combined endpoint, but this runs on
        its own slow loop (`_EQUITY_REFRESH_SECS`) rather than the 3s account cadence: an equity curve is
        not tick data, and four calls every 3 seconds would be rude to a broker that rate-limits.

        A failure for one period must not lose the others. Each is caught separately AND the result is
        merged onto the last published frame — publishing only the periods that succeeded would drop a
        previously good series from the plane, blanking a chart tab that was fine a moment ago.
        (codex review, High.)
        """
        curves: dict[str, dict] = dict(getattr(self, "_last_equity_curves", {}))
        for period, timeframe, extended in _EQUITY_PERIODS:
            try:
                raw = await self._http.get_portfolio_history(
                    period=period, timeframe=timeframe, extended_hours=extended)
            except Exception as exc:  # noqa: BLE001 — one bad period must not drop the rest
                self._log.warning(f"portfolio history {period} failed: {exc!r}")
                continue
            ts = raw.get("timestamp") or []
            eq = raw.get("equity") or []
            pl = raw.get("profit_loss") or []
            # Alpaca pads with nulls for sessions with no data (holidays, pre-first-trade). Drop those
            # rather than plotting a hole — and drop the matching timestamp so the series stays aligned.
            points = [
                {"t": int(t), "equity": float(e), "pnl": float(p) if p is not None else 0.0}
                for t, e, p in zip(ts, eq, list(pl) + [None] * len(ts))
                # `equity == 0` is a session BEFORE the account was funded. Alpaca returns real zeros
                # for those, and plotting them draws a cliff from the axis up to the funded balance,
                # which reads as a catastrophic loss recovered. Drop them with their timestamps.
                if e is not None and float(e) != 0.0
            ]
            if not points:
                continue
            curves[period] = {
                "points": points,
                "base_value": float(raw.get("base_value") or 0.0),
                # PERIOD P&L = last equity - base_value.
                #
                # NOT `points[-1]["pnl"]`. Alpaca's `profit_loss[i]` is the change AT that point, not a
                # running total: on 2026-08-12 its last 1M value was +1161.81 (that day's move) while the
                # month was DOWN 2244.29. Taking the last element printed "+$1,161 this 1M" over a chart
                # falling from 100,085 to 97,755.
                #
                # This is still the broker's arithmetic, not a parallel ledger — both terms are Alpaca's
                # own fields, and the identity holds exactly against its own series:
                #     sum(profit_loss) == equity[-1] - base_value == -2244.29
                "pnl": points[-1]["equity"] - float(raw.get("base_value") or 0.0),
                "timeframe": timeframe,
                # DOES THE SERIES SPAN THE LABEL? (#653) On a 2-week account Alpaca clamps every
                # longer window's base to inception, so NET·3M == NET·1M to the cent with nothing
                # saying so. The number is still a true delta; the COVERAGE is what the UI needs to
                # stop the label overstating it.
                **curve_coverage(period, [pt["t"] for pt in points]),
            }
        if curves:
            self._last_equity_curves = curves
            self._msgbus.publish(_EQUITY_TOPIC, {"curves": curves, "ts": self._clock.timestamp_ns()})

    # -- commands ---------------------------------------------------------------------------------
    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        if order.is_closed:
            self._log.warning(f"Order {order.client_order_id} already closed — not submitting")
            return
        # OWNERSHIP IS NO LONGER ASKED HERE (#1030). It was, inline (#748), and that made it a rule
        # with one call site on one venue — the #782 shape. It is now `gated_exec.install_ownership_gate`,
        # a wrap the factory below installs on this client exactly as the IBKR factory installs it on
        # the vendor's, and it runs BEFORE this method is reached.
        allowed, why, facts = await self._budget_allows(order)
        if not allowed:
            # PLATFORM-LEVEL BUDGET ENFORCEMENT (#320). Here rather than in a strategy because a
            # strategy that polices its own allocation does so correctly until the day it has a bug,
            # and then holds more than it was granted — silently. This is the last hop before the
            # venue, so nothing can bypass it by being wrong.
            #
            # ENTRIES only. Exits are always permitted, and that asymmetry is the whole point: a
            # strategy over its budget NEEDS to sell, and blocking that would trap it above target
            # permanently — the opposite of what lowering its number means.
            # THE DERIVATION, NOT JUST THE VERDICT (#758). `GateDecision.inputs` was added earlier
            # tonight and CONSUMED BY NOTHING: the gate computed target/deployed/room and this line
            # logged only the conclusion, so answering "is the lane overtrading or holding phantoms?"
            # still took a position query and arithmetic by hand. A field nobody reads is the
            # anchors-on-something-absent shape, committed in the change that was meant to end it.
            self._log.warning(
                f"Budget refused {order.client_order_id}: {why}"
                + (f" | {facts}" if facts else "")
            )
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=why,
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        # Reject a wrong-side stop BEFORE the broker round-trip: a BUY stop must trigger above the market and a
        # SELL stop below, else Alpaca rejects it ("stop price must be greater than current price") — and a
        # wrong-side stop that somehow rested would fire instantly. Local reject = a clean reason, no round-trip.
        violation = self._stop_side_violation(order)
        if violation is not None:
            self._log.warning(f"Rejecting {order.client_order_id} pre-submit: {violation}")
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=violation,
                ts_event=self._clock.timestamp_ns(),
            )
            return
        await self._place_order(order)

    #: After an UNANSWERED submit: how many times to ask the venue whether the order landed, how long
    #: each ask may take, and the pause between asks. BOUNDED WELL INSIDE NAUTILUS'S INFLIGHT WINDOW
    #: (`inflight_check_threshold_ms` 5 s x `inflight_check_max_retries` ≈ 25 s, after which it resolves a
    #: SUBMITTED order to REJECTED "UNKNOWN" on its own, `live/execution_engine.py:777`): a late
    #: `generate_order_accepted` against an order Nautilus has already rejected is refused by the FSM
    #: (review H2). 3 x (5 s + 2 s) < 25 s. A venue still silent after that leaves the order SUBMITTED,
    #: and Nautilus's own `QueryOrder` keeps asking through `generate_order_status_report`.
    _SUBMIT_LOOKUP_ATTEMPTS = 3
    _SUBMIT_LOOKUP_TIMEOUT_S = 5.0
    _SUBMIT_LOOKUP_DELAY_S = 2.0

    #: Venue statuses that mean the order LANDED and rests or ran — accept. Anything terminal-and-dead
    #: (rejected / canceled / expired) is a rejection carrying the venue's own word (review H3).
    _LANDED_STATUSES = frozenset({"new", "accepted", "pending_new", "accepted_for_bidding", "held",
                                  "partially_filled", "filled", "done_for_day", "calculated", "stopped"})

    async def _place_order(self, order) -> None:
        """The venue call and what its outcome MEANS (#354) — the one seam every single-order submit crosses.

        An ANSWERED failure (`AlpacaHttpError`, a 4xx the venue wrote) rejects the order, as before.
        An UNANSWERED submit (timeout, connection error) is not a rejection: the request may have
        landed. The cache held eight `PROT-SELL-*` stops REJECTED "Connection timeout to host
        …/v2/orders" — corpses if any of them rested at the venue. So: ask by our client id, a few
        times, bounded. Found and alive → accepted, exactly as the success path does. Found dead
        (venue says rejected/canceled/expired) → rejected with the venue's word. 404 that SURVIVES the
        whole window → the venue never saw it → rejected with the original cause; a first-attempt 404
        is not proof (read-your-writes on `orders:by_client_order_id` is unmeasured, review H1). Still
        unanswered → the order stays SUBMITTED, said at ERROR and counted; Nautilus's inflight check
        keeps asking and resolves it on its own terms.
        """
        response = await self._post_order(order, self._build_order_request(order),
                                          reject=lambda reason: self._reject(order, reason))
        if response is None:
            return
        venue_order_id = VenueOrderId(response["id"])
        self.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self._log.info(f"Order {order.client_order_id} accepted (venue_order_id={venue_order_id})")

    async def _post_order(self, order, payload: dict, *, reject) -> dict | None:
        """POST one payload and say what the outcome MEANS. Returns the venue's order when it landed
        alive, or None after `reject(reason)` was called or the order was deliberately left SUBMITTED.

        `reject` is a callable because a bracket is ONE payload and THREE orders (#832): the venue's
        answer — or its silence — applies to every leg, and `_submit_order_list` folded a transport
        timeout and a 4xx into the same triple `OrderRejected`, the exact shape #354 removed from
        single orders. Both submit paths now cross this seam.
        """
        try:
            return await self._http.submit_order(payload)
        except AlpacaHttpError as exc:
            self._log.error(f"Submit REJECTED by the venue for {order.client_order_id}: {exc!r}")
            reject(_reject_reason(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — transport: the venue did not ANSWER
            self._log.error(f"Submit UNANSWERED for {order.client_order_id}: {exc!r} — asking the venue whether it landed (#354)")
            response = await self._lookup_after_unanswered_submit(order, exc, reject=reject)
            if response is None:
                return None
            # NAUTILUS MAY HAVE RESOLVED IT MEANWHILE (its inflight check runs on its own clock): an
            # order no longer SUBMITTED must not receive a second, contradictory acceptance.
            cached = self._cache.order(order.client_order_id)
            if cached is not None and cached.status != OrderStatus.SUBMITTED:
                self._log.error(
                    f"Submit for {order.client_order_id} landed at the venue but the cache already holds it "
                    f"{cached.status_string()} — not re-accepting; reconciliation owns it from here (#354)"
                )
                return None
            venue_status = str(response.get("status", "")).lower()
            if venue_status not in self._LANDED_STATUSES:
                self._log.error(f"Submit for {order.client_order_id} landed but the venue reports {venue_status!r} — rejecting with the venue's word")
                reject(f"venue reports {venue_status}")
                return None
            return response

    async def _lookup_after_unanswered_submit(self, order, cause: Exception, *, reject=None) -> dict | None:
        """Did an unanswered submit land? Returns the venue's order (it did), rejects and returns None
        (a 404 that held for the whole window), or leaves the order SUBMITTED and returns None (still
        unanswered). Every ask is time-boxed so the whole window stays inside Nautilus's inflight one."""
        attempts = getattr(self, "_SUBMIT_LOOKUP_ATTEMPTS", 3)
        delay = getattr(self, "_SUBMIT_LOOKUP_DELAY_S", 2.0)
        timeout = getattr(self, "_SUBMIT_LOOKUP_TIMEOUT_S", 5.0)
        not_found = 0
        for i in range(attempts):
            try:
                return await asyncio.wait_for(
                    self._http.get_order_by_client_order_id(order.client_order_id.value), timeout=timeout
                )
            except AlpacaHttpError as exc:
                if exc.status == 404:
                    not_found += 1
                    self._log.warning(f"lookup after unanswered submit for {order.client_order_id}: venue 404, attempt {i + 1}/{attempts}")
                else:
                    self._log.warning(f"lookup after unanswered submit for {order.client_order_id}: HTTP {exc.status}, attempt {i + 1}/{attempts}")
            except Exception as exc:  # noqa: BLE001
                self._log.warning(f"lookup after unanswered submit for {order.client_order_id}: {exc!r}, attempt {i + 1}/{attempts}")
            if i + 1 < attempts and delay > 0:
                await asyncio.sleep(delay)
        if not_found == attempts:
            self._log.error(f"Submit for {order.client_order_id} never landed (venue: 404 on every ask) — rejecting with the original cause")
            (reject or (lambda reason: self._reject(order, reason)))(_reject_reason(cause))
            return None
        self._venue_unanswered_lookups = getattr(self, "_venue_unanswered_lookups", 0) + 1
        self._log.error(
            f"Submit for {order.client_order_id} is UNANSWERED after {attempts} asks ({cause!r}; {not_found} of them 404); leaving it "
            f"SUBMITTED — Nautilus's inflight check will keep asking, and it is NOT a rejection (#354)"
        )
        return None

    def _reject(self, order, reason: str) -> None:
        self.generate_order_rejected(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=reason,
            ts_event=self._clock.timestamp_ns(),
        )

    #: Sleeves, cached. Re-read on a timer rather than per order: this runs on the submit path and a
    #: database round-trip per order would put Postgres between a strategy and the venue.
    _BUDGET_TTL_NS = 30 * 1_000_000_000

    async def _budget_allows(self, order) -> tuple[bool, str]:
        """May this order proceed under its strategy's budget? (allowed, reason, inputs).

        DELEGATES. The rule lives in `api.budget_guard` so that this client and the gated IBKR
        client cannot drift (#782) — a capital rule implemented twice is two rules. The behaviour,
        including FAILING OPEN, is unchanged and pinned by the tests that were written against it
        here.
        """
        from api.budget_guard import budget_allows
        from api.budget_journal import journal_row

        return await budget_allows(order, cache=self._cache,
                                   book_loader=self._budget_book, log=self._log,
                                   journal=journal_row, now_ns=int(self._clock.timestamp_ns()))

    async def _load_budget_book(self):
        """Read the sleeve book. Its own method so it can be driven in a test.

        Plain `await` — no thread handoff. The previous form scheduled onto the loop it was already
        running on (`_submit_order` is a coroutine, and nautilus creates it as a task on that loop:
        `live/execution_client.py:279`), so it could never complete and timed out on every submit.
        """
        from api.budget_store import load_book
        from api.db.engine import session_factory

        async with session_factory() as session:
            return await load_book(session)

    async def _budget_book(self):
        cached = getattr(self, "_budget_cache", None)
        now = self._clock.timestamp_ns()
        if cached is not None and now - getattr(self, "_budget_cache_ns", 0) < self._BUDGET_TTL_NS:
            return cached
        try:

            book = await self._load_budget_book()
        except Exception as exc:  # noqa: BLE001
            # SAY SO. This used to return the cache silently — and the cache was never populated,
            # because the only writer is below a call that always raised. A gate that cannot read
            # its book must not look like a gate that read an empty one (#642).
            self._log.error(
                "budget gate could not read the sleeve book (%r) — NOT enforcing sleeves this "
                "submit. Every order is allowed while this persists.", exc)
            return cached
        self._budget_cache = book
        self._budget_cache_ns = now
        return book

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        """Submit a bracket as a NATIVE Alpaca `order_class=bracket` (entry + protective STOP + take-profit) in
        # THE BRACKET PATH CONSULTS THE GATE TOO (#642). It never did, so every bracketed entry
        # walked around the sleeve check — a gate one order type can bypass is not the last hop
        # before the venue.
        first = next(iter(command.order_list.orders), None)
        if first is not None:
            allowed, why, facts = await self._budget_allows(first)
            if not allowed:
                self.generate_order_denied(
                    strategy_id=first.strategy_id,
                    instrument_id=first.instrument_id,
                    client_order_id=first.client_order_id,
                    reason=why,
                    ts_event=self._clock.timestamp_ns(),
                )
                return
        ONE request, so the protective legs REST AT THE BROKER and are broker-enforced. Nautilus's bracket uses
        LAST_PRICE emulation by default, which holds the SL/TP engine-side — a naked position if the engine or
        the tick feed drops (codex-confirmed). This path replaces that: the bracket action is built with
        NO emulation so the list routes here. Alpaca returns the entry with nested `legs`; we map each leg's
        venue id back to our SL/TP orders so the blotter + reconciliation track them."""
        orders = list(command.order_list.orders)
        entry = orders[0]
        children = orders[1:]
        sl = next((o for o in children if o.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)), None)
        tp = next((o for o in children if o.order_type == OrderType.LIMIT), None)
        for o in orders:
            self.generate_order_submitted(
                strategy_id=o.strategy_id, instrument_id=o.instrument_id,
                client_order_id=o.client_order_id, ts_event=self._clock.timestamp_ns(),
            )
        if sl is None or tp is None:
            for o in orders:
                self.generate_order_rejected(
                    strategy_id=o.strategy_id, instrument_id=o.instrument_id, client_order_id=o.client_order_id,
                    reason="Bracket must carry a protective stop and a take-profit leg", ts_event=self._clock.timestamp_ns(),
                )
            return
        def reject_all(reason: str) -> None:
            for o in orders:
                self._reject(o, reason)

        try:
            payload = self._build_bracket_request(entry, sl, tp)
        except Exception as exc:  # noqa: BLE001 — a shape Alpaca cannot take; nothing was sent
            self._log.error(f"Bracket {entry.client_order_id} cannot be expressed: {exc!r}")
            reject_all(_reject_reason(exc))
            return
        # THE SAME SEAM AS A SINGLE ORDER (#832). An answered 4xx rejects all three legs; a timed-out
        # POST asks the venue by the ENTRY's client id — Alpaca returns the bracket with its `legs` —
        # and a found, alive bracket is accepted exactly as the success path does. Still unanswered
        # → all three stay SUBMITTED for Nautilus's inflight check. Before this, every failure was a
        # triple OrderRejected, corpses if the bracket had landed.
        resp = await self._post_order(entry, payload, reject=reject_all)
        if resp is None:
            return
        try:
            self.generate_order_accepted(
                strategy_id=entry.strategy_id, instrument_id=entry.instrument_id,
                client_order_id=entry.client_order_id, venue_order_id=VenueOrderId(resp["id"]),
                ts_event=self._clock.timestamp_ns(),
            )
            # Alpaca returns the two protective legs (its own ids) nested under `legs`; attribute by type.
            for leg in resp.get("legs") or []:
                ltype = str(leg.get("type", ""))
                child = tp if ltype == "limit" else (sl if ltype.startswith("stop") else None)
                if child is None or not leg.get("id"):
                    continue
                self.generate_order_accepted(
                    strategy_id=child.strategy_id, instrument_id=child.instrument_id,
                    client_order_id=child.client_order_id, venue_order_id=VenueOrderId(leg["id"]),
                    ts_event=self._clock.timestamp_ns(),
                )
            self._log.info(f"Bracket {entry.client_order_id} accepted (venue={resp['id']}, legs={len(resp.get('legs') or [])})")
        except Exception as exc:  # noqa: BLE001 — the venue ANSWERED and we could not read it; never crash the client
            self._log.error(f"Bracket {entry.client_order_id} landed but its answer could not be read: {exc!r} — "
                            f"leaving it SUBMITTED for reconciliation, not rejecting a resting bracket")

    @staticmethod
    def _build_bracket_request(entry, sl, tp) -> dict:
        """Nautilus bracket OrderList → Alpaca native bracket payload. Entry is market|limit; the protective
        STOP rests via `stop_loss.stop_price`, take-profit via `take_profit.limit_price`. Fails closed on any
        combination Alpaca won't accept, rather than degrading to an unprotected single order."""
        entry_type = _ORDER_TYPE_TO_ALPACA.get(entry.order_type)
        if entry_type not in ("market", "limit"):
            raise ValueError(f"Bracket entry must be market or limit, got {entry.order_type!r}")
        tif = _TIF_TO_ALPACA.get(entry.time_in_force)
        if tif not in ("day", "gtc"):
            raise ValueError("Bracket time-in-force must be DAY or GTC")
        if sl.trigger_price is None or tp.price is None:
            raise ValueError("Bracket needs a stop trigger and a take-profit limit price")
        request = {
            "symbol": entry.instrument_id.symbol.value,
            "qty": str(entry.quantity),
            "side": "buy" if entry.side == OrderSide.BUY else "sell",
            "type": entry_type,
            "time_in_force": tif,
            "order_class": "bracket",
            "client_order_id": str(entry.client_order_id),
            "take_profit": {"limit_price": str(tp.price)},
            "stop_loss": {"stop_price": str(sl.trigger_price)},
        }
        if entry.has_price and entry.price is not None:
            request["limit_price"] = str(entry.price)
        return request

    def _last_price(self, instrument_id: InstrumentId) -> float | None:
        """Best available reference price from the shared cache: last trade, else quote mid. None when the
        instrument has no ticks yet (a symbol just added, or an illiquid pre-open book)."""
        trade = self._cache.trade_tick(instrument_id)
        if trade is not None:
            return float(trade.price)
        quote = self._cache.quote_tick(instrument_id)
        if quote is not None:
            return float((quote.bid_price + quote.ask_price) / 2)
        return None

    def _stop_side_violation(self, order) -> str | None:
        """Reason string if `order` is a wrong-side stop entry, else None. BUY stops trigger ABOVE the market,
        SELL stops BELOW. Skips silently when the order isn't a stop, has no trigger, or we have no price to
        compare against (let the broker decide — still cleanly, via AlpacaHttpError.reason)."""
        if order.order_type not in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
            return None
        trigger = order.trigger_price
        if trigger is None:
            return None
        ref = self._last_price(order.instrument_id)
        if ref is None:
            return None
        trig = float(trigger)
        if order.side == OrderSide.BUY and trig <= ref:
            return f"Buy-stop trigger ${trig:.2f} must be above the current price ${ref:.2f}"
        if order.side == OrderSide.SELL and trig >= ref:
            return f"Sell-stop trigger ${trig:.2f} must be below the current price ${ref:.2f}"
        return None

    @staticmethod
    def _build_order_request(order) -> dict:
        """Nautilus order → Alpaca `/v2/orders` payload (US equities).

        Fails closed: an order type or time-in-force we don't explicitly map raises rather than silently
        degrading to market/day (a trader who asked for a STOP must never get a MARKET).
        """
        alpaca_type = _ORDER_TYPE_TO_ALPACA.get(order.order_type)
        if alpaca_type is None:
            raise ValueError(f"Alpaca order type unsupported: {order.order_type!r}")
        if order.time_in_force == TimeInForce.GTD:
            # GTD needs an Alpaca expire_time we don't yet forward — reject rather than mis-book as GTC.
            raise ValueError("GTD orders not supported (would silently become GTC)")
        alpaca_tif = _TIF_TO_ALPACA.get(order.time_in_force)
        if alpaca_tif is None:
            raise ValueError(f"Alpaca time-in-force unsupported: {order.time_in_force!r}")

        request = {
            "symbol": order.instrument_id.symbol.value,
            "qty": str(order.quantity),
            "side": "buy" if order.side == OrderSide.BUY else "sell",
            "type": alpaca_type,
            "time_in_force": alpaca_tif,
            "client_order_id": str(order.client_order_id),
        }
        if order.has_price and order.price is not None:
            request["limit_price"] = str(order.price)
        if order.has_trigger_price and order.trigger_price is not None:
            request["stop_price"] = str(order.trigger_price)
        # Trailing stop (#46, PEAK) — Alpaca requires trail_percent OR trail_price, mutually exclusive
        # (confirmed via Alpaca docs, not assumed). Only BASIS_POINTS is ever built by this codebase's
        # `trailing_stop` order action (order_actions/standard.py) — PRICE-offset trailing stops would need
        # their own branch here if that ever changes; fail closed on anything else rather than silently
        # sending an incomplete request Alpaca would reject anyway.
        if order.order_type in (OrderType.TRAILING_STOP_MARKET, OrderType.TRAILING_STOP_LIMIT):
            if order.trailing_offset_type == TrailingOffsetType.BASIS_POINTS:
                request["trail_percent"] = str(Decimal(str(order.trailing_offset)) / 100)
            else:
                raise ValueError(
                    f"Alpaca trailing-offset type unsupported: {order.trailing_offset_type!r} "
                    "(only BASIS_POINTS -> trail_percent is wired)"
                )
        # Extended-hours (pre/post-market) — flagged via an order tag. Alpaca requires a limit order for it.
        if order.tags and "extended_hours" in order.tags:
            request["extended_hours"] = True
        return request

    async def _modify_order(self, command: ModifyOrder) -> None:
        if command.venue_order_id is None:
            self._log.error(f"Cannot modify {command.client_order_id} — no venue_order_id")
            return
        payload: dict = {}
        if command.quantity is not None:
            payload["qty"] = str(command.quantity)
        if command.price is not None:
            payload["limit_price"] = str(command.price)
        if command.trigger_price is not None:
            payload["stop_price"] = str(command.trigger_price)
        try:
            replaced = await self._http.replace_order(command.venue_order_id.value, payload)
        except Exception as exc:  # noqa: BLE001
            self._log.error(f"Modify failed for {command.client_order_id}: {exc!r}")
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=_reject_reason(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        # CONSUME the response. Alpaca's replace is not an in-place edit: the old order goes to
        # `replaced` and a NEW order takes its place with a NEW id (measured 2026-08-12). Dropping that
        # left the cache holding a dead id, so the next cancel or modify targeted an order that no longer
        # existed — silently, since cancelling a replaced order is not an error.
        #
        # OUTSIDE the try (codex review, High): the replace has already been ACCEPTED by the venue at this
        # point. A failure while reporting it must not be caught by the modify-failed handler above and
        # re-reported as a rejection, which would tell the engine the change did not happen when it did.
        new_id = str((replaced or {}).get("id") or "")
        if not new_id or new_id == command.venue_order_id.value:
            return

        # `quantity` must be a real Quantity — Nautilus rejects None on OrderUpdated. Resolved from the
        # command, then the cached order, then the venue's own echo; if none of the three has it, the id
        # handoff is skipped and logged rather than raising after a successful replace. (codex, High.)
        quantity = command.quantity
        if quantity is None:
            order = self._cache.order(command.client_order_id)
            quantity = order.quantity if order is not None else None
        if quantity is None:
            raw_qty = (replaced or {}).get("qty")
            if raw_qty is not None:
                try:
                    instrument = self._cache.instrument(command.instrument_id)
                    quantity = instrument.make_qty(float(raw_qty)) if instrument else None
                except Exception:  # noqa: BLE001 — a bad echo must not strand the handoff
                    quantity = None
        if quantity is None:
            self._log.error(
                f"replace of {command.client_order_id} moved venue id "
                f"{command.venue_order_id.value} -> {new_id} but no quantity could be resolved; "
                "the cache will keep the OLD id and a later cancel may miss"
            )
            return

        # `venue_order_id_modified=True` is Nautilus's own signal for this; without it the engine treats
        # a changed id as a mismatch.
        self.generate_order_updated(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=VenueOrderId(new_id),
            quantity=quantity,
            price=command.price,
            trigger_price=command.trigger_price,
            ts_event=self._clock.timestamp_ns(),
            venue_order_id_modified=True,
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        if command.venue_order_id is None:
            self._log.error(f"Cannot cancel {command.client_order_id} — no venue_order_id")
            return
        try:
            await self._http.cancel_order(command.venue_order_id.value)
        except Exception as exc:  # noqa: BLE001
            self._log.error(f"Cancel failed for {command.client_order_id}: {exc!r}")
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=_reject_reason(exc),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # Alpaca's DELETE /v2/orders is account-wide; only use it when the command is unfiltered.
        # A scoped cancel (by instrument, and optionally side) must cancel just the matching open
        # orders — never every open order on the account.
        instrument_id = getattr(command, "instrument_id", None)
        side = getattr(command, "order_side", OrderSide.NO_ORDER_SIDE)
        if instrument_id is None:
            try:
                await self._http.cancel_all_orders()
            except Exception as exc:  # noqa: BLE001
                self._log.error(f"Cancel-all failed: {exc!r}")
            return
        symbol = instrument_id.symbol.value
        # PAGINATED even though open orders are fewer: a scoped cancel that silently skips an
        # order past page one leaves it resting while the caller believes it was cancelled, which
        # is the failure a cancel exists to prevent.
        try:
            raws = await self._http.list_orders(status="open", paginate=True)
        except Exception as exc:  # noqa: BLE001
            # The LISTING died, so NOTHING was cancelled — and silence here reads as "done" (#649
            # item 3). Reject every open order in scope so the state machine hears the truth rather
            # than believing orders are gone while they can still fill. The cache is what the caller
            # sees, so its open set is exactly the set the caller believes this command cancelled.
            self._log.error(f"Scoped cancel-all for {symbol}: listing open orders failed: {exc!r}")
            for order in self._cache.orders_open(instrument_id=instrument_id, side=side):
                self.generate_order_cancel_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=order.venue_order_id,
                    reason=_reject_reason(exc),
                    ts_event=self._clock.timestamp_ns(),
                )
            return
        for raw in raws:
            if raw.get("symbol") != symbol:
                continue
            if side != OrderSide.NO_ORDER_SIDE:
                raw_side = "buy" if side == OrderSide.BUY else "sell"
                if raw.get("side") != raw_side:
                    continue
            # PER-ORDER failure, and LOUD (#649 item 3). One failed DELETE must neither abort the
            # remaining cancels nor stay a log line: an order the operator believes cancelled can
            # fill later. Symmetric with `_cancel_order` above — same event, same reason mapping.
            try:
                await self._http.cancel_order(raw["id"])
            except Exception as exc:  # noqa: BLE001
                client_order_id = self._client_order_id_for(raw)
                self._log.error(
                    f"Scoped cancel-all: cancel failed for venue id {raw['id']!r} "
                    f"(client id {client_order_id}): {exc!r}"
                )
                if client_order_id is None:
                    # An external order Nautilus never saw: `OrderCancelRejected` requires a
                    # client_order_id, so there is no event to route — the error above is the
                    # loudest honest signal. It still rests at the venue.
                    continue
                self.generate_order_cancel_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=VenueOrderId(raw["id"]),
                    reason=_reject_reason(exc),
                    ts_event=self._clock.timestamp_ns(),
                )

    # -- reconciliation reports -------------------------------------------------------------------
    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        # `paginate=True` — WITHOUT IT THIS SILENTLY TRUNCATES at Alpaca's 500-row page, NEWEST FIRST,
        # and this method is what EVERY typed consumer goes through: the protection reconciler, the exit
        # path, and Nautilus's own `generate_mass_status` during startup reconciliation.
        #
        # Truncation is the dangerous direction because a short list does not look like an error, it
        # looks like "nothing else is resting". An older protective stop that falls off page one goes
        # invisible: the reconciler reads the position as naked and arms a duplicate, and the exit path
        # reads zero shares reserved and submits an order the venue refuses on `available: 0`. That is
        # #387 and #245 restated, from a missing keyword argument. The account held 269 orders on
        # 2026-08-20; the margin was one busy week.
        #
        # And NEWEST FIRST is what makes it bite hardest at boot: without the cursor every restart
        # reconciled against a list whose OLDEST entries were missing, and a long-lived GTC protective
        # stop is the oldest thing on the account (#387, #451).
        orders = await self._http.list_orders(status="all", paginate=True)
        # PER-ROW GUARD (#643). One unrepresentable row must cost exactly that row, never the batch:
        # a DOLLAR-BASED (notional) order carries `"qty": null` — key PRESENT, so the `"0"` default
        # never applies and `Quantity.from_str(None)` raises TypeError. Without this guard that one
        # row aborted the whole batch; Nautilus's `generate_mass_status` swallows the exception and
        # returns None → "Cannot reconcile execution state" → ZERO strategies start while the node
        # logs RUNNING — the #613 inert shape, with #635's phantom positions minted on recovery.
        # External orders are deliberately retained (`filter_unclaimed_external_orders=False`), so a
        # notional order placed anywhere on the account reaches this path.
        #
        # The skip is NOT silent (absence-is-not-permission): each offender is NAMED in an error log
        # AND published on the health plane below, because a skipped order is invisible to
        # reconciliation — any shares it reserves or protection it provides are not being counted.
        reports: list[OrderStatusReport] = []
        unreconciled: list[dict] = []
        for raw in orders:
            try:
                report = self._parse_order_report(raw)
            except Exception as exc:  # noqa: BLE001 — any per-row parse failure is this defect's class
                unreconciled.append({
                    "venue_order_id": str(raw.get("id")),
                    "symbol": raw.get("symbol"),
                    "client_order_id": raw.get("client_order_id"),
                    "reason": repr(exc),
                })
                self._log.error(
                    f"Skipping unreconcilable order id={raw.get('id')!r} symbol={raw.get('symbol')!r} "
                    f"client_order_id={raw.get('client_order_id')!r}: {exc!r} — this order is INVISIBLE "
                    f"to reconciliation (surfaced on the health plane); the batch continues (#643)"
                )
                continue
            if report is not None:
                reports.append(report)
        # Published EVERY batch, empty included — three states, never two: [] states KNOWN-CLEAN (so a
        # fixed offender clears rather than the last bad frame standing forever), non-empty names the
        # offenders, and a topic never published is "never asked". Guarded: the guard's own reporting
        # must not become a new abort path for the batch it exists to protect.
        try:
            self._msgbus.publish(
                _UNRECONCILED_TOPIC, {"orders": unreconciled, "ts": self._clock.timestamp_ns()}
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(f"unreconciled-orders publish failed: {exc!r}")
        return reports

    #: Cached statuses a HOLD report may claim. RESTING states only:
    #:   - not SUBMITTED — a HOLD would make Nautilus mint an OrderAccepted with no venue id
    #:     (`live/execution_engine.py:3265`, codex #354 review);
    #:   - not PENDING_UPDATE / PENDING_CANCEL — those are INFLIGHT states Nautilus's own inflight check
    #:     owns; a HOLD reconciles "successfully" and resets its retry tracking every cycle
    #:     (`:3048`), so under a persistent 5xx the pending order would never resolve (sub-agent
    #:     review §5). `None` lets `_resolve_inflight_order` cancel it after its retries — the benign
    #:     outcome for a cancel that may or may not have landed.
    _HOLDABLE = frozenset({OrderStatus.ACCEPTED, OrderStatus.TRIGGERED, OrderStatus.PARTIALLY_FILLED})

    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport
    ) -> OrderStatusReport | None:
        """ONE order's status — an ANSWER, or an honest refusal to invent one (#354).

        THREE STATES, NEVER TWO. Nautilus's resolver (`live/execution_engine.py:1426`) treats `None`
        AND a raised exception identically: it REJECTS the cached order as `ORDER_NOT_FOUND_AT_VENUE`.
        So a transport failure here — one timed-out GET after five "missing" batch cycles — used to
        turn a resting stop into a corpse that kept resting at the venue (#807: sixteen of them,
        thirteen fired). The batch read already refuses to draw that conclusion (`exec_read_guard`,
        #791); this is the same rule on the targeted read.

          - 200        → the venue's report. Reconciled as always.
          - 404        → `None`. The venue ANSWERED: no such order. Rejecting is then correct.
          - transport failure, 429, 5xx → UNANSWERED. For an order the cache holds RESTING, a HOLD
            report built from the cache — same status, quantity, fills and prices — so Nautilus's
            reconcile is a no-op and the resolver returns without rejecting. For anything else
            (SUBMITTED, unknown to the cache) `None`, and Nautilus's own inflight retries run on.
            Logged at ERROR with the cause and COUNTED (`_venue_unanswered_lookups`, published on
            the reconcile snapshot) — a venue that never answers must be visible, not quiet.

        Alpaca's id is asked first when the cache knows it: a bracket's protective legs exist there
        under ALPACA's own client id (#242), so ours can never find them.
        """
        target_coid = command.client_order_id
        target_void = command.venue_order_id
        if target_void is None and target_coid is not None:
            target_void = self._cache.venue_order_id(target_coid)
        if target_coid is None and target_void is None:
            return None
        # OUR ID FIRST. Alpaca answers `orders:by_client_order_id` with the CURRENT order for that id,
        # so a replace (new venue id) is followed; asking by a cached venue id after a replace returns
        # the OLD row — "replaced" → CANCELED on a live order (sub-agent review §5). The venue id is the
        # fallback for the one case our id cannot find: a bracket leg Alpaca named itself (#242).
        try:
            if target_coid is not None:
                try:
                    raw = await self._http.get_order_by_client_order_id(target_coid.value)
                except AlpacaHttpError as exc:
                    if exc.status != 404 or target_void is None:
                        raise
                    raw = await self._http.get_order(target_void.value)
            else:
                raw = await self._http.get_order(target_void.value)
        except AlpacaHttpError as exc:
            if exc.status == 404:
                return None  # ANSWERED: the venue holds no such order
            return self._hold_or_none(target_coid, f"HTTP {exc.status}: {exc!r}")
        except Exception as exc:  # noqa: BLE001 — a transport failure is the unanswered case, by definition
            return self._hold_or_none(target_coid, repr(exc))
        return self._parse_order_report(raw)

    def _hold_or_none(self, client_order_id, cause: str) -> OrderStatusReport | None:
        """The UNANSWERED outcome, made explicit. A HOLD only for an order the cache holds resting."""
        self._venue_unanswered_lookups = getattr(self, "_venue_unanswered_lookups", 0) + 1
        order = self._cache.order(client_order_id) if client_order_id is not None else None
        if order is None or order.status not in self._HOLDABLE:
            self._log.error(
                f"venue UNANSWERED for {client_order_id!r} ({cause}); order is "
                f"{order.status_string() if order is not None else 'not in the cache'} — reporting nothing, "
                f"NOT 'not found' (#354)"
            )
            return None
        self._log.error(
            f"venue UNANSWERED for {client_order_id!r} ({cause}); HOLDING the cached "
            f"{order.status_string()} rather than rejecting a resting order on a question nobody answered (#354)"
        )
        now = self._clock.timestamp_ns()
        trailing = {}
        if order.order_type == OrderType.TRAILING_STOP_MARKET and getattr(order, "trailing_offset", None) is not None:
            trailing = {"trailing_offset": order.trailing_offset, "trailing_offset_type": order.trailing_offset_type}
        return OrderStatusReport(
            account_id=order.account_id or self.account_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            order_side=order.side,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            order_status=order.status,
            quantity=order.quantity,
            filled_qty=order.filled_qty,
            avg_px=Decimal(str(order.avg_px)) if order.filled_qty > 0 else None,
            price=getattr(order, "price", None) if order.has_price else None,
            trigger_price=getattr(order, "trigger_price", None) if order.has_trigger_price else None,
            trigger_type=order.trigger_type if order.has_trigger_price else TriggerType.NO_TRIGGER,
            **trailing,
            report_id=UUID4(),
            ts_accepted=order.ts_last,
            ts_last=order.ts_last,
            ts_init=now,
        )

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports
    ) -> list[PositionStatusReport]:
        positions = await self._http.list_positions()
        reports = [self._parse_position_report(p) for p in positions]
        kept = [r for r in reports if r is not None]
        dropped = len(reports) - len(kept)
        if dropped:
            # COUNTED, not just per-row named (#649 item 2): a shortened list looks like "the whole
            # book", and reconciliation treats what it does not see as external/absent. "1 of 2" is a
            # different statement from "here are your positions".
            self._log.warning(
                f"{dropped} of {len(positions)} broker positions could not be mapped and are "
                f"MISSING from position reconciliation — the book below is incomplete, not smaller"
            )
        return kept

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        """Per-execution fills from Alpaca's account activities (FILL) → Nautilus FillReports, honoring the
        command's instrument / venue-order / time filters. Fail-loud: an HTTP error propagates (never swallowed
        to []), so reconciliation knows fills are unresolved rather than assuming a flat book."""
        # Server `after` bounds Alpaca's activity-CREATION time, but we reconcile on `transaction_time`
        # (execution time). Creation >= execution always, so `after=start` never drops an in-window fill —
        # but `until=end` WOULD drop a fill executed in-window yet created just after `end`. So we send only
        # the safe lower bound and let the client-side `_fill_in_window` (on transaction_time) be authoritative
        # for both ends. (codex-flagged: server datetime bounds vs transaction_time mismatch.)
        # Normalize the window once — Nautilus may hand start/end as datetime OR pd.Timestamp; pd.Timestamp()
        # accepts both. `after` is the (safe) server lower bound; start_ns/end_ns drive the authoritative
        # client-side transaction_time window.
        start_ns = pd.Timestamp(command.start).value if command.start is not None else None
        end_ns = pd.Timestamp(command.end).value if command.end is not None else None
        after = pd.Timestamp(command.start).isoformat() if command.start is not None else None
        activities = await self._http.list_activities(activity_type="FILL", after=after)

        want_symbol = command.instrument_id.symbol.value if command.instrument_id is not None else None
        want_void = command.venue_order_id.value if command.venue_order_id is not None else None
        ts_init = self._clock.timestamp_ns()

        reports: list[FillReport] = []
        for raw in activities:
            symbol = raw.get("symbol", "")
            # Command filters (not-a-fill for us) → drop quietly; these aren't malformed data.
            if want_symbol is not None and symbol != want_symbol:
                continue
            if want_void is not None and str(raw.get("order_id")) != want_void:
                continue
            ts_event = _fill_ts_ns(raw)
            if ts_event is not None and not _fill_in_window(ts_event, start_ns, end_ns):
                continue
            instrument_id = self._symbol_to_id.get(symbol)
            report = (
                _parse_fill_activity(raw, instrument_id, self.account_id, ts_event, ts_init)
                if instrument_id is not None and ts_event is not None
                else None
            )
            if report is None:
                # A FILL that passed the command filters but can't become a FillReport (unknown symbol,
                # bad/missing timestamp, unmapped side, missing ids/price/qty) is NOT dropped silently — a
                # reconciliation source that hides fills causes position drift. Warn (batch continues; one
                # bad row must not sink the others).
                self._log.warning(
                    f"Dropping unmappable Alpaca FILL activity id={raw.get('id')!r} "
                    f"symbol={symbol!r} order_id={raw.get('order_id')!r} — cannot build FillReport"
                )
                continue
            reports.append(report)
        return reports

    # -- report parsing ---------------------------------------------------------------------------
    def _client_order_id_for(self, raw: dict) -> ClientOrderId | None:
        """OUR `ClientOrderId` for an Alpaca order — resolved by VENUE id first (#242).

        A native bracket request carries no per-leg client-id field, so Alpaca MINTS ITS OWN for the
        protective legs (dashed UUIDs; ours are 32-char hex or `FL-`-prefixed). Passing that straight
        through as a `ClientOrderId` made reconciliation match nothing and file both legs as EXTERNAL
        — while `_submit_order_list` had already linked them correctly by VENUE id at submit time.

        The consequence was not cosmetic: two unclaimed sell legs each reserved the full position, so
        Alpaca reported `available: 0` and every exit — flatten, PEAK's trailing stop, a manual sell —
        was rejected for insufficient quantity. A bracketed position could not be closed from the
        cockpit. Meanwhile our own leg orders, holding client ids Alpaca had never seen, failed
        `ORDER_NOT_FOUND_AT_VENUE`.

        So: ask the cache what WE call this venue order. Only when the cache has never seen it do we
        fall back to Alpaca's field — which is correct for genuinely external activity (a fill placed
        in Alpaca's own UI), the case the #79 quarantine plane exists for.

        Resolving by venue id rather than sniffing the id FORMAT is deliberate: a format test would
        misclassify the moment either side changes its id scheme, and would be guessing at ownership
        the cache can answer exactly.

        NOT wrapped in a try/except. A cache MISS returns `None` — it does not raise — so a catch here
        could only swallow a genuine cache or integrity bug and then fall back to Alpaca's minted id,
        silently recreating the unclaimed-leg failure this exists to prevent. Batch resilience, if it
        is ever wanted, belongs around per-order parsing in `generate_order_status_reports`, not around
        an index lookup. (codex review.)
        """
        venue_id = raw.get("id")
        if venue_id:
            mine = self._cache.client_order_id(VenueOrderId(str(venue_id)))
            if mine is not None:
                return mine
        coid = raw.get("client_order_id")
        return ClientOrderId(coid) if coid else None

    def _parse_order_report(self, raw: dict) -> OrderStatusReport | None:
        instrument_id = self._symbol_to_id.get(raw.get("symbol", ""))
        if instrument_id is None:
            # WARN, do not drop in silence. Both this and the unmapped-status branch below shorten the
            # reconciliation list rather than failing it, and a short list reads as "nothing else is
            # resting" — the same silent shape as the truncation above. The status case already warned;
            # the symbol case did not, so an instrument missing from the map removed its orders from
            # reconciliation with no trace anywhere.
            self._log.warning(
                f"Skipping order {raw.get('id')} — symbol {raw.get('symbol')!r} is not in the "
                f"instrument map, so its orders are INVISIBLE to reconciliation"
            )
            return None
        status = _ALPACA_TO_ORDER_STATUS.get(raw.get("status", ""))
        if status is None:
            # Unknown status → skip rather than assume ACCEPTED (fail closed on reconciliation).
            self._log.warning(f"Skipping order {raw.get('id')} — unmapped Alpaca status {raw.get('status')!r}")
            return None
        side = _ALPACA_ORDER_SIDE.get(raw.get("side", ""))
        if side is None:
            # Same posture as the unmapped status above (#652 item 8a): an unknown/absent side must not
            # default to SELL — a phantom reducing order on reconciliation is how protection gets stripped.
            self._log.warning(f"Skipping order {raw.get('id')} — unmapped Alpaca side {raw.get('side')!r}")
            return None
        avg = raw.get("filled_avg_price")
        limit = raw.get("limit_price")
        stop = raw.get("stop_price")
        now = self._clock.timestamp_ns()
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            client_order_id=self._client_order_id_for(raw),
            venue_order_id=VenueOrderId(raw["id"]),
            order_side=side,
            order_type=_ALPACA_TO_ORDER_TYPE.get(raw.get("type", "market"), OrderType.MARKET),
            time_in_force=_ALPACA_TO_TIF.get(raw.get("time_in_force", "day"), TimeInForce.DAY),
            order_status=status,
            quantity=Quantity.from_str(raw.get("qty", "0")),
            filled_qty=Quantity.from_str(raw.get("filled_qty", "0")),
            avg_px=Decimal(avg) if avg else None,
            price=Price.from_str(limit) if limit else None,
            trigger_price=Price.from_str(stop) if stop else None,
            # Nautilus rejects a trigger_price without a trigger_type — Alpaca stops fire on last trade.
            trigger_type=TriggerType.LAST_PRICE if stop else TriggerType.NO_TRIGGER,
            # A TRAILING stop cannot be rebuilt without its offset. Reconciliation constructs a real
            # `TrailingStopMarketOrder` from this report, and its constructor does a bare `init['trailing_offset']`
            # — so omitting it raises `KeyError: 'trailing_offset'` INSIDE startup reconciliation, which
            # takes the whole node down in a crash loop. It did, on 2026-08-13: a trailing stop placed
            # outside Nautilus (raw REST) came back through reconciliation and the engine could not boot
            # until the lookback window rolled past it.
            #
            # Alpaca reports `trail_percent` OR `trail_price`, never both, so the offset type follows
            # whichever it sent.
            **_trailing_fields(raw),
            report_id=UUID4(),
            ts_accepted=now,
            ts_last=now,
            ts_init=now,
        )

    def _parse_position_report(self, raw: dict) -> PositionStatusReport | None:
        instrument_id = self._symbol_to_id.get(raw.get("symbol", ""))
        if instrument_id is None:
            # WARN, do not drop in silence — the mirror of `_parse_order_report`'s fix (#649 item 2).
            # `_symbol_to_id` is built ONCE at `_connect`, so a post-connect listing or delisting is
            # exactly when this branch fires, and a silent None makes that position vanish from
            # reconciliation: an incomplete book rendered as a confident smaller one.
            self._log.warning(
                f"Skipping position in {raw.get('symbol')!r} (qty {raw.get('qty')!r}) — symbol is "
                f"not in the instrument map (built at connect), so this position is INVISIBLE to "
                f"reconciliation"
            )
            return None
        qty = Decimal(raw.get("qty", "0"))
        if qty > 0:
            side = PositionSide.LONG
        elif qty < 0:
            side = PositionSide.SHORT
        else:
            side = PositionSide.FLAT
        now = self._clock.timestamp_ns()
        # Carry Alpaca's average entry price so a reconciled/synthesized position has the correct cost basis
        # (else avg/P&L show wrong while quantity looks right — codex finding). Absent → None (Nautilus fills
        # from fills when available).
        avg_raw = raw.get("avg_entry_price")
        avg_px = Decimal(str(avg_raw)) if avg_raw not in (None, "") else None
        return PositionStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            position_side=side,
            # Preserve Alpaca's fractional-share quantity as reported (str keeps its precision) rather
            # than truncating to whole shares.
            quantity=Quantity.from_str(str(abs(qty))),
            avg_px_open=avg_px,
            report_id=UUID4(),
            ts_last=now,
            ts_init=now,
        )


class AlpacaLiveExecClientFactory(LiveExecClientFactory):
    """Constructs `AlpacaExecutionClient` (with its REST client + instrument provider) for the node."""

    #: The budget rule is INLINE in `_submit_order` above (`_budget_allows` → `budget_guard`), so
    #: this client is gated without the wrap; the marker states the property the class-level guard
    #: checks. The ownership guard is NOT inline any more (#1030) — it is the wrap `create` installs.
    gates_budget = True
    gates_ownership = True

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: AlpacaExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> AlpacaExecutionClient:
        http_client = AlpacaHttpClient(
            key=config.api_key or "",
            secret=config.api_secret or "",
            trading_base=config.trading_base_url,
            data_base=config.data_base_url,
        )
        instrument_provider = AlpacaInstrumentProvider(http_client)
        from api.providers.gated_exec import install_ownership_gate

        return install_ownership_gate(AlpacaExecutionClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            http_client=http_client,
            config=config,
        ))


def _reference_assets_fetcher(trading_base_url: str, api_key: str, api_secret: str):
    """The reference-asset ledger (#647): every US-equity row from THIS account's configured
    endpoint — never a hardcoded paper URL, which is what sent live keys to `paper-api`.

    NO `status=` filter and NO `tradable` filter: a delisted or halted name is exactly the row the
    consumer (qc345's `terminal_buckets`) exists to notice while the strategy still HOLDS it;
    filtering it out of the reference data is how it disappears quietly instead of being flagged.

    Synchronous by contract: the consumer calls it once at strategy BUILD time, before the node's
    event loop exists, so it must not need one.
    """

    def _fetch() -> list[dict]:
        import json
        import urllib.request

        req = urllib.request.Request(
            f"{trading_base_url}/v2/assets?asset_class=us_equity",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())

    return _fetch


def build(exec_config: dict) -> ExecClientSpec:
    """Build the Alpaca exec client spec from the `[execution.alpaca]` table.

    Secrets come from the environment (`APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`, override via the table's
    `key_env`/`secret_env`); run-api.sh injects them from the keychain. routing.default=True so this one
    client serves orders across every US-equity MIC venue.
    """
    key_env = exec_config.get("key_env", _KEY_ENV)
    secret_env = exec_config.get("secret_env", _SECRET_ENV)
    api_key = os.environ.get(key_env)
    api_secret = os.environ.get(secret_env)
    if not api_key or not api_secret:
        raise RuntimeError(
            f"{key_env}/{secret_env} unset — launch via backend/scripts/run-api.sh (injects from keychain)."
        )
    if "ws_base_url" in exec_config:
        # LOUD ignore, not a silent one (#652 item 8b): the exec path has no WebSocket (fills arrive
        # via reconciliation), so this key connects to nothing. Silent is how KUMO_DATA sat dead (#574).
        _log.warning(
            "[execution.alpaca] ws_base_url is IGNORED — the Alpaca exec client has no WebSocket; "
            "remove the key from feed.toml (fills arrive via reconciliation)"
        )
    defaults = AlpacaExecClientConfig()
    config = AlpacaExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        trading_base_url=exec_config.get("trading_base_url", defaults.trading_base_url),
        data_base_url=exec_config.get("data_base_url", defaults.data_base_url),
        routing=RoutingConfig(default=True),
    )
    return ExecClientSpec(
        client_id=ALPACA,
        config=config,
        factory=AlpacaLiveExecClientFactory,
        # Alpaca reserves per account per symbol against ANY resting reducing order and refuses a
        # second claim with `insufficient qty available (available: 0)` — #358, INCIDENT provenance in
        # api/venues/facts.py. The exit path must cancel-and-await the release before submitting.
        reserves_shares_against_resting_stop=True,
        # The delisting/status ledger (#647), from THIS config's endpoint so it follows the account.
        reference_assets=_reference_assets_fetcher(
            config.trading_base_url, api_key, api_secret
        ),
    )
