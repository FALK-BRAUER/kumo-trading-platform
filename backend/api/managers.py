"""Manager framework (#55, first slice) — attach→watch→trigger→act automation with a leash.

The primitive #55 specified but never had a concrete shape. A manager is a small piece of automation:
something attaches it to a position with typed params (`ATTACHED`), it watches a trigger condition, and when
that condition is met it applies an effect — usually an order. Every manager KIND (deferred_flatten today;
PEAK #46 / STOP-AND-REENTER #47 later) shares this same lifecycle, registry, and dispatch shape; only the
trigger predicate and the apply effect differ per kind.

Two tables back this, not one — `Manager` (current state, fast query) and `ManagerEvent` (append-only). This
codebase already learned why on `PositionTransferEvent` earlier the same day: a manager's `apply()` has the
identical partial-failure shape a transfer leg does — an order can be submitted successfully and then the DB
write recording that crashes. An INTENT_RECORDED event lands BEFORE the effect, carrying the deterministic
order id, so a crash is recoverable by checking the durable Nautilus cache for that id, not guessed at.

LEASH (AUTO/CONFIRM/ALERT) is a state-machine dimension, not a special case: AUTO skips straight from ARMED to
APPLYING when the trigger fires; CONFIRM stops at PROPOSED until a human command approves or rejects it; ALERT
stops at PROPOSED permanently (flags, never acts). Only AUTO is exercised by `deferred_flatten` today — the
slide-to-flatten IS the human confirmation; only the TIMING is deferred, not the decision — but the reducer
below is tested against all three so the machine is validated ahead of #46/#47 needing CONFIRM for real.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

_log = logging.getLogger(__name__)

ManagerState = Literal["ARMED", "PROPOSED", "APPROVED", "APPLYING", "APPLIED", "FAILED", "CANCELLED"]
Leash = Literal["AUTO", "CONFIRM", "ALERT"]
ManagerEventType = Literal[
    "ATTACHED", "PROPOSED", "APPROVED", "REJECTED", "INTENT_RECORDED", "APPLIED", "FAILED", "CANCELLED",
    "CANCEL_REQUESTED",
]


@dataclass(frozen=True)
class ManagerRow:
    """A manager instance, read from the `manager` table."""

    manager_id: str
    kind: str
    kind_version: int
    account_id: str
    client_id: str
    instrument_id: str
    strategy_id: str
    cycle_id: str | None
    leash: Leash
    params: dict
    state: ManagerState
    # Surfaced for #47 (STOP-AND-REENTER): distinguishing "closed via THIS manager's own chain" from an
    # unrelated older closing fill on the same instrument+strategy needs to know when this row was attached.
    # Already a DB column (`Manager.created_at`); just wasn't exposed on the row before this had a caller.
    created_at: datetime
    # WHEN IT LAST CHANGED (#400). For a terminal row — FAILED or CANCELLED — this is when it DIED,
    # because nothing touches it afterwards. `Manager.updated_at` has existed on the model since it was
    # written, with `onupdate=func.now()`; this projection simply never carried it, and #400 added it to
    # the API response reading `r.updated_at` off THIS class. The route 500'd on every call.
    #
    # THE BUG WAS A CLASS AWAY FROM THE TEST. #400's suite asserted `hasattr(Manager, "updated_at")` —
    # true, and about the ORM model — while the route reads a `ManagerRow`. A green suite and a dead
    # endpoint. Test the seam, not the unit.
    updated_at: datetime | None = None


class ManagerHandler(Protocol):
    """What a manager KIND provides. Deliberately minimal — no engine-facade abstraction guessing at what
    PEAK's price-tick hooks or STOP-REENTER's fill-event hooks will need; those extend this when they exist,
    not before."""

    def validate_params(self, params: dict) -> str | None:
        """Reject reason, or None. Called at ATTACH — a malformed params blob must never reach the table."""
        ...

    def client_order_id_for(self, manager_id: str) -> str:
        """Deterministic — a redelivered or replayed apply collides on the id instead of duplicating."""
        ...

    async def trigger_met(self, strategy, row: ManagerRow) -> bool:
        """Pure/cheap — called every dispatch tick for every ARMED row of this kind. No blocking I/O beyond
        a clock read or a cache lookup already in memory."""
        ...

    async def apply(self, strategy, row: ManagerRow) -> tuple[ManagerState, str | None]:
        """(new_state, detail). Runs once the trigger fired and the row was claimed (state already APPLYING).
        Must be safe to call from a startup recovery pass with a row whose prior apply may have partially
        landed — same idempotency contract flatten.decide() already has."""
        ...


_REGISTRY: dict[str, ManagerHandler] = {}


def register_manager(kind: str, handler: ManagerHandler) -> None:
    """Idempotent by key, matching every other registry in this codebase (detail registry, tile registry) —
    a second registration of the same kind is a no-op, not a crash (module re-import under a dev reloader)."""
    _REGISTRY.setdefault(kind, handler)


def handler_for(kind: str) -> ManagerHandler | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    """Every kind the dispatch loop should sweep — new kinds (#46/#47) need no change to the dispatch code,
    only a `register_manager()` call at import time."""
    return list(_REGISTRY.keys())


def next_state(current: ManagerState, event: ManagerEventType, leash: Leash) -> ManagerState:
    """Pure reducer — the state machine, decoupled from any I/O so it can be tested against every leash level
    even though only AUTO has a real caller today (code review: don't let the machine be generic in name only).
    """
    if event == "ATTACHED":
        return "ARMED"
    if event == "CANCELLED":
        return "CANCELLED"

    if current == "ARMED" and event in ("PROPOSED", "INTENT_RECORDED"):
        if leash == "AUTO":
            # AUTO never stops at PROPOSED — the trigger firing IS the decision, apply immediately.
            return "APPLYING" if event == "INTENT_RECORDED" else "ARMED"
        return "PROPOSED"  # CONFIRM / ALERT always stop here, regardless of which event named it

    if current == "PROPOSED":
        if event == "APPROVED":
            return "APPROVED"
        if event == "REJECTED":
            return "CANCELLED"
        return current  # ALERT: nothing else ever moves it — flags, never acts

    if current == "APPROVED" and event == "INTENT_RECORDED":
        return "APPLYING"

    if current == "APPLYING":
        if event == "APPLIED":
            return "APPLIED"
        if event == "FAILED":
            return "FAILED"

    return current  # no legal transition for this (state, event) — stay put rather than guess


async def attach(
    session,
    *,
    manager_id: str,
    kind: str,
    account_id: str,
    client_id: str,
    instrument_id: str,
    strategy_id: str,
    cycle_id: str | None,
    leash: Leash,
    params: dict,
    command_id: str,
    kind_version: int = 1,
) -> None:
    """Create a manager instance at ARMED. Idempotency on the ATTACH itself is the caller's job — this
    reuses the existing `CommandLedgerStore` (#78), the SAME primitive order commands already use, rather
    than a second mechanism. A caller that already RESERVEd this `command_id` and got DUPLICATE_COMMAND
    never calls this at all; a fresh `manager_id` is only minted on first sight."""
    from api.db.models import Manager, ManagerEvent

    session.add(
        Manager(
            manager_id=manager_id,
            kind=kind,
            kind_version=kind_version,
            account_id=account_id,
            client_id=client_id,
            instrument_id=instrument_id,
            strategy_id=strategy_id,
            cycle_id=cycle_id,
            leash=leash,
            params=params,
            state="ARMED",
        )
    )
    session.add(ManagerEvent(manager_id=manager_id, event_type="ATTACHED", command_id=command_id, detail=None))
    await session.commit()


async def claim(session, manager_id: str) -> bool:
    """Atomic ARMED -> APPLYING. True if THIS call claimed it — false if another dispatch tick (or a
    concurrent claim on the same row) already did. The caller may have found this row via a plain SELECT
    (`armed_of_kind`) that is NOT itself atomic with this call — that's fine: the mutation is a single
    UPDATE...WHERE, so Postgres's row-level locking — not application code — decides which of two racing
    claims wins. A caller that gets False back MUST skip the row, never proceed to apply() regardless."""
    from sqlalchemy import update

    from api.db.models import Manager

    result = await session.execute(
        update(Manager).where(Manager.manager_id == manager_id, Manager.state == "ARMED").values(state="APPLYING")
    )
    await session.commit()
    return result.rowcount == 1


_CANCELABLE_STATES = ("ARMED", "PROPOSED", "APPROVED")


async def cancel_if_cancelable(session, manager_id: str) -> ManagerState | None:
    """Atomic conditional cancel — mirrors `claim()`'s shape (a single UPDATE...WHERE, not read-then-write).
    A concurrent dispatch tick's `claim()` (ARMED -> APPLYING) racing a cancel command must NOT let a stale
    read here cancel a row that's actively being applied — the reducer's CANCELLED transition is
    unconditional BY DESIGN once reached (`next_state`), so the race has to be closed BEFORE that, not
    patched after (code review, #47: a read-then-write cancel could overwrite a real APPLIED outcome with a
    misleading CANCELLED). Deliberately excludes APPLYING from the cancelable set — a row already claimed by
    a dispatch tick is past the point cancellation can safely intervene.

    Returns `"CANCELLED"` if THIS call won the race, or `None` if the row was already APPLYING/terminal (or
    doesn't exist) — the caller must not report success in that case."""
    from sqlalchemy import update

    from api.db.models import Manager, ManagerEvent

    result = await session.execute(
        update(Manager)
        .where(Manager.manager_id == manager_id, Manager.state.in_(_CANCELABLE_STATES))
        .values(state="CANCELLED")
    )
    if result.rowcount != 1:
        await session.commit()  # nothing changed, but still release the transaction cleanly
        return None
    session.add(ManagerEvent(manager_id=manager_id, event_type="CANCELLED", detail=None))
    await session.commit()
    return "CANCELLED"


async def record_event(
    session, manager_id: str, event_type: ManagerEventType, leash: Leash, detail: dict | None = None
) -> ManagerState:
    """Append one event and advance `manager.state` via the pure reducer. Returns the new state so the
    caller doesn't need a second read."""
    from sqlalchemy import update

    from api.db.models import Manager, ManagerEvent

    row = await session.get(Manager, manager_id)
    if row is None:
        raise ValueError(f"no manager {manager_id!r} to record an event against")
    new_state = next_state(row.state, event_type, leash)

    event = ManagerEvent(manager_id=manager_id, event_type=event_type, detail=detail)
    session.add(event)
    await session.flush()  # need event.id before writing it as last_event_id

    await session.execute(
        update(Manager).where(Manager.manager_id == manager_id).values(state=new_state, last_event_id=event.id)
    )
    await session.commit()
    return new_state


def _to_row(m) -> ManagerRow:
    return ManagerRow(
        manager_id=m.manager_id,
        kind=m.kind,
        kind_version=m.kind_version,
        account_id=m.account_id,
        client_id=m.client_id,
        instrument_id=m.instrument_id,
        strategy_id=m.strategy_id,
        cycle_id=m.cycle_id,
        leash=m.leash,
        params=m.params,
        state=m.state,
        created_at=m.created_at,
        updated_at=getattr(m, "updated_at", None),
    )


# A CHAIN can span more than one kind. STOP-AND-REENTER alternates `stop_reenter_watch` →
# `stop_reenter_rearm` → `stop_reenter_watch`, and the UI toggles both as ONE control
# (`PositionDetail.tsx` treats either kind as the active manager). Cancelling only the kind the UI
# happened to name would leave the other half of the chain armed and the toggle springing back to ON —
# the same defect `chain_cancelled` exists to fix, one level up.
_CHAIN_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"stop_reenter_watch", "stop_reenter_rearm"}),
)


def family_of(kind: str) -> frozenset[str]:
    """Every kind belonging to the same logical chain as `kind` — itself, if it chains alone."""
    for fam in _CHAIN_FAMILIES:
        if kind in fam:
            return fam
    return frozenset({kind})


async def chain_cancelled(session, kind: str, instrument_id: str, strategy_id: str, cycle_id: str | None, since) -> bool:
    """Has the operator turned this manager kind OFF for this position since `since`?

    Some kinds are CHAINS, not single rows: `_PeakWatch` hands off to a freshly attached successor after
    every trim or tighten, so one logical "PEAK is on" is a sequence of manager_ids. Cancel targets ONE
    id, which meant OFF could not stop it — cancel row N, row N+1 was already attached (or was attached
    moments later by an apply that was already in flight), and the toggle sprang back to ON.

    It also loses a race by design: `cancel_if_cancelable` deliberately refuses a row already APPLYING,
    because a claimed row is past the point cancellation can safely intervene. With a 30s dispatch tick
    that refusal is common, and the successor then spawned regardless.

    So a spawn asks this instead of trusting its own row's state: if ANY row of this kind for this
    position was cancelled after the current chain link was created, the operator has said stop — do not
    hand off. The in-flight orders of the current apply still stand (they cannot be unsent), but the
    chain ends there.

    Reads the EVENT log, not `manager.state`. A cancel that arrived while every row was APPLYING flips
    no state at all (`cancel_if_cancelable` refuses a claimed row, by design) — but the operator still
    pressed OFF, and that intent has to survive. `cancel_chain` always records the event, so the handoff
    sees it either way.
    """
    from sqlalchemy import select

    from api.db.models import Manager, ManagerEvent

    row = (
        await session.execute(
            select(ManagerEvent.id)
            .join(Manager, Manager.manager_id == ManagerEvent.manager_id)
            .where(
                Manager.kind.in_(family_of(kind)),
                Manager.instrument_id == instrument_id,
                Manager.strategy_id == strategy_id,
                (Manager.cycle_id == cycle_id) if cycle_id is not None else Manager.cycle_id.is_(None),
                ManagerEvent.event_type.in_(("CANCELLED", "CANCEL_REQUESTED")),
                ManagerEvent.event_ts >= since,
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def cancel_chain(session, kind: str, instrument_id: str, strategy_id: str, cycle_id: str | None) -> int:
    """Cancel EVERY cancelable row of this kind for this position+cycle. Returns how many were cancelled.

    The toggle's OFF path. Cancelling a single manager_id is not enough for a chaining kind (see
    `chain_cancelled`), and the UI can only ever name one — whichever row it happened to consider
    "active" when it rendered.

    ONE conditional `UPDATE ... WHERE state IN (...) RETURNING` — never SELECT-then-UPDATE. A read
    followed by a write reintroduces exactly the race `cancel_if_cancelable` was written to avoid: a
    dispatch tick claiming a row between the two statements would have its APPLYING (or even APPLIED)
    state overwritten with CANCELLED while its orders were already going to the venue. (codex review,
    Critical.)

    Scoped by `cycle_id` as well as instrument+strategy: a stale OFF for a finished chain must not kill a
    chain armed afterwards on the same name. (codex review, High.)
    """
    from sqlalchemy import update

    from api.db.models import Manager, ManagerEvent

    stmt = (
        update(Manager)
        .where(
            Manager.kind.in_(family_of(kind)),
            Manager.instrument_id == instrument_id,
            Manager.strategy_id == strategy_id,
            Manager.state.in_(_CANCELABLE_STATES),
        )
        .values(state="CANCELLED")
        .returning(Manager.manager_id)
    )
    stmt = stmt.where(Manager.cycle_id == cycle_id) if cycle_id is not None else stmt.where(Manager.cycle_id.is_(None))
    ids = list((await session.execute(stmt)).scalars().all())
    for mid in ids:
        session.add(ManagerEvent(manager_id=mid, event_type="CANCELLED", detail=None))
    if not ids:
        # Nothing cancelable — every row is APPLYING or already terminal. Record the INTENT anyway
        # against the most recent row for this chain, so an apply in flight still sees OFF at its
        # handoff check. Without this the toggle is a no-op exactly when the race makes it matter.
        #
        # A DISTINCT event type, not `CANCELLED`: overloading the terminal event would make an event-log
        # fold of INTENT_RECORDED -> CANCELLED -> APPLIED settle on CANCELLED, corrupting replay for a
        # manager that actually applied. (codex review, Medium.)
        from sqlalchemy import select

        recent_stmt = (
            select(Manager.manager_id)
            .where(
                Manager.kind.in_(family_of(kind)),
                Manager.instrument_id == instrument_id,
                Manager.strategy_id == strategy_id,
            )
            .order_by(Manager.created_at.desc())
            .limit(1)
        )
        # Same cycle scope as the UPDATE above. Without it the intent could land on a DIFFERENT cycle's
        # row — failing to stop the chain actually in flight, or stopping a newer one that the operator
        # never touched. (codex review, High.)
        recent_stmt = (
            recent_stmt.where(Manager.cycle_id == cycle_id)
            if cycle_id is not None
            else recent_stmt.where(Manager.cycle_id.is_(None))
        )
        recent = (await session.execute(recent_stmt)).scalars().first()
        if recent is not None:
            session.add(ManagerEvent(manager_id=recent, event_type="CANCEL_REQUESTED", detail={"intent_only": True}))
    await session.commit()
    return len(ids)


async def record_trim(session, manager_id: str, trim_count: int) -> bool:
    """Durably record that this manager has now taken `trim_count` trims (#266).

    Written by the row that PERFORMED the trim, at the moment it submits, rather than being carried only
    by the successor it attaches. The successor is attached after `chain_cancelled` is consulted, so a
    PEAK switched off mid-apply spends a trim that no row would otherwise record — and a later re-arm of
    the same cycle would be handed back a budget it had already used.

    Returns True if the write landed. It must NOT break the trading path — the trim has already been
    submitted and cannot be unsent — but it cannot be a silent no-op either (codex review, High): this is
    the only durable record in the OFF-mid-apply case, and losing it hands the spent trim back to the
    next re-arm. So a failure is returned and logged loudly, and the caller says so in its outcome, where
    the operator can see it.
    """
    from sqlalchemy import update

    from api.db.models import Manager

    try:
        result = await session.execute(
            update(Manager)
            .where(Manager.manager_id == manager_id)
            .values(params=Manager.params.op("||")({"trim_count": trim_count}))
        )
        await session.commit()
        if result.rowcount != 1:
            # A zero-row UPDATE commits perfectly cleanly. Treating that as success would silence both
            # the log and the operator-visible warning for a missing or stale manager_id — the case most
            # likely to lose the count. (codex review, Medium.)
            _log.error(
                "recording trim %s for manager %s matched %s rows — this cycle's trim budget may "
                "over-count by one if PEAK is re-armed", trim_count, manager_id, result.rowcount,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — never break the order path; see above
        _log.error(
            "could not record trim %s for manager %s: %r — this cycle's trim budget may over-count "
            "by one if PEAK is re-armed", trim_count, manager_id, exc,
        )
        return False


def max_trim_count(param_sets) -> int:
    """The highest `trim_count` across a cycle's manager rows — the number of trims already taken.

    Pure, so the reduction can be tested without a database (the same split the rest of this module
    uses for the reducer). Each successor records its predecessor's count plus one, so the maximum is
    the running total rather than a sum.

    A row with a missing or unparsable `trim_count` contributes 0 rather than raising: a single malformed
    params blob must not make PEAK unarmable.
    """
    spent = 0
    for params in param_sets:
        try:
            spent = max(spent, int((params or {}).get("trim_count") or 0))
        except (TypeError, ValueError):
            continue
    return spent


async def trims_spent_on_cycle(
    session, kind: str, instrument_id: str, strategy_id: str, cycle_id: str | None
) -> int:
    """How many trims this position's CURRENT CYCLE has already used (#266).

    `trim_max` is meant to be a budget for the position — the operator's reasoning was transaction cost, "not
    tiny repeated nibbles". But it was only ever checked against `trim_count`, which lives on a chain and
    restarts at 0 whenever PEAK is armed afresh. So the cap bounded one chain's nibbles while the
    position could be nibbled without limit:

        OKTA 2026-08-12  chain 1 spent its two trims, 58 -> 6
                         re-armed 13:48:12, trim_count back to 0
                         chain 2 spent two more within 34 seconds, 6 -> 2, at a HIGHER price

    The cycle is the right scope because it already survives re-arming: it has a durable id, and it ends
    exactly when the position does. Returns the highest `trim_count` recorded by any row of this kind on
    this cycle, which is the number of trims already taken — each successor records its predecessor's
    count plus one.

    A null `cycle_id` (legacy rows predating #68) cannot be scoped, so it reports 0 rather than guessing
    — the old per-chain behaviour, which is the safe direction: it can only ever permit trims, never
    invent extra ones.
    """
    if cycle_id is None:
        return 0
    from sqlalchemy import select

    from api.db.models import Manager

    rows = (
        await session.execute(
            select(Manager.params).where(
                Manager.kind == kind,
                Manager.instrument_id == instrument_id,
                Manager.strategy_id == strategy_id,
                Manager.cycle_id == cycle_id,
            )
        )
    ).scalars().all()
    return max_trim_count(rows)


async def get(session, manager_id: str) -> ManagerRow | None:
    """One manager row by id, or None."""
    from sqlalchemy import select

    from api.db.models import Manager

    m = (await session.execute(select(Manager).where(Manager.manager_id == manager_id))).scalars().first()
    return _to_row(m) if m is not None else None


async def armed_of_kind(session, kind: str, strategy_id: str) -> list[ManagerRow]:
    """Candidates for this dispatch tick — filtered to THIS strategy at the query level (not left to each
    handler to remember): a shared dispatch loop must never claim or apply a row belonging to a strategy it
    doesn't own (code review — the coordinator, #72, is the only correct place for real cross-strategy
    dispatch; this process is not that)."""
    from sqlalchemy import select

    from api.db.models import Manager

    rows = (
        await session.execute(
            select(Manager).where(Manager.kind == kind, Manager.strategy_id == strategy_id, Manager.state == "ARMED")
        )
    ).scalars().all()
    return [_to_row(m) for m in rows]


async def applying_of_kind(session, kind: str, strategy_id: str) -> list[ManagerRow]:
    """Rows a crash may have left mid-apply — startup reconciliation candidates."""
    from sqlalchemy import select

    from api.db.models import Manager

    rows = (
        await session.execute(
            select(Manager).where(
                Manager.kind == kind, Manager.strategy_id == strategy_id, Manager.state == "APPLYING"
            )
        )
    ).scalars().all()
    return [_to_row(m) for m in rows]


async def last_event_detail(session, manager_id: str, event_type: ManagerEventType) -> dict | None:
    """The most recent event of a type for this manager — used by recovery to read back the
    deterministic order id an INTENT_RECORDED event carried before checking the durable cache for it."""
    from sqlalchemy import select

    from api.db.models import ManagerEvent

    row = (
        await session.execute(
            select(ManagerEvent)
            .where(ManagerEvent.manager_id == manager_id, ManagerEvent.event_type == event_type)
            .order_by(ManagerEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.detail if row else None


async def all_managers(session) -> list[ManagerRow]:
    """Every manager instance, whatever its state — `GET /managers` reads this directly (a screen re-opened
    hours later can't rely on the original command's ack, which has long expired)."""
    from sqlalchemy import select

    from api.db.models import Manager

    rows = (await session.execute(select(Manager).order_by(Manager.created_at))).scalars().all()
    return [_to_row(m) for m in rows]


async def revert_to_armed(session, manager_id: str, detail: dict | None = None) -> None:
    """Recovery path: an APPLYING row whose effect was never actually seen in the cache reverts to ARMED so
    the next dispatch tick picks it up again, with a FAILED event recording why (audit — not silently
    forgotten)."""
    from sqlalchemy import update

    from api.db.models import Manager, ManagerEvent

    session.add(ManagerEvent(manager_id=manager_id, event_type="FAILED", detail=detail))
    await session.execute(update(Manager).where(Manager.manager_id == manager_id).values(state="ARMED"))
    await session.commit()
