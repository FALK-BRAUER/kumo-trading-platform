"""Is the stack able to do the thing it has been told to do?

A health surface can honestly check one thing: whether what is DECLARED matches what is RUNNING. This
module owns that comparison for the trading path, and it exists because on 2026-08-24 `/health`
answered `ok` for a stack that would have placed nothing all day — disarmed, with two of four lanes
missing, and every subsystem genuinely up.

Pure and side-effect free, so the rule is testable without an engine, a database or a venue.
"""
from __future__ import annotations

#: Named rather than counted, everywhere. A lane COUNT went from 4 to 2 in the live incident and read
#: as a healthy ratio; the names are what showed which two were gone.
__all__ = ["inert_contradictions"]


def inert_contradictions(
    *,
    orders_armed: bool | None,
    declared_trading: list[str] | None,
    registered: list[str] | None,
) -> list[str]:
    """Ways this stack contradicts its own instructions. Empty means no contradiction FOUND.

    `None` for any input means "could not ask", and yields nothing — the same three-state rule the
    provenance stamps use. A surface that cannot tell "unknown" from "wrong" either cries wolf while
    the engine is starting or passes vacuously forever, and both end with nobody reading it.
    """
    # PER CHECK, NOT AT THE TOP. `registered` being unknown must not silence the ORDERS_ARMED check,
    # which does not use it — and that is the check that caught the live incident. Collapsing both
    # guards into one early return meant a stale bridge disabled the whole detector, which is a worse
    # failure than the false alarm it was added to prevent: one cries wolf, the other goes quiet.
    if declared_trading is None:
        return []

    out: list[str] = []
    declared = sorted(set(declared_trading))

    # A strategy in TRADING is an operator's declaration of intent. A stack that cannot submit makes
    # every one of them inert — and `status: ok` says otherwise.
    if orders_armed is False and declared:
        out.append(
            f"KUMO_ORDERS_ARMED is false while {len(declared)} strategies are set to TRADING "
            f"({', '.join(declared)}) — they will decide and place nothing")

    # Declared but absent. This is the half a lane COUNT cannot show: 2/2 looks healthy when the other
    # two never arrived.
    # Only askable when the engine's registration list is actually known. `None` here means the
    # bridge is stale, not that nothing registered.
    missing = [] if registered is None else [s for s in declared if s not in set(registered)]
    if missing:
        out.append(
            f"{len(missing)} strategies are set to TRADING but never registered in the engine "
            f"({', '.join(missing)}) — the lane count cannot show this, because the ones that are "
            f"missing are simply not counted")
    return out
