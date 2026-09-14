/**
 * Prefill-mechanism catalog (#65) — a pluggable registry of LEVEL-derivation mechanisms, one per leg
 * (entry / stop / target). Mirrors the backend order-action registry (#50): ship a standard set, add new
 * mechanisms with no core change. Each mechanism computes ONE raw candidate price for its leg; the CALLER
 * (`prefill.ts` resolvers) owns tick-rounding, protective-side validation, the cross-mechanism fallback
 * chain, and the human-readable note strings — fallback is order POLICY, not per-mechanism behavior.
 *
 * The same catalog feeds three surfaces off one definition of "how a level is computed": the MANUAL prefill
 * (`computePrefill`), the strategy-first tile's mechanism pickers (#51/#67), and the auto-trade managers
 * (#37/#46/#47). Pure + unit-tested — no React, no side effects.
 */
import type { BarDTO } from "@/lib/api/types";
import { TICK_SIZE } from "@/config/orderTicket";
import type { Levels } from "@/lib/ichimoku";
import { atr, breakoutLevel } from "@/lib/order/indicators";

export type Leg = "entry" | "stop" | "target";

/** Everything a mechanism may read. `entry`/`stop` are the ALREADY-resolved upstream legs (a stop needs the
 *  entry; an r-multiple target needs both) — null until the caller has resolved them. */
export interface MechanismCtx {
  bars: BarDTO[];
  action: "BUY" | "SELL";
  last: number | null;
  mid: number | null;
  spreadPct: number | null;
  levels: Levels | null;
  entry?: number | null;
  stop?: number | null;
}

/** A raw candidate price (UNROUNDED — the caller rounds on the protective side) plus an optional note that is
 *  INTRINSIC to this mechanism's own decision (e.g. marketable falling from mid to last on a wide spread).
 *  Cross-mechanism fallback notes ("… → ATR stop") are emitted by the caller, not here. */
export interface MechanismResult {
  price: number | null;
  note?: string;
}

export interface MechanismDescriptor<P extends Record<string, unknown> = Record<string, unknown>> {
  id: string;
  leg: Leg;
  label: string;
  /** JSON Schema for this mechanism's params — drives the #49 settings-rendered inputs in the tile. */
  paramsSchema: object;
  defaults: P;
  compute(ctx: MechanismCtx, params: P): MechanismResult;
}

// Registry keyed by (leg, id): `percent` exists as both a stop and a target mechanism, so id alone collides.
const REGISTRY = new Map<string, MechanismDescriptor>();
const key = (leg: Leg, id: string) => `${leg}:${id}`;

export function registerMechanism(d: MechanismDescriptor): void {
  const k = key(d.leg, d.id);
  if (REGISTRY.has(k)) throw new Error(`mechanism already registered: ${k}`);
  REGISTRY.set(k, d);
}

export function getMechanism(leg: Leg, id: string): MechanismDescriptor {
  const d = REGISTRY.get(key(leg, id));
  if (!d) throw new Error(`unknown mechanism: ${key(leg, id)}`);
  return d;
}

export function hasMechanism(leg: Leg, id: string): boolean {
  return REGISTRY.has(key(leg, id));
}

export function mechanismIds(leg: Leg): string[] {
  return [...REGISTRY.values()].filter((d) => d.leg === leg).map((d) => d.id);
}

