# framework — tile framework core

Generic, tile-agnostic machinery for the composable dashboard (#7). Knows nothing about any concrete
tile (portfolio/chart/order).

- `types.ts` — the tile contract: `TileDefinition`, `TileProps`.
- `registry.ts` — `type → TileDefinition` map.
- `container.tsx` — `TileContainer`: lookup → validate config → render + chrome.
- `store/` — the one Zustand store (sliced: layout, ui; live-data slice lands phase 3).
- `layout/` — Zod `Layout`/`PlacedTile` schema + versioned migration.

**Dependency rule:** core imports nothing downstream. Tiles (`@/tiles`) import core; config (`@/config`)
imports both. Don't import a tile or config from here.

Not here: concrete tile components, registration, layout/data wiring — those live in `@/tiles` / `@/config`.
