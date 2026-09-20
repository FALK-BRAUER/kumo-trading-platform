/**
 * Layout config — the source of truth for the board's views. There is no runtime editor; layouts are
 * edited HERE. Each view is a named list of placed tile instances with grid geometry (24-col grid).
 * Today: one Portfolio view (full-width portfolio tile), one Watch view (watchlist tile). Add views
 * (→ read-only tabs) / tiles by editing this list. Validated against the Zod layout schema at the boundary.
 */
import { LAYOUT_SCHEMA_VERSION, type Layout } from "@/lib/framework/layout/schema";

export const DEFAULT_LAYOUTS: Layout[] = [
  {
    // #212. FIRST, so it is the landing view — a homescreen that is not the first thing you see is
    // just another tab. Reorder this entry to change that; it is one line.
    id: "home",
    name: "Home",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    // Option A composition (PR #223): the money first, then the machine. `book` mirrors predecessor-repo's
    // PnlCard sitting above everything else — but split by who decided the trade, because one total
    // across both books is the figure most likely to be misread.
    // Full width both, stacked. On the grid a w:12 tile is half the board; on a phone the Board stacks
    // them anyway (#60), so w:24 is the shape that reads correctly in both.
    tiles: [
      { instanceId: "book-1", type: "book", config: {}, x: 0, y: 0, w: 24, h: 12 },
      // The account's own history sits BELOW the book: Book answers "what is open worth", this answers
      // "where is the account against where the period started". Different questions, and the second
      // only makes sense once you have read the first.
      { instanceId: "equity-1", type: "equity", config: {}, x: 0, y: 12, w: 24, h: 11 },
      { instanceId: "strategy-1", type: "strategy", config: {}, x: 0, y: 23, w: 24, h: 10 },
    ],
  },
  {
    id: "portfolio",
    name: "Portfolio",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    tiles: [
      { instanceId: "portfolio-1", type: "managed-portfolio", config: {}, x: 0, y: 0, w: 24, h: 24 },
    ],
  },
  {
    id: "watch",
    name: "Watch",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    tiles: [
      {
        instanceId: "watchlist-1",
        type: "watchlist",
        config: { symbols: ["AAPL.XNAS", "MSFT.XNAS"] },
        x: 0,
        y: 0,
        w: 24,
        h: 16,
      },
    ],
  },
  {
    id: "market",
    name: "Market",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    // Its OWN view rather than a strip on Home (#351). Home answers "what do I hold and how is it
    // doing"; this answers "where is money moving", which is a question you ask before deciding, not
    // while reading a position. Crowding it onto Home would also push EQUITY and STRATEGY below the
    // fold on a phone, and the tile it would displace is the one the operator opens the app for.
    tiles: [{ instanceId: "market-1", type: "market", config: {}, x: 0, y: 0, w: 24, h: 20 }],
  },
  {
    id: "orders",
    name: "Orders",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    tiles: [{ instanceId: "orders-1", type: "orders", config: {}, x: 0, y: 0, w: 24, h: 20 }],
  },
  {
    id: "pool",
    name: "Pool",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    tiles: [{ instanceId: "pool-1", type: "pool", config: { filter: "all" }, x: 0, y: 0, w: 24, h: 20 }],
  },
  {
    id: "settings",
    name: "Settings",
    schemaVersion: LAYOUT_SCHEMA_VERSION,
    tiles: [{ instanceId: "settings-1", type: "settings", config: {}, x: 0, y: 0, w: 24, h: 16 }],
  },
];
