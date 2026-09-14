/**
 * Pool tile definition — the symbols MOMENTUM ranks, plus the operator whitelist/blacklist.
 *
 * `dataSources` is empty and the tile talks to REST itself, the same way the Watchlist tile does. The
 * usual rule (the container wires data, the tile only renders) fits a streaming source; this is a
 * small mutable list where every write returns the new state, so a react-query cache keyed `["pool"]`
 * is both the read and the write path. Routing that through a datasource would need a second
 * invalidation channel to keep the two in step.
 */
import type { TileDefinition } from "@/lib/framework/types";
import { PoolTile } from "./PoolTile";
import { poolConfigSchema, type PoolConfig } from "./schema";

export { poolConfigSchema };
export type { PoolConfig };

export const poolDefinition: TileDefinition<PoolConfig> = {
  type: "pool",
  title: "Pool",
  component: PoolTile,
  defaultSize: { w: 12, h: 16 },
  minSize: { w: 4, h: 8 },
  defaultConfig: { filter: "all" },
  configSchema: poolConfigSchema,
  dataSources: [],
  chrome: false, // renders its own header, filters and bordered list
};
