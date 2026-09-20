/**
 * Order-ticket prefill (#43 Phase 1; refactored to the mechanism catalog #65) — PURE functions that turn
 * market data + a profile into suggested entry / stop / target / shares. No React, no side effects →
 * unit-testable. The ticket calls this on open and whenever the stop source or profile changes; every field
 * stays hand-editable afterwards.
 *
 * Level DERIVATION now lives in the pluggable mechanism catalog (`lib/order/mechanisms.ts`). This module is
 * the thin ORCHESTRATION: it maps a profile's entry/stop/target choice to catalog mechanisms, owns tick
 * rounding + protective-side validation + the cross-mechanism fallback chain (order policy, not per-mechanism
 * behavior), and keeps the one capped sizing path. Behavior is byte-identical to the pre-#65 monolith — the
 * existing `prefill.test.ts` is the regression guard.
 *
 * Ichimoku-led (the operator's steer): structure stops come from the levels we already compute. ATR + percent are
 * the alternates. Sizing risks a % of REAL equity (#41) and never exceeds affordability (buying_power).
 */
import type { BarDTO } from "@/lib/api/types";
import type { EntrySource, OrderProfile, SizingParams, StopModel } from "@/config/orderTicket";
import { TICK_SIZE } from "@/config/orderTicket";
import { lastLevels } from "@/lib/ichimoku";
import { atr, breakoutLevel, roundTick, spreadPct } from "@/lib/order/indicators";
import { getMechanism, type MechanismCtx } from "@/lib/order/mechanisms";

// Re-exported for back-compat: callers (and tests) still import these from "@/lib/prefill".
export { atr, breakoutLevel, roundTick, spreadPct };

export interface PrefillInputs {
  action: "BUY" | "SELL";
  profile: OrderProfile;
  /** Ticket-level override of the profile's stop model (the "stop source" toggle). */
  stopModel?: StopModel;
  /** Ticket-level override of the entry mechanism (the strategy-first tile's Entry seg, #51). Undefined →
   *  the profile's `entry.source`, keeping the legacy path byte-identical. */
  entryMechanism?: { id: string; params?: Record<string, unknown> };
  bars: BarDTO[];
  last: number | null;
  mid: number | null;
  spreadPct: number | null;
  equity: number | null;
  buyingPower: number | null;
}

export interface PrefillResult {
  entry: number | null;
  stop: number | null;
  target: number | null;
  shares: number;
  riskUsd: number | null;
  notes: string[];
}

export interface SizeResult {
  shares: number;
  riskUsd: number | null;
  notes: string[];
}

/** Risk-based share count, capped by max-position % + affordability, clamped to min/max. The ONE sizing
 *  path — the ticket's manual re-size calls this too, so an edited stop can never bypass the caps. Risk is
 *  SIGNED by side: a stop on the wrong side (BUY stop >= entry, SELL stop <= entry) yields riskPerShare <= 0
 *  → 0 shares (never size a long off a nonsensical stop). Buying-power constrains BUYs only — selling shares
 *  you already hold doesn't consume buying power. */
export function sizePosition(
  entry: number,
  stop: number,
  action: "BUY" | "SELL",
  sizing: SizingParams,
  equity: number | null,
  buyingPower: number | null,
): SizeResult {
  const notes: string[] = [];
  const riskPerShare = action === "BUY" ? entry - stop : stop - entry;
  const riskUsd = sizing.riskMode === "fixed_usd" ? sizing.riskUsd : equity != null ? sizing.riskFraction * equity : null;
  if (riskUsd == null || riskPerShare <= 0 || entry <= 0) return { shares: 0, riskUsd, notes }; // wrong-side/zero stop
  let raw = riskUsd / riskPerShare;
  if (equity != null) {
    const maxByPos = (sizing.maxPositionFraction * equity) / entry;
    if (raw > maxByPos) {
      raw = maxByPos;
      notes.push("capped by max position %");
    }
  }
  if (action === "BUY" && buyingPower != null) {
    const maxByBp = buyingPower / entry;
    if (raw > maxByBp) {
      raw = maxByBp;
      notes.push("capped by buying power");
    }
  }
  let shares = Math.max(0, Math.min(sizing.maxShares, Math.floor(raw)));
  // Apply the min-shares floor ONLY when it's consistent with the cap — a misconfigured minShares > maxShares
  // (both individually valid in the editable settings) must not silently zero every order forever.
  if (shares < sizing.minShares && sizing.minShares <= sizing.maxShares) shares = 0;
  return { shares, riskUsd, notes };
}

interface Resolved {
  price: number | null;
  notes: string[];
}

// A profile's entry `source` maps 1:1 to an entry-leg mechanism id (the enum stays stable for callers/tests).
const ENTRY_MECHANISM: Record<EntrySource, string> = {
  last: "last",
  mid: "marketable",
  kijun: "kijun_pullback",
};

