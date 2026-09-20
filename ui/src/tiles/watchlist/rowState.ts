"use client";

/**
 * Watchlist row-state store (#182 follow-up, Phase 4: sort + filter) — a tiny external store, same shape
 * as `datasource/ws-manager.ts`, that lets each `WatchRow` report the values the PARENT needs to sort/
 * filter the symbol list, without lifting `useInstrument`/`useBars` up to the parent.
 *
 * Why this exists: sort/filter determine RENDER ORDER, a parent-level concern, but the values driving them
 * (bar-derived Chg%/signal-status, or a configurable KPI's `sortValue`) are only available INSIDE each
 * `WatchRow` — that's the "one `useInstrument` per symbol, rules-of-hooks safe" pattern (#179) the
 * watchlist already depends on for a dynamic instrument count. Calling those hooks in a loop at the
 * parent would violate rules-of-hooks for a variable-length symbol list. Instead each `WatchRow` reports
 * its own state here via a `useEffect`; the parent subscribes and computes order from the reported values.
 * A row that hasn't reported yet (first render, still loading) sorts last, never crashes the comparator.
 *
 * Store is MODULE-level (shared across every mounted tile), so the key a caller reports under must be
 * globally unique, not just per-symbol — the watchlist tile keys by `${instanceId}:${instrumentId}` so two
 * tile instances (e.g. two Watchlist tiles with different sortBy configs) can't clobber each other's
 * `sortValue` for a symbol they both show (codex review, #182 follow-up Phase 4).
 */
export interface RowState {
  symbol: string;
  status: string; // rec.status: WATCH/HOLD/EXIT/ADD/TRAIL
  sortValue: number | string | null; // for whatever sortBy is CURRENTLY configured
  tier: string | null; // Blue Flag checklist tier (+++/++/+/=/--/---/?), null if no checklist result yet
  cloudPos: string | null; // 1d cloud position (above/in/below), null if not enough bars yet
}

let state = new Map<string, RowState>();
const listeners = new Set<() => void>();

export function reportRowState(key: string, next: RowState): void {
  const prev = state.get(key);
  if (
    prev &&
    prev.symbol === next.symbol &&
    prev.status === next.status &&
    prev.sortValue === next.sortValue &&
    prev.tier === next.tier &&
    prev.cloudPos === next.cloudPos
  ) {
    return; // no-op — avoids a listener notification (and parent re-render) on every unrelated row re-render
  }
  state = new Map(state).set(key, next); // new Map reference — useSyncExternalStore needs a
  // snapshot that changes identity, not a mutated-in-place one, to know something changed.
  listeners.forEach((l) => l());
}

/** Called from the reporting `WatchRow`'s own unmount cleanup — NOT from a specific "remove" mutation's
 *  onSuccess — so this fires for every unmount reason (this tile's own remove, an external refetch that
 *  drops the symbol, a query-cache update from elsewhere), not just one path (codex review, #182 follow-up
 *  Phase 4: a stale entry from an untracked removal path could otherwise wrongly exclude/include a later
 *  re-added symbol under an active filter before its `WatchRow` remounts and re-reports). */
export function clearRowState(key: string): void {
  if (!state.has(key)) return;
  state = new Map(state);
  state.delete(key);
  listeners.forEach((l) => l());
}

export function subscribeRowState(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function getRowStateSnapshot(): Map<string, RowState> {
  return state;
}
