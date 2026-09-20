# kumo-trading-platform

Kumo is an operator platform for running multiple algorithmic trading strategies on one
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) engine.

It is built for the part of trading automation that usually breaks quietly: ownership, capital
allocation, broker reconciliation, protective exits, stale data, and numbers that look precise while
nobody can prove where they came from.

The platform is broker-adapter bound, not broker-specific. Strategies produce deterministic
decisions. Nautilus owns the execution boundary. Kumo coordinates strategy lanes, budgets,
protection, attribution, observability, and the operator cockpit around that runtime.

This public release is for paper trading, local operation, and engineering review. It is not a hosted
service, not investment advice, and not an AI trading bot.

```text
   RESEARCH                     DECISION                        OPERATIONS
   --------                     --------                        ----------

  +--------------+        +------------------------+       +----------------+
  | RESEARCH LAB |        |    CONTRACT LAYER      |       |    OPERATOR    |
  | hypotheses,  |------->| identity - decisions   |       |    COCKPIT     |
  | features,    |        | orders - fills         |       |  render-only,  |
  | kill criteria|        +-----------+------------+       |  derives       |
  +--------------+                    |                    |  nothing       |
                                      v                    +-------^--------+
  +--------------+        +------------------------+               |
  |  EVIDENCE    |------->|   DECISION ENGINES     |       +-------+--------+
  |  ARCHIVE     |        | pure - no broker,      |       |   READ MODEL   |
  |  backtests,  |        | no I/O, no UI state    |       | query-only,    |
  |  replays     |        +-----------+------------+       | never writes   |
  +--------------+                    |                    +-------^--------+
                                      v                            |
                          +------------------------+       +-------+--------+
                          |  EVALUATION LEDGER     |       |   STATE BUS    |
                          | replay + backtest of   |       | display-only   |
                          | the SAME decisions     |       | and droppable  |
                          +-----------+------------+       +-------^--------+
                                      |                            |
                                      v                            |
                          +------------------------+               |
                          |   RUNTIME ADAPTER      |               |
                          | decisions -> orders    |               |
                          +-----------+------------+               |
                                      |                            |
        replay - paper - live ---- one decision contract ----      |
                                      v                            |
   +-------------------------------------------------------------------------+
   |                         NAUTILUS RUNTIME                                |
   |                 one node - shared feed, cache, account                  |
   |                                                                         |
   |   STRATEGY LANES                                                        |
   |   strategy A    strategy B    strategy C    manual lane    ...          |
   |      +-------------+-------------+-------------+-------------+          |
   |                                  v                                      |
   |   COORDINATOR      capital sleeves - order arbitration - quarantine     |
   |                                  v                                      |
   |   RISK GATE        slot count - exposure - budget limits - no leverage  |
   |                                  v                                      |
   |   STOP MANAGER     place the NEW stop, then cancel the OLD              |
   |                                  v                                      |
   |   POSITION BOOK    one net position per instrument per strategy         |
   |                    P&L from the book itself, not a side ledger          |
   |                                  v                                      |
   |   CYCLE PROJECTION held - armed - watch - closed, durable across flats  |
   +------------------+-----------------------------------+------------------+
                      | MARKET DATA                       | EXECUTION
                      | Nautilus adapters                 | Nautilus adapters
                      | or custom packages                | or custom packages
                      v                                   v
                  broker/data venues supported by NautilusTrader

                  ========= BROKER NET POSITION =========
                   the hard reconciliation anchor;
                   everything above it is platform attribution
```

## Why It Exists

A broker account sees one net position per instrument. A multi-strategy operator needs a different
view:

```text
        Broker view                         Kumo view
        -----------                         ---------

        AAPL +100                           AAPL +40  strategy lane A
                                            AAPL +60  strategy lane B

        one average price                   separate entries
        one net quantity                    separate budgets
        one account P&L                     separate strategy P&L
        one position to close               separate owners and exits
```

Kumo treats that split as core state. Every order and position is tied to account, client,
instrument, strategy, and cycle. The broker remains the source of truth for what exists; Kumo owns the
attribution, controls, projections, and checks that make one-account multi-strategy operation usable.

