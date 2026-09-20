"""Internal position transfers between strategies (#80 spin-off).

Moving a position — or part of one — from one strategy to another. `EXTERNAL → MANUAL` ("claiming" a broker
position the cockpit didn't originate) is just the common case; `MOMENTUM → MANUAL` is the same operation.

A transfer is **bookkeeping, not trading**. It is applied as two INTERNAL orders and their fills: the source
strategy's position is reduced and the target's increased, both at the same price. Nothing is submitted to a
venue, so the broker net is identical before and after. What changes is which strategy natively owns the
quantity — and because the position genuinely moves, the target can then manage it like anything else.

Two prices are possible and the choice is a product decision, recorded per transfer:

* ``CARRY_OVER`` — both legs fill at the source's ``avg_px_open``. Nothing is realized; the basis migrates.
  The target inherits whatever unrealized P&L was already there. Easiest to reason about.
* ``MARKET`` — both legs fill at the current mark. The source crystallizes realized P&L up to the handoff and
  the target starts clean, so each strategy's number reflects only its own decisions.

Either way the price is **sourced programmatically**. Whoever picks the price picks which strategy keeps the
P&L, so an operator-supplied price would turn transfers into a P&L-shifting tool.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import PositionTransferEvent

PricingMode = Literal["MARKET", "CARRY_OVER"]
TransferState = Literal["PREPARED", "SOURCE_APPLIED", "DEST_APPLIED", "COMPLETED", "FAILED"]

# Every internal leg carries this tag so blotters, turnover and execution-quality metrics can exclude it.
# Untagged, a transfer would count as two fills and double the turnover it touches.
INTERNAL_TAG = "INTERNAL_TRANSFER"

# Ordered by progress, so recovery can compare how far a transfer got. FAILED deliberately ranks BELOW
# COMPLETED: a failure is only terminal if nothing was applied, and that is decided against the cache, not
# by the label. Ranking it alongside COMPLETED once hid a half-applied transfer from recovery entirely.
_PROGRESS: dict[str, int] = {
    "PREPARED": 0,
    "FAILED": 1,
    "SOURCE_APPLIED": 2,
    "DEST_APPLIED": 3,
    "COMPLETED": 4,
}


@dataclass(frozen=True)
class TransferRequest:
    """A validated intent to move quantity between two strategies."""

    transfer_id: str
    command_id: str
    account_id: str
    client_id: str
    instrument_id: str
    source_strategy_id: str
    target_strategy_id: str
    side: str  # LONG | SHORT — the side of the SOURCE position being moved
    quantity: Decimal
    pricing_mode: PricingMode
    transfer_px: Decimal
    source_avg_px: Decimal
    source_ts_last: int
    reason_code: str
    reversal_of_transfer_id: str | None = None

    @property
    def _short_id(self) -> str:
        """A compact, deterministic digest of the transfer id for the leg order ids.

        Nautilus caps identifiers at 36 characters and a leg id also carries a `-T` trade suffix, so the full
        command uuid doesn't fit. This HASHES rather than truncates: a prefix inherits any structure the id
        has, and a time-ordered id (uuid7) shares long prefixes, so truncation would collide between
        unrelated transfers far sooner than the bit count suggests. 20 hex chars = 80 bits, deterministic
        across restarts, and `TR-<20>-S` plus `-T` still fits the cap.
        """
        return hashlib.blake2s(self.transfer_id.encode(), digest_size=10).hexdigest()

    @property
    def source_order_id(self) -> str:
        """Deterministic, so replaying a transfer collides on the id instead of duplicating the leg."""
        return f"TR-{self._short_id}-S"

    @property
    def dest_order_id(self) -> str:
        return f"TR-{self._short_id}-D"


@dataclass(frozen=True)
class TransferProgress:
    """How far a transfer got, derived from its event stream."""

    transfer_id: str
    state: TransferState
    request: TransferRequest


def leg_sides(side: str) -> tuple[str, str]:
    """(source_leg_side, dest_leg_side) for a transfer of a `side` position.

    Moving a LONG means the source SELLs it away and the target BUYs it; a SHORT is the mirror. Getting this
    backwards would not just mis-book — it would double the account's exposure in the internal view while the
    broker net stayed flat, which is the failure mode worth being paranoid about.
    """
    if side.upper() == "LONG":
        return "SELL", "BUY"
    if side.upper() == "SHORT":
        return "BUY", "SELL"
    raise ValueError(f"cannot transfer side {side!r} — expected LONG or SHORT")


def resolve_transfer_px(
    pricing_mode: str, source_avg_px: Decimal, mark_px: Decimal | None
) -> Decimal | None:
    """The price both legs fill at, or None when it can't be sourced (the caller must then refuse).

    CARRY_OVER uses the source's average open price, so the basis migrates untouched and nothing is realized.
    MARKET needs a live mark; refusing without one is deliberate — falling back to the basis would silently
    turn a MARKET transfer into a CARRY_OVER and misstate both strategies' P&L.
    """
    if pricing_mode == "CARRY_OVER":
        return source_avg_px if source_avg_px > 0 else None
    if pricing_mode == "MARKET":
        return mark_px if mark_px and mark_px > 0 else None
    return None


def validate(
    *,
    quantity: Decimal,
    source_qty: Decimal,
    source_ts_last: int,
    current_ts_last: int | None,
    source_strategy_id: str,
    target_strategy_id: str,
    manageable_strategies: set[str],
    source_reducing_qty: Decimal,
    target_opposite_qty: Decimal,
    transfer_px: Decimal | None,
) -> str | None:
    """Reject reason, or None when the transfer may be applied. Runs on the ENGINE against its live cache.

    On working orders, only one case is actually unsafe: orders that REDUCE the SOURCE position. The source
    loses quantity, so a resting exit sized against the old position would, on trigger, sell more than the
    source still holds and flip it the other way. That is refused rather than silently resized — cancelling
    someone's protective exit as a side effect of a bookkeeping move is worse than saying no.

    Everything else is allowed. An opening order on either side is unaffected, and the TARGET only gains
    quantity, so nothing resting against it can be invalidated in a dangerous direction (an existing
    protective stop merely ends up under-sized, which is a risk-sizing question, not a correctness one).
    """
    if quantity <= 0:
        return "transfer quantity must be positive"
    if source_qty <= 0:
        return "source strategy holds no position on that side"
    if quantity > source_qty:
        return f"cannot transfer {quantity} — source holds {source_qty}"
    if target_strategy_id == source_strategy_id:
        return "source and target are the same strategy — nothing to move"
    if target_opposite_qty > 0:
        # Under NETTING the destination leg would NET against that position rather than add to it: moving a
        # long into a strategy that is short the same instrument closes part of the short instead of handing
        # it the position. That is a change in exposure disguised as bookkeeping, so it is refused.
        return (
            f"the target already holds {target_opposite_qty} on the OPPOSITE side of this instrument — the "
            "move would net against it rather than transfer the position. Flatten that first"
        )
    if target_strategy_id not in manageable_strategies:
        return (
            f"{target_strategy_id} is not a registered strategy — transferring into it would strand the "
            "position with no owner able to manage it"
        )
    if current_ts_last is not None and current_ts_last != source_ts_last:
        return "source position moved since it was read — re-read and retry"
    remaining = source_qty - quantity
    if source_reducing_qty > remaining:
        return (
            f"the source has {source_reducing_qty} resting in reducing orders but would keep only {remaining} "
            "after this move — they would sell more than it holds and flip it. Cancel or resize them first"
        )
    if transfer_px is None or transfer_px <= 0:
        return "no valid transfer price available — refusing rather than guessing one"
    return None


async def record(
    session: AsyncSession, req: TransferRequest, event_type: str, error: str | None = None
) -> None:
    """Append one outbox event and COMMIT it. Never updates — the sequence IS the state.

    Commits here rather than leaving it to the caller: the outbox only works if an event is durable BEFORE
    the next native effect runs. A flush that a later crash rolls back would leave the engine-side effect
    applied with no record of it, which is precisely the state recovery cannot reason about.
    """
    session.add(
        PositionTransferEvent(
            event_type=event_type,
            transfer_id=req.transfer_id,
            command_id=req.command_id,
            account_id=req.account_id,
            client_id=req.client_id,
            instrument_id=req.instrument_id,
            source_strategy_id=req.source_strategy_id,
            target_strategy_id=req.target_strategy_id,
            side=req.side,
            quantity=req.quantity,
            pricing_mode=req.pricing_mode,
            transfer_px=req.transfer_px,
            source_avg_px=req.source_avg_px,
            source_order_id=req.source_order_id,
            dest_order_id=req.dest_order_id,
            source_ts_last=req.source_ts_last,
            reversal_of_transfer_id=req.reversal_of_transfer_id,
            reason_code=req.reason_code,
            actor="engine",  # server-stamped; a caller-supplied identity would make the audit spoofable
            error=error,
        )
    )
    await session.commit()


def _to_request(row: PositionTransferEvent) -> TransferRequest:
    return TransferRequest(
        transfer_id=row.transfer_id,
        command_id=row.command_id,
        account_id=row.account_id,
        client_id=row.client_id,
        instrument_id=row.instrument_id,
        source_strategy_id=row.source_strategy_id,
        target_strategy_id=row.target_strategy_id,
        side=row.side,
        quantity=Decimal(row.quantity),
        pricing_mode=row.pricing_mode,
        transfer_px=Decimal(row.transfer_px),
        source_avg_px=Decimal(row.source_avg_px),
        source_ts_last=row.source_ts_last,
        reason_code=row.reason_code,
        reversal_of_transfer_id=row.reversal_of_transfer_id,
    )


def fold(rows: list[PositionTransferEvent]) -> list[TransferProgress]:
    """Fold the event stream into one progress record per transfer, furthest state wins."""
    best: dict[str, PositionTransferEvent] = {}
    for row in rows:
        prior = best.get(row.transfer_id)
        if prior is None or _PROGRESS[row.event_type] >= _PROGRESS[prior.event_type]:
            best[row.transfer_id] = row
    return [
        TransferProgress(transfer_id=tid, state=row.event_type, request=_to_request(row))
        for tid, row in best.items()
    ]


async def all_transfers(session: AsyncSession) -> list[TransferProgress]:
    rows = (
        (await session.execute(select(PositionTransferEvent).order_by(PositionTransferEvent.id)))
        .scalars()
        .all()
    )
    return fold(list(rows))


def incomplete(progress: list[TransferProgress]) -> list[TransferProgress]:
    """Transfers that may have got part-way and must be resolved before trading resumes.

    A transfer stuck after the source leg has taken quantity out of one strategy without giving it to the
    other — the internal books do not add up to the broker net until it is finished.

    FAILED counts too. `_apply_transfer` only records it once a leg has been ATTEMPTED, so a failure can sit
    on top of an applied source leg; treating the label as terminal is what let that state hide. Whether
    anything actually landed is decided by looking for the legs in the cache, which recovery does — and
    recovery is idempotent, so a genuinely empty FAILED costs one lookup and changes nothing.
    """
    return [p for p in progress if p.state != "COMPLETED"]


async def already_applied(session: AsyncSession, command_id: str) -> bool:
    """Transport idempotency — a redelivered command must not book a second transfer."""
    hit = await session.execute(
        select(PositionTransferEvent.id).where(PositionTransferEvent.command_id == command_id)
    )
    return hit.first() is not None
