/**
 * Order-ticket config types + helpers (#43/#49).
 *
 * The MANUAL ticket's live sizing/prefill params come from the `autofill` SETTINGS domain (editable in the
 * Settings UI, #49) — see `AutofillSettings` / `profileFromAutofill` below; `AUTOFILL_DEFAULTS` is only the
 * pre-load fallback (must mirror config/settings/autofill.schema.json).
 *
 * The `PROFILES` (scalp/intraday/swing) below are NOT manual presets — they're the fully-automated trading
 * MODES, which get their own settings later. Kept here as their reference config.
 */

export type RiskMode = "fixed_usd" | "fraction_of_equity";
export type StopModel = "ichimoku" | "atr" | "percent";
export type TargetModel = "r_multiple" | "percent";
export type EntrySource = "last" | "mid" | "kijun"; // marketable-mid gated by spread sanity (IEX is wide)

export interface SizingParams {
  riskMode: RiskMode;
  /** Risk budget when riskMode = fixed_usd. */
  riskUsd: number;
  /** Risk budget as a fraction of equity when riskMode = fraction_of_equity (0.005 = 0.5%). */
  riskFraction: number;
  /** Cap a single position at this fraction of equity (notional). */
  maxPositionFraction: number;
  minShares: number;
  maxShares: number;
}

export interface StopParams {
  model: StopModel;
  atrLookback: number;
  atrMult: number;
  /** Percent stop distance (0.02 = 2%). */
  percentStop: number;
  /** Ichimoku structure reference for a structure stop. */
  ichimokuRef: "kijun" | "cloud";
}

export interface TargetParams {
  model: TargetModel;
  /** Take-profit as a multiple of risk (entry→stop distance). */
  targetR: number;
  /** Percent target distance (0.04 = 4%). */
  percentTarget: number;
}

export interface EntryParams {
  source: EntrySource;
  /** Ticks to offset the entry from its source (marketable). */
  offsetTicks: number;
  /** Spread-sanity gate: refuse `mid` entry when spread/price exceeds this (IEX quotes are wide). */
  maxSpreadPct: number;
}

export interface OrderProfile {
  label: string;
  entry: EntryParams;
  stop: StopParams;
  target: TargetParams;
  sizing: SizingParams;
  defaultOrderType: "market" | "limit";
  defaultTif: "day" | "gtc";
}

const SIZING_BASE: SizingParams = {
  riskMode: "fraction_of_equity",
  riskUsd: 500,
  riskFraction: 0.005, // 0.5% of equity
  maxPositionFraction: 0.1, // 10% of equity per name
  minShares: 1,
  maxShares: 10_000,
};

export const PROFILES: Record<string, OrderProfile> = {
  scalp: {
    label: "Scalp",
    entry: { source: "last", offsetTicks: 1, maxSpreadPct: 0.003 },
    stop: { model: "atr", atrLookback: 14, atrMult: 0.8, percentStop: 0.005, ichimokuRef: "kijun" },
    target: { model: "r_multiple", targetR: 1.5, percentTarget: 0.01 },
    sizing: { ...SIZING_BASE, riskFraction: 0.002 },
    defaultOrderType: "limit",
    defaultTif: "day",
  },
  intraday: {
    label: "Intraday",
    entry: { source: "last", offsetTicks: 1, maxSpreadPct: 0.005 },
    stop: { model: "ichimoku", atrLookback: 14, atrMult: 1.0, percentStop: 0.01, ichimokuRef: "kijun" },
    target: { model: "r_multiple", targetR: 2.0, percentTarget: 0.02 },
    sizing: { ...SIZING_BASE },
    defaultOrderType: "limit",
    defaultTif: "day",
  },
  swing: {
    label: "Swing",
    entry: { source: "last", offsetTicks: 2, maxSpreadPct: 0.01 },
    stop: { model: "ichimoku", atrLookback: 14, atrMult: 1.5, percentStop: 0.03, ichimokuRef: "cloud" },
    target: { model: "r_multiple", targetR: 3.0, percentTarget: 0.06 },
    sizing: { ...SIZING_BASE, riskFraction: 0.01 },
    defaultOrderType: "limit",
    defaultTif: "gtc",
  },
};

