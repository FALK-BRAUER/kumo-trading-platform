# backend/

Python backend — FastAPI bridge wrapping a NautilusTrader engine. Hosts the trading lanes, exposes REST +
WebSocket to the UI, and routes orders to the execution client (Backtest/Sandbox now, IBKR later).

Setup: `uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e .` (Python 3.12–3.13;
NOT 3.14 — Nautilus has no wheels yet). Nautilus pinned at 1.229.0. Run the paper spike to verify:
`./.venv/bin/python ../spikes/nautilus_paper/backtest_spike.py`. Holds: api/, strategies/, adapters/, actions/.
