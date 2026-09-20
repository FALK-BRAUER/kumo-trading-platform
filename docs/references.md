# External references — code to mine, not depend on

Repos/resources worth reading for patterns. **Reference only** — none are dependencies. Lift ideas under
their license; don't adopt the stack.

## Nautilus-Web-Interface (Black101081)
- **Repo:** https://github.com/Black101081/Nautilus-Web-Interface — MIT, React/Vite + FastAPI over a
  `NautilusTradingSystem` wrapper. Solo author, ~35★, last push 2026-03-21.
- **Why not adopt:** market data = Binance public API (crypto) — wrong market (we're US equities/ETFs via
  Databento). SQLite for alerts (we use Postgres). Its data path is a hand-wrapped FastAPI bridge, same
  category as ours, and does **not** use Nautilus MessageBus external streaming — so it doesn't advance our
  UI-data abstraction (see CLAUDE.md "UI-data abstraction").
- **What's worth mining (MIT):**
  1. **Admin / engine panel** — engine components, adapters, system monitoring, DB ops. An ops surface our
     cockpit lacks; read how they wired it to the running node.
  2. **Backtest-run UI** patterns — but backtests belong in **predecessor-repo** (sim/research arm), not this live
     cockpit. Use this as a reference *there*, if/when we build a backtest UI.
