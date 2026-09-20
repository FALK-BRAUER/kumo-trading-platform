# config/settings

JSON Schema files (Draft 2020-12), one per settings domain (#49) — the source-of-truth contract for each
domain's fields, **enums**, defaults, and bounds. Git-versioned. Read by both the api (validate) and the UI
(render forms). Values (the overrides) do NOT live here — they're on the `KUMO_SETTINGS_DIR` volume.

Add a domain = drop `<domain>.schema.json` here (it auto-registers). One exists: `alpaca` (market-data feed).
Never put secrets in a schema/values — API keys stay in the keychain/env.
