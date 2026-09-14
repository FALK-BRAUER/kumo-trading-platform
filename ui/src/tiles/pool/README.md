# tiles/pool

The symbol pool MOMENTUM ranks each session, with the operator whitelist (pin) and blacklist
(exclude) layered on top, plus per-source freshness.

Holds: `PoolTile.tsx` (render + REST writes), `definition.ts`, `schema.ts`.
Does NOT hold: the pool rules themselves — a pin surviving a refresh, an exclude beating every
source — those live in `kumo-strategies` (`PgSymbolPool`) and reach here via `GET /pool`.
