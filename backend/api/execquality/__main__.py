"""CLI: record execution quality for past sessions. Read-only against Alpaca; never submits anything.

    python -m api.execquality --since 2026-08-03 [--out /data/execquality.csv] [--dry-run]

Safe to re-run: `append_csv` is idempotent on (session, symbol, side), so a cron that repeats a day
adds nothing. Intended to run after the close, not during a session — it has no bearing on trading
either way, but there is no reason to spend an API call while the engine wants them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from api.execquality.measure import append_csv, compare, summarise

DEFAULT_OUT = Path(os.environ.get("KUMO_EXECQUALITY_CSV", "/data/execquality.csv"))


async def collect(since: str) -> list:
    from api.feed_config import load_feed_config
    from api.instrument_search import build_search_client

    http = build_search_client(load_feed_config().provider_config)
    await http.connect()
    try:
        acts = await http._get(http._trading, "/v2/account/activities/FILL",
                               {"after": since, "page_size": 100})
        if not acts:
            return []
        symbols = sorted({str(a["symbol"]).upper() for a in acts if a.get("symbol")})
        days = sorted({str(a["transaction_time"])[:10] for a in acts if a.get("transaction_time")})
        # `feed=sip` is the only feed auctions are published on.
        auctions = await http._get(http._data, "/v2/stocks/auctions",
                                   {"symbols": ",".join(symbols), "start": days[0], "end": days[-1],
                                    "feed": "sip", "limit": 10000})
        return compare(acts, auctions)
    finally:
        await http.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    legs = asyncio.run(collect(args.since))
    if not legs:
        print("no measurable legs — no fills, or none within the lag window")
        return 0

    for leg in legs:
        print(f"{leg.session} {leg.symbol:<6} {leg.side:<4} lag {leg.lag_minutes:6.1f}m "
              f"fill {leg.fill_vwap:10.4f} open {leg.auction_px:10.4f} "
              f"{leg.drift_bps:+8.1f} bps {leg.cost_usd:+9.2f}")
    print()
    print(json.dumps(summarise(legs), indent=1))
    if not args.dry_run:
        print(f"\nappended {append_csv(args.out, legs)} new legs to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
