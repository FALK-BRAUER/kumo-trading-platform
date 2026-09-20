/**
 * Managed Portfolio tile definition (#77) — the managed book. Binds the `trades` DataSource (per-strategy
 * trade cycles) + `account` (for %-deployed). Net-anchor per instrument → per-strategy cycle drill-down, dual
 * accounting lens. Replaces the flat position `portfolio` tile as the default portfolio view. configSchema
 * lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { ManagedPortfolioTile } from "./ManagedPortfolioTile";

export const managedPortfolioConfigSchema = z.object({
  /** Strategy filter (MANUAL / MOMENTUM / ETF_AUTO) — unused until multi-strategy lands; absent = all. */
  strategy: z.string().optional(),
});

export type ManagedPortfolioConfig = z.infer<typeof managedPortfolioConfigSchema>;

export const managedPortfolioDefinition: TileDefinition<ManagedPortfolioConfig> = {
  type: "managed-portfolio",
  title: "Portfolio",
  component: ManagedPortfolioTile,
  defaultSize: { w: 24, h: 10 },
  minSize: { w: 8, h: 5 },
  defaultConfig: {},
  configSchema: managedPortfolioConfigSchema,
  dataSources: ["trades", "account", "external_activity", "managers"],
  chrome: false, // renders its own header + bordered table (like OrdersTile)
};
