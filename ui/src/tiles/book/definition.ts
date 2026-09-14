/**
 * Book tile definition — the P&L hero on Home (Option A, PR #223 / #212 / #107).
 *
 * Binds the same planes the Portfolio tile reads (`trades` + `account` + `external_activity`), so the
 * two are computed from one source and cannot disagree. configSchema lives here, never in the .tsx.
 */
import { z } from "zod";
import type { TileDefinition } from "@/lib/framework/types";
import { BookTile } from "./BookTile";

export const bookConfigSchema = z.object({});

export type BookConfig = z.infer<typeof bookConfigSchema>;

export const bookDefinition: TileDefinition<BookConfig> = {
  type: "book",
  title: "Book",
  component: BookTile,
  defaultSize: { w: 24, h: 12 },
  minSize: { w: 6, h: 10 },
  defaultConfig: {},
  configSchema: bookConfigSchema,
  // `equity_curve` carries the broker's per-period P&L, which is NET (#336) — bound here so NET and
  // the EQUITY header read the SAME number rather than two derivations that can drift.
  dataSources: ["trades", "account", "external_activity", "equity_curve"],
  // Renders its own header, like ManagedPortfolioTile — without this the frame's title and the tile's
  // own heading both draw, which read as "Book / BOOK" stacked on a phone.
  chrome: false,
};
