"""Freeze a reproducible RAW historical OHLCV snapshot for out-of-tree backtesting.

This is the Cockpit↔kumo-trading-strategies seam, and it is deliberately one-directional: Cockpit owns the
provider connection and the canonical `TICKER.MIC` instrument identity, so it exports the bars; the
strategy rules, the backtest, and every derived artifact stay in `kumo-trading-strategies`. Nothing here knows
what GEM-VT is, and nothing here decides anything.

**Raw prices, always.** The snapshot is unadjusted executable OHLCV — the price a Cockpit order could
actually have filled at on that date. Split/dividend-adjusted series rewrite history so a backtest fills
at prices that were never tradable, and the corruption is invisible in the result. Distributions and cash
yield belong in the consuming backtest's ledger as explicit cash flows, never folded into a price. See
kumo-trading-strategies `docs/momentum-rotation-literature.md`; the wire-level guard is
`api/providers/alpaca/test_http.py`.

**Corporate actions are NOT validated here.** A raw split-unadjusted series contains real price
discontinuities. Detecting them is the consumer's data-quality gate (kumo-trading-strategies Phase 1), not this
script's — an exporter that silently "fixed" a split would be exactly the adjustment this policy forbids.
The manifest records the policy so the consumer can enforce it.

Usage (from `backend/`, venv active, Alpaca keys in the environment — same env the engine uses, e.g.
`export APCA_API_KEY_ID=$(security find-generic-password -s alpaca-paper-key -w)`):

    python -m scripts.export_bars --symbols SPY,EFA,AGG,BIL --start 2005-01-01 --out ../data/gem-vt-raw

Output:  <out>/bars.csv  +  <out>/manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.feed_config import load_feed_config
from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.alpaca.providers import EXCHANGE_TO_MIC

# The one policy constant. `AlpacaHttpClient.get_bars` pins the wire parameter; this is what the manifest
# publishes to the consumer so a snapshot can never be mistaken for an adjusted series.
ADJUSTMENT = "raw"
MANIFEST_SCHEMA_VERSION = 1
BARS_FILENAME = "bars.csv"
MANIFEST_FILENAME = "manifest.json"

# Alpaca daily-bar keys → snapshot column names. `n` (trade count) and `vw` (provider VWAP) ride along
# because they are free, raw, and useful for liquidity screening; they are NOT execution prices.
BAR_COLUMNS = ("symbol", "venue", "instrument_id", "ts", "open", "high", "low", "close", "volume",
               "trade_count", "vwap")


class ExportError(RuntimeError):
    """The snapshot could not be produced completely. Nothing is written — a partial snapshot with a
    valid-looking manifest is worse than no snapshot."""


# -- pure logic (unit-tested offline) ---------------------------------------------------------------


def venue_map(assets: list[dict], symbols: list[str]) -> dict[str, str]:
    """symbol → MIC, using the same Alpaca-exchange table the instrument provider uses, so an exported
    `instrument_id` is byte-identical to the one the live node streams for that symbol.

    Raises if any requested symbol is missing or maps to an unsupported venue — an unmappable symbol
    would otherwise land in the snapshot under a different identity than Cockpit uses for it.
    """
    by_symbol = {a.get("symbol"): a for a in assets}
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    unmapped: list[str] = []
    for symbol in symbols:
        asset = by_symbol.get(symbol)
        if asset is None:
            unknown.append(symbol)
            continue
        mic = EXCHANGE_TO_MIC.get(asset.get("exchange", ""))
        if mic is None:
            unmapped.append(f"{symbol} (exchange={asset.get('exchange')!r})")
            continue
        resolved[symbol] = mic
    if unknown:
        raise ExportError(f"symbols not in Alpaca's tradable asset list: {', '.join(sorted(unknown))}")
    if unmapped:
        raise ExportError(f"symbols on venues with no MIC mapping: {', '.join(sorted(unmapped))}")
    return resolved


def normalize_bars(symbol: str, venue: str, raw_bars: list[dict]) -> list[dict[str, Any]]:
    """Alpaca bar dicts → snapshot rows. Values pass through UNCHANGED: no rounding, no rescaling, no
    timestamp reformatting (Alpaca's ISO8601 `t` is kept verbatim). Preserving the provider's exact bytes
    is what makes the snapshot auditable against the provider later."""
    return [
        {
            "symbol": symbol,
            "venue": venue,
            "instrument_id": f"{symbol}.{venue}",
            "ts": bar["t"],
            "open": bar["o"],
            "high": bar["h"],
            "low": bar["l"],
            "close": bar["c"],
            "volume": bar.get("v"),
            "trade_count": bar.get("n"),
            "vwap": bar.get("vw"),
        }
        for bar in raw_bars
    ]


def cockpit_git_commit() -> str | None:
    """The Cockpit commit that produced the snapshot — reproducibility provenance. None outside a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def build_manifest(
    *,
    provider: str,
    feed: str,
    timeframe: str,
    start: str,
    end: str | None,
    venues: dict[str, str],
    row_counts: dict[str, int],
    created_at: str,
    git_commit: str | None,
) -> dict[str, Any]:
    """The snapshot's self-description. `adjustment` is hard-wired to `raw` — it is a statement of what
    the bars ARE, not a knob; a consumer that reads anything else here must refuse the snapshot."""
    symbols = sorted(venues)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": created_at,
        "created_by": "kumo-trading-platform backend/scripts/export_bars.py",
        "cockpit_git_commit": git_commit,
        "provider": provider,
        "feed": feed,
        "adjustment": ADJUSTMENT,
        "corporate_actions": "unvalidated — consumer must run a split/discontinuity gate before use",
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "symbols": symbols,
        "venues": {s: venues[s] for s in symbols},
        "instrument_ids": [f"{s}.{venues[s]}" for s in symbols],
        "row_counts": {s: row_counts.get(s, 0) for s in symbols},
        "total_rows": sum(row_counts.get(s, 0) for s in symbols),
        "bars_file": BARS_FILENAME,
        "bars_columns": list(BAR_COLUMNS),
    }


