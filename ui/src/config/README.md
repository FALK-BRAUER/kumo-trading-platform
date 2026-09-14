# config — composition root

The one place that wires the cockpit together. Imports tiles + framework; nothing imports it except
the app shell.

- `tiles.ts` — registers the tile catalog (one `registerTile(...)` per type).
- `layouts.ts` — seed views (named lists of placed tiles). Replaced by `GET /layouts` at phase 5.
- `datasources.ts` — *(phase 2)* source-name → topic/endpoint/params mapping.

Swap this directory → a different cockpit on the same core. Keep deployment-specific wiring here, not
in tiles or core.
