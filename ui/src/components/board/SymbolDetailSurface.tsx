"use client";

/**
 * SymbolDetailSurface (#27) — the detail view for the focused symbol. Rendered by `Board` in place of the
 * tile grid (grid unmounted) when `detailOpen`. Shows the symbol's Ichimoku chart + last price + signal,
 * with a close affordance back to the current view.
 *
 * Mounted ONLY while open (Board gates on `detailOpen && focusedInstrument`), so its `bars` subscription
 * tears down on close and swaps cleanly when the focused symbol changes — no per-searched-symbol leak.
 * Focus itself persists (closeDetail keeps `focusedInstrument`); re-selecting the same symbol reopens.
 */
import { useEffect, useState } from "react";
import { ChartTile } from "@/components/tiles/ChartTile";
import { StrategyOrderForm } from "@/tiles/order/StrategyOrderForm";
import { requestStream } from "@/lib/api/client";
import { useInstrument } from "@/lib/framework/instrument";
import { useCockpitStore, type FocusedInstrument } from "@/lib/framework/store";
import { lastLevels, recommend, TONE_PILL } from "@/lib/ichimoku";
import { CloseButton } from "@/components/ds/CloseButton";
import { DetailIdentity } from "@/components/board/detail/DetailIdentity";

export function SymbolDetailSurface({ instrument }: { instrument: FocusedInstrument }) {
  const closeDetail = useCockpitStore((s) => s.closeDetail);
  const [ordering, setOrdering] = useState(false); // order affordance (#69/#71) — renders the shared order.vanilla ticket body

  // One framework primitive: bars (chart) + live mark price + status. No hand-wired subscriptions.
  const { bars, price, status } = useInstrument(instrument.instrument_id);
  const hasData = bars.length > 0;
  const levels = lastLevels(bars);

  // Ask the engine to stream this symbol on demand — so viewing ANY searched symbol (not just watchlist
  // ones) gets bars + live price, instead of "Loading" forever.
  useEffect(() => {
    requestStream(instrument.instrument_id);
  }, [instrument.instrument_id]);
  const rec = recommend(levels, 0); // no position here → signal from cloud position alone

  // Esc closes the surface — but not while the user is typing in the header search input.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null;
      const typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");
      if (e.key === "Escape" && !typing) closeDetail();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [closeDetail]);

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col overflow-hidden rounded-xl border border-ds-line bg-ds-bg">
      {/* Header: identity + signal + price + close */}
      <div className="flex items-center justify-between gap-3 border-b border-ds-line px-4 py-2.5">
        <DetailIdentity symbol={instrument.symbol} name={instrument.name} venue={instrument.venue} />
        <div className="flex items-center gap-3">
          <span className={`whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[10px] font-semibold ${TONE_PILL[rec.tone]}`}>
            {rec.status}
          </span>
          <span className="font-mono text-base font-bold text-t1">
            {price != null ? `$${price.toFixed(2)}` : "—"}
          </span>
          <button
            type="button"
            onClick={() => setOrdering(true)}
            className="rounded bg-status-bull px-3 py-1 font-mono text-[11px] font-bold text-ds-bg transition-colors hover:bg-status-bull/90"
          >
            BUY / SELL
          </button>
          <CloseButton onClick={closeDetail} label="Close detail" />
        </div>
      </div>
      {/* Body: the order ticket when ordering, else the chart (or an explicit loading / no-feed state — never
          a blank chart). The ticket is the SHARED order.vanilla body, MANUAL-pinned to this symbol. */}
      <div className="min-h-0 flex-1">
        {ordering ? (
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-b border-ds-line px-4 py-2">
              <span className="font-mono text-[11px] text-t2">
                Order ticket · MANUAL · paper — places a real paper order only when the engine is armed
              </span>
              <CloseButton onClick={() => setOrdering(false)} label="Close order ticket" size="sm" />
            </div>
            <div className="min-h-0 flex-1">
              {/* Keyed by instrument → the ticket resets cleanly when the focused symbol changes. */}
              <StrategyOrderForm key={instrument.instrument_id} instrumentId={instrument.instrument_id} />
            </div>
          </div>
        ) : hasData ? (
          <ChartTile instrumentId={instrument.instrument_id} fill />
        ) : (
          <div className="flex h-full items-center justify-center px-4 text-center">
            <p className="font-mono text-sm text-t3">
              {status === "error"
                ? `No live data for ${instrument.symbol}`
                : `Loading ${instrument.symbol}…`}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
