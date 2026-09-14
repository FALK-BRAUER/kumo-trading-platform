/**
 * Tile composition root — the ONE place that registers the catalog. Imported for its side effect by
 * the app shell. Add a tile = drop its folder under `@/tiles` + one `registerTile(...)` line here.
 * This module may import tiles + framework; nothing imports it except the app shell.
 */
import { registerTile } from "@/lib/framework/registry";
import { bookDefinition } from "@/tiles/book/definition";
import { equityDefinition } from "@/tiles/equity/definition";
import { poolDefinition } from "@/tiles/pool/definition";
import { managedPortfolioDefinition } from "@/tiles/managed-portfolio/definition";
import { marketDefinition } from "@/tiles/market/definition";
import { orderVanillaDefinition } from "@/tiles/order/definition";
import { ordersDefinition } from "@/tiles/orders/definition";
import { portfolioDefinition } from "@/tiles/portfolio/definition";
import { positionTransferDefinition } from "@/tiles/position-transfer/definition";
import { settingsDefinition } from "@/tiles/settings/definition";
import { strategyDefinition } from "@/tiles/strategy/definition";
import { watchlistDefinition } from "@/tiles/watchlist/definition";

registerTile(portfolioDefinition); // flat position list (raw / debug view)
registerTile(managedPortfolioDefinition); // #77 the managed book (default portfolio)
registerTile(watchlistDefinition);
registerTile(ordersDefinition);
registerTile(orderVanillaDefinition);
registerTile(positionTransferDefinition); // #80 move a position into a strategy
registerTile(poolDefinition); // the symbol pool + operator whitelist/blacklist
registerTile(strategyDefinition); // #212 the strategy's own state — lifecycle, decision, what needs you
registerTile(bookDefinition); // Option A's P&L hero on Home — net, split by who decided the trade
registerTile(equityDefinition); // #243 the account's equity curve — the BROKER's history, per period
registerTile(marketDefinition); // #351 which rotation the market is in — served, not computed
registerTile(settingsDefinition);
