/**
 * order.vanilla tile definition (#66) — the plain broker order ticket as a placeable tile. Binds no
 * DataSource (it resolves its own instrument via the focused symbol / config); the ticket body is keyed by
 * instrument_id so it resets on symbol change. configSchema lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { OrderVanillaTile } from "./OrderVanillaTile";

export const orderVanillaConfigSchema = z.object({
  /** Strategy this ticket submits into (attribution, ADR vocab) — defaults to MANUAL. */
  strategy_id: z.string().optional(),
  /** Pin a symbol; absent → the tile follows the board's focused instrument. */
  instrument_id: z.string().optional(),
});

export type OrderVanillaConfig = z.infer<typeof orderVanillaConfigSchema>;

export const orderVanillaDefinition: TileDefinition<OrderVanillaConfig> = {
  type: "order.vanilla",
  title: "Order",
  component: OrderVanillaTile,
  defaultSize: { w: 8, h: 12 },
  minSize: { w: 6, h: 10 },
  defaultConfig: {},
  configSchema: orderVanillaConfigSchema,
  dataSources: [],
  chrome: false, // renders its own compact ticket
};
