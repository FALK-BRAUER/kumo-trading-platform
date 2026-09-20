/**
 * Strategy catalog — the ONE place the UI learns which strategies exist and which are live.
 *
 * A strategy is a trading STYLE plus a capital sleeve, and its id is the Nautilus `StrategyId` that keys
 * positions (`{instrument}-{strategy_id}`). Ids here are the bare NAME; the wire id carries the tag
 * (`MANUAL` → `MANUAL-001`), which is what `wireId()` builds.
 *
 * Config, not a runtime registry: which strategies exist is a deployment decision, so it lives here next to
 * the tile and layout catalogs rather than being discovered. Previously this list was duplicated inside the
 * order ticket and again inside the transfer UI, which meant a new strategy had to be remembered in three
 * places — this is the single source.
 */

export interface StrategyDef {
  /** Bare name — the Nautilus StrategyId NAME part. */
  id: string;
  /** Human label for pickers. */
  label: string;
  /** Order-id tag bound at registration; the wire id is `${id}-${tag}`. */
  tag: string;
  /** False = shown but disabled ("soon"). Only live strategies can own positions or take orders. */
  live: boolean;
}

export const STRATEGIES: readonly StrategyDef[] = [
  { id: "MANUAL", label: "Manual", tag: "001", live: true },
  { id: "MOMENTUM", label: "Momentum", tag: "001", live: false },
  { id: "ETF_AUTO", label: "ETF Auto", tag: "001", live: false },
] as const;

/** `MANUAL` → `MANUAL-001` — the id positions and orders are actually keyed by. */
export function wireId(strategyId: string): string {
  const def = STRATEGIES.find((s) => s.id === strategyId);
  return def ? `${def.id}-${def.tag}` : strategyId;
}

/** Wire ids a position may be moved INTO. Only live strategies: transferring into one that isn't running
 *  would strand the position with no owner able to manage it (the engine rejects it too). */
export function transferTargets(): string[] {
  return STRATEGIES.filter((s) => s.live).map((s) => wireId(s.id));
}

/** `MANUAL-001` → `MANUAL`, for display and for matching detail variants registered by strategy NAME. */
export function bareName(wire: string): string {
  return wire.replace(/-\d+$/, "");
}
