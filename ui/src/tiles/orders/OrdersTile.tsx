"use client";

/**
 * OrdersTile (#33) — the working-order blotter, IBKR Orders-tab style. Reads the shared `orders`
 * DataSource (WS, re-pushed each tick): working orders first, then recent terminal. Each row shows
 * side/type/qty(filled)/limit/stop/TIF/status, with Cancel + Modify wired to the command layer
 * (cancel_order / modify_order). Render-only; the engine still gates every command on KUMO_ORDERS_ARMED.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation } from "@tanstack/react-query";
import { cancelOrder, modifyOrder } from "@/lib/api/client";
import { humanizeOrderReason } from "@/lib/order/reason";
import { useCommandStatus } from "@/lib/framework/useCommandStatus";
import { useCockpitStore } from "@/lib/framework/store";
import { strategyLabel } from "@/lib/framework/position";
import { TileState } from "@/components/board/TileState";
import { StatusBadge } from "@/components/ds/StatusBadge";
import type { TileProps } from "@/lib/framework/types";
import type { OrderDTO, OrdersResponse } from "@/lib/api/types";
import type { OrdersConfig } from "./definition";
import { CloseButton } from "@/components/ds/CloseButton";

const TERMINAL = new Set(["FILLED", "CANCELED", "REJECTED", "EXPIRED", "DENIED"]);

const fmt = (n: number | null | undefined) => (n == null ? "—" : `$${n.toFixed(2)}`);
const sym = (id: string) => id.split(".")[0];

function ModifyModal({ order, onClose }: { order: OrderDTO; onClose: () => void }) {
  const [qty, setQty] = useState(String(order.quantity));
  const [price, setPrice] = useState(order.price != null ? String(order.price) : "");
  const [trigger, setTrigger] = useState(order.trigger_price != null ? String(order.trigger_price) : "");
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(closeTimer.current), []);

  // Send ONLY fields that actually changed and are valid (>0). A no-op modify is blocked (button disabled).
  const qN = parseFloat(qty);
  const pN = parseFloat(price);
  const tN = parseFloat(trigger);
  const changed = {
    quantity: qN > 0 && qN !== order.quantity ? qN : undefined,
    price: order.price != null && pN > 0 && pN !== order.price ? pN : undefined,
    trigger_price: order.trigger_price != null && tN > 0 && tN !== order.trigger_price ? tN : undefined,
  };
  const hasChange = changed.quantity != null || changed.price != null || changed.trigger_price != null;

  // #39: enqueue → poll the engine ack; close only on `accepted` (not on enqueue). The `orders` WS re-pushes
  // each tick, so the blotter also reflects the change authoritatively.
  const [commandId, setCommandId] = useState<string | null>(null);
  const mut = useMutation({
    mutationFn: () => modifyOrder(order.client_order_id, changed),
    onSuccess: (data) => setCommandId(data.command_id),
  });
  const { state: cmdState, error: cmdError } = useCommandStatus(commandId);
  useEffect(() => {
    if (cmdState !== "accepted") return;
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(onClose, 700);
    return () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, [cmdState, onClose]);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-xs rounded-xl border border-ds-line2 bg-ds-surf p-4 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-sm font-bold text-t1">
            Modify {sym(order.instrument_id)} · {order.side}
          </span>
          <CloseButton onClick={onClose} />
        </div>
        <div className="mb-3 grid grid-cols-1 gap-2">
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] uppercase text-t3">Quantity</span>
            <input type="number" step="1" value={qty} onChange={(e) => setQty(e.target.value)}
              className="w-full rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-info sm:text-xs" />
          </label>
          {order.price != null && (
            <label className="flex flex-col gap-0.5">
              <span className="text-[9px] uppercase text-t3">Limit price</span>
              <input type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)}
                className="w-full rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-info sm:text-xs" />
            </label>
          )}
          {order.trigger_price != null && (
            <label className="flex flex-col gap-0.5">
              <span className="text-[9px] uppercase text-t3">Stop trigger</span>
              <input type="number" step="0.01" value={trigger} onChange={(e) => setTrigger(e.target.value)}
                className="w-full rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-watch sm:text-xs" />
            </label>
          )}
        </div>
        <button onClick={() => mut.mutate()} disabled={!hasChange || mut.isPending || mut.isSuccess}
          className="w-full rounded bg-status-info py-1.5 font-mono text-xs font-bold text-ds-bg transition-colors hover:bg-status-info/90 disabled:cursor-not-allowed disabled:opacity-40">
          {mut.isPending
            ? "Sending…"
            : cmdState === "accepted"
              ? "Modified →"
              : cmdState === "rejected"
                ? "Rejected ✕"
                : cmdState === "unknown"
                  ? "Enqueued — check blotter"
                  : mut.isSuccess
                    ? "Awaiting engine…"
                    : hasChange
                      ? "Modify order"
                      : "No change"}
        </button>
        {mut.isError && <div className="mt-2 font-mono text-[10px] text-status-bear">Failed — {String(mut.error)}</div>}
        {cmdState === "rejected" && (
          <div className="mt-2 font-mono text-[10px] text-status-bear">Engine rejected{cmdError ? ` — ${cmdError}` : ""}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/**
 * The Modify/Cancel pair, rendered twice per row — in its own column from `sm:` up, on the full-width
 * sub-line below that (#258). One component rather than two copies so the placements cannot drift in
 * behaviour; only the alignment differs, and `stopPropagation` stays with the buttons either way (both
 * host rows open the detail pane on click).
 */