export const DEFAULT_PROFILE = "intraday";

// --- Manual-ticket autofill (#49) -----------------------------------------------------------------
// The MANUAL order ticket uses ONE config, sourced from the `autofill` settings domain (editable in the
// Settings UI). The scalp/intraday/swing PROFILES above are for the fully-automated trading modes (which
// get their own settings later) — not manual-ticket presets.

export interface AutofillSettings {
  riskMode: RiskMode;
  riskFraction: number;
  riskUsd: number;
  maxPositionFraction: number;
  atrLookback: number;
  atrMult: number;
  percentStop: number;
  targetR: number;
  entryOffsetTicks: number;
  maxSpreadPct: number;
  minShares: number;
  maxShares: number;
}

/** Fallback defaults (mirror config/settings/autofill.schema.json) used until the settings load / if a
 *  field is absent — so the ticket always has a complete, valid config. */
export const AUTOFILL_DEFAULTS: AutofillSettings = {
  riskMode: "fraction_of_equity",
  riskFraction: 0.005,
  riskUsd: 500,
  maxPositionFraction: 0.1,
  atrLookback: 14,
  atrMult: 1.0,
  percentStop: 0.01,
  targetR: 2.0,
  entryOffsetTicks: 1,
  maxSpreadPct: 0.005,
  minShares: 1,
  maxShares: 10_000,
};

/** Build the effective manual-ticket profile from the autofill settings + the per-order stop source.
 *  Entry source is `last` (manual); the stop-source toggle picks the stop model. */
export function profileFromAutofill(s: AutofillSettings, stopModel: StopModel): OrderProfile {
  return {
    label: "manual",
    entry: { source: "last", offsetTicks: s.entryOffsetTicks, maxSpreadPct: s.maxSpreadPct },
    stop: { model: stopModel, atrLookback: s.atrLookback, atrMult: s.atrMult, percentStop: s.percentStop, ichimokuRef: "kijun" },
    target: { model: "r_multiple", targetR: s.targetR, percentTarget: 0.04 },
    sizing: {
      riskMode: s.riskMode,
      riskUsd: s.riskUsd,
      riskFraction: s.riskFraction,
      maxPositionFraction: s.maxPositionFraction,
      minShares: s.minShares,
      maxShares: s.maxShares,
    },
    defaultOrderType: "limit",
    defaultTif: "day",
  };
}

/** US-equity default tick. Per-symbol precision is a follow-up (sub-$1 = 4dp). */
export const TICK_SIZE = 0.01;

/** Auto-select order type (#43 Phase 3) — thresholds for the market/limit/stop decision. Spread inputs
 *  come from the quote plane; with the IEX feed spreads run wide, so the rules treat wide/unknown spread
 *  as "use a resting limit" (safe) rather than firing a market order. Real thresholds need SIP. */
export interface AutoSelectConfig {
  spreadTight: number; // <= this (spread/price) → marketable / market ok
  spreadWide: number; // >= this → force resting limit (don't cross a wide book)
  volHigh: number; // ATR/price above this = high volatility (breakout → stop-market, not stop-limit)
  extendedSpreadMax: number; // refuse an extended-hours order if spread exceeds this
  openPhaseMins: number; // minutes after 09:30 ET treated as the volatile open
  closePhaseMins: number; // minutes before 16:00 ET treated as the close
}

export const AUTO_SELECT: AutoSelectConfig = {
  spreadTight: 0.0005, // 5 bps
  spreadWide: 0.0025, // 25 bps
  volHigh: 0.03, // 3% ATR
  extendedSpreadMax: 0.01, // 1%
  openPhaseMins: 15,
  closePhaseMins: 15,
};
