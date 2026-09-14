"use client";

/**
 * Unclaimed position detail (#80 spin-off) — the screen for a broker position no strategy owns yet.
 *
 * Its own detail variant rather than a row affordance: assigning a position to a strategy is a decision, not
 * a click. It wants the numbers in front of you (what it cost, what it's worth, what the P&L already is,
 * where it came from) and it wants room to say plainly what the operation does and does not do.
 *
 * What it does NOT do: place an order. A transfer moves the position between internal books; the broker net
 * is identical before and after. The screen says so, because a confirm button next to a position that
 * submits nothing to the venue is exactly the thing a trader must not have to guess about.
 *
 * Partial moves are first-class — the remainder stays unattributed rather than disappearing.
 */

import { useSource } from "@/lib/framework/datasource/useSource";
import { useCockpitStore } from "@/lib/framework/store";
import { DetailIdentity, venueOf } from "./DetailIdentity";
import { pnlToneClass } from "@/components/ds/pnl";
import { PositionTransferTile } from "@/tiles/position-transfer/PositionTransferTile";
import { DEFAULT_PRICING_MODE } from "@/config/transfers";
import { signedQty } from "@/lib/framework/signedQty";
import type { DetailProps } from "@/lib/framework/detail/registry";

interface ExternalRow {
  instrument_id: string;
  source: string;
  strategy_id: string;
  side: string;
  quantity: number;
  origin: string;
  avg_px?: number | null;
  last_px?: number | null;
  market_value?: number | null;
  unrealized_pl?: number | null;
  unrealized_plpc?: number | null;
}

const ORIGIN_COPY: Record<string, string> = {
  RECONCILIATION: "reconciled from the broker — the cockpit has no record of the order that opened it",
  VENUE: "placed directly at the broker, outside the cockpit",
  FOREIGN: "belongs to a strategy this cockpit doesn't run",
  UNKNOWN: "origin unknown",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="font-mono text-[10px] uppercase tracking-wide text-t3">{label}</span>
      <span className="font-mono text-[12px] text-t1">{children}</span>
    </div>
  );
}

const usd = (n: number | null | undefined) =>
  n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function UnclaimedPositionDetailView({ row, onClose }: { row: ExternalRow; onClose?: () => void }) {
  const symbol = row.instrument_id.split(".")[0];
  const signed = signedQty(row.side, row.quantity);

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col overflow-hidden rounded-xl border border-ds-line bg-ds-surf">
      <div className="flex items-center justify-between border-b border-ds-line px-4 py-2.5">
        <DetailIdentity symbol={symbol} venue={venueOf(row.instrument_id)}>
          <span className="rounded bg-ds-surf2 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-t2">
            UNCLAIMED
          </span>
          {/* Colour and quantity from ONE derivation (#855), so the badge cannot disagree with the
              number below it, and the number is a share COUNT — the direction is the badge's job. */}
          <span className={`font-mono text-[10px] ${signed < 0 ? "text-status-bear" : "text-status-bull"}`}>
            {row.side}
          </span>
        </DetailIdentity>
        {onClose && (
          <button onClick={onClose} className="rounded-md bg-ds-surf2 px-2 py-1 font-mono text-[10px] text-t2">
            close
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        <div className="divide-y divide-ds-line">
          <Row label="quantity">{Math.abs(signed)}</Row>
          <Row label="cost basis">{row.avg_px != null ? `$${row.avg_px.toFixed(2)}` : "—"}</Row>
          <Row label="last">{row.last_px != null ? `$${row.last_px.toFixed(2)}` : "—"}</Row>
          <Row label="market value">{usd(row.market_value)}</Row>
          <Row label="unrealized">
            <span className={pnlToneClass(row.unrealized_pl ?? 0)}>
              {usd(row.unrealized_pl)}
              {row.unrealized_plpc != null && ` (${row.unrealized_plpc > 0 ? "+" : ""}${(row.unrealized_plpc * 100).toFixed(2)}%)`}
            </span>
          </Row>
          <Row label="owned by">{row.strategy_id}</Row>
        </div>

        <p className="mt-2 font-mono text-[10px] leading-relaxed text-t3">
          {ORIGIN_COPY[row.origin] ?? ORIGIN_COPY.UNKNOWN}
        </p>

        {/* The move itself is the position-transfer TILE, composed in — same pattern as the symbol
            detail composing ChartTile + StrategyOrderForm. The tile owns targets, pricing mode and the
            command; this surface only supplies the position facts and the pin. */}
        <div className="mt-5">
          <PositionTransferTile
            instanceId={`transfer:${row.instrument_id}:${row.strategy_id}:${row.side}`}
            config={{
              instrumentId: row.instrument_id,
              sourceStrategyId: row.strategy_id,
              side: row.side,
              pricingMode: DEFAULT_PRICING_MODE,
              onDone: onClose,
            }}
            data={{ external_activity: { external: [row] } }}
            status={{ external_activity: "live" }}
            onConfigChange={() => {}}
          />
        </div>
      </div>
    </div>
  );
}

export function UnclaimedPositionDetail({ focus }: DetailProps) {
  const closeDetail = useCockpitStore((s) => s.closeDetail);
  const external = useSource("external_activity", {}).data as { external?: ExternalRow[] } | undefined;

  if (focus.kind !== "unclaimed") return null;

  // Re-read live by identity rather than trusting a snapshot handed over by the row, so a position that
  // stops being unclaimed while this is open says so instead of offering a move that would fail.
  const row = (external?.external ?? []).find(
    (r) =>
      r.source === "POSITION" &&
      r.instrument_id === focus.instrumentId &&
      r.strategy_id === focus.sourceStrategyId &&
      r.side === focus.side,
  );

  if (!row) {
    return (
      <div className="flex h-[calc(100vh-9rem)] flex-col items-center justify-center rounded-xl border border-ds-line bg-ds-surf text-t2">
        <p className="font-mono text-sm">this position is no longer unclaimed</p>
        <button onClick={closeDetail} className="mt-3 rounded-md bg-ds-surf2 px-3 py-1.5 font-mono text-xs text-t2">
          close
        </button>
      </div>
    );
  }

  return <UnclaimedPositionDetailView row={row} onClose={closeDetail} />;
}
