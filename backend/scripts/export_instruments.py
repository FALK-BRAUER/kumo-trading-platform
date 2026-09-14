"""Freeze Alpaca's tradable asset definitions for out-of-tree backtesting.

Companion to `export_bars.py` and the same seam: Cockpit owns the provider connection and the
canonical `TICKER.MIC` instrument identity, so it exports; kumo-strategies consumes. A backtest
built on synthetic instruments (every name stamped XNAS, invented tick sizes) cannot be reconciled
against live fills — the ids do not even match.

Usage (from `backend/`, Alpaca keys in the environment):
    python -m scripts.export_instruments --symbols AAPL,MSFT --out ../data/instruments.json
    python -m scripts.export_instruments --all --out ../data/instruments.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.alpaca.providers import EXCHANGE_TO_MIC

TRADING = os.environ.get("ALPACA_TRADING_BASE", "https://paper-api.alpaca.markets")
DATA = os.environ.get("ALPACA_DATA_BASE", "https://data.alpaca.markets")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma-separated; omit with --all")
    ap.add_argument("--all", action="store_true", help="export every tradable us_equity")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    key, sec = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not sec:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY must be in the environment")

    want = None if a.all else set((a.symbols or "").split(","))
    c = AlpacaHttpClient(key, sec, TRADING, DATA)
    await c.connect()
    try:
        assets = await c.list_assets()
    finally:
        await c.close()

    out = {}
    skipped_venue, skipped_untradable = [], []
    for x in assets:
        s = x.get("symbol")
        if x.get("class") != "us_equity" or (want is not None and s not in want):
            continue
        if not x.get("tradable"):
            skipped_untradable.append(s); continue
        mic = EXCHANGE_TO_MIC.get(x.get("exchange", ""))
        if mic is None:
            skipped_venue.append((s, x.get("exchange"))); continue
        out[s] = {
            "symbol": s,
            "mic": mic,                                   # real venue, not a blanket XNAS
            "exchange": x.get("exchange"),
            "name": (x.get("name") or "").strip() or s,
            "fractionable": bool(x.get("fractionable")),
            "shortable": bool(x.get("shortable")),
            "easy_to_borrow": bool(x.get("easy_to_borrow")),
            "marginable": bool(x.get("marginable")),
            "min_order_size": x.get("min_order_size"),
            "min_trade_increment": x.get("min_trade_increment"),
            "price_increment": x.get("price_increment") or "0.01",
        }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(f"wrote {a.out} ({len(out)} instruments)")
    if want:
        missing = sorted(want - set(out) - set(skipped_untradable))
        if missing:
            print(f"  not found: {len(missing)} -> {missing[:12]}")
    if skipped_untradable:
        print(f"  skipped, not tradable: {len(skipped_untradable)}")
    if skipped_venue:
        print(f"  skipped, unmapped venue: {len(skipped_venue)} e.g. {skipped_venue[:5]}")


if __name__ == "__main__":
    asyncio.run(main())
