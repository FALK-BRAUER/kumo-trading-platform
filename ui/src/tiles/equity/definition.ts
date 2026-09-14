/**
 * Equity tile definition (#243) — the account's own equity curve on Home.
 *
 * Binds the `equity_curve` plane, which the engine republishes roughly every two minutes from the
 * broker's `/v2/account/portfolio/history`. Read-only.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { EquityTile } from "./EquityTile";

export const equityConfigSchema = z.object({});

export type EquityConfig = z.infer<typeof equityConfigSchema>;

export const equityDefinition: TileDefinition<EquityConfig> = {
  type: "equity",
  title: "Equity",
  component: EquityTile,
  defaultSize: { w: 24, h: 11 },
  minSize: { w: 6, h: 8 },
  defaultConfig: {},
  configSchema: equityConfigSchema,
  dataSources: ["equity_curve", "account"],
  chrome: false, // renders its own header + period tabs, like the Book tile
};
