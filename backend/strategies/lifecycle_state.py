"""The writer for a runner-produced lifecycle state (#459 part 2).

TODAY THAT IS MOMENTUM AND BCTROT ONLY, both through `SessionGateway`. QC345 reads lifecycle and
never writes it; QC27 passes a read callback upstream. Calling this "the one writer for every
gateway" would be the overclaim this codebase keeps making -- a declaration agreeing with an intent
while the connection is severed. It is written to be the single writer WHEN the others gain a risk
control that can produce a state; it is not one yet.

`pgrunner` never persists. `self.lifecycle.halt(...)` mutates an in-memory dataclass that the gateway
built fresh for the session and returns it in `SessionResult`; writing it is cockpit's job. So a
risk HALT lives exactly as long as the session that raised it unless something here stores it.

**A risk control that fails by never halting is the worst way for one to break. It does not fail
loudly; it fails by continuing.**

COMPARE-AND-SET, because a blind upsert erased the operator once already. The session reads lifecycle
at the start and can run for many seconds; an operator who hits PAUSE or HALT in that window had their
change written to Postgres and then overwritten by the old TRADING state going back. The stop button
worked, the UI showed it working, and the next session traded anyway. If the row still says what it
said, ours is the newer fact. If it does not, a human changed it while we ran and theirs wins --
always, including when ours is a HALT, because an operator HALT and our HALT agree on what matters.

AND AN ABSENT ROW IS NOT A CHANGED ROW. The original was `UPDATE ... WHERE state = expected` and
`return rowcount > 0`. With no row at all the rowcount is 0, which is indistinguishable from "the
operator moved it" -- so the HALT was dropped AND the caller journalled "the operator changed the
state during the session", naming a human who had done nothing. That is worse than losing the halt
quietly, because it explains the loss with a false cause.

It matters because of the default it collides with: an absent row reads as TRADING (2026-08-19,
`momentum._lifecycle`), on the reasoning that anything ever moved to HALTED or DISABLED HAS a row.
That reasoning is sound only if a HALT reliably WRITES a row -- which is this function. Without the
insert, "absent means TRADING" and "we could not persist the halt" combine into a lane that halts on
risk and is TRADING again at the next session, with no record of why.

Not hypothetical: on ibkr-paper-retired today, `exec_strategy_state` holds two rows while the node registers
five lanes. Three lanes there have no row, so for any of them this is the live path, not the edge one.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("kumo.lifecycle_state")


#: What actually happened, because "False" had three meanings and the caller told one story for all.
SAVED = "saved"
OPERATOR_WON = "operator_won"
CONTRADICTION = "contradiction"


async def save_if_unchanged(sm, strategy_id: str, life, expected) -> str:
    """Persist `life`, but only over `expected`. Returns SAVED / OPERATOR_WON / CONTRADICTION.

    NOT A BOOL. The original returned `rowcount > 0`, and the caller rendered every falsy answer as
    "the operator changed the state during the session -- theirs stands". That sentence was true for
    one of the three cases and named an innocent human in the other two. Reporting a real cause is
    the whole point of this fix; collapsing the outcomes again here would reintroduce it one level
    down, which is exactly what the first version of this function did.
    """
    from kumo_strategies.runtime.executor.lifecycle import State
    from kumo_strategies.runtime.executor.store import StrategyState, utcnow
    from sqlalchemy import insert, select, update

    async with sm() as s, s.begin():
        res = await s.execute(
            update(StrategyState)
            .where(StrategyState.strategy_id == strategy_id,
                   StrategyState.state == expected.value)
            .values(state=life.state.value, reason=life.reason, updated_at=utcnow()))
        if res.rowcount > 0:
            return SAVED

        # Rowcount 0 has TWO causes and they need opposite handling. Ask which.
        present = (await s.execute(
            select(StrategyState.state)
            .where(StrategyState.strategy_id == strategy_id))).scalars().first()
        if present is not None:
            # A human moved it while we ran. Theirs stands.
            return OPERATOR_WON

        # No row. The state we read WAS the default, so nothing has changed under us -- but only if
        # what we read is in fact what an absent row means. If a caller claims it read HALTED from a
        # table with no row, one of the two is wrong and writing either would bury that.
        if expected is not State.TRADING:
            _log.error(
                "%s: no lifecycle row, but the session began at %s — an absent row reads as TRADING, "
                "so these disagree and the %s is NOT being written",
                strategy_id, expected.value, life.state.value)
            return CONTRADICTION

        # A concurrent insert between our UPDATE and this INSERT raises IntegrityError, and that is
        # left to propagate deliberately. It means a human wrote a row in the microseconds we were
        # deciding; swallowing it would silently discard either their state or ours, and this runs
        # AFTER the session, so raising costs a stack trace and no orders. Loud beats guessing.
        # NEVER A TRADING BIRTH. `trg_no_birth_in_trading` (migration 0016, #443) raises on
        # `INSERT ... state = 'TRADING'`: a row inserted already trading has no transition, so no
        # promotion is ever journalled and the strategy is live with nothing on the record -- how
        # TECHIVOL-005 went live on 2026-08-21 and formed 8 orders against other strategies'
        # positions. The caller only reaches here when the state CHANGED, so a TRADING insert should
        # be unreachable; refusing it explicitly beats a Postgres exception naming a trigger, and
        # says which rule was hit.
        if life.state is State.TRADING:
            _log.error(
                "%s: refusing to CREATE a row already in TRADING (trg_no_birth_in_trading, #443) — "
                "a strategy born trading has no promotion event to journal", strategy_id)
            return CONTRADICTION

        await s.execute(insert(StrategyState).values(
            strategy_id=strategy_id, state=life.state.value,
            reason=life.reason, updated_at=utcnow()))
        return SAVED
