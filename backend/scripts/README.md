# scripts/

Operational helper scripts for the backend.

- `run-api.sh` — launch the FastAPI/Nautilus backend with secrets injected from the macOS keychain
  (`DATABENTO_API_KEY` ← service `databento-kumo-trading-platform`). Use this instead of bare `python -m api` once
  the feed needs the key.
- `run-engine.sh` — launch the Nautilus engine process (Redis publisher; #20).
- `run-tests.sh` — the canonical backend test gate. Runs the default suite AND the `-m engine` replay harness
  (#76) in two separate processes (a second BacktestEngine per interpreter segfaults). Bare `pytest` skips the
  harness — always use this script in CI / before pushing.
- `session_watch.py` — the session monitor (#390): one compact line plus anomalies. Counts protection
  from the BROKER (`GET /trades` -> `broker_protected`), never from the orders cache — the cache-derived
  count reported "10 held 14 stops" when the broker had nine. Lives here rather than in a scratchpad
  because it is the alarm channel and shipped three defects in two days while unreviewed.
- `export_openapi.py` — dump the OpenAPI schema.
- `export_bars.py` — freeze a **raw (unadjusted)** historical OHLCV snapshot + manifest for out-of-tree
  backtesting (`kumo-trading-strategies`). Cockpit exports the data and owns the `TICKER.MIC` identity; strategy
  rules and backtests live in `kumo-trading-strategies`. Reads Alpaca keys from the environment only — never the
  keychain or `.env`. Adjusted prices are not reachable from this path by design (`api/providers/alpaca/
  test_http.py`), and corporate-action validation is the consumer's gate, not this script's.
- `export_instruments.py` — freeze Alpaca's tradable asset definitions (real MIC venue, tick size,
  shortable/fractionable flags) for the same seam. Without it a backtest builds synthetic instruments
  and its ids cannot be reconciled against live fills — 142 of the 220 names the momentum strategy
  trades are NYSE, not NASDAQ.

- `probe_trailing_replace.py` — a one-off measurement, not a tool. Answers the two questions Alpaca's
  docs leave open and that gate #245/#252/#169: does a trailing stop's high-water mark survive a
  `PATCH` replace, and can `qty` be replaced on a trailing stop at all. Places ~$30 of paper notional for
  a couple of minutes; refuses to run against a non-paper endpoint or outside regular hours. Dry run by
  default, `--live` to measure.

Does NOT hold: application code (→ `api/`), strategy/adapter code.

`derive_qc345_universe.py` — fetch Alpaca assets + daily bars and let `QC345ComputedSource` derive
the ranking universe (#324). Read-only: it submits nothing and writes nothing. Reports substrate
size against the ~5,523 measured in issue 43, what the selection actually needed, and
whether the preservation floors have gone stale. `--limit-substrate` is a smoke test only and
suppresses the floor check, because a truncated pool makes the weakest selected name arbitrarily
weak and would manufacture a false finding.
- `repair_cache_807.py` — repair the durable Nautilus cache after corpse stops fired (#807): revives REJECTED-but-resting/filled
  orders to what the venue says, rebuilds the poisoned positions, verifies per symbol against the venue. Dry-run by default;
  `--apply --engine-stopped` writes after a DUMP backup. Runs inside the engine image with the engine STOPPED.
- `merge_gate.py` — THE merge gate (Actions is unfunded, permanently): head == upstream, resolved kumo_strategies vs pin stated, suite summary PARSED (red refuses) against the editable tree AND against the pin in a throwaway checkout (anchor asserted, both counts, a difference is a FINDING), head unmoved before merge, evidence posted, merged == reviewed asserted. `python scripts/merge_gate.py <pr> [--merge] [--skip-pin]` — run it with the venv's python.
