# tiles/position-transfer

Move a position, or part of one, into a strategy (#80 spin-off).

- `PositionTransferTile.tsx` — the tile + a pure `PositionTransferView` (mockable in `/dev/ui`).
- `definition.ts` — the `TileDefinition`: binds `external_activity`, pins to one position via
  `instrumentId`/`sourceStrategyId`/`side`, and takes `pricingMode` as config.

Targets come from `@/config/strategies` — never hardcode a strategy list here. Placeable on a board, and
composed into the unclaimed-position detail surface.

Not here: the transfer command itself (`@/lib/api/client`) or the position facts panel (the detail surface).
