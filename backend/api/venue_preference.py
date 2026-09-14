"""Which identity to keep when IB reports one symbol on two venues (#625).

MEASURED on staging 2026-08-29, after #622 made `load_contracts` declare the full universe: 64 of
209 cached instruments carry TWO identities.

    id            primaryExchange   venue
    SPY.ARCX      ARCA              ARCX     <- venue matches
    SPY.XNAS      ARCA              XNAS     <- does not

`providers/ibkr.py` declares `exchange="SMART"`, IB returns contract details for multiple listings,
and Nautilus builds an instrument per contract. IB IS CORRECT TO DO THAT. The defect was what came
next: a dict comprehension kept whichever the cache iterated last, so a lane asked for `SPY.XNAS` —
an instrument that never has data — and starved in silence.

THE SPURIOUS TWIN IS SELF-IDENTIFYING: it carries the REAL primary exchange while wearing the wrong
venue. No external comparison, no tie-break.

WHY HERE AND NOT IN THE RESOLVER. `exchange_to_mic_venue` needs `ibapi`, which kumo-strategies does
not install. And "ARCA" is not "ARCX", so a naive `primaryExchange == venue` compare matches NOTHING
and falls through on every symbol while looking like a working rule. Teaching `symbol_resolution.py`
IB's exchange names would also recreate #622 inside the resolver written to fix it — the layer that
knows the RULE is not the layer that knows the VENDOR. So the preference is INJECTED, and it lives
next to the module that declares `exchange="SMART"` and causes the duplicates.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("kumo.venue_preference")


def prefer_primary_exchange(cache, symbol: str, candidates: list):
    """The candidate whose venue IS its own primary exchange, or `None` for "I cannot tell".

    `None` is a real answer, not a failure: the resolver's own deterministic fallback then takes
    over AND records the ambiguity. Inventing a second fallback here would give two rules that
    disagree, which is the shape this codebase keeps paying for.

    Never raises. A candidate the cache does not hold, or one with no contract details, is simply
    not a match — mid-load that is expected rather than exceptional.
    """
    from nautilus_trader.adapters.interactive_brokers.parsing.instruments import (
        exchange_to_mic_venue,
    )

    for iid in candidates:
        try:
            inst = cache.instrument(iid)
            primary = ((getattr(inst, "info", None) or {}).get("contract") or {}).get("primaryExchange")
            if primary and exchange_to_mic_venue(primary) == iid.venue.value:
                return iid
        except Exception:                                               # noqa: BLE001
            continue
    _log.warning(
        "venue preference: %s has %d candidates and none matches its own primary exchange (%s) — "
        "falling back to the resolver's deterministic pick",
        symbol, len(candidates), ", ".join(str(c) for c in candidates))
    return None
