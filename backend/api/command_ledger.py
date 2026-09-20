"""CommandLedgerStore (#78) — durable command idempotency.

In-memory dedup (`_seen_orders`) is lost on restart, so a restart + still-pending ui:commands entries could
DOUBLE-SEND an order. The engine RESERVEs a row here before acting on a command; a re-delivered `command_id`
(transport dup, at-least-once redelivery of the same stream entry) or a re-used `client_order_id` (economic
dup, a different command asking for the same order) is rejected → skip. Async over the shared asyncpg engine,
same pattern as CycleEnvelopeStore. Fail-CLOSED at the call site: if this store is unreachable, the engine must
NOT submit (a late surprise order is worse than requiring a deliberate retry).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from api.db.engine import session_factory
from api.db.models import CommandLedgerEntry

_COID_INDEX = "uq_command_ledger_client_order_id"


class Reserve(str, Enum):
    RESERVED = "reserved"  # first time — proceed to act on the command
    DUPLICATE_COMMAND = "duplicate_command"  # same command_id already seen (transport redelivery)
    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"  # same order asked for under a different command


@dataclass(frozen=True)
class ReserveResult:
    """The outcome of a reserve. On a DUPLICATE_COMMAND, `existing_status` is the stored status of the prior
    attempt (RESERVED=interrupted mid-submit / DONE / REJECTED) and `hash_mismatch` flags a same-id-different-
    payload anomaly — the caller MUST mirror the real prior outcome, never blanket-accept (codex-flagged: a
    RESERVED-but-never-DONE redelivery shown as accepted is a silent lost order)."""

    outcome: Reserve
    existing_status: str | None = None
    hash_mismatch: bool = False


def payload_hash(payload: dict) -> str:
    """Stable hash of a command payload — surfaces a same-command_id-different-payload anomaly."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _is_client_order_id_violation(exc: IntegrityError) -> bool:
    """True ONLY for the client_order_id unique-index violation (SQLSTATE 23505 on that constraint). Any other
    IntegrityError (NOT NULL, a future check, a different unique) must propagate → fail-closed, never be masked
    as a harmless duplicate (codex-flagged)."""
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    constraint = getattr(orig, "constraint_name", None) or getattr(cause, "constraint_name", None)
    return sqlstate == "23505" and constraint == _COID_INDEX


class CommandLedgerStore:
    def __init__(self, session_factory_=None) -> None:
        self._sf = session_factory_ or session_factory

    async def reserve(
        self,
        command_id: str,
        command_type: str,
        client_order_id: str | None,
        payload_hash_: str,
        entry_id: str,
    ) -> ReserveResult:
        """Atomically claim this command. RESERVED on first sight (status defaults RESERVED — the caller marks
        DONE/REJECTED after acting). A duplicate command_id returns the PRIOR row's status so the caller can
        mirror it; a duplicate client_order_id (economic) is rejected. The INSERT does the dedup — no
        read-then-write race."""
        stmt = (
            pg_insert(CommandLedgerEntry)
            .values(
                command_id=command_id,
                command_type=command_type,
                client_order_id=client_order_id,
                payload_hash=payload_hash_,
                entry_id=entry_id,
                status="RESERVED",
            )
            .on_conflict_do_nothing(index_elements=["command_id"])
            .returning(CommandLedgerEntry.command_id)
        )
        async with self._sf() as session:
            try:
                result = await session.execute(stmt)
                inserted = result.scalar_one_or_none() is not None
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_client_order_id_violation(exc):
                    return ReserveResult(Reserve.DUPLICATE_CLIENT_ORDER_ID)
                raise  # any other integrity error → propagate → caller fails closed
            if inserted:
                return ReserveResult(Reserve.RESERVED)
            existing = await session.get(CommandLedgerEntry, command_id)  # transport redelivery — mirror it
            return ReserveResult(
                Reserve.DUPLICATE_COMMAND,
                existing_status=existing.status if existing else None,
                hash_mismatch=bool(existing and existing.payload_hash != payload_hash_),
            )

    async def mark(self, command_id: str, status: str, error: str | None = None) -> None:
        """Record the terminal outcome (DONE / REJECTED) for audit. Best-effort — the RESERVE already prevents
        double-send; the status is for the audit trail."""
        async with self._sf() as session:
            row = await session.get(CommandLedgerEntry, command_id)
            if row is not None:
                row.status = status
                row.error = (error or "")[:256] or None
                await session.commit()
