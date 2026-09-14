"use client";

/**
 * PortfolioTile (#114) — the positions LIST as the dense mock table (matches Watchlist/Orders). Each position
 * is a symbol-first MAIN row (symbol · signal · side+qty · last · unrealized P&L) + an always-visible full-width
 * SUB line (strategy · avg cost · realized · cloud). Replaces the old MasterCard card + P&L ring. Row tap →
 * the position detail surface; P&L tints the number (pnlToneClass). The header labels the main row only.
 *
 * One `PositionRow` per position so each gets its own `useInstrument` (live price + Ichimoku) — the
 * rules-of-hooks-safe pattern for a dynamic list. Render-only, no polling.
 */
import type { PositionDTO, PositionsResponse } from "@/lib/api/types";
import { lastLevels, recommend, TONE_PILL } from "@/lib/ichimoku";
import { pnlToneClass } from "@/components/ds/pnl";
import { strategyLabel } from "@/lib/framework/position";
import { signedQty } from "@/lib/framework/signedQty";
import { TileState } from "@/components/board/TileState";
import { computePnl, useInstrument } from "@/lib/framework/instrument";
import { useCockpitStore } from "@/lib/framework/store";
import type { TileProps } from "@/lib/framework/types";
import type { PortfolioConfig } from "./definition";

function PositionRow({ position: p }: { position: PositionDTO }) {
  const { bars, price } = useInstrument(p.instrument_id);
  const openDetail = useCockpitStore((s) => s.openDetail);
  // Work in flight on THIS position (#269 follow-up). The operator pressed FLATTEN, the exit took 11 seconds
  // cancelling a resting stop and waiting for the venue to release the shares, and this row — the thing
  // he was actually looking at — did not change at all: "the stock looked unchanged."
  const busy = useCockpitStore((s) => s.busyCommands[`${p.strategy_id}:${p.instrument_id}`]);
  const long = p.side === "LONG";
  // ONE derivation of direction-and-size for the side badge and the quantity beside it (#855), so the
  // two cannot disagree, and so the column shows a share COUNT rather than a signed number — the side
  // already has its own cell right next to it.
  const signed = signedQty(p.side, p.quantity);
  const { pct, amt } = computePnl(p, price);
  const levels = lastLevels(bars);
  // THE SIDE GOES IN (#855). `recommend` is LONG-only by construction and was never told which side it
  // was reading, so a winning short (+$1,080, +56.8% on a rendered row) was labelled EXIT — close the
  // position that is working. It now refuses for a SHORT rather than guessing or mirroring.
  const rec = recommend(levels, pct, p.side);
  const sigText = TONE_PILL[rec.tone].split(" ")[1] ?? "text-t2";
  const symbol = p.instrument_id.split(".")[0];

  const cloud =
    price == null || levels?.cloudTop == null || levels.cloudBot == null
      ? null
      : price > levels.cloudTop
        ? "above cloud"
        : price > levels.cloudBot
          ? "in cloud"
          : "below cloud";
  const sub = [`avg ${p.avg_px_open.toFixed(2)}`, `real ${p.realized_pnl}`, cloud].filter(Boolean).join(" · ");

  const open = () =>
    openDetail({
      kind: "position",
      positionKey: `${p.strategy_id}:${p.instrument_id}`,
      instrumentId: p.instrument_id,
      context: { tab: "portfolio", strategy_id: p.strategy_id },
    });

  return (
    <>
      <tr
        onClick={open}
        aria-busy={busy ? true : undefined}
        className={`cursor-pointer hover:bg-ds-surf2/40 ${busy ? "bg-sky-950/40" : ""}`}
      >
        <td className="sticky left-0 z-10 bg-ds-surf px-2 pb-0.5 pt-2 font-mono text-[13px] font-bold text-t1">
          {symbol}
        </td>
        <td className="px-2 pb-0.5 pt-2">
          {/* The in-flight verb REPLACES the signal rather than sitting beside it. A row reading
              "TRIM · PROCESSING" invites acting on a recommendation that a destructive command is
              already superseding. */}
          {busy ? (
            <span className="animate-pulse font-mono text-[10px] font-semibold uppercase text-sky-300">
              {busy.verb}…
            </span>
          ) : (
            // The refusal carries its reason in the cell's title, the way an unavailable control on this
            // board always says why rather than simply going quiet.
            <span title={rec.reason} className={`font-mono text-[10px] font-semibold ${sigText}`}>{rec.status}</span>
          )}
        </td>
        <td className="whitespace-nowrap px-2 pb-0.5 pt-2 font-mono text-[12px]">
          {/* THREE STATES, from the one derivation (#855). `long ? "LONG" : "SHORT"` rendered a FLAT
              row — a documented value of this field — as SHORT. */}
          <span className={signed > 0 ? "text-status-bull" : signed < 0 ? "text-status-bear" : "text-t2"}>
            {signed > 0 ? "LONG" : signed < 0 ? "SHORT" : "FLAT"}
          </span>{" "}
          <span className="text-t1">{Math.abs(signed)}</span>
        </td>
        <td className="px-2 pb-0.5 pt-2 text-right font-mono text-[13px] text-t1">
          {price != null ? price.toFixed(2) : "—"}
        </td>
        <td className={`whitespace-nowrap px-2 pb-0.5 pt-2 text-right font-mono text-[12px] ${pnlToneClass(amt)}`}>
          {amt != null
            ? `${amt >= 0 ? "+" : "-"}$${Math.abs(Math.round(amt)).toLocaleString()} ${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`
            : "—"}
        </td>
      </tr>
      <tr onClick={open} className="cursor-pointer border-b border-ds-line">
        <td colSpan={5} className="px-2 pb-2 pt-0.5 font-mono text-[10px] text-t3">
          <span className="text-t2">{strategyLabel(p.strategy_id)}</span> · {sub}
        </td>
      </tr>
    </>
  );
}

export function PortfolioTile({ data, status: srcStatus }: TileProps<PortfolioConfig>) {
  const positions = (data.positions as PositionsResponse | undefined)?.positions ?? [];

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Portfolio</h2>
        <span className="font-mono text-xs text-t3">{positions.length} open</span>
      </div>

      <TileState status={srcStatus.positions} isEmpty={positions.length === 0} emptyLabel="No open positions">
        <div className="overflow-x-auto rounded-xl border border-ds-line bg-ds-surf">
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-ds-surf2/60 text-left font-mono text-[9px] uppercase tracking-wider text-t3">
                <th className="sticky left-0 z-10 bg-ds-surf2 px-2 py-1.5">Symbol</th>
                <th className="px-2 py-1.5">Sig</th>
                <th className="px-2 py-1.5">Qty</th>
                <th className="px-2 py-1.5 text-right">Last</th>
                <th className="px-2 py-1.5 text-right">U-P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={`${p.strategy_id}:${p.instrument_id}`} position={p} />
              ))}
            </tbody>
          </table>
        </div>
      </TileState>
    </div>
  );
}
