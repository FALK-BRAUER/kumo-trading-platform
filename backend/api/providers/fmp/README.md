# providers/fmp

Financial Modeling Prep REST client — watchlist fundamentals KPIs (#182 follow-up, Phase 2: Market Cap,
Beta, EPS, trailing P/E, Dividend Amount). Independent of the execution/market-data providers
(`providers/alpaca/`, `providers/databento/`) — fundamentals is a display-enrichment concern, not a
Nautilus adapter, so it has its own credential (`FMP_API_KEY`) and its own low-frequency timer in
`engine_node.py`, unrelated to `[data].provider`.

**Holds:**
- `http.py` — `FmpHttpClient`: quote, company profile, annual income statement. Single-symbol only — FMP's
  batch/multi-symbol quote endpoints are a paid-tier feature, confirmed 403 on the free tier.

**Does not hold:** an InstrumentProvider, DataClient, or ExecutionClient — deliberately NOT a Nautilus
adapter. Fundamentals barely change intraday, so they don't need Nautilus's subscription/backtest
machinery; if that ever changes (a strategy consumes these values), promote to a real custom `Data`
type + DataClient then, not before.

Secrets never live here — injected from env (`FMP_API_KEY`) by the launch scripts from the keychain, same
convention as Alpaca's `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`.
