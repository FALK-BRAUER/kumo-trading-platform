# deploy/ — containerized paper + prod stacks (#21)

Two **physically isolated** systems. Each is a separate Docker Compose project with its own network,
volumes, and ports. There is **no runtime mode flag** — the compose file *is* the environment, so you
cannot accidentally point paper config at the live account.

| | paper | prod (live) |
|--|--|--|
| launch | `make up-paper` | `make up-prod` (type `LIVE`) |
| compose project | `kumo-paper` | `kumo-prod` |
| gateway | paper, port 4002 | live, port 4001 |
| IBKR user | the paper login (one session per login) | **live secondary user** |
| UI / API | `:3000` / `:8000` | `:3001` / `:8001` |
| badge | PAPER (amber) | **LIVE (red)** |
| secrets | `.env.paper` | `.env.prod` |

Both can run at the same time without collision.

## Services (per stack)
`redis` (UI bus) · `ib-gateway` (gnzsnz/ib-gateway) · `engine` (Nautilus node) · `api` (FastAPI + Redis
consumer) · `ui` (Next.js).

## Prerequisites
- Docker + Docker Compose. On this Mac, `export DOCKER_HOST=unix://~/.docker/run/docker.sock`.
- Secrets: `cp .env.paper.example .env.paper` (and `.env.prod.example .env.prod`), fill in. Both are
  gitignored. This replaces the macOS keychain used by the local (non-container) run scripts.

## Run
```bash
cd deploy
make build-paper && make up-paper     # → http://localhost:3000
make logs-paper
make down-paper
```
Prod is identical with `-prod`, and `up-prod` refuses unless you type `LIVE`.

## Live secondary user (why the gateway drops)
IBKR allows one session per username. Opening the IBKR phone app as the same user bumps the gateway.
Create a **dedicated secondary user** under the live account (Client Portal → Settings → Users & Access
Rights), grant it API + market data, and put it in `.env.prod`. Your phone keeps the primary user.

**2FA caveat:** a headless gateway can't complete IBKR Mobile 2FA interactively. The secondary/API user
must be set up so auto-login works — IBKR's second-factor handling for API users / trusted-IP. If login
hangs, that's why. (Paper has no 2FA.)

## Safety
- The cockpit builds **no order path** — prod connects for positions + data (read). `READ_ONLY_API=no` is
  required only for Nautilus reconciliation (IB error 321). No orders are submitted.
- Prod UI shows a red **LIVE** badge at all times.
- Do not commit `.env.paper` / `.env.prod`.

## Notes
- Live intraday price movement still needs a real-time data feed (Databento historical lags intraday;
  live license is separate). Tracked outside this ticket.
- `NEXT_PUBLIC_*` are build-time in Next → each stack builds its own UI image (different API base + badge).
