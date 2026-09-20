/**
 * DataSource Registry — named data sources tiles bind to by name. The container resolves a tile's
 * declared `dataSources` through `useSource`, so tiles never fetch. Config (`@/config/datasources`)
 * populates this; core stays tile- and deployment-agnostic.
 *
 * `kind` is the seam that keeps the WS swap invisible to tiles (#7 phase 3): a source can move
 * rest → hybrid (REST bootstrap + WS freshen) without any tile change, because tiles only ever see
 * the source `name` and the resolved `{ data, status }`. Topic derivation stays explicit and boring.
 */
import type { Channel } from "./protocol";

export type DataSourceKind = "rest" | "ws" | "hybrid";

export interface DataSource<Cfg = unknown> {
  name: string;
  kind: DataSourceKind;
  /** REST path for `rest` / `hybrid` (the bootstrap snapshot). */
  endpoint?: string;
  /** WS channel for `ws` / `hybrid` — combined with `params(cfg)` into a structured Topic. */
  channel?: Channel;
  /** Derive request params from the binding tile's config (e.g. `{ symbol }` from `cfg.ticker`). */
  params?: (cfg: Cfg) => Record<string, unknown>;
  /** Freshness window → TanStack `staleTime` (default 2000ms if omitted). */
  cacheMs?: number;
  /** Cache retention → TanStack `gcTime` (default 60000ms if omitted). */
  gcMs?: number;
}

const registry = new Map<string, DataSource<unknown>>();

export function registerSource<Cfg>(source: DataSource<Cfg>): void {
  registry.set(source.name, source as unknown as DataSource<unknown>);
}

export function getSource(name: string): DataSource<unknown> | undefined {
  return registry.get(name);
}

export function allSources(): DataSource<unknown>[] {
  return [...registry.values()];
}
