"""The `universe` probe is REQUIRED from every lane (#637 follow-up, kumo-strategies#80).

Every lane now emits, at preflight:

    {"source": "symbols"|"instrument_ids", "requested": 97, "resolved": 60,
     "unresolved": [...], "ambiguous": {"SPY": ["SPY.ARCX", "SPY.XNAS"]}}

Since #622, `_unresolved_symbols` and `_ambiguous_symbols` were REAL degradation that nothing
in-process could read — a lane quietly trading 60% of its pool looked identical to a healthy one,
and whether PAPER also carries duplicate identities was unanswerable without grepping a container.
The probe makes both a field on every lane, on both tenants.

WHY IT MUST BE IN COCKPIT'S REQUIRED SET, not only emitted: the peer's words — "or this is enforced
only by my suite, and a lane that stops emitting it goes unnoticed on your side — which is the exact
failure QC345 had with `price`, reporting `no such probe` on every boot until #532."

THE RELATIONSHIP IS THE DETECTOR, not the counts: requested == resolved + len(unresolved). It
disagrees loudly if any path stops recording. `ambiguous` is a SUBSET of resolved — those symbols
DID resolve, to an identity chosen among several.
"""

from __future__ import annotations

from api.preflight import REQUIRED_FROM_STRATEGY, REQUIRED_PROBES


def test_universe_is_REQUIRED_from_every_lane():
    assert "universe" in REQUIRED_FROM_STRATEGY, (
        "the universe probe is not required, so a lane that stops emitting it goes unnoticed here — "
        "the QC345 `price` failure shape (kumo-strategies#80)"
    )
    assert "universe" in REQUIRED_PROBES


def test_the_existing_required_probes_survive():
    """Additive. `armed` stays FIRST — it is the one capability cockpit cannot observe externally,
    and QC345 sat unarmed four days behind exactly that blindness (#637)."""
    assert REQUIRED_FROM_STRATEGY[0] == "armed"
    for p in ("equity", "price", "owned"):
        assert p in REQUIRED_FROM_STRATEGY
