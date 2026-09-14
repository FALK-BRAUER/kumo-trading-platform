# backend/

The FastAPI application and the NautilusTrader node host.

- **Goes in:** `api/` routes and the node lifecycle, `adapters/` out-of-tree venue/data adapters, `actions/` operator actions, `config/` the settings framework and JSON schemas, `scripts/` operator tools incl. `merge_gate.py`; tests next to the code
- **Stays out:** strategy logic (that is `kumo-trading-strategies`), instance config (that is `instances/`)
