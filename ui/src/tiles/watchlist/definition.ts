/**
 * Watchlist tile definition — a config-driven list of symbols to monitor (no position). The symbols
 * live in the tile's `config` (edit them in `@/config/layouts`), NOT a backend watchlist endpoint.
 * Each symbol binds the `bars` WS source per row, same as a portfolio row, and shows last price +
 * Ichimoku cloud state. Schema lives in `./schema.ts` (not here) so a pure-logic consumer like
 * `persistence.ts` can import it without pulling in `WatchlistTile.tsx`'s JSX transitively.
 */
import type { TileDefinition } from "@/lib/framework/types";
import { WatchlistTile } from "./WatchlistTile";
import { DEFAULT_WATCHLIST_KPIS } from "./defaults";
import { watchlistConfigSchema, type WatchlistConfig } from "./schema";

export { DEFAULT_WATCHLIST_KPIS, watchlistConfigSchema };
export type { WatchlistConfig };

export const watchlistDefinition: TileDefinition<WatchlistConfig> = {
  type: "watchlist",
  title: "Watchlist",
  component: WatchlistTile,
  defaultSize: { w: 12, h: 16 },
  minSize: { w: 4, h: 6 },
  defaultConfig: { symbols: ["AAPL.XNAS", "MSFT.XNAS"], sortDir: "asc" },
  configSchema: watchlistConfigSchema,
  dataSources: [], // bars bind per-row inside the tile (one useSource per symbol)
  chrome: false, // renders its own header + bordered list
};