/** Resolve the entry: mechanism candidate + intrinsic note, then apply the entry offset + tick round. The
 *  entry mechanism is the ticket override (#51) if present, else the profile's `entry.source` (byte-identical
 *  legacy path). `offsetTicks` is applied EXACTLY ONCE, from the effective params. */
function resolveEntry(
  ctx: MechanismCtx,
  profile: OrderProfile,
  override?: { id: string; params?: Record<string, unknown> },
): Resolved {
  const id = override?.id ?? ENTRY_MECHANISM[profile.entry.source];
  const desc = getMechanism("entry", id);
  // Profile params are the base for the legacy path; a per-order override merges on top. Defaults fill gaps.
  const params = {
    ...desc.defaults,
    offsetTicks: profile.entry.offsetTicks,
    maxSpreadPct: profile.entry.maxSpreadPct,
    ...(override?.params ?? {}),
  };
  const r = desc.compute(ctx, params);
  const notes = r.note ? [r.note] : [];
  if (r.price == null) return { price: null, notes };
  const long = ctx.action === "BUY";
  const offsetTicks = (params.offsetTicks as number) ?? 0;
  const offset = offsetTicks * TICK_SIZE * (long ? 1 : -1);
  return { price: roundTick(r.price + offset), notes };
}

/** Resolve the stop with the exact pre-#65 fallback chain: ichimoku (one tick beyond structure, protective
 *  side of entry) → ATR (for model atr or ichimoku) → percent (final). Notes match the monolith verbatim. */
function resolveStop(ctx: MechanismCtx, profile: OrderProfile, stopModel: StopModel): Resolved {
  const long = ctx.action === "BUY";
  const notes: string[] = [];
  if (ctx.entry == null) return { price: null, notes };

  if (stopModel === "ichimoku") {
    // The mechanism applies the one-tick structure buffer; the caller rounds on the protective side.
    const cand = getMechanism("stop", "ichimoku").compute(ctx, { ichimokuRef: profile.stop.ichimokuRef, bufTicks: 1 }).price;
    if (cand != null) {
      const s = roundTick(cand, long ? "floor" : "ceil");
      // Must be on the PROTECTIVE side of entry — else it's not a stop; fall through to ATR.
      if (long ? s < ctx.entry : s > ctx.entry) return { price: s, notes };
      notes.push("Ichimoku stop wrong side → ATR");
    } else {
      notes.push("no Ichimoku level → ATR stop");
    }
  }

  if (stopModel === "atr" || stopModel === "ichimoku") {
    const a = getMechanism("stop", "atr").compute(ctx, { atrLookback: profile.stop.atrLookback, atrMult: profile.stop.atrMult }).price;
    if (a != null) return { price: roundTick(a, long ? "floor" : "ceil"), notes };
  }

  const p = getMechanism("stop", "percent").compute(ctx, { percentStop: profile.stop.percentStop }).price;
  const s = p != null ? roundTick(p, long ? "floor" : "ceil") : null;
  if (stopModel !== "percent") notes.push("fell back to % stop");
  return { price: s, notes };
}

/** Resolve the target (R-multiple off entry→stop, or percent). Needs entry (+ stop for R-multiple). */
function resolveTarget(ctx: MechanismCtx, profile: OrderProfile): Resolved {
  if (ctx.entry == null || ctx.stop == null) return { price: null, notes: [] };
  const id = profile.target.model; // "r_multiple" | "percent" — both are target-leg mechanism ids
  const params = id === "r_multiple" ? { targetR: profile.target.targetR } : { percentTarget: profile.target.percentTarget };
  const r = getMechanism("target", id).compute(ctx, params);
  return { price: r.price != null ? roundTick(r.price) : null, notes: [] };
}

export function computePrefill(inp: PrefillInputs): PrefillResult {
  const { action, profile, bars, last, mid, spreadPct: spread, equity, buyingPower } = inp;
  const notes: string[] = [];
  const levels = lastLevels(bars);
  const ctx: MechanismCtx = { bars, action, last, mid, spreadPct: spread, levels, entry: null, stop: null };

  // --- entry ---
  const e = resolveEntry(ctx, profile, inp.entryMechanism);
  notes.push(...e.notes);
  const entry = e.price;

  // --- stop (ticket-level stop-source override wins over the profile default) ---
  const stopModel: StopModel = inp.stopModel ?? profile.stop.model;
  const s = resolveStop({ ...ctx, entry }, profile, stopModel);
  notes.push(...s.notes);
  const stop = s.price;

  // --- target ---
  const t = resolveTarget({ ...ctx, entry, stop }, profile);
  notes.push(...t.notes);
  const target = t.price;

  // --- size (the ONE capped sizing path; the ticket's manual re-size shares it) ---
  let shares = 0;
  let riskUsd: number | null = null;
  if (entry != null && stop != null) {
    const sized = sizePosition(entry, stop, action, profile.sizing, equity, buyingPower);
    shares = sized.shares;
    riskUsd = sized.riskUsd;
    notes.push(...sized.notes);
  }

  return { entry, stop, target, shares, riskUsd, notes };
}
