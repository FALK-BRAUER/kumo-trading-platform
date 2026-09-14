# lib/order

The shared, framework-agnostic order **core** — pure TypeScript, no React. Every order surface (the vanilla
tile, the assisted tile #67, the symbol-detail ticket) builds on these so validation and level derivation have
one definition.

- `payload.ts` — order-payload builders + geometry validators (`buildOrderPayload`, `buildBracketPayload`,
  `stopTriggerOk`, `bracketGeoOk`, `Action`/`OrderType` types). WHAT order to submit.
- `indicators.ts` — pure price indicators shared by prefill and the catalog (`atr`, `spreadPct`,
  `breakoutLevel`, `roundTick`). Extracted so `prefill.ts` and `mechanisms.ts` don't import each other.
- `mechanisms.ts` — the prefill-mechanism catalog (#65): a `(leg, id)`-keyed registry of entry/stop/target
  level-derivation mechanisms. Each computes ONE raw candidate; rounding/validation/fallback are the caller's.
  WHERE each price sits. Sibling axis to the backend order-action registry (#50, WHAT order).
- `*.test.ts` — alongside each source.

Goes here: order payload/validation/level-derivation primitives reused across order surfaces.
Does NOT go here: the orchestration that composes them (`@/lib/prefill` — sizing + fallback policy), tile
components (`@/tiles/order`), or the command-layer client (`@/lib/api/client`).
