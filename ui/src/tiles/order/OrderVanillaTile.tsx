"use client";

/**
 * OrderVanillaTile (#66) — the plain broker order ticket as a placeable TILE: side · market/limit/stop ·
 * qty · price/trigger · TIF · optional bracket (protective stop + target). No prefill, no AUTO, no sizing
 * mechanisms — "vanilla" = you type it. It shares the order-core (buildOrderPayload/buildBracketPayload) +
 * the #39 ack flow with every other order surface.
 *
 * Long-lived (a tile persists), so the form is KEYED by instrument_id → it resets cleanly when the focused
 * symbol changes, instead of carrying a stale ticket. No onClose/portal — a tile has nothing to close.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { submitBracket, submitOrder, type BracketPayload, type OrderPayload } from "@/lib/api/client";
import { SlideToConfirm } from "@/components/ds/SlideToConfirm";
import { marketOrderWouldBeCanceled } from "@/lib/market";
import {
  bracketGeoOk,
  buildBracketPayload,
  buildOrderPayload,
  protectionExpiresForTif,
  stopTriggerOk,
  type Action,
  type OrderType,
} from "@/lib/order/payload";
import { useCommandStatus } from "@/lib/framework/useCommandStatus";
import { useInstrument } from "@/lib/framework/instrument";
import { useCockpitStore } from "@/lib/framework/store";
import type { TileProps } from "@/lib/framework/types";
import type { OrderVanillaConfig } from "./definition";

const TIFS = ["day", "gtc", "opg", "cls"] as const;

// Exported so on-demand surfaces (the symbol detail order affordance, #71) render the same ticket body as the
// placed tile — one order-entry component, no legacy modal fork.
export function VanillaForm({ instrumentId, strategyId }: { instrumentId: string; strategyId: string }) {
  const [action, setAction] = useState<Action>("BUY");
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [tif, setTif] = useState<string>("day");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState(""); // limit price OR stop trigger
  const [bracket, setBracket] = useState(false);
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");

  const { price: livePrice } = useInstrument(instrumentId);

  const qtyNum = parseInt(qty) || 0;
  const priceNum = parseFloat(price) || 0;
  const stopNum = parseFloat(stop) > 0 ? parseFloat(stop) : null;
  const targetNum = parseFloat(target) > 0 ? parseFloat(target) : null;
  const priceRequired = orderType !== "market";
  const priceOk = !priceRequired || priceNum > 0;
  // Geometry: a stop trigger must be on the protective side; a bracket needs stop/entry/target ordered
  // correctly. Entry ref = the limit price (limit entry) else the live price. Same safety as the modal.
  const entryRef = orderType === "limit" && priceNum > 0 ? priceNum : (livePrice ?? 0);
  const triggerOk = stopTriggerOk(action, orderType, priceNum, livePrice ?? 0);
  const bracketOk = !bracket || (stopNum != null && targetNum != null && bracketGeoOk(action, entryRef, stopNum, targetNum));
  // Block a market order (incl. market-entry bracket) while the market is closed — the broker cancels it in
  // seconds (it can't rest). Steer to limit/stop (#113). EXEMPT market-on-open/close (opg/cls), whose whole
  // point is to be placed while closed and rest to the auction. Limit/stop entries are never blocked.
  const marketBlocked = marketOrderWouldBeCanceled(orderType, Date.now()) && tif !== "opg" && tif !== "cls";
  const canSubmit = qtyNum > 0 && priceOk && triggerOk && bracketOk && !marketBlocked;
  // Why is submit disabled? Surface it so a greyed button is never a mystery. `marketBlocked` has its own
  // banner (explains the broker cancel); this covers the silent input-validation cases.
  const blockReason = !marketBlocked
    ? qtyNum <= 0
      ? "Enter a quantity."
      : !priceOk
        ? orderType === "stop" ? "Enter a stop trigger price." : "Enter a limit price."
        : !triggerOk
          ? `Stop trigger must be ${action === "BUY" ? "above" : "below"} the current price.`
          : !bracketOk
            ? "Set a valid protective stop and target."
            : null
    : null;

  const [commandId, setCommandId] = useState<string | null>(null);
  const order = useMutation({
    mutationFn: (v: { kind: "order"; p: OrderPayload } | { kind: "bracket"; p: BracketPayload }) =>
      v.kind === "bracket" ? submitBracket(v.p) : submitOrder(v.p),
    onSuccess: (data) => setCommandId(data.command_id),
  });
  const { state: cmdState, error: cmdError } = useCommandStatus(commandId);

  function submit() {
    if (!canSubmit) return;
    if (bracket && stopNum != null && targetNum != null) {
      order.mutate({
        kind: "bracket",
        // bracket entry is market|limit only (stop entry is coerced off when BRACKET is enabled)
        p: buildBracketPayload({
          instrumentId, action, orderType: orderType === "limit" ? "limit" : "market",
          priceNum, sharesNum: qtyNum, stopNum, targetNum, tif,
        }),
      });
      return;
    }
    order.mutate({
      kind: "order",
      p: buildOrderPayload({ instrumentId, action, orderType, extended: false, priceNum, tif, sharesNum: qtyNum }),
    });
  }

  // A tile has no close/reopen (unlike the modal), so a terminal ack must offer an explicit reset — else the
  // ticket is stuck disabled after accepted/rejected/unknown until the symbol changes.
  const terminal = cmdState === "accepted" || cmdState === "rejected" || cmdState === "unknown";
  function newOrder() {
    order.reset();
    setCommandId(null);
  }
  const enqueued = order.isSuccess;
  // 16px on mobile stops iOS auto-zoom-on-focus; 12px on desktop.
  const field = "w-full rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-info sm:text-xs";
  const seg = (on: boolean) =>
    `px-2 py-0.5 rounded font-mono text-[11px] transition-colors ${on ? "bg-status-info/20 text-t1 ring-1 ring-status-info/40" : "bg-ds-surf2 text-t2 hover:bg-ds-line2"}`;

  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-3">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-sm font-bold text-t1">{instrumentId.split(".")[0]}</span>
        <span className="font-mono text-[11px] text-t2">
          Vanilla · {strategyId}
          {livePrice != null ? ` · $${livePrice.toFixed(2)}` : ""}
        </span>
      </div>

      <div className="flex gap-1.5">
        {(["BUY", "SELL"] as Action[]).map((a) => (
          <button key={a} onClick={() => setAction(a)}
            className={`px-2 py-0.5 rounded font-mono text-[11px] font-bold transition-colors ${
              action === a ? (a === "BUY" ? "bg-status-bull/15 text-status-bull ring-1 ring-status-bull/40" : "bg-status-bear/15 text-status-bear ring-1 ring-status-bear/40") : "bg-ds-surf2 text-t2 hover:bg-ds-surf2"
            }`}>{a}</button>
        ))}
        <div className="mx-1 w-px bg-ds-surf2" />
        {(["market", "limit", "stop"] as OrderType[]).map((t) => (
          <button key={t} onClick={() => setOrderType(t)} disabled={bracket && t === "stop"} className={seg(orderType === t)}>
            {t.toUpperCase()}
          </button>
        ))}
      </div>

      <label className="flex flex-col gap-0.5">
        <span className="text-[9px] uppercase text-t2">Quantity</span>
        <input type="number" step="1" value={qty} onChange={(e) => setQty(e.target.value)} className={field} />
      </label>
      {priceRequired && (
        <label className="flex flex-col gap-0.5">
          <span className="text-[9px] uppercase text-t2">{orderType === "stop" ? "Stop trigger" : "Limit price"}</span>
          <input type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} className={field} />
        </label>
      )}

      <div className="flex items-center gap-1.5">
        <span className="text-[9px] uppercase text-t2">TIF</span>
        {TIFS.map((t) => (
          <button key={t} onClick={() => setTif(t)} className={seg(tif === t)}>{t.toUpperCase()}</button>
        ))}
        <button onClick={() => { setBracket((b) => !b); if (!bracket && orderType === "stop") setOrderType("market"); }}
          className={`ml-auto ${seg(bracket)}`}>BRACKET</button>
      </div>
      {bracket && (
        <div className="grid grid-cols-2 gap-2">
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] uppercase text-t2">Protective stop</span>
            <input type="number" step="0.01" value={stop} onChange={(e) => setStop(e.target.value)} className={field} />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] uppercase text-t2">Target</span>
            <input type="number" step="0.01" value={target} onChange={(e) => setTarget(e.target.value)} className={field} />
          </label>
        </div>
      )}

      {marketBlocked && !terminal && (
        <div className="rounded border border-status-warn/40 bg-status-warn/10 px-2 py-1.5 font-mono text-[10px] text-status-warn">
          Market closed — a market order will be canceled by the broker. Use a <b>Limit</b> or <b>Stop</b> to rest until open.
        </div>
      )}

      {terminal ? (
        <button onClick={newOrder}
          className="mt-auto w-full rounded bg-ds-surf2 py-1.5 font-mono text-xs font-bold text-t1 transition-colors">
          New order →
        </button>
      ) : (
        // Committing a live order slides — a tap must never place it (#108). blockReason surfaces in-track.
        <SlideToConfirm
          className="mt-auto"
          label={`${action}${bracket ? " bracket" : ""} ${qtyNum || ""} ${instrumentId.split(".")[0]}`.trim()}
          intent={action === "SELL" ? "sell" : "buy"}
          disabled={!canSubmit}
          pending={order.isPending || enqueued}
          reason={enqueued ? "Awaiting engine…" : blockReason}
          onConfirm={submit}
        />
      )}
      {/* This tile lets the operator PICK the time-in-force, which is right — but nothing said that the
          choice governs the protective leg too (#315). Alpaca gives both bracket legs the parent's TIF and
          offers no per-leg field, so a DAY bracket's stop expires at 16:00 and the position is naked until
          the #239 backstop's first regular-hours tick. Choice kept; the cost is now stated. */}
      {bracket && protectionExpiresForTif(tif) && (
        <div className="font-mono text-[10px] leading-snug text-status-watch">
          Protective stop expires at the close — both bracket legs take the entry&apos;s time-in-force.
          Choose GTC to keep it resting.
        </div>
      )}
      {cmdState === "accepted" && <div className="font-mono text-[10px] text-status-bull">Placed ✓</div>}
      {cmdState === "rejected" && <div className="font-mono text-[10px] text-status-bear">Rejected{cmdError ? ` — ${cmdError}` : ""}</div>}
      {cmdState === "unknown" && <div className="font-mono text-[10px] text-status-watch">No engine ack — check the Orders tab.</div>}
      {order.isError && <div className="font-mono text-[10px] text-status-bear">Failed — {String(order.error)}</div>}
    </div>
  );
}

export function OrderVanillaTile({ config }: TileProps<OrderVanillaConfig>) {
  const focused = useCockpitStore((s) => s.focusedInstrument);
  const instrumentId = config.instrument_id ?? focused?.instrument_id ?? null;
  const strategyId = config.strategy_id ?? "MANUAL";

  if (!instrumentId) {
    return (
      <div className="flex h-full items-center justify-center p-4 font-mono text-[11px] text-t3">
        No symbol — search or select one to order.
      </div>
    );
  }
  // Key by (instrument, strategy) so the form resets cleanly when either changes (no stale ticket).
  return <VanillaForm key={`${instrumentId}:${strategyId}`} instrumentId={instrumentId} strategyId={strategyId} />;
}
