/**
 * Ichimoku levels + a simple recommendation, computed from the WS bar history.
 *
 * Pure (no chart dependency) so both the chart tile and the Portfolio rows can read the same levels.
 * The recommendation is a lightweight read off price-vs-cloud/Kijun/Tenkan — NOT the full risk/agent
 * engine (stops, trail, give-back, ratings), which arrives with lanes (#8) and cross-lane risk (#12).
 */
import type { BarDTO } from "@/lib/api/types";
import { sideSign } from "@/lib/framework/signedQty";

export interface Levels {
  price: number;
  tenkan: number | null;
  kijun: number | null;
  spanA: number | null;
  spanB: number | null;
  cloudTop: number | null;
  cloudBot: number | null;
  ma200: number | null;
}

/** Midpoint of highest-high / lowest-low over the last `period` bars. */
export function midpoint(bars: BarDTO[], period: number): number | null {
  if (bars.length < period) return null;
  const w = bars.slice(bars.length - period);
  return (Math.max(...w.map((b) => b.high)) + Math.min(...w.map((b) => b.low))) / 2;
}

function smaLast(bars: BarDTO[], period: number): number | null {
  if (bars.length < period) return null;
  const w = bars.slice(bars.length - period);
  return w.reduce((s, b) => s + b.close, 0) / period;
}

export function lastLevels(bars: BarDTO[]): Levels | null {
  if (bars.length === 0) return null;
  const tenkan = midpoint(bars, 9);
  const kijun = midpoint(bars, 26);
  const spanB = midpoint(bars, 52);
  const spanA = tenkan != null && kijun != null ? (tenkan + kijun) / 2 : null;
  const cloudTop = spanA != null && spanB != null ? Math.max(spanA, spanB) : null;
  const cloudBot = spanA != null && spanB != null ? Math.min(spanA, spanB) : null;
  return {
    price: bars[bars.length - 1].close,
    tenkan,
    kijun,
    spanA,
    spanB,
    cloudTop,
    cloudBot,
    ma200: smaLast(bars, 200),
  };
}

/** Standard Ichimoku Chikou Span comparison: current close vs close `n` bars ago (26 = Chikou's own
 *  displacement). Null if there isn't enough history yet — never guesses. #181. */
export function chikouAbovePrice(bars: BarDTO[], n = 26): boolean | null {
  if (bars.length <= n) return null;
  return bars[bars.length - 1].close > bars[bars.length - 1 - n].close;
}

export interface ShiftedCloud {
  cloudTop: number | null;
  cloudBot: number | null;
  green: boolean | null;
}

/** The cloud AS PLOTTED for the CURRENT bar (#181) — standard Ichimoku Span A/B are computed from data
 *  `displacement` bars ago, then projected forward onto today; today's cloud boundary was set 26 bars
 *  back, not by today's fresh Tenkan/Kijun/52-period data (which instead describes the FUTURE cloud).
 *  predecessor-repo's scanner shifts explicitly (`.shift(26)`, `scanner/ichimoku.py:15-16`); `lastLevels()`
 *  above deliberately does NOT (#179/#180's single-cloud chips read the unshifted/future cloud, by
 *  original design, unchanged here). This is a SEPARATE helper for #181's Blue Flag checklist, which
 *  needs the scanner-exact (shifted) reading — the two will occasionally disagree with the existing cloud
 *  chips on the same row; that's expected, not a bug (code review). */
export function shiftedCloud(bars: BarDTO[], displacement = 26): ShiftedCloud {
  const asOfIndex = bars.length - 1 - displacement;
  if (asOfIndex < 51) return { cloudTop: null, cloudBot: null, green: null }; // need 52 bars ending there
  const asOf = bars.slice(0, asOfIndex + 1);
  const tenkan = midpoint(asOf, 9);
  const kijun = midpoint(asOf, 26);
  const spanB = midpoint(asOf, 52);
  if (tenkan == null || kijun == null || spanB == null) return { cloudTop: null, cloudBot: null, green: null };
  const spanA = (tenkan + kijun) / 2;
  return { cloudTop: Math.max(spanA, spanB), cloudBot: Math.min(spanA, spanB), green: spanA > spanB };
}

export type CloudPosition = "above" | "in" | "below";

/** Where price sits relative to the cloud — the ONE comparison, so a row's main cloud line and any
 *  per-granularity chip can never disagree at the boundary (code review, #179: they previously used
 *  different operators at cloudBot, so price === cloudBot read "below" in one place and "in" in another). */
export function cloudPosition(price: number | null, levels: Levels | null): CloudPosition | null {
  if (price == null || levels?.cloudTop == null || levels.cloudBot == null) return null;
  if (price > levels.cloudTop) return "above";
  if (price < levels.cloudBot) return "below";
  return "in";
}

