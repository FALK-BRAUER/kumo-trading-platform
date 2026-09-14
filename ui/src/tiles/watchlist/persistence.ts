/**
 * Gear-icon config persistence (#182 follow-up, Phase 4) — localStorage, per tile INSTANCE (keyed by
 * `instanceId`, stable across reorders per `TileProps`'s own contract). Operator: "setting file, we have a
 * concept for this" (not Postgres) — this is the inferred mechanism: a lightweight per-browser override
 * layered on top of the layout config's default, matching "the versioned layout file is the source of
 * truth for the default, small user tweaks don't need a database" (the same reasoning `config/layouts.ts`
 * already documents for layout geometry). NOT the same as `updateTileConfig` (the store's existing
 * self-edit action) — that's in-memory/Zustand only and explicitly documented "ephemeral, not persisted";
 * this module is what makes a gear-icon change survive a page reload, calling `onConfigChange` too so the
 * CURRENT session's UI updates immediately without needing a reload to pick up the localStorage write.
 */
import { watchlistConfigSchema, type WatchlistConfig } from "./schema";

function storageKey(instanceId: string): string {
  return `kumo:watchlist-config:${instanceId}`;
}

/** The persisted override for this tile instance, or null if none/corrupted/stale-schema. Always
 *  Zod-validated before use — never trust raw localStorage content (a prior schema version, a hand-edited
 *  value, or a corrupted write must degrade to "no override" instead of crashing the tile). */
export function readPersistedConfig(instanceId: string): WatchlistConfig | null {
  if (typeof window === "undefined") return null; // SSR/build — no localStorage
  try {
    const raw = window.localStorage.getItem(storageKey(instanceId));
    if (!raw) return null;
    const parsed = watchlistConfigSchema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null; // malformed JSON, storage disabled/full, etc. — never let a read crash the tile
  }
}

export function writePersistedConfig(instanceId: string, config: WatchlistConfig): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey(instanceId), JSON.stringify(config));
  } catch {
    // storage full/disabled (private browsing) — the in-memory onConfigChange update still applies this
    // session, it just won't survive a reload. Not worth surfacing as an error for a display preference.
  }
}
