"""The standard order actions (#50) — market / limit / stop_market / stop_limit / bracket. Extracted verbatim
from engine_node so behaviour is IDENTICAL; new types register alongside without touching core. Schemas allow
string|number for prices so the existing accepted shapes (numeric strings like "190.00") are unchanged."""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.enums import TrailingOffsetType, TriggerType
from nautilus_trader.model.objects import Price

from api.order_actions.registry import OrderActionDescriptor, OrderBuildContext, register_action

_PRICE = {"type": ["string", "number"]}


def _market(ctx: OrderBuildContext, params: dict):
    return ctx.factory.market(
        ctx.instrument_id, ctx.side, ctx.quantity,
        time_in_force=ctx.time_in_force, client_order_id=ctx.client_order_id, tags=ctx.tags,
    )


def _limit(ctx: OrderBuildContext, params: dict):
    return ctx.factory.limit(
        ctx.instrument_id, ctx.side, ctx.quantity, Price.from_str(str(params["price"])),
        time_in_force=ctx.time_in_force, client_order_id=ctx.client_order_id, tags=ctx.tags,
    )


def _stop_market(ctx: OrderBuildContext, params: dict):
    """Fixed-trigger stop. `reduce_only` is passed through for #872's entry floors and DEFAULTS FALSE,
    so every existing caller builds exactly the order it built before.

    A protective stop must never OPEN a position: if it rests and the position closes by another route
    — a manual flatten, a PEAK exit, a reconciliation — firing does not close anything, it opens the
    opposite side. Confirmed by CALLING `OrderFactory.stop_market(..., reduce_only=True)` rather than
    reading its signature (the #47 lesson): it returns a StopMarketOrder with `is_reduce_only` True.
    """
    return ctx.factory.stop_market(
        ctx.instrument_id, ctx.side, ctx.quantity, Price.from_str(str(params["trigger_price"])),
        reduce_only=bool(params.get("reduce_only", False)),
        time_in_force=ctx.time_in_force, client_order_id=ctx.client_order_id, tags=ctx.tags,
    )


def _stop_limit(ctx: OrderBuildContext, params: dict):
    return ctx.factory.stop_limit(
        ctx.instrument_id, ctx.side, ctx.quantity,
        Price.from_str(str(params["price"])), Price.from_str(str(params["trigger_price"])),
        time_in_force=ctx.time_in_force, client_order_id=ctx.client_order_id, tags=ctx.tags,
    )


def _bracket(ctx: OrderBuildContext, params: dict):
    """Native Nautilus bracket OrderList (entry + protective STOP_MARKET + take-profit LIMIT). NO emulation:
    the list routes to the Alpaca exec client's `_submit_order_list`, which posts a NATIVE Alpaca
    `order_class=bracket` so the protective legs REST AT THE BROKER (broker-enforced). The old
    `emulation_trigger=LAST_PRICE` held the SL/TP engine-side (a naked position if the engine/feed dropped —
    codex-confirmed). SL/TP TIF default to GTC per the factory unless supplied — preserved. `entry_order_type`
    is a Nautilus OrderType; the bracket-group tag goes on all legs so the blotter groups the trio (#34)."""
    tag = [f"bracket:{params['group']}"]
    return ctx.factory.bracket(
        instrument_id=ctx.instrument_id,
        order_side=ctx.side,
        quantity=ctx.quantity,
        entry_order_type=params["entry_order_type"],
        entry_price=Price.from_str(str(params["price"])) if params.get("price") is not None else None,
        sl_trigger_price=Price.from_str(str(params["stop_trigger"])),
        tp_price=Price.from_str(str(params["target_price"])),
        tp_post_only=False,
        time_in_force=ctx.time_in_force,
        entry_client_order_id=ctx.client_order_id,
        sl_client_order_id=params["sl_client_order_id"],
        tp_client_order_id=params["tp_client_order_id"],
        entry_tags=tag,
        sl_tags=tag,
        tp_tags=tag,
    )


def _trailing_stop(ctx: OrderBuildContext, params: dict):
    """Native trailing stop, BASIS_POINTS offset (#46, PEAK's adaptive trail — the wide/tight width is
    just a different `trail_bps` value, this action doesn't know which). `emulation_trigger` is left at
    the factory default (`NO_TRIGGER`) — deliberately NOT engine-held emulation (the same "broker-enforced,
    not held by this process" choice already made for `bracket`/`stop_bracket` — Nautilus's OWN trailing-
    stop machinery, `execution/trailing.pyx`, is built for the ENGINE to recompute the trigger price itself
    tick-by-tick, which is exactly the emulated-and-naked-if-the-engine-drops pattern this codebase moved
    away from). Confirmed by calling `OrderFactory.trailing_stop_market` directly (not just reading its
    signature — the #47 lesson) before this action was written."""
    return ctx.factory.trailing_stop_market(
        reduce_only=bool(params.get("reduce_only", False)),
        instrument_id=ctx.instrument_id,
        order_side=ctx.side,
        quantity=ctx.quantity,
        trailing_offset=Decimal(str(params["trail_bps"])),
        trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
        trigger_type=TriggerType.LAST_PRICE,
        time_in_force=ctx.time_in_force,
        client_order_id=ctx.client_order_id,
        tags=ctx.tags,
    )


def register_standard_actions() -> None:
    """Register the standard 5 (idempotent — safe to import once). New types add a register_action call."""
    for desc in (
        OrderActionDescriptor("market", "both", {"type": "object"}, _market),
        OrderActionDescriptor(
            "limit", "both", {"type": "object", "properties": {"price": _PRICE}, "required": ["price"]}, _limit
        ),
        OrderActionDescriptor(
            "stop_market", "both",
            {"type": "object", "properties": {"trigger_price": _PRICE}, "required": ["trigger_price"]},
            _stop_market,
        ),
        OrderActionDescriptor(
            "stop_limit", "both",
            {"type": "object", "properties": {"price": _PRICE, "trigger_price": _PRICE},
             "required": ["price", "trigger_price"]},
            _stop_limit,
        ),
        OrderActionDescriptor(
            "bracket", "entry",
            # v0 schema covers the price legs; the full bracket param contract (entry_order_type, group,
            # sl/tp client_order_ids) is engine-wrapper-enforced (_handle_submit_bracket) until the UI drives it.
            {"type": "object", "properties": {"stop_trigger": _PRICE, "target_price": _PRICE},
             "required": ["stop_trigger", "target_price"]},
            _bracket,
        ),
        OrderActionDescriptor(
            "trailing_stop", "exit",
            {"type": "object", "properties": {"trail_bps": _PRICE}, "required": ["trail_bps"]},
            _trailing_stop,
        ),
    ):
        register_action(desc)
