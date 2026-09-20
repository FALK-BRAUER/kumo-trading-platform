# components/settings

Settings framework (#49) UI primitives.

- `SchemaForm.tsx` — renders a settings form DYNAMICALLY from a JSON Schema (enum→select, bool→toggle,
  number→bounded input, string→text). No per-setting UI. The backend validates authoritatively on save
  (422 → shown); this is the render + edit surface. Save payload is cleaned via `@/lib/settingsForm`.

Used by `@/tiles/settings/SettingsTile`. Add a settings domain on the backend → it appears automatically.
