# instance-template

Copy this directory into `kumo-cockpit-instances/instances/<name>/` and edit it. **The platform never
reads anything in here** — it is the contract, stated as a working example.

```
instance.env       identity, providers, ports, gates
feed.toml          data/exec provider, engine, venue, universe   (KUMO_FEED_CONFIG points at it)
settings/*.json    budgets, slots, universes, enable flags       (seeded into the settings volume)
sources/           optional: scripts that refresh pool sources
versions.lock      COCKPIT_REF, STRATEGIES_REF
```

## The values here are ILLUSTRATIVE, never a copy of a real deployment

Synthetic, not scrubbed. A scrubbed copy fails twice: the scrubbing can miss a key, and a reader cannot
tell which numbers were guidance and which were leftovers.

The test: **would this be wrong for a stranger to run as-is?** If it would trade someone else's universe
with someone else's position sizes, it is a copy. Gates off, illustrative amounts, three-name universes,
no identities.

## Two names, deliberately separate

`COMPOSE_PROJECT_NAME` names containers and networks. `KUMO_VOLUME_PREFIX` names **volumes**, which
carry state. Docker creates an empty volume silently when a name changes — a fresh Postgres and an empty
Nautilus cache against open positions, with no error. So the volume prefix is pinned per instance and
never changes again, whatever the project is later renamed to.

## Secrets

Never here. They come from the macOS keychain at deploy time. `feed.toml` names environment *variables*
(`key_env`, `secret_env`), never values.