export type Tone = "pos" | "neg" | "warn" | "neutral";

export interface Recommendation {
  //: EXIT | WATCH | TRAIL | ADD | HOLD, or `REFUSED_SHORT` when the caller named a SHORT side.
  status: string;
  tone: Tone;
  reason: string;
}

/**
 * The label a SHORT row shows instead of a recommendation (#855).
 *
 * Exported as a constant and returned as the STATUS, rather than mapped to a label in the tile,
 * because a second mapping would be a second derivation of one fact — the thing this ticket exists to
 * remove. The wording of `reason` matches the backend's own refusal for peak_watch.
 */
export const REFUSED_SHORT = "REFUSED · short";

/**
 * The label a row that holds NOTHING shows (#855). `PositionDTO.side` is a three-state field —
 * `LONG | SHORT | FLAT` (`backend/api/models.py:109`) — and a FLAT row has no position to recommend
 * on. It used to receive the long-only reading and render its badge as "SHORT", because the tile
 * asked `long ? "LONG" : "SHORT"`: a two-state read of a three-state field.
 */
export const REFUSED_FLAT = "REFUSED · flat";

/**
 * Derive a status/recommendation from Ichimoku position + unrealized P&L.
 *
 * LONG-ONLY BY CONSTRUCTION, AND IT NOW SAYS SO. Every branch below reads as a long: below the cloud
 * is "trend broken", above it is "thesis intact", above Tenkan with a gain is "add on the pullback".
 * For a SHORT those readings are inverted in meaning, and the most expensive one is EXIT — a short
 * below the cloud is the thesis WORKING, and the cell told the operator to close the position that
 * was winning, on the day it started winning. Measured on a rendered row: a short up $1,080 (+56.8%)
 * labelled EXIT.
 *
 * IT REFUSES; IT DOES NOT MIRROR. Mirroring would assert that all five branches invert cleanly for a
 * short, which is a trading claim nobody here has made and which Ichimoku does not obviously support.
 * A refusal is an honest three-state — a long reading, an explicit "not answered for this side", and
 * never a blank cell that reads as a dead signal plane.
 *
 * `side` is OPTIONAL so the two callers with no position (`WatchlistTile`, `SymbolDetailSurface`, both
 * passing `pnlPct` 0) are unchanged and can never see a refusal.
 */
export function recommend(levels: Levels | null, pnlPct: number, side?: string): Recommendation {
  // THROUGH THE PREDICATE, not through a sixth hand-written reading of the side. This landed as
  // `side === "SHORT"` in the very commit that banned the other five copies, and the expression scan
  // could not see it because this file contains no `.quantity`. `sideSign` is also the one place to
  // change if a third side ever arrives.
  if (side != null && sideSign(side) !== 1) {
    return sideSign(side) < 0
      ? { status: REFUSED_SHORT, tone: "neutral", reason: "signal math isn't mirrored for SHORT yet" }
      : { status: REFUSED_FLAT, tone: "neutral", reason: "this row holds nothing to read a signal on" };
  }
  if (!levels) return { status: "HOLD", tone: "neutral", reason: "awaiting price data" };
  const { price, tenkan, kijun, cloudTop, cloudBot } = levels;

  if (cloudBot != null && price < cloudBot)
    return { status: "EXIT", tone: "neg", reason: "below the cloud — trend broken" };
  if (kijun != null && price < kijun)
    return { status: "WATCH", tone: "warn", reason: "below Kijun — momentum fading" };
  if (pnlPct >= 20 && cloudTop != null && price > cloudTop)
    return { status: "TRAIL", tone: "pos", reason: `+${pnlPct.toFixed(0)}% above cloud — raise stop, lock gains` };
  if (pnlPct >= 5 && tenkan != null && price > tenkan)
    return { status: "ADD", tone: "pos", reason: "strong above Tenkan — add on Kijun pullback" };
  if (cloudTop != null && price > cloudTop)
    return { status: "HOLD", tone: "pos", reason: "above the cloud — thesis intact" };
  return { status: "HOLD", tone: "neutral", reason: "inside the cloud — wait for direction" };
}

// Semantic tokens (day/night aware). Format kept as `bg-… text-…` (two tokens) — some callers
// `.split(" ")[1]` to reuse just the text colour for the signal circle.
export const TONE_PILL: Record<Tone, string> = {
  pos: "bg-status-bull/15 text-status-bull",
  neg: "bg-status-bear/15 text-status-bear",
  warn: "bg-status-watch/15 text-status-watch",
  neutral: "bg-ds-surf2 text-t2",
};
