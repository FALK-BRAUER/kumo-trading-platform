"""Importing the provider registry must not construct a DatabentoDataLoader (#652 item 8c).

`api/providers/databento.py` built `DatabentoDataLoader()` (and the venue→dataset map from it) at
MODULE IMPORT, and `api/providers/__init__.py` imports the module unconditionally — on EVERY venue.
So an IBKR-only instance paid Databento's import-time construction, and any failure in it killed the
whole provider registry: `build_node()` raises, the node crash-loops, `/positions` serves [] while
the broker holds the book — the exact #622 failure shape (`_instrument_ids` needing an Alpaca key on
an IBKR instance), one provider over.

The loader is now constructed lazily, on first use by `build()`/`dataset_for_venue`.
"""

from __future__ import annotations

import importlib

import nautilus_trader.adapters.databento as nd

import api.providers.databento as databento_module


def test_importing_the_module_does_not_construct_the_loader():
    """Reload the module with a loader whose CONSTRUCTOR raises: with import-time construction the
    reload dies (seen red pre-fix); lazy construction imports clean. The real class is restored and
    the module re-reloaded either way, so no other test inherits the booby-trap."""
    real = nd.DatabentoDataLoader

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("DatabentoDataLoader constructed at import time")

    nd.DatabentoDataLoader = _Boom
    try:
        importlib.reload(databento_module)
    finally:
        nd.DatabentoDataLoader = real
        importlib.reload(databento_module)


def test_the_dataset_map_still_resolves_lazily():
    """Coverage, not just absence: the map the live client needs must still be derivable on demand
    from the real loader (this constructs one — first use, exactly as designed)."""
    mapping = databento_module._venue_dataset_map()
    assert mapping.get("XNAS") == "XNAS.ITCH"
    assert set(mapping) == set(databento_module._LISTING_VENUES)
