# example/

The instance template. Copy to `instances/<name>/` and edit. This repository ships no `make up
INSTANCE=` — that target belongs to a private instances repository that is not public. What the public
tree offers is `deploy/Makefile` (`build-paper`, `up-paper`, `verify-stack-paper`, `down-paper`,
`logs-paper`) over `deploy/.env.paper` and `deploy/compose.paper.yml`; copy the values from your
`instances/<name>/instance.env` into `deploy/.env.paper` to run that instance.

- **Goes in:** `instance.env`, `feed.toml`, `secrets.manifest` (names only), `settings/`, `versions.lock`
- **Stays out:** secret values, real account ids, operating notes
