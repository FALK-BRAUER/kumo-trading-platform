"""Offline tests for the raw-bar exporter. No network: a fake Alpaca HTTP client serves fixtures.

Follows the repo's sync-test + `asyncio.run` pattern (no pytest-asyncio).

The load-bearing assertions here are the DATA-POLICY ones — raw values survive the round-trip byte-for-byte
and the manifest states `raw`. Everything downstream (kumo-strategies' GEM-VT backtest) trusts those two
facts and cannot re-derive them from the snapshot alone.
"""

from __future__ import annotations

import asyncio
import csv
import json

import pytest

from scripts.export_bars import (
    ADJUSTMENT,
    BARS_FILENAME,
    MANIFEST_FILENAME,
    ExportError,
    build_manifest,
    export,
    fetch_symbol_bars,
    normalize_bars,
    venue_map,
    write_snapshot,
)

ASSETS = [
    {"symbol": "SPY", "exchange": "ARCA", "class": "us_equity"},
    {"symbol": "EFA", "exchange": "ARCA", "class": "us_equity"},
    {"symbol": "AAPL", "exchange": "NASDAQ", "class": "us_equity"},
    {"symbol": "WEIRD", "exchange": "MOON", "class": "us_equity"},
]

# Deliberately awkward values: a price needing full float precision, a zero-volume row, a huge volume.
SPY_BARS = [
    {"t": "2005-01-03T05:00:00Z", "o": 121.56, "h": 121.76, "l": 120.37, "c": 120.3, "v": 55748000, "n": 1, "vw": 120.9},
    {"t": "2005-01-04T05:00:00Z", "o": 120.46, "h": 120.54, "l": 118.44, "c": 118.83, "v": 69167600, "n": 2, "vw": 119.1},
]
EFA_BARS = [
    {"t": "2005-01-03T05:00:00Z", "o": 50.1, "h": 50.2, "l": 49.9, "c": 49.999999999999996, "v": 0, "n": 0, "vw": 50.0},
]


class FakeHttp:
    """Stands in for `AlpacaHttpClient`. Paginates when a symbol's fixture is a list-of-pages."""

    def __init__(self, assets: list[dict], bars: dict[str, list], *, feed_seen: list | None = None) -> None:
        self._assets = assets
        self._bars = bars
        self.calls: list[dict] = []
        self.feed_seen = feed_seen if feed_seen is not None else []

    async def list_assets(self, **_kwargs) -> list[dict]:
        return self._assets

    async def get_bars(self, *, symbol, timeframe, start, end=None, feed="iex", page_token=None, **_kw) -> dict:
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "start": start, "end": end,
             "feed": feed, "page_token": page_token}
        )
        self.feed_seen.append(feed)
        pages = self._bars.get(symbol, [])
        if pages and isinstance(pages[0], dict):  # single page of bar dicts
            return {"bars": pages, "next_page_token": None}
        index = 0 if page_token is None else int(page_token)
        page = pages[index] if index < len(pages) else []
        nxt = str(index + 1) if index + 1 < len(pages) else None
        return {"bars": page, "next_page_token": nxt}


# -- venue mapping ----------------------------------------------------------------------------------


def test_venue_map_uses_the_cockpit_instrument_identity() -> None:
    """The exported id must match what the live node streams — same Alpaca-exchange → MIC table."""
    assert venue_map(ASSETS, ["SPY", "AAPL"]) == {"SPY": "ARCX", "AAPL": "XNAS"}


def test_venue_map_rejects_unknown_symbol() -> None:
    with pytest.raises(ExportError, match="not in Alpaca's tradable asset list"):
        venue_map(ASSETS, ["SPY", "NOPE"])


def test_venue_map_rejects_unmappable_venue() -> None:
    """A symbol whose exchange has no MIC would be exported under an id Cockpit never uses — refuse it
    rather than inventing an identity."""
    with pytest.raises(ExportError, match="no MIC mapping"):
        venue_map(ASSETS, ["WEIRD"])


# -- normalization ----------------------------------------------------------------------------------


def test_normalize_preserves_raw_values_exactly() -> None:
    rows = normalize_bars("SPY", "ARCX", SPY_BARS)
    assert [r["close"] for r in rows] == [120.3, 118.83]
    assert rows[0]["ts"] == "2005-01-03T05:00:00Z"  # provider timestamp kept verbatim
    assert rows[0]["instrument_id"] == "SPY.ARCX"
    assert rows[0]["volume"] == 55748000


def test_normalize_keeps_zero_volume_rows() -> None:
    """A zero-volume session is real market information (halt/illiquidity). Dropping it here would hide a
    data-quality problem the consumer's gate is supposed to catch."""
    rows = normalize_bars("EFA", "ARCX", EFA_BARS)
    assert len(rows) == 1
    assert rows[0]["volume"] == 0


def test_normalize_tolerates_missing_optional_fields() -> None:
    rows = normalize_bars("SPY", "ARCX", [{"t": "x", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}])
    assert rows[0]["volume"] is None and rows[0]["trade_count"] is None and rows[0]["vwap"] is None


# -- pagination -------------------------------------------------------------------------------------


def test_fetch_follows_every_page() -> None:
    http = FakeHttp(ASSETS, {"SPY": [[SPY_BARS[0]], [SPY_BARS[1]], []]})
    bars = asyncio.run(fetch_symbol_bars(http, "SPY", "1Day", "2005-01-01", None, "iex"))
    assert len(bars) == 2
    assert [c["page_token"] for c in http.calls] == [None, "1", "2"]


# -- manifest ---------------------------------------------------------------------------------------


