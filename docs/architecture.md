# Architecture

```
ui (Next.js) ──REST/WS──▶ backend (FastAPI) ──▶ NautilusTrader node ──▶ broker (paper)
                                │                       │
                                ▼                       ▼
                             redis stream            execution store (postgres)
```

- The backend hosts one Nautilus `TradingNode` per instance. Strategies are Nautilus `Strategy`
  subclasses installed from `kumo-trading-strategies`.
- The UI is render-only. It never computes a trading number; it displays what the backend publishes.
- The instance directory is the single source of every knob; the backend refuses to start if it and
  the compose stack disagree.
