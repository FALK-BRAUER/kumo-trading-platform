"""Persisted position-snapshot reader (#74 gap-fill, restart half).

Nautilus (`snapshot_positions=True`) writes each position state to a Redis LIST at
`trader-{trader_id}:snapshots:positions:{pos_id}` — msgpack of `Position.to_dict()` — but NEVER reloads them
(`cache.position_snapshots()` is in-memory and empty after restart). This reader recovers the CLOSED cycle
legs from those durable keys so the TradeCycleProjection can `seed_restored_legs()` on restart and cycle P&L
survives. It reads Nautilus's OWN persisted native state — not a parallel ledger.
"""

from __future__ import annotations

import logging as _logging
import time as _time

import msgspec.msgpack as _msgpack

from api.trade_cycle import CycleLeg, newer_close_wins

_SNAPSHOT_KEY_INFIX = ":snapshots:positions:"


_log = _logging.getLogger(__name__)


class RestoredLegs(dict):
    """`{pos_id: [CycleLeg, ...]}` plus WHETHER THE READ FINISHED (#846). A deadline stop used to be a
    log line only; a realized figure computed over a partial seed is a wrong number wearing a complete
    label, so the stop travels with the result. A plain dict to every existing caller."""

    stopped_at: str | None = None


def read_restored_legs(
    redis_client, trader_id: str, *, deadline: float | None = None
) -> RestoredLegs:
    """Scan the persisted position-state snapshots and return the CLOSED legs per position_id.

    Each key is a Redis list of msgpack position states (open/changed/closed all persisted); we keep only the
    CLOSED states (`ts_closed` set — `CycleLeg.is_closed_state`) and dedup by `ts_opened` (a leg is unique
    within a position by its open time; the last CLOSED state for it wins). `redis_client` is the engine's
    sync client. Returns `{pos_id: [CycleLeg, ...]}` ready for `seed_restored_legs`.

    `deadline` is a `time.monotonic()` instant after which the scan STOPS, returning what it has (#568).

    IT HAS TO LIVE HERE. The restart seed wraps this in `asyncio.wait_for`, and that cannot interrupt a
    blocking call — `scan_iter`/`lrange` hold the thread, so the budget only fires once control returns
    to the loop, which is after the work is already done. The client's `socket_timeout=2.0` bounds ONE
    call; nothing bounded the LOOP. Only the loop that makes the calls can stop them.

    PARTIAL IS BETTER THAN NONE, and it is reported. Legs are per-position P&L detail; the cycle
    BOUNDARY comes from the envelope, a separate Postgres read. A position either got its legs or did
    not, so stopping early degrades later positions' P&L rather than corrupting any of them. Silence
    would be the bad outcome — "no legs" and "we ran out of time before your legs" must not look alike.
    """
    prefix = f"trader-{trader_id}{_SNAPSHOT_KEY_INFIX}"
    out = RestoredLegs()
    stopped_at: str | None = None
    for key in redis_client.scan_iter(match=f"{prefix}*"):
        # CHECKED BEFORE THE READ, not after: an already-spent budget must cost zero keys, not one.
        if deadline is not None and _time.monotonic() >= deadline:
            stopped_at = key.decode() if isinstance(key, bytes) else str(key)
            break
        key_s = key.decode() if isinstance(key, bytes) else key
        pos_id = key_s[len(prefix):]
        # Dedup by ts_opened, keeping the state with the LATEST ts_closed — independent of Redis list order (do
        # not assume newest-first vs -last). A leg is unique within a position by its open time.
        best: dict[int, tuple[int, CycleLeg]] = {}
        for raw in redis_client.lrange(key_s, 0, -1):
            state = _msgpack.decode(raw)
            if not CycleLeg.is_closed_state(state):
                continue
            leg = CycleLeg.from_state_dict(state, position_id=pos_id)
            prior = best.get(leg.ts_opened)
            if newer_close_wins(None if prior is None else prior[0], leg.ts_closed):
                best[leg.ts_opened] = (leg.ts_closed or 0, leg)
        if best:
            out[pos_id] = [leg for _rank, leg in best.values()]
    out.stopped_at = stopped_at
    if stopped_at is not None:
        _log.warning(
            "snapshot read hit its deadline after %d position(s) — stopped at %s. Later cycles seed "
            "without their closed legs, so their leg P&L is incomplete; the cycle boundary is "
            "unaffected because it comes from the envelope (#568).",
            len(out), stopped_at)
    return out
