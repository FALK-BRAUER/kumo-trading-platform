"use client";

/**
 * Detail-variant registrations (#70/#71). Module-level, side-effect import (pulled in by DetailHost) — the
 * registry is static config, not runtime-mutated. New detail variants register here, swap-by-id, no core change.
 * Adapts the existing SymbolDetailSurface as the symbol descriptor (no rewrite); order/position are new.
 */
import { registerDetail, type DetailProps } from "@/lib/framework/detail/registry";
import { SymbolDetailSurface } from "@/components/board/SymbolDetailSurface";
import { OrderDetail } from "@/components/board/detail/OrderDetail";
import { PositionDetail } from "@/components/board/detail/PositionDetail";
import { UnclaimedPositionDetail } from "@/components/board/detail/UnclaimedPositionDetail";

function SymbolDetail({ focus }: DetailProps) {
  if (focus.kind !== "symbol") return null;
  return <SymbolDetailSurface instrument={focus.instrument} />;
}

registerDetail({ id: "detail.symbol", kind: "symbol", strategy: "*", component: SymbolDetail, title: "Instrument" });
registerDetail({ id: "detail.order", kind: "order", strategy: "*", component: OrderDetail, title: "Order" });
registerDetail({ id: "detail.position", kind: "position", strategy: "*", component: PositionDetail, title: "Position" });
registerDetail({ id: "detail.unclaimed", kind: "unclaimed", strategy: "*", component: UnclaimedPositionDetail, title: "Unclaimed position" });