function RowActions({
  onModify,
  onCancel,
  disabled,
  title,
  errored,
  label,
  align,
}: {
  onModify: () => void;
  onCancel: () => void;
  disabled: boolean;
  title?: string;
  errored: boolean;
  label: string;
  align: "start" | "end";
}) {
  return (
    <div className={`flex gap-1 ${align === "end" ? "justify-end" : "justify-start"}`}>
      <button
        onClick={(e) => { e.stopPropagation(); onModify(); }}
        className="rounded bg-ds-surf2 px-2 py-0.5 font-mono text-[10px] text-t2 hover:text-t1"
      >
        Modify
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); onCancel(); }}
        disabled={disabled}
        title={title}
        className={`rounded px-2 py-0.5 font-mono text-[10px] text-status-bear ring-1 ring-status-bear/30 hover:bg-status-bear/15 disabled:cursor-not-allowed disabled:opacity-40 ${
          errored ? "bg-status-bear/20 ring-status-bear/60" : "bg-status-bear/10"
        }`}
      >
        {label}
      </button>
    </div>
  );
}

function OrderRow({ order, onModify }: { order: OrderDTO; onModify: (coid: string) => void }) {
  const working = !TERMINAL.has(order.status);
  // The `orders` WS source re-pushes each tick → the row flips to CANCELED once the engine actually cancels.
  // #39: don't claim ✓ on mere enqueue — poll the ack. A disarmed engine REJECTS (row never flips) → show ✕.
  const [cancelCmd, setCancelCmd] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: () => cancelOrder(order.client_order_id),
    onSuccess: (d) => setCancelCmd(d.command_id),
  });
  const { state: cancelState } = useCommandStatus(cancelCmd);
  const openDetail = useCockpitStore((s) => s.openDetail);

  const inBracket = order.tags?.some((t) => t.startsWith("bracket:"));
  const open = () =>
    openDetail({
      kind: "order",
      clientOrderId: order.client_order_id,
      context: { tab: "orders", strategy_id: order.strategy_id },
    });

  // Sub-line (mock: symbol-first main row + full-width detail line): strategy · type/price · fills · reason.
  // A TRAILING stop carries neither a limit nor (until the venue sets one) a trigger price, so a
  // price-only rule labelled it "MKT" — and a resting "MKT · GTC" reads as a market order left working
  // overnight, which is alarming and wrong. The TYPE decides the label; the price only decorates it.
  const trailing = order.order_type.startsWith("TRAILING_STOP");
  const priceStr = trailing
    ? `TRAIL${order.trigger_price != null ? ` ${fmt(order.trigger_price)}` : ""}`
    : order.price != null && order.trigger_price != null
      ? `STPLMT ${fmt(order.trigger_price)}/${fmt(order.price)}`
      : order.price != null
        ? `LMT ${fmt(order.price)}`
        : order.trigger_price != null
          ? `STP ${fmt(order.trigger_price)}`
          : "MKT";
  const tif = order.time_in_force === "AT_THE_OPEN" ? "OPG" : order.time_in_force === "AT_THE_CLOSE" ? "CLS" : order.time_in_force;
  const sub = [
    priceStr,
    order.filled_qty > 0 ? `${order.filled_qty}/${order.quantity} filled${order.avg_px != null ? ` @ ${order.avg_px.toFixed(2)}` : ""}` : null,
    tif,
    order.tags?.includes("extended_hours") ? "EXT" : null,
    inBracket ? "⛓ bracket" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  // Built once, spread into both placements — see RowActions.
  const actions = {
    onModify: () => onModify(order.client_order_id),
    onCancel: () => cancel.mutate(),
    disabled: cancel.isPending || (cancel.isSuccess && cancelState !== "rejected" && cancelState !== "unknown"),
    title:
      cancelState === "rejected"
        ? "Engine rejected the cancel"
        : cancel.isError
          ? `Cancel failed: ${String(cancel.error)}`
          : undefined,
    errored: cancel.isError || cancelState === "rejected",
    label: cancel.isPending
      ? "…"
      : cancelState === "accepted"
        ? "✓"
        : cancelState === "rejected"
          ? "✕"
          : cancelState === "unknown"
            ? "?"
            : cancel.isSuccess
              ? "…"
              : cancel.isError
                ? "retry"
                : "Cancel",
  };

  return (
    <>
      <tr
        onClick={open}
        className={`cursor-pointer hover:bg-ds-surf2/40 ${working ? "" : "opacity-60"} ${inBracket ? "border-l-2 border-l-status-watch/60" : ""}`}
      >
        {/* ONE frozen column, and only from `sm:` up (#258).
            STATUS used to be frozen too, at a hardcoded `left-16`. That offset asserts the symbol column
            measures exactly 4rem, which it never did: `w-16` is a hint, not a constraint, under
            `table-layout: auto`. The opaque STATUS cell therefore scrolled over the symbol's tail and
            SIDE vanished underneath it. `left-0` is the only offset that assumes nothing about the width
            of what sits beside it, so it is the only one worth freezing — and below `sm:` the pair
            claimed ~170px of a ~390px viewport, which is why neither is frozen there. */}
        <td className="bg-ds-surf px-2 pb-0.5 pt-2 font-mono text-[13px] font-bold text-t1 sm:sticky sm:left-0 sm:z-10">
          {sym(order.instrument_id)}
        </td>
        <td className="whitespace-nowrap px-2 pb-0.5 pt-2">
          <StatusBadge status={order.status} />
        </td>
        <td className="whitespace-nowrap px-2 pb-0.5 pt-2 font-mono text-[12px]">
          <span className={order.side === "BUY" ? "text-status-bull" : "text-status-bear"}>{order.side}</span>{" "}
          <span className="text-t1">{order.quantity}</span>
        </td>
        <td className="hidden px-2 pb-0.5 pt-2 font-mono text-[11px] text-t2 sm:table-cell">
          {order.order_type.replace("_", " ")}
        </td>
        {/* The Modify/Cancel pair is ~130px — on a phone that is a third of the viewport and it is what
            pushed the table past the screen. Below `sm:` the buttons ride the full-width sub-line
            instead, where there is room; the column itself only exists from `sm:` up. (#258) */}
        <td className="hidden px-2 pb-0.5 pt-2 text-right sm:table-cell">
          {working && <RowActions {...actions} align="end" />}
        </td>
      </tr>
      <tr
        onClick={open}
        className={`cursor-pointer border-b border-ds-line ${working ? "" : "opacity-60"} ${inBracket ? "border-l-2 border-l-status-watch/60" : ""}`}
      >
        <td colSpan={5} className="px-2 pb-2 pt-0.5 font-mono text-[10px] text-t3">
          <span className="text-t2">{strategyLabel(order.strategy_id)}</span> · {sub}
          {humanizeOrderReason(order.reason) && (
            <span className="text-status-bear"> · {humanizeOrderReason(order.reason)}</span>
          )}
          {working && (
            <div className="mt-1.5 sm:hidden">
              <RowActions {...actions} align="start" />
            </div>
          )}
        </td>
      </tr>
    </>
  );
}

