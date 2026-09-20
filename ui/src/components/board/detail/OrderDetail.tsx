"use client";

import { useMutation } from "@tanstack/react-query";
import { cancelOrder } from "@/lib/api/client";
import { useSource } from "@/lib/framework/datasource/useSource";
import { useCockpitStore } from "@/lib/framework/store";
import { StatusBadge } from "@/components/ds/StatusBadge";
import { toneForStatus } from "@/components/ds/status";
import type { DetailProps } from "@/lib/framework/detail/registry";
import type { OrderDTO, OrdersResponse } from "@/lib/api/types";
import { DetailIdentity, venueOf } from "./DetailIdentity";
import { CloseButton } from "@/components/ds/CloseButton";

/**
 * Order detail (#70/#71) — the ORDER-kind detail variant. Re-reads the order LIVE by client_order_id from
 * the shared `orders` source (never a snapshot captured at tap time), so it stays current as the order
 * fills/cancels while open. Surfaces the full lifecycle + fills + bracket legs and — the headline — the
 * deny/reject REASON (OrderDTO.reason, #122): a DENIED order explains WHY here, not in the engine logs.
 */

const TERMINAL = new Set(["FILLED", "CANCELED", "REJECTED", "EXPIRED", "DENIED"]);

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="font-mono text-[10px] uppercase tracking-wide text-t3">{label}</span>
      <span className="font-mono text-[12px] text-t1">{children}</span>
    </div>
  );
}

function bracketGroup(order: OrderDTO): string | null {
  const tag = order.tags?.find((t) => t.startsWith("bracket:"));
  return tag ? tag.slice("bracket:".length) : null;
}

export function OrderDetail({ focus }: DetailProps) {
  const closeDetail = useCockpitStore((s) => s.closeDetail);
  const clientOrderId = focus.kind === "order" ? focus.clientOrderId : "";
  const { data } = useSource("orders", {});
  const orders = (data as OrdersResponse | undefined)?.orders ?? [];
  const order = orders.find((o) => o.client_order_id === clientOrderId);

  const cancel = useMutation({ mutationFn: () => cancelOrder(clientOrderId) });

  if (!order) {
    return (
      <div className="flex h-[calc(100vh-9rem)] flex-col items-center justify-center rounded-xl border border-ds-line bg-ds-surf text-t2">
        <p className="font-mono text-sm">order not found</p>
        <button onClick={closeDetail} className="mt-3 rounded-md bg-ds-surf2 px-3 py-1.5 font-mono text-xs text-t2">
          close
        </button>
      </div>
    );
  }

  const symbol = order.instrument_id.split(".")[0];
  const isError = toneForStatus(order.status) === "error";
  const working = !TERMINAL.has(order.status);
  const group = bracketGroup(order);
  const legs = group ? orders.filter((o) => bracketGroup(o) === group && o.client_order_id !== order.client_order_id) : [];

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col overflow-hidden rounded-xl border border-ds-line bg-ds-surf">
      {/* header */}
      <div className="flex items-center justify-between border-b border-ds-line px-4 py-2.5">
        <DetailIdentity symbol={symbol} venue={venueOf(order.instrument_id)}>
          <span className="font-mono text-[11px] text-t2">
            {order.side} {order.quantity} · {order.order_type.replace(/_/g, " ")}
          </span>
        </DetailIdentity>
        <CloseButton onClick={closeDetail} />
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {/* status + the deny reason — the reason a DENIED order died, in full */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <StatusBadge status={order.status} />
          {order.reason && (
            <span className="font-mono text-[11px] text-status-bear">{order.reason}</span>
          )}
        </div>

        <div className="divide-y divide-ds-line/60">
          <Row label="Status">{order.status.replace(/_/g, " ")}</Row>
          {order.reason && (
            <Row label={isError ? "Reason" : "Note"}>
              <span className="text-status-bear">{order.reason}</span>
            </Row>
          )}
          <Row label="Fills">
            {order.filled_qty} / {order.quantity}
            {order.avg_px != null ? ` @ ${order.avg_px.toFixed(2)}` : ""}
          </Row>
          {order.price != null && <Row label="Limit">{order.price.toFixed(2)}</Row>}
          {order.trigger_price != null && <Row label="Stop trigger">{order.trigger_price.toFixed(2)}</Row>}
          <Row label="Time in force">{order.time_in_force}</Row>
          <Row label="Strategy">{order.strategy_id.replace(/-\d+$/, "")}</Row>
          {order.venue_order_id && <Row label="Venue id">{order.venue_order_id}</Row>}
          <Row label="Client id">
            <span className="text-[10px] text-t2">{order.client_order_id}</span>
          </Row>
        </div>

        {/* bracket legs (OCO) */}
        {legs.length > 0 && (
          <div className="mt-4">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-t3">Bracket · OCO</div>
            <div className="divide-y divide-ds-line/60 rounded-lg border border-ds-line">
              {legs.map((leg) => (
                <div key={leg.client_order_id} className="flex items-center justify-between px-3 py-1.5">
                  <span className="font-mono text-[11px] text-t2">
                    {leg.order_type.replace(/_/g, " ")} {leg.price ?? leg.trigger_price ?? ""}
                  </span>
                  <StatusBadge status={leg.status} />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* actions — cancel only while working; a terminal order has nothing to cancel */}
      {working && (
        <div className="border-t border-ds-line px-4 py-2.5">
          <button
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
            className="w-full rounded-md bg-status-bear/15 py-2 font-mono text-xs font-bold text-status-bear ring-1 ring-status-bear/30 disabled:opacity-40"
          >
            {cancel.isPending ? "cancelling…" : cancel.isError ? "retry cancel" : "Cancel order"}
          </button>
        </div>
      )}
    </div>
  );
}
