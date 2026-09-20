/**
 * Position-transfer policy — how moving a position between strategies is priced.
 *
 * A deployment decision, not a per-click one: the mode changes what every strategy's P&L number MEANS, so it
 * belongs beside the other catalogs rather than as a literal at a call site.
 *
 * * `CARRY_OVER` — both legs book at the source's cost basis. Nothing is realized; the basis migrates, and
 *   the target inherits whatever unrealized P&L was already there. Easiest to read on a dashboard.
 * * `MARKET` — both legs book at the current mark. The source crystallizes its P&L up to the handoff and the
 *   target starts clean, so each strategy's number reflects only its own decisions. More correct for
 *   comparing sleeves; less obvious at a glance.
 */

export type PricingMode = "MARKET" | "CARRY_OVER";

/** the operator's call (2026-07-28): comprehensibility over accounting purity for a cockpit read daily. */
export const DEFAULT_PRICING_MODE: PricingMode = "CARRY_OVER";
