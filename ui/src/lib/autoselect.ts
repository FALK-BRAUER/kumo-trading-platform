/**
 * Auto-select order type (#43 Phase 3) — a PURE rules engine that picks market / limit / stop from spread,
 * volatility, entry intent, and time-of-day, per the Perplexity decision tree. The ticket only has three
 * order types, so "marketable-limit" and "resting-limit" both map to `limit` (prefill sets the price);
 * `stop` = stop-market entry. IEX spreads run wide, so wide/unknown spread → resting `limit` (never a
 * blind market cross). SIP would tighten the thresholds; the shape stays the same.
 */
import type { AutoSelectConfig } from "@/config/orderTicket";

export type EntryStyle = "take" | "pullback" | "breakout";
export type SessionPhase = "extended" | "open" | "mid" | "close" | "closed";
export type PickedType = "market" | "limit" | "stop";

/** US-equity session phase for a moment in time (ET). Regular 09:30–16:00; extended 04:00–09:30 & 16:00–20:00. */
export function sessionPhase(now: Date, cfg: AutoSelectConfig): SessionPhase {
  // Minutes since ET midnight, via the en-US/New_York wall clock (DST-correct).
  const et = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(now);
  const hh = Number(et.find((p) => p.type === "hour")?.value ?? "0");
  const mm = Number(et.find((p) => p.type === "minute")?.value ?? "0");
  const t = hh * 60 + mm;
  const OPEN = 9 * 60 + 30; // 09:30
  const CLOSE = 16 * 60; // 16:00
  if (t < 4 * 60 || t >= 20 * 60) return "closed";
  if (t < OPEN || t >= CLOSE) return "extended";
  if (t < OPEN + cfg.openPhaseMins) return "open";
  if (t >= CLOSE - cfg.closePhaseMins) return "close";
  return "mid";
}

export interface AutoSelectInputs {
  entryStyle: EntryStyle;
  /** spread / price. Null when no quote. */
  spreadPct: number | null;
  /** ATR / price (volatility ratio). Null when insufficient bars. */
  volRel: number | null;
  phase: SessionPhase;
  cfg: AutoSelectConfig;
}

export interface AutoSelectResult {
  orderType: PickedType;
  tif: "day" | "gtc";
  reason: string;
}

export function autoSelectOrderType(inp: AutoSelectInputs): AutoSelectResult {
  const { entryStyle, spreadPct, volRel, phase, cfg } = inp;
  // Crossed (< 0), missing, or wide spread is all "don't trust it" → never a blind market/stop-market.
  const wideOrUnknown = spreadPct == null || spreadPct < 0 || spreadPct >= cfg.spreadWide;
  const tight = spreadPct != null && spreadPct >= 0 && spreadPct <= cfg.spreadTight;

  // Extended / closed: never a market order; rest a limit.
  if (phase === "extended" || phase === "closed") {
    return { orderType: "limit", tif: "day", reason: `${phase} hours → resting limit` };
  }

  // Breakout intent → stop-entry at the level. Our `stop` is STOP_MARKET, so a wide/unknown book would
  // trigger an unbounded market fill — refuse it there and rest a limit instead (stop-limit is a follow-up).
  if (entryStyle === "breakout") {
    if (wideOrUnknown) {
      return { orderType: "limit", tif: "day", reason: "breakout, wide/unknown spread → resting limit" };
    }
    const hot = volRel != null && volRel >= cfg.volHigh;
    return { orderType: "stop", tif: "day", reason: hot ? "breakout, fast tape → stop-market" : "breakout → stop entry" };
  }

  // Pullback intent → rest a limit at structure.
  if (entryStyle === "pullback") {
    return { orderType: "limit", tif: "day", reason: "pullback → rest limit at structure" };
  }

  // Liquidity-take (just get in):
  if (wideOrUnknown) {
    return { orderType: "limit", tif: "day", reason: "wide/unknown spread → resting limit" };
  }
  if (phase === "open" || phase === "close") {
    // volatile auction windows: don't market unless the spread is genuinely tight
    return tight
      ? { orderType: "market", tif: "day", reason: `${phase}, tight spread → market` }
      : { orderType: "limit", tif: "day", reason: `${phase} window → marketable limit` };
  }
  return tight
    ? { orderType: "market", tif: "day", reason: "liquid, tight spread → market" }
    : { orderType: "limit", tif: "day", reason: "normal spread → marketable limit" };
}
