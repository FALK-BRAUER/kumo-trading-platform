# kpi — generic watchlist-row KPI framework

Framework-agnostic mechanism for "named stat → formatted display value", mirroring
`../datasource/registry.ts` and `../checklist/registry.ts`'s registry shape. Knows nothing about volume,
VWAP, or any specific stat — that's config, in `@/config/kpis.ts`.

- `types.ts` — `KpiContext`/`KpiDef`.
- `registry.ts` — `registerKpi`/`getKpi`/`allKpis`.

Not here: any concrete KPI logic (e.g. "volume formatted as 1.2M") — that's config-specific and lives in
`@/config/kpis.ts`, composed from data already on `KpiContext` or pulled from a registered data source.
