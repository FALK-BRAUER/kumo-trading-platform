/**
 * Framework core types — the tile contract. Tile-agnostic: this module knows nothing about
 * portfolio/chart/order. Concrete tiles live in `@/tiles`, registration in `@/config`.
 *
 * Dependency rule: core (this dir) imports nothing downstream. tiles import core. config imports both.
 */
import type { ComponentType } from "react";
import type { ZodType } from "zod";

/** Per-source resolution state the container hands each tile (data plane lands in #7 phase 2/3). */
export type SourceStatus = "loading" | "live" | "stale" | "error";

/** Props every tile component receives. Tiles NEVER fetch — data arrives here, resolved by name. */
export interface TileProps<Cfg = unknown> {
  /** Unique per placed instance (stable across reorders) — keys RGL, dedups subscriptions. */
  instanceId: string;
  /** Validated instance config (e.g. ticker, lane filter). */
  config: Cfg;
  /** Resolved data keyed by the source name the tile declared in `dataSources`. */
  data: Record<string, unknown>;
  /** Per-source status, keyed the same as `data`. */
  status: Record<string, SourceStatus>;
  /** Tile self-edits (e.g. change chart ticker) → container persists into the layout. */
  onConfigChange: (next: Cfg) => void;
}

/** One definition per tile *type*. The schema lives HERE (the contract), never in the component. */
export interface TileDefinition<Cfg = unknown> {
  type: string;
  title: string;
  component: ComponentType<TileProps<Cfg>>;
  /** Grid units (react-grid-layout lands in #7 phase 4). */
  defaultSize: { w: number; h: number };
  minSize?: { w: number; h: number };
  defaultConfig: Cfg;
  configSchema: ZodType<Cfg>;
  /** Names of DataSources this tile subscribes to — bound by the container, not fetched by the tile. */
  dataSources: string[];
  /** Render edge-to-edge (no card padding) — charts want this. */
  bleed?: boolean;
  /** false → render bare (the tile provides its own header/list chrome). */
  chrome?: boolean;
}
