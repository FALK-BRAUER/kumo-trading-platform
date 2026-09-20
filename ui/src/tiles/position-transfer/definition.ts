/**
 * Position transfer tile definition (#80 spin-off) — move a position into a strategy.
 *
 * Binds the `external_activity` source (unattributed broker positions). `instrumentId` / `sourceStrategyId`
 * / `side` PIN the instance to one position, which is how a detail surface points the tile at whatever the
 * user opened; unpinned, it acts on the first unattributed position it finds.
 *
 * `pricingMode` is config rather than a per-click control on purpose: MARKET and CARRY_OVER change what
 * every strategy's P&L number MEANS, so it's a deployment decision. configSchema lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { PositionTransferTile } from "./PositionTransferTile";
import { DEFAULT_PRICING_MODE } from "@/config/transfers";

export const positionTransferConfigSchema = z.object({
  /** Pin to one position (all three together identify it — instrument alone is ambiguous). */
  instrumentId: z.string().optional(),
  sourceStrategyId: z.string().optional(),
  side: z.string().optional(),
  /** CARRY_OVER: basis migrates, nothing realized. MARKET: source crystallizes P&L, target starts clean. */
  pricingMode: z.enum(["MARKET", "CARRY_OVER"]).default(DEFAULT_PRICING_MODE),
});

export type PositionTransferConfig = z.infer<typeof positionTransferConfigSchema> & {
  /** Host hook — a detail surface closes itself once the position has moved. Not persisted. */
  onDone?: () => void;
};

export const positionTransferDefinition: TileDefinition<PositionTransferConfig> = {
  type: "position-transfer",
  title: "Move to strategy",
  component: PositionTransferTile,
  defaultSize: { w: 8, h: 5 },
  minSize: { w: 6, h: 4 },
  defaultConfig: { pricingMode: DEFAULT_PRICING_MODE },
  configSchema: positionTransferConfigSchema,
  dataSources: ["external_activity"],
};
