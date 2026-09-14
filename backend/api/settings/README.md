# api/settings

Settings framework (#49) — per-domain config as a **JSON Schema** (the contract, in `../../config/settings/`)
+ a **JSON values file** (overrides, on the `KUMO_SETTINGS_DIR` volume).

- `store.py` — `resolve(domain)` (defaults+file, coerce-never-crash), `save(domain, values)` (strict
  validate → `SettingsError`/422 → atomic write), `domains()`, `load_schema(domain)`.
- Consumed by: `app.py` (`GET/PUT /settings/{domain}`) and the engine (reads `alpaca.feed`).

What goes here: settings load/save/validate logic. What doesn't: the schemas (repo `config/settings/`),
the values (the volume), secrets (keychain/env — never in settings).
