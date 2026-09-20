# api/

FastAPI bridge between the UI and the Nautilus `TradingNode`. Runs in the same Python process as the engine;
reads positions/orders from the Nautilus cache, relays fills/quotes/bars to the UI over WebSocket, and submits
manual/automated orders into the engine.

Holds: REST endpoints, WebSocket topic handlers, request/response Pydantic models, the OpenAPI schema source.
Does NOT hold: trading logic (→ `strategies/`), order primitives (→ Nautilus), UI code (→ `ui/`).
