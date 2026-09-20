/**
 * Orders tile definition (#33) — the working-order blotter (IBKR Orders-tab style). Binds the shared
 * `orders` DataSource (WS, re-pushed each tick); renders working orders first, then recent terminal,
 * with Cancel/Modify wired to the command layer. configSchema lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { OrdersTile } from "./OrdersTile";

export const ordersConfigSchema = z.object({
  /** Lane filter (MANUAL / ETF_AUTO / BCT_AUTO) — unused until lanes land; absent = all lanes. */
  lane: z.string().optional(),
});

export type OrdersConfig = z.infer<typeof ordersConfigSchema>;

export const ordersDefinition: TileDefinition<OrdersConfig> = {
  type: "orders",
  title: "Orders",
  component: OrdersTile,
  defaultSize: { w: 24, h: 10 },
  minSize: { w: 8, h: 5 },
  defaultConfig: {},
  configSchema: ordersConfigSchema,
  dataSources: ["orders"],
  chrome: false, // renders its own header + bordered blotter
};
