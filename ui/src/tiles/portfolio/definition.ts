/**
 * Portfolio tile definition — the type's contract (schema, sources, default size). Co-located with
 * the component; registered by `@/config/tiles`. The configSchema lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { PortfolioTile } from "./PortfolioTile";

export const portfolioConfigSchema = z.object({
  /** Strategy filter (MANUAL / MOMENTUM / ETF_AUTO) — unused until multi-strategy lands; absent = all. */
  strategy: z.string().optional(),
});

export type PortfolioConfig = z.infer<typeof portfolioConfigSchema>;

export const portfolioDefinition: TileDefinition<PortfolioConfig> = {
  type: "portfolio",
  title: "Portfolio",
  component: PortfolioTile,
  defaultSize: { w: 12, h: 8 },
  minSize: { w: 4, h: 4 },
  defaultConfig: {},
  configSchema: portfolioConfigSchema,
  dataSources: ["positions"], // bars source binds in #7 phase 3 (3d)
  chrome: false, // renders its own header + bordered list
};
