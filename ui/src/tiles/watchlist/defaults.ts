/** Reasonable default KPI set for the sub-line inline row (#182 follow-up), split out from `definition.ts`
 *  so `WatchlistTile.tsx` doesn't import back from the module that imports it (codex review: circular
 *  import). "vwap" is deliberately absent — it has its own fixed main-row column now (Operator: deserves "a
 *  true column"), not a toggle-able sub-line entry; `kpiInlineRow` filters it out defensively too, but
 *  there's no reason to list it here as if it were still optional.
 *
 *  "beta"/"eps"/"dividend_amount" are registered (config/kpis.ts) but NOT in this default set — market_cap
 *  + pe are the two most commonly wanted at a glance; the row is already getting long. All three remain
 *  available once Phase 4 (gear icon) lands and this becomes per-tile configurable. */
export const DEFAULT_WATCHLIST_KPIS: string[] = ["volume", "today_range", "prior_close", "market_cap", "pe"];
