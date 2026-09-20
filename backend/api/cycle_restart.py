"""Restart seed wiring (#74b.2) — the glue that rebuilds a TradeCycleProjection from durable state.

Extracted from the engine so the wiring (envelope load + snapshot read → projection seed) is testable with
fakes, not only via the direct-projection restart test. `UiFeedStrategy._seed_cycles` calls this.
"""

from __future__ import annotations

from api.snapshot_reader import read_restored_legs


async def seed_projection(
    projection, store, snap_redis, trader_id: str, client_id: str, strategy_id: str
) -> None:
    """Rebuild `projection`'s active cycles from durable state, in the ORDER that makes the fold correct:
    1. restored LEGS from Nautilus's persisted position snapshots (leg P&L), then
    2. cycle BOUNDARY/identity from the envelope (scoped to this node's client + strategy).
    Legs first so a seeded cycle's `opened_ts` window already has its legs to include. The SAME reducer resumes
    on the SAME cycle_ids afterward."""
    for pos_id, legs in read_restored_legs(snap_redis, trader_id).items():
        projection.seed_restored_legs(pos_id, legs)
    for row in await store.load_active(client_id=client_id, strategy_id=strategy_id):
        projection.seed_cycle(
            row.account_id, row.instrument_id, row.cycle_id, row.opened_ts, row.last_event_ts
        )
