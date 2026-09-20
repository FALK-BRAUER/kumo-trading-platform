/**
 * Market tile (#351) — which rotation the market is in.
 *
 * REST-backed rather than plane-backed: the payload is a daily Ichimoku read produced outside the
 * cockpit, not a stream — so it binds the `rotation` source (`kind: "rest"`) and never sees an
 * endpoint. The first cut had `dataSources: []` and fetched inside the component, citing the Pool
 * tile as precedent. Pool's exemption is ARGUED — it is a mutable list whose writes return the new
 * state, so its react-query cache is both read and write path — and that argument does not transfer
 * to a read-only snapshot. Precedent was cited without checking whether its reason applied.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { MarketTile } from "./MarketTile";

export const marketConfigSchema = z.object({
  /**
   * Which panels this instance shows. Config, not a runtime control: layouts are TS
   * (`config-over-ui-editor`), and a per-tile view switch would be a second place to change what the
   * board already decides. A board wanting only the scatter mounts a second Market tile with
   * `panel: "map"`.
   */
  panel: z.enum(["both", "map", "rows"]).default("both"),
  /**
   * Plot height in px. A prop with a default, the same shape `EquityCurve` uses — the house pattern for
   * a fixed-geometry chart in a resizable tile. Hardcoded at 224 in the first cut, which meant a tile
   * the operator resized kept a plot that did not.
   */
  mapHeight: z.number().int().positive().default(224),
});

export type MarketConfig = z.infer<typeof marketConfigSchema>;

export const marketDefinition: TileDefinition<MarketConfig> = {
  type: "market",
  title: "Market",
  component: MarketTile,
  defaultSize: { w: 24, h: 16 },
  minSize: { w: 6, h: 8 },
  defaultConfig: { panel: "both", mapHeight: 224 },
  configSchema: marketConfigSchema,
  dataSources: ["rotation"],
  chrome: false, // renders its own header and period tabs, like Book and Equity
};
