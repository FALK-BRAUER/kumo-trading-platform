/**
 * Watchlist config schema — split out from `definition.ts` (which also imports `WatchlistTile.tsx` for
 * its `component` field) so a pure-logic module like `persistence.ts` can import just the schema without
 * transitively pulling the whole React component (JSX) into a node-environment test. Same class of fix
 * as `defaults.ts` (codex review, #182 follow-up: circular import) — a config-only file shouldn't force
 * consumers through a file that also re-exports a component.
 */
import { z } from "zod";

/** Statuses `recommend()` can actually return for a WATCHLIST row (pnlPct is always hardcoded 0 there,
 *  so the pnlPct-gated TRAIL/ADD branches never fire on this tile; and no `side` is passed — a watchlist
 *  row is a symbol, not a position — so the SHORT refusal `REFUSED_SHORT` added in #855 is unreachable
 *  here too). Single source of truth for both the schema (reject anything else — a stale/corrupted
 *  localStorage value must degrade to "no filter", not apply an impossible filter, codex review #182
 *  follow-up Phase 4) and the gear popover's filter chips. */
export const WATCHLIST_STATUS_FILTERS = ["WATCH", "HOLD", "EXIT"] as const;

/** Blue Flag checklist tiers (`TIER_CLS` in WatchlistTile.tsx is the other consumer of this exact set —
 *  "?" is a real, selectable tier meaning "insufficient history", not an error state). Phase 5. */
export const WATCHLIST_TIER_FILTERS = ["+++", "++", "+", "=", "--", "---", "?"] as const;

/** 1d cloud position (`CloudPosition` in `@/lib/ichimoku`) — kept as a literal tuple here rather than
 *  importing the type, so this schema file (persistence's trust boundary) stays a pure zod definition with
 *  no dependency beyond zod itself. Phase 5. */
export const WATCHLIST_CLOUD_FILTERS = ["above", "in", "below"] as const;

export const watchlistConfigSchema = z.object({
  /** Full instrument ids (e.g. "AAPL.XNAS"). Only symbols the backend feeds will show live data. */
  symbols: z.array(z.string()).default([]),
  /** Registered KPI ids (`@/lib/framework/kpi/registry`) to render per row, in order. Omitted →
   *  `DEFAULT_WATCHLIST_KPIS` (defaults.ts). An id with no matching registration is skipped, never a
   *  crash — see `getKpi`'s contract. */
  kpis: z.array(z.string()).optional(),
  /** Gear icon (#182 follow-up, Phase 4) — sort/filter, per-tile-instance. `sortBy` is a base row field
   *  ("symbol"/"last"/"chg_pct") or any registered KPI id with a `sortValue`. Omitted `sortBy` → no sort,
   *  rows render in their existing (watchlist-add) order. */
  sortBy: z.string().optional(),
  sortDir: z.enum(["asc", "desc"]).default("asc"),
  /** Signal-status filter (WATCH/HOLD/EXIT from `recommend()`) — empty/omitted → show all. */
  statusFilter: z.array(z.enum(WATCHLIST_STATUS_FILTERS)).optional(),
  /** Blue Flag tier filter (Phase 5) — empty/omitted → show all. */
  tierFilter: z.array(z.enum(WATCHLIST_TIER_FILTERS)).optional(),
  /** 1d cloud-position filter (Phase 5) — empty/omitted → show all. */
  cloudFilter: z.array(z.enum(WATCHLIST_CLOUD_FILTERS)).optional(),
});

export type WatchlistConfig = z.infer<typeof watchlistConfigSchema>;
