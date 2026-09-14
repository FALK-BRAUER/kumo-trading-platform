# tiles/settings

The **Settings** tile (#49) — schema-driven settings surface.

- `SettingsTile.tsx` — lists the backend's settings domains (`GET /settings`) and renders a form per domain.
- `definition.ts` — the tile contract (type `settings`, no DataSource).

Form rendering lives in `@/components/settings/SchemaForm` (generic JSON-Schema → form). Add a settings
domain = drop a `<domain>.schema.json` on the backend; it appears here automatically, no UI change.