## What The Platform Does

- **Runs many strategies on one account.** Strategy lanes share one engine, account, feed, cache, and
  broker session while keeping separate capital, positions, protective orders, and P&L.
- **Supports overlapping ownership.** Two strategies can hold the same symbol at the same time, each
  with its own entry, size, protection, and exit path.
- **Allocates capital per strategy.** A strategy can be funded, armed, disarmed, ramped up, or wound
  down without flattening the whole book.
- **Adds strategies without rebuilding the platform.** A new strategy brings decisions; the platform
  already supplies capital controls, protection, arbitration, monitoring, and operator surfaces.
- **Uses one decision contract.** The same deterministic decisions can be evaluated in backtests,
  paper trading, and runtime adapters, then replayed against recorded data.
- **Separates decision code from venue access.** Strategies do not call brokers directly. Orders,
  instruments, fills, cache state, and account reads flow through Nautilus and its adapters.
- **Treats manual trading as a lane.** Operator-originated fills can be claimed and tracked so they do
  not silently corrupt automated strategy attribution.
- **Tracks positions as trades, not just fills.** A position has an opening, a lifecycle, protection,
  ownership, P&L, and a close; it can survive partial fills and re-entry without becoming an
  anonymous row.
- **Moves stops defensively.** When a stop is changed, the replacement is placed before the old one is
  cancelled, avoiding an intentional unprotected window.
- **Keeps the UI out of the order path.** The cockpit renders backend state and sends operator
  commands; it does not derive positions, own P&L, or rebuild state from a browser event stream.
- **Makes the display stream disposable.** WebSocket updates can drop or stop without changing the
  book, because the read model is queryable and the engine remains authoritative.
- **Runs as instances, not scripts.** Each environment has its own compose project, ports, volumes,
  database, cache, feed config, settings, secrets manifest, and version pins. The deployment file is
  the environment.
- **Keeps market data and execution separable.** Venue changes and custom adapters belong at the
  Nautilus connector boundary, not inside strategy logic.
- **Makes failures loud.** Missing prices, stale feeds, unreadable orders, unknown ownership, absent
  frames, and impossible reconciliations become named states instead of quiet fallbacks.
- **Keeps machine-readable evidence.** Logs and ledgers are designed so humans and tools can inspect
  the same record instead of reverse-engineering text output.

## Feature Map

```text
        +----------------------+        +----------------------+
        | Strategy Lanes       |        | Managed Book         |
        | budgets              |        | ownership claims     |
        | arm / disarm         |        | cycle attribution    |
        | settings             |        | projected P&L        |
        | ramp / wind-down     |        | broker reconciliation|
        +----------+-----------+        +-----------+----------+
                   |                                |
                   v                                v
        +----------------------+        +----------------------+
        | Protection           |        | Operator Cockpit     |
        | stop state           |        | positions            |
        | exit release         |        | orders               |
        | oversize detection   |        | health               |
        | venue-read checks    |        | command surfaces     |
        +----------+-----------+        +-----------+----------+
                   |                                |
                   v                                v
        +----------------------+        +----------------------+
        | Runtime Services     |        | Observability        |
        | Redis cache/bus      |        | structured logs      |
        | Postgres state       |        | ledgers              |
        | settings store       |        | reconciliation       |
        | merge/preflight gate |        | alerts               |
        +----------+-----------+        +-----------+----------+
                   |                                |
                   +--------------+-----------------+
                                  |
                                  v
                       +----------------------+
                       | Nautilus Runtime     |
                       | order lifecycle      |
                       | fills and cache      |
                       | account state        |
                       | adapter boundary     |
                       +----------------------+
```

## Broker And Data Access

Kumo does not hard-code a trading venue into strategy code. Broker and market-data access belong at
the Nautilus boundary.

NautilusTrader ships adapters for multiple execution venues and data providers. Kumo can use those
where available, and custom adapters can be installed as ordinary runtime packages when a venue is not
covered. Changing a venue should change instance configuration and adapter wiring, not the strategy
decision engines.

