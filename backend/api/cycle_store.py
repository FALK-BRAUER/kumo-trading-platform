"""CycleEnvelopeStore (#74) — the engine's durable read/write of the trade-cycle envelope.

The ENGINE process owns this: it writes an envelope row on each cycle lifecycle transition and reads the active
rows on startup to seed the TradeCycleProjection (so restart reproduces the SAME cycle_id + boundary). The
envelope is engine-owned domain metadata — NOT native trade state, NOT a P&L ledger.

Async over the shared asyncpg engine (no sync driver in the stack). Writes are idempotent at-least-once: an
upsert keyed on `cycle_id`, guarded by a monotonic `last_event_ts` so a stale/replayed write can't clobber a
newer state. Reads on startup are a simple SELECT of the still-open cycles.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.db.engine import session_factory
from api.db.models import TradeCycleEnvelope
from api.models import TradeDTO


@dataclass(frozen=True)
class EnvelopeRow:
    """A plain snapshot of one cycle's durable envelope — the projection builds this from its fold state and
    hands it to the store; the store hands it back on restart. Decoupled from the ORM row so the projection
    never imports SQLAlchemy."""

    cycle_id: str
    account_id: str
    client_id: str
    instrument_id: str
    strategy_id: str
    opened_ts: int
    closed_ts: int | None
    state: str
    last_event_ts: int
    close_reason: str | None = None
    close_reason_source: str | None = None
    import_status: str = "ok"


def envelope_key(row: EnvelopeRow) -> tuple:
    """What this envelope MEANS, with the ordering token removed — the key the engine coalesces writes on.

    `last_event_ts` is deliberately excluded. The fold advances it on EVERY emit
    (`trade_cycle._build_dto`: `max(now_ns, native_ts, cycle.last_event_ts + 1)`) so that every transition
    strictly out-orders the previous durable write. That makes it an ordering token, not envelope content:
    two rows differing only in `last_event_ts` say exactly the same thing about the cycle.

    Coalescing on whole-row equality therefore NEVER fires in production, because no two emits are ever
    equal. That was the first version of the #564 fix — a no-op that passed its tests only because the
    double re-emitted an identical DTO, which the real projection cannot do. `test_the_real_projection_
    advances_last_event_ts_every_emit` pins the fact this exclusion depends on.

    Skipping a write leaves the stored `last_event_ts` behind the fold's, which is harmless: `upsert` guards
    monotonically, so the next write that carries real news has a strictly greater value and wins.
    """
    return dataclasses.astuple(dataclasses.replace(row, last_event_ts=0))


class CycleEnvelopeStore:
    """Async persistence for the cycle envelope. One instance per engine; call `upsert` on transitions and
    `load_active` once on startup. `session_factory_` is injectable (default = the shared app engine) so tests
    can point at a throwaway database."""

    def __init__(self, session_factory_=None) -> None:
        self._sf = session_factory_ or session_factory

    async def upsert(self, row: EnvelopeRow) -> None:
        """Idempotent write. INSERT the cycle, or UPDATE it only when this event is newer than the stored one
        (`last_event_ts` monotonic) — so at-least-once delivery and out-of-order retries converge, never regress."""
        values = {
            "cycle_id": row.cycle_id,
            "account_id": row.account_id,
            "client_id": row.client_id,
            "instrument_id": row.instrument_id,
            "strategy_id": row.strategy_id,
            "opened_ts": row.opened_ts,
            "closed_ts": row.closed_ts,
            "state": row.state,
            "close_reason": row.close_reason,
            "close_reason_source": row.close_reason_source,
            "import_status": row.import_status,
            "last_event_ts": row.last_event_ts,
        }
        stmt = pg_insert(TradeCycleEnvelope).values(**values)
        # On a re-seen cycle_id, advance the mutable fields ONLY if this event is newer — a stale write is a no-op.
        mutable = {
            k: stmt.excluded[k]
            for k in ("closed_ts", "state", "close_reason", "close_reason_source", "import_status", "last_event_ts")
        }
        mutable["updated_at"] = func.now()  # onupdate doesn't fire inside ON CONFLICT DO UPDATE — set it here
        stmt = stmt.on_conflict_do_update(
            index_elements=["cycle_id"],
            set_=mutable,
            where=TradeCycleEnvelope.last_event_ts < stmt.excluded["last_event_ts"],
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def load_active(
        self, client_id: str | None = None, strategy_id: str | None = None
    ) -> list[EnvelopeRow]:
        """The still-live cycles to re-seed on restart. A cycle is live only if it is NOT terminal: both
        `closed_ts IS NULL` AND `state != 'CLOSED'` — a no-fill ARMED cancel closes with no native ts, so state
        is the authoritative terminal signal, not closed_ts alone (codex-flagged). Closed cycles are history.

        `client_id`/`strategy_id` scope the load to ONE node's own cycles — the envelope is a shared table, so a
        node must NOT seed a projection with another account/client/strategy's active rows (codex-flagged)."""
        conditions = [
            TradeCycleEnvelope.closed_ts.is_(None),
            TradeCycleEnvelope.state != "CLOSED",
        ]
        if client_id is not None:
            conditions.append(TradeCycleEnvelope.client_id == client_id)
        if strategy_id is not None:
            conditions.append(TradeCycleEnvelope.strategy_id == strategy_id)
        async with self._sf() as session:
            result = await session.execute(select(TradeCycleEnvelope).where(*conditions))
            return [_to_row(r) for r in result.scalars().all()]


def envelope_from_dto(dto: TradeDTO) -> EnvelopeRow:
    """Map a projection TradeDTO → the durable envelope row the engine persists on each transition. The DTO
    already carries the full canonical identity + boundary; the envelope adds only the close reason. v0: any
    CLOSED with no explicit human/strategy intent defaults to `manual_walk_away` (source `default`) —
    `strategy_disarm` / `thesis_dead` need an explicit signal that doesn't exist in MANUAL v0."""
    close_reason = None
    close_reason_source = None
    if dto.state == "CLOSED":
        close_reason = "manual_walk_away"
        close_reason_source = "default"
    return EnvelopeRow(
        cycle_id=dto.cycle_id,
        account_id=dto.account_id,
        client_id=dto.client_id,
        instrument_id=dto.instrument_id,
        strategy_id=dto.strategy_id,
        opened_ts=dto.opened_ts,
        closed_ts=dto.closed_ts,
        state=dto.state,
        last_event_ts=dto.last_event_ts,
        close_reason=close_reason,
        close_reason_source=close_reason_source,
    )


def _to_row(orm: TradeCycleEnvelope) -> EnvelopeRow:
    return EnvelopeRow(
        cycle_id=orm.cycle_id,
        account_id=orm.account_id,
        client_id=orm.client_id,
        instrument_id=orm.instrument_id,
        strategy_id=orm.strategy_id,
        opened_ts=orm.opened_ts,
        closed_ts=orm.closed_ts,
        state=orm.state,
        last_event_ts=orm.last_event_ts,
        close_reason=orm.close_reason,
        close_reason_source=orm.close_reason_source,
        import_status=orm.import_status,
    )
