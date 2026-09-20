# tiles — concrete tile types

One folder per tile *type*. Each holds the component + its `definition.ts` (the `TileDefinition`:
configSchema, dataSources, default size). Tiles depend only on the framework core (`@/lib/framework`)
and receive data via `TileProps` — they never fetch directly (data wiring is the container's job).

Add a tile: drop a folder here, export a `TileDefinition`, then register it in `@/config/tiles`.

Not here: registration, layouts, data-source→topic mapping (those are `@/config`); generic machinery
(that's `@/lib/framework`).
