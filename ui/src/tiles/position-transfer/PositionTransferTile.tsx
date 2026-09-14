"use client";

/**
 * Position transfer tile (#80 spin-off) — move a position, or part of one, into a strategy.
 *
 * A real tile, not a bespoke panel: it declares its data source and config in `definition.ts`, receives data
 * through `TileProps`, and can be placed on a board or composed into a detail surface (which is how the
 * unclaimed-position detail uses it, the same way the symbol detail composes ChartTile + StrategyOrderForm).
 *
 * What it does NOT do: place an order. A transfer moves the position between internal books — the broker net
 * is identical before and after; only the owning strategy changes. The copy says so, because a confirm
 * button next to a position that submits nothing to the venue is exactly the thing a trader must not have to
 * guess about.
 *
 * Targets come from the strategy catalog (`@/config/strategies`), so a strategy going live appears here with
 * no change to this file. Pricing mode is tile config, since MARKET and CARRY_OVER mean genuinely different
 * things for strategy P&L and that is a deployment decision, not a per-click one.
 */

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { transferPosition } from "@/lib/api/client";
import { magnitude } from "@/lib/framework/signedQty";
import { useTransferTargets } from "@/lib/framework/useTransferTargets";
import type { PricingMode } from "@/config/transfers";
import { useCommandStatus } from "@/lib/framework/useCommandStatus";
import type { TileProps } from "@/lib/framework/types";
import type { PositionTransferConfig } from "./definition";

export interface TransferSource {
  instrument_id: string;
  strategy_id: string;
  side: string;
  quantity: number;
}

interface ExternalRow extends TransferSource {
  source: string;
}

/** Pure view — mockable in /dev/ui, where there is usually nothing unattributed to browse. */
export function PositionTransferView({
  source,
  targets,
  pricingMode,
  onTransfer,
  pending,
  error,
}: {
  source: TransferSource;
  targets: string[];
  pricingMode: PricingMode;
  onTransfer?: (qty: number, target: string) => void;
  pending?: boolean;
  error?: string | null;
}) {
  // THE MAGNITUDE, EVERYWHERE ON THIS TILE (#855). A broker row signs its quantity, and every use here
  // read the raw value: the field opened on "-10", the gate `qtyNum > 0 && qtyNum <= source.quantity`
  // closed the valid interval from BOTH ends so no number could satisfy it, and the tile rendered
  // "quantity must be between 1 and -10" with the button dead. An unclaimed short could not be adopted
  // by any strategy, and the refusal read like ordinary validation rather than like a tile that could
  // never accept anything.
  const size = magnitude(source.quantity);
  const [qty, setQty] = useState(String(size));
  const [target, setTarget] = useState(targets[0] ?? "");

  const symbol = source.instrument_id.split(".")[0];
  const qtyNum = Number(qty);
  const qtyOk = Number.isFinite(qtyNum) && qtyNum > 0 && qtyNum <= size;
  const remainder = qtyOk ? size - qtyNum : 0;

  return (
    <div className="rounded-lg border border-ds-line p-3">
      <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-t2">
        Move to a strategy
      </h3>
      <p className="mt-1 font-mono text-[10px] leading-relaxed text-t3">
        Hands this position to a strategy so it can be managed like any other. <strong>No order is
        placed</strong> — the broker net is identical before and after; only the owning book changes.
        {pricingMode === "CARRY_OVER"
          ? " The cost basis moves with the shares, so nothing is realized."
          : " Booked at the current mark, so the source realizes its P&L up to now and the target starts clean."}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] uppercase text-t3">qty</span>
          <input
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            inputMode="decimal"
            className="w-24 rounded border border-ds-line bg-transparent px-2 py-1 text-right font-mono text-[12px] text-t1"
          />
        </label>
        <span className="font-mono text-[10px] text-t3">of {size} →</span>
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          className="rounded border border-ds-line bg-transparent px-2 py-1 font-mono text-[11px] text-t1"
        >
          {targets.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {qtyOk && remainder > 0 && (
        <p className="mt-2 font-mono text-[10px] text-t3">{remainder} will stay with {source.strategy_id}.</p>
      )}
      {!qtyOk && (
        <p className="mt-2 font-mono text-[10px] text-status-bear">
          quantity must be between 1 and {size}
        </p>
      )}
      {error && <p className="mt-2 font-mono text-[10px] text-status-bear">{error}</p>}

      <button
        disabled={!qtyOk || pending || !target}
        onClick={() => onTransfer?.(qtyNum, target)}
        className="mt-3 rounded-md border border-ds-line bg-ds-surf2 px-3 py-1.5 font-mono text-[11px] text-t1 disabled:opacity-40"
      >
        {pending ? "moving…" : `MOVE ${qtyOk ? qtyNum : ""} ${symbol} TO ${target}`}
      </button>
    </div>
  );
}

