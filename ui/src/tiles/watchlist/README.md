# tiles/watchlist

The Watchlist tile — monitor a config-driven list of symbols (no position).

- `definition.ts` — type/config schema (`{ symbols: string[] }`)/default size. Symbols are config, not a backend endpoint.
- `WatchlistTile.tsx` — header + bordered list of `WatchRow`s. Each row subscribes its `bars` WS topic, shows last price + Ichimoku cloud state + signal, expands to the embedded chart.

Goes here: the watchlist tile's component + contract. Symbols are edited in `@/config/layouts`. Only
symbols the backend feeds show live data (today: AAPL.XNAS, MSFT.XNAS — broader universe = market-data phase).