## Repository Layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app, Nautilus node host, runtime services, provider glue, tests |
| `ui/` | Next.js cockpit for positions, orders, P&L, health, protection, and controls |
| `deploy/` | Dockerfiles and compose support for local paper-trading stacks |
| `instances/example/` | Public example instance config, env template, feed settings, version pins |
| `bin/` | Public-tree and release-safety checks |
| `docs/` | Architecture notes, operational guidance, and design records |

Companion packages:

- `kumo-trading-strategies`: strategy contracts, decision engines, runtime adapters, replay runners,
  and backtest evidence.
- Custom Nautilus adapter packages: optional connector packages for venues not covered by the
  Nautilus distribution.

## Quick Start

Clone the two repositories as SIBLINGS — the docker build reads the strategies checkout from
`../kumo-trading-strategies` (`deploy/compose.paper.yml`, `additional_contexts: strategies`):

```text
<workdir>/
├── kumo-trading-platform/      this repository
└── kumo-trading-strategies/    the strategies package, at the tag backend/pyproject.toml pins
```

Backend — install and run the tests (no services needed):

```bash
cd backend
uv sync --all-extras
cp ../instances/example/instance.env .env
uv run pytest -q
```

UI:

```bash
cd ui
npm ci
npm run dev
```

Paper stack — Redis, Postgres, the engine, the api and the UI. It needs an **Alpaca paper account**:
put `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` into `deploy/.env.paper` (the compose file marks both
`required`; a blank key boots a stack that can reach nothing).

```bash
cd deploy
cp .env.paper.example .env.paper        # then fill in the two Alpaca paper keys
docker compose --env-file .env.paper -f compose.paper.yml up --build
```

Running the api outside docker (`cd backend && uv run ./scripts/run-api.sh`) needs Redis and Postgres
reachable at their default ports — the compose stack above brings both; start it first, or point the
api at your own.

See `deploy/README.md` and `instances/example/README.md` for the compose and instance layout.

## Safety Boundaries

- This repository is published for **paper trading and local operation**.
- It has **no built-in public authentication layer**. Bind it to localhost or put it behind your own
  private network and authentication.
- It is designed as a **single-operator stack**, not a multi-tenant service.
- It does not include private instances, credentials, account identifiers, raw market-data dumps,
  production captures, research notebooks, or operating diaries.
- It does not use AI at runtime to choose trades.

```text
        Public boundary

        included                         excluded
        --------                         --------
        platform runtime                 private deployments
        render-only cockpit              account identifiers
        public example instance          credentials and local paths
        tests and invariants             raw market data
        release hygiene checks           research notebooks
```

## Development Checks

```bash
KUMO_PUBLIC_DENYLIST=/path/to/denylist.txt bash bin/check-public-tree.sh
cd backend && uv run pytest -q
cd ui && npm test -- --run
```

The merge gate lives in `backend/scripts/merge_gate.py`. Public-release hygiene is enforced by
`bin/check-public-tree.sh`.

## Disclaimer

This repository is research software. Nothing in it is investment, trading, financial or legal advice, an
offer or a recommendation to buy or sell any security, or a claim that any strategy it runs is profitable.
It is published for paper trading, local operation and engineering review. It CAN place real orders if an
operator connects it to a live brokerage account; whoever does so does it on their own responsibility, after
their own review, and in compliance with their own jurisdiction's rules and their broker's terms. Trading
securities involves risk, including the loss of the amount invested and, for short positions, losses beyond it.

The software is provided under the LGPL-3.0-or-later **without warranty of any kind** (see `LICENSE`). The
authors are not registered investment advisers or broker-dealers and accept no liability for any order,
decision or loss arising from its use. Interactive Brokers and Alpaca are trademarks of their owners; this
project is not affiliated with or endorsed by them or by NautilusTrader. Market data belongs to its providers
and is subject to their terms.

## License

LGPL-3.0-or-later. See `LICENSE` and `LICENSE.GPL-3.0`.

This project links NautilusTrader as an unmodified library. NautilusTrader is licensed under
LGPL-3.0; its terms apply to NautilusTrader itself.
