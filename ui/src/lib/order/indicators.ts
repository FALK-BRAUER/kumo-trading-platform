/**
 * Pure price/level indicators shared by the prefill orchestration (`prefill.ts`) and the mechanism catalog
 * (`mechanisms.ts`). Extracted so the two can depend on these without importing each other (no cycle).
 * No React, no side effects.
 */
import type { BarDTO } from "@/lib/api/types";
import { TICK_SIZE } from "@/config/orderTicket";

/** Average True Range over the last `n` bars. Null until there are n+1 bars. */
export function atr(bars: BarDTO[], n = 14): number | null {
  if (bars.length < n + 1) return null;
  let sum = 0;
  for (let i = bars.length - n; i < bars.length; i++) {
    const h = bars[i].high;
    const l = bars[i].low;
    const pc = bars[i - 1].close;
    sum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
  }
  return sum / n;
}

/** Spread as a fraction of mid, or null when no usable quote. One definition so prefill + auto-select agree. */
export function spreadPct(quote: { mid: number; spread: number } | null | undefined): number | null {
  return quote && quote.mid > 0 ? quote.spread / quote.mid : null;
}

/** A breakout trigger level: the recent swing high (BUY) / low (SELL) over `lookback` bars, so an AUTO
 *  breakout stop actually waits for a break of structure instead of firing one tick off the last price. */
export function breakoutLevel(bars: BarDTO[], action: "BUY" | "SELL", lookback = 10): number | null {
  if (bars.length === 0) return null;
  const recent = bars.slice(-lookback);
  return action === "BUY" ? Math.max(...recent.map((b) => b.high)) : Math.min(...recent.map((b) => b.low));
}

export const roundTick = (p: number, mode: "nearest" | "floor" | "ceil" = "nearest"): number => {
  const q = p / TICK_SIZE;
  const r = mode === "floor" ? Math.floor(q) : mode === "ceil" ? Math.ceil(q) : Math.round(q);
  return Math.round(r * TICK_SIZE * 100) / 100;
};
