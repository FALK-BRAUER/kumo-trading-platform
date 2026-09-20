# checklist — generic condition-checklist framework

Framework-agnostic mechanism (#181) for "N boolean conditions → score → tier" indicators, mirroring
`../datasource/registry.ts`'s registry shape. Knows nothing about Ichimoku, ADX, or any specific
methodology — that's config, in `@/config/checklists.ts`.

- `types.ts` — `ChecklistCondition`/`ChecklistDef`/`ChecklistResult`, `evaluateChecklist()`.
- `registry.ts` — `registerChecklist`/`getChecklist`/`allChecklists`.

Not here: any concrete condition logic (e.g. "price above weekly cloud") — that's methodology-specific and
lives in config, composed from pure indicator helpers (`@/lib/ichimoku.ts`, `@/lib/adx.ts`).