def write_snapshot(out_dir: Path, rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Write `bars.csv` + `manifest.json`. Rows are sorted by (symbol, ts) so a re-export of the same
    window is byte-identical and diffable."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r["symbol"], r["ts"]))
    with open(out_dir / BARS_FILENAME, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(BAR_COLUMNS))
        writer.writeheader()
        writer.writerows(ordered)
    (out_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2) + "\n")


# -- provider I/O -----------------------------------------------------------------------------------


async def fetch_symbol_bars(
    http: AlpacaHttpClient, symbol: str, timeframe: str, start: str, end: str | None, feed: str
) -> list[dict]:
    """All raw bars for one symbol across the window, following Alpaca's `next_page_token` to the end."""
    bars: list[dict] = []
    page_token: str | None = None
    while True:
        payload = await http.get_bars(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=feed,
            page_token=page_token,
        )
        bars.extend(payload.get("bars") or [])
        page_token = payload.get("next_page_token")
        if not page_token:
            return bars


async def export(
    *,
    http: AlpacaHttpClient,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str | None,
    feed: str,
    out_dir: Path,
    provider: str = "alpaca",
) -> dict[str, Any]:
    """Fetch → validate → write. Validation happens BEFORE any file is written, so a failed export leaves
    no half-snapshot behind. Returns the manifest."""
    venues = venue_map(await http.list_assets(), symbols)

    rows: list[dict[str, Any]] = []
    row_counts: dict[str, int] = {}
    for symbol in symbols:
        raw = await fetch_symbol_bars(http, symbol, timeframe, start, end, feed)
        symbol_rows = normalize_bars(symbol, venues[symbol], raw)
        rows.extend(symbol_rows)
        row_counts[symbol] = len(symbol_rows)

    empty = sorted(s for s, n in row_counts.items() if n == 0)
    if empty:
        # Fail loud. A silently-empty symbol becomes a silently-absent asset in the rotation universe,
        # which changes the strategy's decisions without changing its config.
        raise ExportError(
            f"no bars returned for: {', '.join(empty)} — check the window, the symbol, "
            f"and whether feed={feed!r} covers them"
        )

    manifest = build_manifest(
        provider=provider,
        feed=feed,
        timeframe=timeframe,
        start=start,
        end=end,
        venues=venues,
        row_counts=row_counts,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        git_commit=cockpit_git_commit(),
    )
    write_snapshot(out_dir, rows, manifest)
    return manifest


# -- CLI --------------------------------------------------------------------------------------------


def _alpaca_settings() -> tuple[str, str, str, str]:
    """(key, secret, data_base_url, feed) from the environment + `config/feed.toml`.

    Env-var NAMES come from feed.toml's `[data.alpaca]` (`key_env`/`secret_env`) exactly as the engine
    resolves them — the values are read from the environment only. This script never reads the keychain,
    a `.env` file, or any credential store.
    """
    cfg = load_feed_config()
    alpaca = cfg.provider_config if cfg.data_provider == "alpaca" else {}
    key_env = alpaca.get("key_env", "APCA_API_KEY_ID")
    secret_env = alpaca.get("secret_env", "APCA_API_SECRET_KEY")
    key, secret = os.environ.get(key_env), os.environ.get(secret_env)
    if not key or not secret:
        raise ExportError(
            f"missing Alpaca credentials in {key_env}/{secret_env} — export them first "
            f"(the engine's launcher, backend/scripts/run-engine.sh, shows the keychain services)"
        )
    return key, secret, alpaca.get("data_base_url", "https://data.alpaca.markets"), alpaca.get("feed", "sip")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a raw (unadjusted) historical OHLCV snapshot + manifest for backtesting."
    )
    parser.add_argument("--symbols", required=True, help="comma-separated, e.g. SPY,EFA,AGG,BIL")
    parser.add_argument("--start", required=True, help="ISO date, inclusive lower bound (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="ISO date, upper bound; omit for the latest session")
    parser.add_argument("--timeframe", default="1Day", help="Alpaca timeframe (default: 1Day)")
    parser.add_argument("--feed", default=None, help="iex | sip (default: [data.alpaca].feed in feed.toml)")
    parser.add_argument("--out", required=True, type=Path, help="output directory for bars.csv + manifest.json")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise ExportError("--symbols resolved to an empty list")

    key, secret, data_base, default_feed = _alpaca_settings()
    feed = args.feed or default_feed
    if feed == "iex":
        # Not fatal — but IEX-sourced history is a single venue's prints, not the consolidated tape, so
        # daily bars can differ from what a SIP-sourced backtest would see. The manifest records it; say
        # it out loud too, because it silently caps the evidence grade of everything downstream.
        print("warning: feed=iex — IEX-sourced history, not the consolidated (SIP) tape", file=sys.stderr)

    http = AlpacaHttpClient(key=key, secret=secret, trading_base="https://paper-api.alpaca.markets",
                            data_base=data_base)
    await http.connect()
    try:
        manifest = await export(
            http=http,
            symbols=symbols,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            feed=feed,
            out_dir=args.out,
        )
    finally:
        await http.close()

    print(f"wrote {args.out / BARS_FILENAME} ({manifest['total_rows']} rows, {len(symbols)} symbols)")
    print(f"wrote {args.out / MANIFEST_FILENAME} (adjustment={manifest['adjustment']}, feed={feed})")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(parse_args(argv)))
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
