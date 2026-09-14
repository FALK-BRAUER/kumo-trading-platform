"""The claim, brought back to what the lane's OWN book says — ONE predicate for every gateway (#950).

Called from the adapters' terminal handlers with `(symbol, the lane's NETTING quantity, fill price
or None)`; the quantity is SIGNED — kumo-strategies `contract.py:231` hands
`int(sum(p.signed_qty …))`, and driving `protective_close` with a −1234 short delivered
`runner.sync_claim("XYZQ", -1234, 12.34)` here (measured 2026-09-11). Both cockpit gateways used to
do `if qty > 0: record else: drop` — two identical bodies — so a live 57-share short arrived as −57,
took the `else`, and the claim on a REAL position was DELETED; `_foreign_claims` then reported
nothing and every long lane's residue on that symbol grew by shares that were the short lane's.

ZERO IS FLAT, AND NOTHING ELSE IS. The sign is carried through unchanged; kumo-strategies#178 stores
claims signed. NEVER RAISES — this runs inside a Nautilus event handler.

    qty != 0 with a price   → recorded, signed (an existing row moves `qty` alone)
    qty != 0, no price      → a rejection while the lane still holds some: the submit-time claim
                              stands (an over-claim only narrows OTHER lanes, the safe direction)
    qty == 0                → the lane holds none → the claim is dropped
"""
from __future__ import annotations

import logging

_log = logging.getLogger("kumo.claims_sync")


async def sync_claim_from_book(journal, strategy_id: str, symbol: str, qty: int, px: float | None) -> None:
    try:
        from kumo_strategies.runtime.executor.store import drop_claim, record_claim
        if int(qty) != 0:
            if px is None:
                _log.warning("%s: %s claim left as submitted — lane holds %s, no fill price to record",
                             strategy_id, symbol, qty)
                return
            await record_claim(journal, strategy_id, symbol, int(qty), float(px))
        else:
            await drop_claim(journal, strategy_id, symbol)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never raise into an event handler
        _log.warning("%s: claim for %s not synced to %s (%r)", strategy_id, symbol, qty, exc)