def test_manifest_declares_raw_adjustment() -> None:
    m = build_manifest(
        provider="alpaca", feed="iex", timeframe="1Day", start="2005-01-01", end="2026-01-01",
        venues={"SPY": "ARCX"}, row_counts={"SPY": 2}, created_at="2026-08-01T00:00:00Z", git_commit="abc",
    )
    # If this ever reads anything but "raw", the consumer must refuse the snapshot.
    assert m["adjustment"] == "raw" == ADJUSTMENT


def test_manifest_carries_the_full_reproducibility_record() -> None:
    m = build_manifest(
        provider="alpaca", feed="sip", timeframe="1Day", start="2005-01-01", end=None,
        venues={"EFA": "ARCX", "SPY": "ARCX"}, row_counts={"SPY": 2, "EFA": 1},
        created_at="2026-08-01T00:00:00Z", git_commit="abc123",
    )
    assert m["provider"] == "alpaca" and m["feed"] == "sip"
    assert m["symbols"] == ["EFA", "SPY"]  # sorted → stable across runs
    assert m["venues"] == {"EFA": "ARCX", "SPY": "ARCX"}
    assert m["instrument_ids"] == ["EFA.ARCX", "SPY.ARCX"]
    assert (m["start"], m["end"]) == ("2005-01-01", None)
    assert m["created_at"] == "2026-08-01T00:00:00Z"
    assert m["cockpit_git_commit"] == "abc123"
    assert m["total_rows"] == 3
    assert "consumer must run a split" in m["corporate_actions"]


# -- end-to-end (fake provider) ---------------------------------------------------------------------


def test_export_writes_bars_and_manifest(tmp_path) -> None:
    http = FakeHttp(ASSETS, {"SPY": SPY_BARS, "EFA": EFA_BARS})
    manifest = asyncio.run(
        export(http=http, symbols=["SPY", "EFA"], timeframe="1Day", start="2005-01-01",
               end="2026-01-01", feed="iex", out_dir=tmp_path)
    )

    on_disk = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    assert on_disk == manifest
    assert on_disk["adjustment"] == "raw"
    assert on_disk["row_counts"] == {"EFA": 1, "SPY": 2}

    with open(tmp_path / BARS_FILENAME) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    # Sorted by (symbol, ts) → deterministic, diffable re-exports.
    assert [r["symbol"] for r in rows] == ["EFA", "SPY", "SPY"]
    # Raw close survives the CSV round-trip byte-for-byte, including full float precision.
    assert [r["close"] for r in rows if r["symbol"] == "SPY"] == ["120.3", "118.83"]
    assert float(next(r["close"] for r in rows if r["symbol"] == "EFA")) == 49.999999999999996


def test_export_requests_raw_window_for_every_symbol(tmp_path) -> None:
    http = FakeHttp(ASSETS, {"SPY": SPY_BARS, "EFA": EFA_BARS})
    asyncio.run(
        export(http=http, symbols=["SPY", "EFA"], timeframe="1Day", start="2005-01-01",
               end="2026-01-01", feed="sip", out_dir=tmp_path)
    )
    assert {c["symbol"] for c in http.calls} == {"SPY", "EFA"}
    assert all(c["start"] == "2005-01-01" and c["end"] == "2026-01-01" for c in http.calls)
    assert http.feed_seen == ["sip", "sip"]  # the configured feed reaches the provider unchanged


def test_export_fails_loud_on_an_empty_symbol_and_writes_nothing(tmp_path) -> None:
    """A symbol that returns no bars would silently drop out of the rotation universe — changing the
    strategy's decisions without changing its config. Refuse, and leave no partial snapshot behind."""
    http = FakeHttp(ASSETS, {"SPY": SPY_BARS, "EFA": []})
    with pytest.raises(ExportError, match="no bars returned for: EFA"):
        asyncio.run(
            export(http=http, symbols=["SPY", "EFA"], timeframe="1Day", start="2005-01-01",
                   end=None, feed="iex", out_dir=tmp_path)
        )
    assert not (tmp_path / BARS_FILENAME).exists()
    assert not (tmp_path / MANIFEST_FILENAME).exists()


def test_export_refuses_before_fetching_when_a_symbol_is_unmappable(tmp_path) -> None:
    http = FakeHttp(ASSETS, {"SPY": SPY_BARS})
    with pytest.raises(ExportError):
        asyncio.run(
            export(http=http, symbols=["SPY", "NOPE"], timeframe="1Day", start="2005-01-01",
                   end=None, feed="iex", out_dir=tmp_path)
        )
    assert http.calls == []  # identity is validated before a single bar is pulled
    assert not tmp_path.joinpath(BARS_FILENAME).exists()


def test_write_snapshot_is_deterministic(tmp_path) -> None:
    """Same input → byte-identical output, so a re-export can be diffed against a prior snapshot."""
    rows = normalize_bars("SPY", "ARCX", list(reversed(SPY_BARS))) + normalize_bars("EFA", "ARCX", EFA_BARS)
    manifest = build_manifest(
        provider="alpaca", feed="iex", timeframe="1Day", start="2005-01-01", end=None,
        venues={"SPY": "ARCX", "EFA": "ARCX"}, row_counts={"SPY": 2, "EFA": 1},
        created_at="2026-08-01T00:00:00Z", git_commit=None,
    )
    first = tmp_path / "a"
    second = tmp_path / "b"
    write_snapshot(first, rows, manifest)
    write_snapshot(second, list(reversed(rows)), manifest)
    assert (first / BARS_FILENAME).read_bytes() == (second / BARS_FILENAME).read_bytes()