export function PositionTransferTile({ config, data }: TileProps<PositionTransferConfig>) {
  const qc = useQueryClient();
  const [commandId, setCommandId] = useState<string | null>(null);
  // A POST only ENQUEUES; the engine's real answer arrives on the ack (#39). Reporting success on the enqueue
  // silently swallowed rejections — a refused transfer just closed the screen and looked like nothing
  // happened. Nothing is claimed here until the engine has actually accepted it.
  const ack = useCommandStatus(commandId);
  const external = (data.external_activity as { external?: ExternalRow[] } | undefined)?.external ?? [];

  // Which position this instance acts on: pinned by config, else the first unattributed one. Pinning is what
  // lets a detail surface point the tile at the position the user opened.
  const source =
    external.find(
      (r) =>
        r.source === "POSITION" &&
        (!config.instrumentId || r.instrument_id === config.instrumentId) &&
        (!config.sourceStrategyId || r.strategy_id === config.sourceStrategyId) &&
        (!config.side || r.side === config.side),
    ) ?? null;

  const transfer = useMutation({
    mutationFn: (v: { qty: number; target: string }) =>
      transferPosition({
        instrument_id: source!.instrument_id,
        target_strategy_id: v.target,
        quantity: v.qty,
        side: source!.side,
        source_strategy_id: source!.strategy_id,
        pricing_mode: config.pricingMode,
        reason_code: "manual_transfer",
      }),
    onSuccess: (res) => setCommandId(res.command_id),
  });

  useEffect(() => {
    if (ack.state !== "accepted") return;
    // The position genuinely moves, so every plane changes server-side — refetch, never patch locally.
    qc.invalidateQueries({ queryKey: ["external-activity"] });
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["trades"] });
    config.onDone?.();
  }, [ack.state, qc, config]);

  // FROM THE LIVE REGISTRY (#783). The old constant returned ["MANUAL-001"] on every instance and
  // could never return anything else, so a real unclaimed holding could not be given to the lane
  // that should run it.
  const registry = useTransferTargets();

  if (!source) {
    return <p className="font-mono text-[10px] text-t3">nothing here to move.</p>;
  }

  if (registry.state === "unavailable") {
    return (
      <p className="font-mono text-[10px] text-danger">
        cannot move: {registry.reason}
      </p>
    );
  }

  return (
    <PositionTransferView
      source={source}
      targets={registry.targets}
      pricingMode={config.pricingMode}
      // `pending` only counts once a command is in flight — the hook reports "pending" with no
      // command id too, which would disable the button before anything had been submitted.
      pending={transfer.isPending || (commandId !== null && ack.state === "pending")}
      error={
        transfer.isError
          ? String(transfer.error)
          : ack.state === "rejected"
            ? (ack.error ?? "the engine rejected this transfer")
            : ack.state === "unknown"
              ? "no answer from the engine — check the position before retrying"
              : null
      }
      onTransfer={(qty, target) => {
        setCommandId(null); // a retry must wait on its OWN ack, not the previous one
        transfer.mutate({ qty, target });
      }}
    />
  );
}
