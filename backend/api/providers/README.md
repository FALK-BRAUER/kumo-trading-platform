# api/providers/

Data-provider registry: maps a `[data].provider` name (from `config/feed.toml`) to the Nautilus
live data client that gets registered on the `TradingNode`.

- `base.py` — `DataClientSpec` / `ExecClientSpec` (client id, Nautilus config, factory).
- `databento.py` — data `build()` for Databento's native live client (no instrument provider → no live
  definition await; the backfill strategy preloads historical definitions instead).
- `ibkr.py` — exec `build()` for IBKR's native client. Connects to a running IB Gateway (docker, paper —
  start it with `scripts/ib_gateway.py`); reconciliation loads account positions into the cache.
- `gated_exec.py` — `install_budget_gate`: wraps `_submit_order` on the exec client a vendor factory
  returned, so per-strategy sleeves are enforced on a client this repo did not write (#782).
- `ib_refless_orders.py` — `install_refless_order_filter`: wraps `_client.get_open_orders` so one IB
  order with no usable `orderRef` cannot kill the whole order-status batch, and publishes what it
  skipped on `UNRECONCILED_TOPIC` (#785 — #643 on the IBKR side).
- `asset_meta.py` — the venue-neutral `asset_meta(instrument)` accessor over `Instrument.info`
  (name / asset_type / exchange); the ONE place that knows each adapter's info shape (#624/#647).
- `__init__.py` — the registries + `build_data_client_spec` / `build_exec_client_spec`.

Add a provider: new module with `build(...) -> *ClientSpec`, add one line to the matching registry.
Write an out-of-tree Nautilus adapter only if the vendor has no native one (no-fork rule). To
correct a vendor client's behaviour, add an `install_*` that wraps a bound method on the instance
its own factory returned — never subclass its constructor and never edit the adapter.

Does NOT hold: strategy logic (→ `strategies/`).
