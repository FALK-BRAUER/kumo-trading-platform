# kumo-trading-platform

An operator platform for automated equity strategies on
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader): a FastAPI backend that hosts a
Nautilus trading node, and a Next.js UI that renders its state over REST and WebSocket. Paper trading
on Interactive Brokers and Alpaca.

*kumo* (雲) is Japanese for cloud — the Ichimoku cloud the first strategies were built on. The name
stayed; the platform is now five layers: runtime, execution, operations, strategies, UI.

## What it is

- One backend process per broker session (Interactive Brokers paper, Alpaca paper) running a
  NautilusTrader node. Strategies from `kumo-trading-strategies`, the Alpaca adapter from
  `kumo-nautilus-alpaca-adapter`, IBKR via Nautilus's own adapter — all three pinned in
  `backend/pyproject.toml`.
- Execution, protective orders, venue reconciliation and a per-strategy operator view.
- An instance model: everything an instance needs — feed, settings, secrets *names*, pinned versions —
  lives in `instances/<name>/`; see `instances/example/`.
- `deploy/` docker compose stacks; `Makefile` + `bin/` checks that refuse a deploy whose parts disagree.

## What it is not

- **Not authenticated.** The API binds to `KUMO_BIND_HOST` (example default `127.0.0.1`) with open
  CORS. Run it on a private network or behind your own auth. Do not expose it to the internet.
- **Not a live-trading system.** It is used with paper accounts. Nothing here is investment advice
  and nothing here has been validated for real capital.
- **Not a strategy library, not a venue adapter.** Strategies live in `kumo-trading-strategies`
  (public, the whole library); the Alpaca adapter lives in `kumo-nautilus-alpaca-adapter`. This
  repo pins and installs both.
- **Not multi-tenant.** One instance, one broker session, one operator.

## Quick start

```bash
# backend
cd backend
uv sync --all-extras
cp ../instances/example/instance.env .env    # then edit
uv run pytest -q
uv run ./scripts/run-api.sh

# ui
cd ../ui
npm ci
npm run dev
```

Full stack with docker compose: `make up INSTANCE=example` (see `deploy/README.md`).

## Layout

| path | holds |
|---|---|
| `backend/` | FastAPI app, Nautilus node host, adapters, settings framework, tests |
| `ui/` | Next.js cockpit, render-only; types generated from the backend's OpenAPI |
| `deploy/` | docker compose stacks and Dockerfiles |
| `instances/example/` | the instance template: env, feed, settings, secrets manifest, version pins |
| `bin/` | shell checks the Makefile runs before a deploy |
| `docs/` | architecture and engineering principles |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The merge gate (`backend/scripts/merge_gate.py`, also the
required CI check) refuses stale heads and red suites.

## License

LGPL-3.0-or-later. See [`LICENSE`](LICENSE) (LGPL-3.0) and [`LICENSE.GPL-3.0`](LICENSE.GPL-3.0), which
the LGPL incorporates by reference.

This project links [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) as an
unmodified library. NautilusTrader is licensed under the LGPL-3.0; its terms apply to it, not to
this code beyond what the LGPL requires of a work that uses the library. If you redistribute a
combined work, the LGPL's requirements for the library portion apply.
