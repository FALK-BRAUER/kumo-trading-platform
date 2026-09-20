# config/

Runtime configuration as data, not code — edit to change behaviour without touching `api/`.

- `feed.toml` — market-data feed: `[data].provider` + the provider's table, backfill window, engine
  venue/trader id, the symbol universe, and the chart range→candle defaults. Read by `api/feed_config.py`.

Does NOT hold: Python code (→ `api/`), secrets (→ macOS keychain, injected by `scripts/run-api.sh`).