export function mechanisms(leg: Leg): MechanismDescriptor[] {
  return [...REGISTRY.values()].filter((d) => d.leg === leg);
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────────────
// Standard mechanisms. Behavior migrated VERBATIM from the pre-#65 monolithic computePrefill so the existing
// prefill.test.ts contract holds byte-for-byte. compute() returns a raw candidate; the caller rounds/validates.
// ─────────────────────────────────────────────────────────────────────────────────────────────────────────

const num = (title: string) => ({ type: "number", title } as const);

// --- entry ---

/** Spread-gated marketable price: mid only when the quote is sane (non-crossed, tight enough — IEX runs
 *  wide), else last. The note is intrinsic — the mechanism itself chose to fall to last. */
const marketable: MechanismDescriptor<{ offsetTicks: number; maxSpreadPct: number }> = {
  id: "marketable",
  leg: "entry",
  label: "Marketable (mid/last)",
  paramsSchema: { type: "object", properties: { offsetTicks: num("Offset ticks"), maxSpreadPct: num("Max spread %") } },
  defaults: { offsetTicks: 1, maxSpreadPct: 0.003 },
  compute(ctx, { maxSpreadPct }) {
    const { mid, spreadPct, last } = ctx;
    if (mid != null && spreadPct != null && spreadPct >= 0 && spreadPct <= maxSpreadPct) return { price: mid };
    return { price: last, note: mid != null ? "spread too wide → last price" : undefined };
  },
};

/** Plain last price. */
const lastEntry: MechanismDescriptor<{ offsetTicks: number }> = {
  id: "last",
  leg: "entry",
  label: "Last price",
  paramsSchema: { type: "object", properties: { offsetTicks: num("Offset ticks") } },
  defaults: { offsetTicks: 0 },
  compute(ctx) {
    return { price: ctx.last };
  },
};

/** Pullback to the Kijun (structure). Falls back to last when no level yet — preserves pre-#65 behavior
 *  (`levels?.kijun ?? last`) so a missing cloud doesn't null the entry. */
const kijunPullback: MechanismDescriptor<{ offsetTicks: number }> = {
  id: "kijun_pullback",
  leg: "entry",
  label: "Kijun pullback",
  paramsSchema: { type: "object", properties: { offsetTicks: num("Offset ticks") } },
  defaults: { offsetTicks: 0 },
  compute(ctx) {
    return { price: ctx.levels?.kijun ?? ctx.last };
  },
};

/** Pullback to the Tenkan (faster structure). */
const tenkanPullback: MechanismDescriptor<{ offsetTicks: number }> = {
  id: "tenkan_pullback",
  leg: "entry",
  label: "Tenkan pullback",
  paramsSchema: { type: "object", properties: { offsetTicks: num("Offset ticks") } },
  defaults: { offsetTicks: 0 },
  compute(ctx) {
    return { price: ctx.levels?.tenkan ?? ctx.last };
  },
};

/** Pullback a multiple of ATR below (BUY) / above (SELL) the last price — buy weakness into a level. */
const atrPullback: MechanismDescriptor<{ offsetTicks: number; atrLookback: number; atrMult: number }> = {
  id: "atr_pullback",
  leg: "entry",
  label: "ATR pullback",
  paramsSchema: {
    type: "object",
    properties: { offsetTicks: num("Offset ticks"), atrLookback: num("ATR lookback"), atrMult: num("ATR mult") },
  },
  defaults: { offsetTicks: 0, atrLookback: 14, atrMult: 1 },
  compute(ctx, { atrLookback, atrMult }) {
    const a = atr(ctx.bars, atrLookback);
    if (a == null || ctx.last == null) return { price: null };
    return { price: ctx.action === "BUY" ? ctx.last - atrMult * a : ctx.last + atrMult * a };
  },
};

/** Breakout trigger: recent swing high (BUY) / low (SELL) — enter on a break of structure (#37 base-break). */
const breakout: MechanismDescriptor<{ offsetTicks: number; lookback: number }> = {
  id: "breakout",
  leg: "entry",
  label: "Breakout level",
  paramsSchema: { type: "object", properties: { offsetTicks: num("Offset ticks"), lookback: num("Lookback bars") } },
  defaults: { offsetTicks: 1, lookback: 10 },
  compute(ctx, { lookback }) {
    return { price: breakoutLevel(ctx.bars, ctx.action, lookback) };
  },
};

// --- stop ---

/** Ichimoku structure stop: `bufTicks` beyond the Kijun or the far cloud edge (protective direction). Returns
 *  the raw buffered candidate; the caller rounds on the protective side and validates it sits protective of
 *  entry (else fallback). */
const ichimokuStop: MechanismDescriptor<{ ichimokuRef: "kijun" | "cloud"; bufTicks: number }> = {
  id: "ichimoku",
  leg: "stop",
  label: "Ichimoku structure",
  paramsSchema: {
    type: "object",
    properties: { ichimokuRef: { type: "string", enum: ["kijun", "cloud"], title: "Reference" }, bufTicks: num("Buffer ticks") },
  },
  defaults: { ichimokuRef: "kijun", bufTicks: 1 },
  compute(ctx, { ichimokuRef, bufTicks }) {
    const long = ctx.action === "BUY";
    const ref = ichimokuRef === "cloud" ? (long ? ctx.levels?.cloudBot : ctx.levels?.cloudTop) : ctx.levels?.kijun;
    if (ref == null) return { price: null };
    const buf = bufTicks * TICK_SIZE; // one (default) tick beyond structure, on the protective side
    return { price: long ? ref - buf : ref + buf };
  },
};

/** ATR stop: entry ∓ mult·ATR. */
const atrStop: MechanismDescriptor<{ atrLookback: number; atrMult: number }> = {
  id: "atr",
  leg: "stop",
  label: "ATR",
  paramsSchema: { type: "object", properties: { atrLookback: num("ATR lookback"), atrMult: num("ATR mult") } },
  defaults: { atrLookback: 14, atrMult: 1.5 },
  compute(ctx, { atrLookback, atrMult }) {
    const a = atr(ctx.bars, atrLookback);
    if (a == null || ctx.entry == null) return { price: null };
    return { price: ctx.action === "BUY" ? ctx.entry - atrMult * a : ctx.entry + atrMult * a };
  },
};

/** Percent stop: entry·(1∓p). The always-available final fallback. */
const percentStop: MechanismDescriptor<{ percentStop: number }> = {
  id: "percent",
  leg: "stop",
  label: "Percent",
  paramsSchema: { type: "object", properties: { percentStop: num("Stop %") } },
  defaults: { percentStop: 0.02 },
  compute(ctx, { percentStop: p }) {
    if (ctx.entry == null) return { price: null };
    return { price: ctx.action === "BUY" ? ctx.entry * (1 - p) : ctx.entry * (1 + p) };
  },
};

// --- target ---

/** R-multiple target: entry ± R·|entry−stop|. Needs both entry and stop resolved. */
const rMultiple: MechanismDescriptor<{ targetR: number }> = {
  id: "r_multiple",
  leg: "target",
  label: "R multiple",
  paramsSchema: { type: "object", properties: { targetR: num("Target R") } },
  defaults: { targetR: 2 },
  compute(ctx, { targetR }) {
    if (ctx.entry == null || ctx.stop == null) return { price: null };
    const r = Math.abs(ctx.entry - ctx.stop);
    return { price: ctx.action === "BUY" ? ctx.entry + targetR * r : ctx.entry - targetR * r };
  },
};

/** Percent target: entry·(1±p). */
const percentTarget: MechanismDescriptor<{ percentTarget: number }> = {
  id: "percent",
  leg: "target",
  label: "Percent",
  paramsSchema: { type: "object", properties: { percentTarget: num("Target %") } },
  defaults: { percentTarget: 0.04 },
  compute(ctx, { percentTarget: p }) {
    if (ctx.entry == null) return { price: null };
    return { price: ctx.action === "BUY" ? ctx.entry * (1 + p) : ctx.entry * (1 - p) };
  },
};

/** Structure target: the next Ichimoku level beyond entry in the trade's direction (cloud far edge). */
const structureTarget: MechanismDescriptor<Record<string, never>> = {
  id: "structure",
  leg: "target",
  label: "Structure (Ichimoku)",
  paramsSchema: { type: "object", properties: {} },
  defaults: {},
  compute(ctx) {
    if (ctx.entry == null || ctx.levels == null) return { price: null };
    const long = ctx.action === "BUY";
    const edge = long ? ctx.levels.cloudTop : ctx.levels.cloudBot;
    if (edge == null) return { price: null };
    // Only a target if the level is actually beyond entry in the trade direction.
    return { price: long ? (edge > ctx.entry ? edge : null) : edge < ctx.entry ? edge : null };
  },
};

for (const d of [
  marketable, lastEntry, kijunPullback, tenkanPullback, atrPullback, breakout,
  ichimokuStop, atrStop, percentStop,
  rMultiple, percentTarget, structureTarget,
] as MechanismDescriptor[]) {
  registerMechanism(d);
}
