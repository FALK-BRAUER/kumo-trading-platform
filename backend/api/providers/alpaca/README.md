# providers/alpaca

Out-of-tree Nautilus **Alpaca** market-data adapter (pure Python, no SDK, no pyo3 — runs on the stock
`nautilus_trader` wheel). Alpaca is the cockpit's IBKR-independent data stream (issue #23): the live IBKR
account permanently holds the IBKR market-data entitlement, so data must come from elsewhere.

**Holds:**
- `config.py` — `AlpacaDataClientConfig` (keys, base URLs, `feed` iex/sip).
- `http.py` — thin async REST client (account, assets, historical bars + pagination).
- `providers.py` — `AlpacaInstrumentProvider`: `/v2/assets` → Nautilus `Equity` at canonical `TICKER.MIC`.
- `data_client.py` — `AlpacaDataClient` (`LiveMarketDataClient`) + factory + `build_data` for the registry.
  Connect, instrument definitions, historical bars (REST), live bars (WS).
- `websocket.py` — `AlpacaWebSocketClient`: connect/auth/subscribe/reconnect transport for live frames.
- `exec_client.py` — `AlpacaExecutionClient` (`LiveExecutionClient`) + factory + `build` (exec). Order
  submit/cancel/modify over the trading REST API, account-state + order/position reconciliation reports.
  One client serves every MIC (`routing.default=True`). US equities only.
- `test_*.py` — offline unit tests (timeframe/bar parse, WS subs/dispatch, order-request mapping).

Live data channels: `bars` (minute) + `dailyBars`. Alpaca has **no hourly WS channel** → 1h bars are
REST-history-only (no live). One MD connection per key.

Execution is REST + reconciliation. Fills reconcile per-execution from Alpaca's account activities (FILL) →
Nautilus `FillReport`s (#75): `transaction_time` is the authoritative window bound, `trade_id` = the activity
id (idempotent across reconciliation runs). A real-time account trade-updates WebSocket (push fills between
reconciliation ticks) is a later increment. Options/crypto/margin/multi-leg from PR #3375 are dropped
(equities + MANUAL strategy only). This client never originates orders — it only acts on commands the node
routes to it (an explicit MANUAL click). Paper by default.

**Does not hold:** the account trade-updates WS (real-time push fills — later); trade corrections/busts
(not emitted in Alpaca's paper FILL feed). Secrets never live here — injected from env
(`APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`) by the launch scripts from the keychain.

Dormant until `config/feed.toml` sets `[data].provider = "alpaca"` (baseline stays `databento`).
