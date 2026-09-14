"""External-activity classifier (#79) — detect + flag + quarantine broker activity the cockpit didn't
originate, so it can't silently join a strategy's P&L.

A SEPARATE surface from the TradeCycleProjection (which owns one strategy + a cycle namespace): external
activity has no valid cycle_id until the coordinator (#80) claims it. This scans the native cache for
positions/orders whose strategy_id is NOT one the cockpit owns and emits a QUARANTINED ExternalActivityDTO.

Origin (Nautilus 1.229.0): a reserved `StrategyId("EXTERNAL")` (`is_external()`) is broker-side — VENUE (real/
manual order) vs RECONCILIATION (synthetic position-diff), distinguished by the order's tags. A non-owned,
non-EXTERNAL strategy id is FOREIGN (another cockpit strategy, unverified per the ADR). v0 = detect only; the
claim/import policy is the coordinator's (#80).
"""

from __future__ import annotations

from nautilus_trader.model.identifiers import StrategyId

from api.models import ExternalActivityDTO

_RECONCILIATION_TAG = "RECONCILIATION"


def _has_recon_tag(tags) -> bool:
    # Exact normalized membership — Nautilus tags reconciliation orders as exactly ["RECONCILIATION"].
    return bool(tags) and any(str(t).upper() == _RECONCILIATION_TAG for t in tags)


def _order_origin(strategy_id: StrategyId, tags) -> str:
    if not strategy_id.is_external():
        return "FOREIGN"
    return "RECONCILIATION" if _has_recon_tag(tags) else "VENUE"


def _position_origin(cache, pos) -> str:
    """A position carries no tags; derive origin from its OPENING order's tags. RECONCILIATION positions are
    synthesized from a `["RECONCILIATION"]`-tagged order; a manual VENUE fill has none. UNKNOWN when the opening
    order isn't in the cache (can't distinguish) rather than guessing VENUE (codex-flagged)."""
    if not pos.strategy_id.is_external():
        return "FOREIGN"
    opening = cache.order(pos.opening_order_id) if pos.opening_order_id is not None else None
    if opening is None:
        return "UNKNOWN"
    return "RECONCILIATION" if _has_recon_tag(opening.tags) else "VENUE"


def classify_external(cache, owned_strategy_ids: set[str], client_id: str) -> list[ExternalActivityDTO]:
    """Every OPEN native position + working order NOT owned by the cockpit → a QUARANTINED ExternalActivityDTO.
    `cache` is the Nautilus cache; `owned_strategy_ids` are the strings the cockpit runs (e.g. {"MANUAL-001"});
    `client_id` is the exec client the account sits under. Open-only: quarantine is a CURRENT-RISK surface, not a
    history log (closed externals would reappear forever); a claimed-audit inbox is a later, product-gated add."""
    out: list[ExternalActivityDTO] = []

    for pos in cache.positions_open():
        if str(pos.strategy_id) in owned_strategy_ids:
            continue
        out.append(
            ExternalActivityDTO(
                account_id=str(pos.account_id),
                client_id=client_id,
                instrument_id=str(pos.instrument_id),
                source="POSITION",
                strategy_id=str(pos.strategy_id),
                origin=_position_origin(cache, pos),
                side=pos.side.name,
                quantity=float(pos.quantity),
                realized_pnl=str(pos.realized_pnl),
                ts_last=pos.ts_last if pos.ts_last is not None else 0,
            )
        )

    for order in cache.orders_open():
        if str(order.strategy_id) in owned_strategy_ids:
            continue
        out.append(
            ExternalActivityDTO(
                account_id=str(order.account_id) if order.account_id is not None else "",
                client_id=client_id,
                instrument_id=str(order.instrument_id),
                source="ORDER",
                strategy_id=str(order.strategy_id),
                origin=_order_origin(order.strategy_id, order.tags),
                side=order.side.name,
                quantity=float(order.quantity),
                client_order_id=order.client_order_id.value,
                order_status=order.status.name,
                ts_last=order.ts_last if order.ts_last is not None else 0,
            )
        )

    return out
