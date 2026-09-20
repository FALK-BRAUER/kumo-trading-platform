/**
 * Settings tile definition (#49) — the schema-driven settings surface. Binds no DataSource (it fetches
 * the settings domains + schemas directly via the settings client).
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { SettingsTile } from "./SettingsTile";

export const settingsConfigSchema = z.object({});
export type SettingsConfig = z.infer<typeof settingsConfigSchema>;

export const settingsDefinition: TileDefinition<SettingsConfig> = {
  type: "settings",
  title: "Settings",
  component: SettingsTile,
  defaultSize: { w: 24, h: 12 },
  minSize: { w: 6, h: 6 },
  defaultConfig: {},
  configSchema: settingsConfigSchema,
  dataSources: [],
  chrome: false,
};
