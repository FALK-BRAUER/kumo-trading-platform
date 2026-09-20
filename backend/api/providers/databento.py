"""Databento provider — registers Nautilus's native live Databento data client.

Symbols route to their listing dataset by MIC venue: `venue_dataset_map` tells the live client which
dataset a request belongs to (XNYS→XNYS.PILLAR, XNAS→XNAS.ITCH …), built from Nautilus's own
`get_dataset_for_venue`. Definitions load on demand via `request_instrument` (resolves precision; the
provider's startup auto-load is unreliable here).

The data client is the instrument authority — the node routes every instrument/bar request to it by id.
"""

from __future__ import annotations

import os

import databento as db
import pandas as pd
from nautilus_trader.adapters.databento import (
    DATABENTO,
    DatabentoDataClientConfig,
    DatabentoDataLoader,
    DatabentoLiveDataClientFactory,
)
from nautilus_trader.model.identifiers import Venue

from api.providers.base import DataClientSpec

# One bar interval, backed off the dataset's availability edge (Nautilus's request end is inclusive, so
# requesting the exact edge 422s).
_SCHEMA_INTERVAL = {
    "ohlcv-1m": pd.Timedelta(minutes=1),
    "ohlcv-1h": pd.Timedelta(hours=1),
    "ohlcv-1d": pd.Timedelta(days=1),
}
# US equity listing venues we route to their Databento dataset.
_LISTING_VENUES = ("XNAS", "XNYS", "ARCX", "XASE", "XPSX", "XBOS", "XCIS")
# LAZY, deliberately (#652 item 8c): `api/providers/__init__.py` imports this module unconditionally
# on EVERY venue, so constructing `DatabentoDataLoader()` at import time made a Databento-only
# failure kill the whole provider registry on an instance that never selects Databento — the #622
# failure shape (build_node() raises, crash loop, [] positions), one provider over. First USE pays.
_loader_instance: DatabentoDataLoader | None = None
_end_cache: dict[tuple[str, str], pd.Timestamp] = {}


def _get_loader() -> DatabentoDataLoader:
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = DatabentoDataLoader()
    return _loader_instance


def _venue_dataset_map() -> dict[str, str]:
    return {v: _get_loader().get_dataset_for_venue(Venue(v)) for v in _LISTING_VENUES}


def build(provider_config: dict) -> DataClientSpec:
    """Build the Databento data client spec from the `[data.databento]` table."""
    api_key_env = provider_config["api_key_env"]
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"{api_key_env} unset — launch via backend/scripts/run-api.sh (injects it from the keychain)."
        )
    return DataClientSpec(
        client_id=DATABENTO,
        config=DatabentoDataClientConfig(api_key=api_key, venue_dataset_map=_venue_dataset_map()),
        factory=DatabentoLiveDataClientFactory,
        # Databento's own docs (schemas-and-data-formats/ohlcv, read 2026-08-29): "Our ohlcv-1d
        # schema is based on UTC dates. If you are interested in daily data based on exchange
        # session hours, you may need to request ... and aggregate the data yourself." A UTC
        # calendar day over every trade the feed prints is not the regular session (#616).
        daily_bars_cover="extended",
        # Databento ohlcv is unadjusted by construction (a trade tape aggregated to UTC days). Unused
        # client (see CLAUDE.md), declared so the spec cannot be constructed without a statement.
        price_adjustments=frozenset({"raw"}),
        # Databento serves a consolidated trade tape; the INTERNAL granularities aggregate from it.
        streams_trade_ticks=True,
        # CHECKED IN THE INSTALLED ADAPTER, not assumed from the vendor's marketing: Nautilus's
        # `adapters/databento/data.py` implements `_subscribe_quote_ticks` and serves it from
        # mbp-1/bbo. No current instance selects this provider — the broker supplies its own data
        # (2026-09-09) — but the field is required precisely so a provider cannot sit here
        # with an unanswered capability waiting for someone to select it.
        streams_quote_ticks=True,
    )


def dataset_for_venue(venue: str) -> str:
    """Nautilus's built-in MIC-venue → Databento dataset routing (XNYS→XNYS.PILLAR, XNAS→XNAS.ITCH …)."""
    return _get_loader().get_dataset_for_venue(Venue(venue))


def request_end(api_key: str, dataset: str, schema: str) -> pd.Timestamp:
    """Safe max end (UTC) for a (dataset, schema) historical request — each listing feed has its own edge."""
    key = (dataset, schema)
    if key not in _end_cache:
        rng = db.Historical(api_key).metadata.get_dataset_range(dataset=dataset)
        raw = rng.get("schema", {}).get(schema, {}).get("end") or rng["end"]
        _end_cache[key] = pd.Timestamp(raw) - _SCHEMA_INTERVAL.get(schema, pd.Timedelta(days=1))
    return _end_cache[key]
