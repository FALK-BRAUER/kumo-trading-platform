# lib — shared UI logic

Pure, framework-agnostic helpers used across tiles: `ichimoku.ts` (levels/signal/cloud position),
`market.ts` (session hours), `autoselect.ts`/`orderModes.ts`/`prefill.ts` (order ticket logic),
`config.ts`/`utils.ts`. Subdirectories (`api/`, `chart/`, `design/`, `framework/`, `order/`) have their
own READMEs where the grouping needs more than a file list explains.

Not here: React components (`@/components`) or concrete tiles (`@/tiles`) — this is logic they import,
not JSX.