/** The bracket group id a leg belongs to (from its `bracket:<id>` tag), or null. */
function bracketGroup(o: OrderDTO): string | null {
  return o.tags?.find((t) => t.startsWith("bracket:")) ?? null;
}

/** Cluster bracket legs together (entry+stop+target adjacent) while preserving first-appearance order. */
function clusterBrackets(orders: OrderDTO[]): OrderDTO[] {
  const rows: OrderDTO[] = [];
  const pushed = new Set<string>();
  for (const o of orders) {
    if (pushed.has(o.client_order_id)) continue;
    const g = bracketGroup(o);
    const members = g ? orders.filter((x) => bracketGroup(x) === g) : [o];
    for (const m of members) {
      if (!pushed.has(m.client_order_id)) {
        rows.push(m);
        pushed.add(m.client_order_id);
      }
    }
  }
  return rows;
}

export function OrdersTile({ data, status: srcStatus }: TileProps<OrdersConfig>) {
  const [modifyCoid, setModifyCoid] = useState<string | null>(null);
  // Hide synthetic RECONCILIATION orders — they're internal net placeholders Nautilus makes to match a
  // reconciled position, not real broker orders. The blotter mirrors the broker's actual order history.
  const orders = ((data.orders as OrdersResponse | undefined)?.orders ?? []).filter(
    (o) => !o.tags?.includes("RECONCILIATION"),
  );
  const working = orders.filter((o) => !TERMINAL.has(o.status)).length;
  const rows = clusterBrackets(orders);
  // Look the modify target up LIVE — if it went terminal or vanished from the stream, the modal closes
  // itself (no stale snapshot can be modified).
  const modifyTarget = modifyCoid
    ? (orders.find((o) => o.client_order_id === modifyCoid && !TERMINAL.has(o.status)) ?? null)
    : null;

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Orders</h2>
        <span className="font-mono text-xs text-t3">{working} working · {orders.length} total</span>
      </div>

      <TileState status={srcStatus.orders} isEmpty={orders.length === 0} emptyLabel="No orders">
        <div className="overflow-x-auto rounded-xl border border-ds-line bg-ds-surf">
          {/* Dense mock layout: symbol-first main row (Sym·Status·Side·Type·actions) + full-width sub-line
              (strategy · price · fills · reason). Symbol pins on the frozen left; Status sits next to it so a
              terminal state reads without scrolling; the rest of the detail rides the sub-line. */}
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-ds-surf2/60 text-left font-mono text-[9px] uppercase tracking-wider text-t3">
                {/* Header sticky state must match the body cells exactly (#258) — a frozen header over a
                    scrolling body, or the reverse, misaligns the moment the container scrolls. */}
                <th className="bg-ds-surf2 px-2 py-1.5 sm:sticky sm:left-0 sm:z-10">Symbol</th>
                <th className="px-2 py-1.5">Status</th>
                <th className="px-2 py-1.5">Side</th>
                <th className="hidden px-2 py-1.5 sm:table-cell">Type</th>
                <th className="hidden px-2 py-1.5 sm:table-cell" />
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <OrderRow key={o.client_order_id} order={o} onModify={setModifyCoid} />
              ))}
            </tbody>
          </table>
        </div>
      </TileState>

      {modifyTarget && <ModifyModal key={modifyTarget.client_order_id} order={modifyTarget} onClose={() => setModifyCoid(null)} />}
    </div>
  );
}
