"""Derive QC345's live ranking universe from Alpaca and report what it took (#324).

Run it, read the numbers, and only then decide whether cockpit should do this automatically. That
ordering is the point: issue 43 supplied the bound from LOCAL evidence, and this is the
measurement against the live account. Until the two agree, `strategies.QC345_UNIVERSE` stays manual
and an empty one still refuses to start.

    export APCA_API_KEY_ID=$(security find-generic-password -s alpaca-paper-key -w)
    export APCA_API_SECRET_KEY=$(security find-generic-password -s alpaca-secret-paper -w)
    PYTHONPATH=~/projects/kumo-trading-strategies/src:. \\
        .venv/bin/python scripts/derive_qc345_universe.py --days 420

    --write   also print the settings JSON to paste into strategies.QC345_UNIVERSE

Reads nothing, writes nothing, submits nothing. It fetches and reports.

WHAT TO LOOK AT, in order of what would change the plan:

  substrate         should land near the measured ~5,523. An order-of-magnitude miss means the asset
                    filter is not doing what it did when the research ran.
  rankable          measured min 4,569 / median 4,885 / max 5,460 per rebalance.
  min price /       the floors are `price >= 67.08` and 21-session median dollar volume
  min liquidity     `>= 518,190,000.09`. If a SELECTED name sits below either, the floor is stale —
                    that is the finding, and it is why the fetch is not prefiltered on them.
  wall time         this is ~5.5k symbols x ~420 calendar days. If it is slow enough to delay node
                    startup, the derivation belongs in a scheduled job writing the setting, not in
                    `build_qc345_strategy`. That is a real possible outcome of running this.
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=420,
                    help="calendar days of history; 254 sessions of warmup needs ~420")
    ap.add_argument("--feed", default="sip")
    ap.add_argument("--limit-substrate", type=int, default=0,
                    help="fetch only the first N symbols — a SMOKE TEST, never a universe")
    ap.add_argument("--write", action="store_true", help="print settings JSON for QC345_UNIVERSE")
    args = ap.parse_args()

    import pandas as pd

    from strategies import qc345_universe as U
    from strategies.qc345 import _live_config

    cfg = _live_config()
    t0 = time.time()

    assets = U.fetch_assets()
    print(f"assets            {len(assets):>7,}  ({time.time() - t0:.1f}s)")

    names = U.substrate(assets, cfg)
    print(f"substrate         {len(names):>7,}  (measured {U.EXPECTED_SUBSTRATE:,})")
    if args.limit_substrate:
        names = names[:args.limit_substrate]
        print(f"  !! SMOKE TEST — truncated to {len(names)}; the result is NOT a usable universe")

    start = (pd.Timestamp.now("UTC").normalize() - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")
    t1 = time.time()
    bars = U.fetch_daily_bars(names, start=start, feed=args.feed)
    print(f"bars              {len(bars):>7,} rows over {bars['ticker'].nunique():,} symbols "
          f"({time.time() - t1:.1f}s)")

    selected, diag = U.derive_universe(bars, assets, cfg, check_floors=not args.limit_substrate)
    print(f"\nselected          {len(selected):>7,}")
    for key in ("rankable_names", "min_selected_price", "min_selected_liquidity_proxy"):
        if diag.get(key) is not None:
            print(f"  {key:<32} {diag[key]}")
    if args.limit_substrate:
        # The floors are NOT checked on a truncated run, and this line is why. `--limit-substrate`
        # takes the first N symbols alphabetically, so the pool is both tiny and arbitrary; the
        # weakest selected name is bound to sit far below a floor measured over the full substrate.
        # Reporting that as "the floor is stale" would be a false finding produced by the smoke test
        # itself — which is exactly the class of mistake the floors exist to catch.
        print("  floors                           NOT CHECKED — truncated run cannot evidence them")
    for key in ("price_floor_breached", "liquidity_floor_breached"):
        if diag.get(key):
            print(f"  {key:<32} TRUE — the measured floor is stale")
    print(f"\ntotal             {time.time() - t0:.1f}s")

    if args.write:
        print("\nstrategies.QC345_UNIVERSE:")
        print(json.dumps(selected, indent=2))
    else:
        print(f"\nfirst 15: {selected[:15]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
