/**
 * Strategy tile definition (#212) — the strategy's own state, as opposed to the account's.
 *
 * Binds the `session` datasource, so the container wires the data and the tile only renders. Read
 * only: nothing here can submit, cancel, pause or arm anything. Acting on what it shows is done
 * where those controls already live.
 */
import type { TileDefinition } from "@/lib/framework/types";
import { StrategyTile } from "./StrategyTile";
import { strategyConfigSchema, type StrategyConfig } from "./schema";

export { strategyConfigSchema };
export type { StrategyConfig };

export const strategyDefinition: TileDefinition<StrategyConfig> = {
  type: "strategy",
  title: "Strategy",
  component: StrategyTile,
  defaultSize: { w: 6, h: 10 },
  minSize: { w: 4, h: 6 },
  defaultConfig: { strategyId: "MOMENTUM-002" },
  configSchema: strategyConfigSchema,
  // `trades` carries the broker sweep (`realized_periods`) the window strip reads (#662).
  dataSources: ["session", "trades"],
};
