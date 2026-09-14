/**
 * Pure position helpers (#71) — JSX-free so they unit-test under the node-env vitest config, and shared so
 * the list row and the detail agree on the identity key + the displayed strategy label.
 */
import type { PositionDTO } from "@/lib/api/types";

/** Stable identity of a position: strategy + instrument (a symbol can be held by multiple strategies). */
export function positionKey(p: Pick<PositionDTO, "strategy_id" | "instrument_id">): string {
  return `${p.strategy_id}:${p.instrument_id}`;
}

/** Display label for a Nautilus StrategyId: `NAME-tag` → `NAME`; the synthetic dev engine's
 *  `BridgeStrategy-NNN` aliases to MANUAL (matches the discretionary sleeve it stands in for). */
export function strategyLabel(strategyId: string): string {
  return strategyId.startsWith("BridgeStrategy") ? "MANUAL" : strategyId.replace(/-\d+$/, "");
}


/** The subset of a trade cycle this module needs — keeps it testable without the generated DTO. */
export interface HeldCycle {
  strategy_id: string;
  instrument_id: string;
  state: string;
}

/**
 * The position a portfolio row should open, or null when it can't be named unambiguously.
 *
 * A portfolio row is an instrument GROUP, not a position: it aggregates the cycles across strategies and may
 * be flat or merely ARMED. Opening `kind: "position"` needs one concrete (strategy, instrument), so:
 *
 * * exactly one HELD cycle → that position
 * * none held (flat / ARMED only) → null; there is no position to manage yet
 * * more than one held → null; two strategies hold this instrument and picking either would be a guess
 *
 * Callers fall back to the symbol surface for the null cases — generic, but never *wrong*.
 */
export function heldPositionKey(cycles: readonly HeldCycle[]): string | null {
  const held = cycles.filter((c) => c.state === "HELD");
  return held.length === 1 ? positionKey(held[0]) : null;
}
