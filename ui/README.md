# ui/

Next.js (App Router, TS strict) cockpit — render-only. Forked from kumo-trader's UI, with sim/strategy/results
tabs stripped. Talks to `api/` over REST + WebSocket only; never touches the broker or a DB directly.

Composable tile/tab framework: Tile Registry + Data Source Registry + react-grid-layout; layouts are DB config,
editable at runtime. Reuses `MasterCard`, `IchimokuChart`, `OrderPanel` as the first tiles. Charts subscribe to
WS bar topics (no polling → no 429). Holds: components, tiles, app routes, data-source bindings.
Does NOT hold: trading logic, broker calls, server-side secrets.
