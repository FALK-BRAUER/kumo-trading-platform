# Plan — #21 Containerized packaging: isolated paper + prod stacks

## Objective
Two physically isolated containerized systems (paper, prod). No runtime mode toggle — each stack hardwired
to its environment. Paper reuses the current paper user unchanged. Prod uses the live secondary user.

## Non-goals (this ticket)
- No order path on prod (cockpit submits no orders; unchanged).
- No cloud/remote deploy target yet — local Docker on the Mac first; images are portable for later.
- No CI pipeline yet (follow-up).

## File layout (all new under `deploy/`)
```
deploy/
  Dockerfile.backend        # engine + api (one image, command selects role)
  Dockerfile.ui             # Next.js standalone
  compose.paper.yml         # hardwired paper stack (project: kumo-paper)
  compose.prod.yml          # hardwired prod stack  (project: kumo-prod)
  .env.paper.example        # template — real .env.paper is gitignored
  .env.prod.example
  Makefile                  # up-paper / up-prod (LIVE confirm) / down / logs
  README.md                 # prerequisites, secrets, run, safety
```

## Images
### Dockerfile.backend (python:3.13-slim)
- Install deps in a source-independent layer (explicit list, good caching):
  `nautilus_trader[ib]==1.229.0 databento fastapi uvicorn[standard] websockets redis`.
- Copy source (`api/ strategies/ adapters/ actions/ config/`), `PYTHONPATH=/app`, run from source so
  `feed_config` resolves `/app/config/feed.toml` (do NOT pip-install the package — the config path is
  relative to the source tree).
- No `docker` lib needed (gateway is a compose service now, not Nautilus-managed).
- Default `CMD python -m api`; the engine service overrides `command: python -m api.engine_node`.

### Dockerfile.ui (node:20-slim)
- `next.config.mjs` → `output: "standalone"`. Multi-stage: build → copy `.next/standalone` + static.
- Runtime env: `NEXT_PUBLIC_API_BASE`, `NEXT_PUBLIC_WS_BASE`, `NEXT_PUBLIC_ENV` (paper|live).

## Compose (two hardwired files — the isolation guarantee)
Each file bakes its env; nothing shared. Services: `redis`, `ib-gateway`, `engine`, `api`, `ui`.
- `ib-gateway`: `ghcr.io/gnzsnz/ib-gateway:stable`, env `TWS_USERID/TWS_PASSWORD/TRADING_MODE`,
  `READ_ONLY_API=no` (reconciliation needs it). Paper exposes 4002, prod 4001 internally.
- `engine`: backend image, `command: python -m api.engine_node`, env: `DATABENTO_API_KEY`,
  `IBKR_ACCOUNT_ID`, `KUMO_IBG_HOST=ib-gateway`, `KUMO_IBG_PORT=4002|4001`, `KUMO_EXEC=ibkr`,
  depends_on redis + ib-gateway.
- `api`: backend image, `command: python -m api`, env `KUMO_ENGINE=live` + redis host; ports paper
  8000 / prod 8001.
- `ui`: ui image; env `NEXT_PUBLIC_ENV=paper|live` + API/WS base; ports paper 3000 / prod 3001.
- `redis`: paper 6379 / prod 6380 (host-mapped; internal to the stack network otherwise).
- `COMPOSE_PROJECT_NAME` kumo-paper / kumo-prod → isolated networks + volumes.

## Code changes (small, additive)
1. `api/providers/ibkr.py`: read `ibg_host`/`ibg_port` from env (`KUMO_IBG_HOST`/`KUMO_IBG_PORT`) with the
   config value as fallback — so the container points at the `ib-gateway` service.
2. `ui/next.config.mjs`: `output: "standalone"`.
3. `ui/src/lib/config.ts`: API/WS base + `ENV` from `NEXT_PUBLIC_*` (fallback to today's localhost).
4. UI env badge: drive the existing PAPER badge from `NEXT_PUBLIC_ENV`; LIVE → red.

## Secrets
- Keychain → per-stack `.env` (gitignored). Vars: `DATABENTO_API_KEY`, `TWS_USERID`, `TWS_PASSWORD`,
  `IBKR_ACCOUNT_ID`. `.env.*.example` committed with placeholders.
- `.gitignore`: add `deploy/.env.paper`, `deploy/.env.prod`.

## Safety
- `make up-prod` prompts: refuse unless the user types `LIVE`.
- UI LIVE badge (red) always visible on the prod stack.
- Prod is a separate compose project + separate command — no way to reach it from the paper path.

## Verification (test plan)
1. `docker build -f deploy/Dockerfile.backend` succeeds; `python -m api` imports in-image.
2. `docker compose -f deploy/compose.paper.yml config` validates.
3. `make up-paper` with a real `.env.paper` → api `/health` ok, `/positions` reconciles (paper gateway).
4. UI shows PAPER; live push works.
5. `make up-prod` (type LIVE) with `.env.prod` → reconciles live positions; UI shows LIVE (red); gateway
   stays up while the phone app is open (secondary user).
6. Confirm paper + prod run simultaneously without port/state collision.

## Risks / open points
- `gnzsnz/ib-gateway` image config (env var names, VNC, IBC) — verify against its docs; auto-login + 2FA
  for the LIVE account may need IBKR "second user" without 2FA or an IBKR trusted-IP setup.
- nautilus_trader linux wheel present for py3.13 slim (no compile) — confirm at build.
- Databento key shared across both stacks (historical only) — fine; live data still unsolved (separate).
- First prod connect is real-money-adjacent (read-only in practice). Deliberate, gated.
