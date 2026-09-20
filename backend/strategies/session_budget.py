"""The lane's budget, from the PLATFORM, refreshed immediately before it decides (#537).

WHAT THIS REPLACES. `allocated_equity` was filled from the `strategies` SETTINGS domain — the
operator's TARGET — while `budget_gate.may_submit` bounded orders by the sleeve LEDGER. Two sources
for one fact, and sizing took the larger. Measured live: QC345-003 sized off 20,000 while the ledger
held 10,000 and it was deployed 15,860.

`budget_store._targets()` states the split deliberately: "settings owns TARGET, the sleeve table owns
ACTUAL". The lane was sizing off its ENTITLEMENT rather than its HOLDING.

TWO STEPS, AND THE ORDER IS THE WHOLE POINT:

    1. DISTRIBUTE first. `on_sell_fill` moves capital back to UNALLOCATED whenever a lane sells, and
       nothing put it back to work — 43,182 sat parked for six days while four lanes ran at half
       target (#373). Distributing after the read would fund a sleeve the lane had already sized
       against, one session too late.
    2. READ second, by the SAME rule the order gate applies: `min(actual, target)`. A third definition
       of "what may this lane deploy" is how #537 happened.

WHY IT BELONGS HERE AND NOT IN THE STRATEGIES. The lanes are self-driving — each arms its own
Nautilus time alert and calls its own callback; nothing outside knows a decision is imminent. But both
lanes that use this refresh their limits in COCKPIT-OWNED code immediately before deciding, so this is
the last moment that holds both facts. The alternative — cockpit scheduling its own alert before each
slot — would re-derive slot offsets that #514 just made live-configurable, and two schedulers
computing one fire time is the drift this repo keeps measuring.

NEVER RAISES. It runs inside the decision path. #377 is the precedent: one strategy's construction
over HTTP took MANUAL, MOMENTUM and BCTROT down with it. On any failure the caller keeps whatever it
had, which is the previous behaviour rather than a new one.
"""

from __future__ import annotations

import logging
from datetime import UTC

_log = logging.getLogger("kumo.session_budget")


async def resolve(strategy_id: str, *, fallback: float | None,
                  session: str | None = None) -> float | None:
    """Distribute unallocated capital, then return what this lane may deploy.

    `session` IS THE IDEMPOTENCY KEY AND IT IS NOT OPTIONAL IN PRACTICE. `distribute_unallocated`
    derives `fill_id = f"{run_id}-{recipient}"` and `SleeveTransfer.fill_id` is UNIQUE, so a run_id
    that does not vary per session makes the SECOND distribution collide with the first and log
    "already applied — skipping duplicate" — forever, silently.

    That is not hypothetical: the first version of this file passed `run_id=f"session-{strategy_id}"`,
    a constant, which would have distributed once per lane for the life of the deployment and then
    quietly stopped. Caught reviewing my own diff fourteen minutes after deploying it.

    Falling back to the UTC date when the caller has no session keeps it varying daily rather than
    never — wrong-but-daily beats right-once.

    Returns `fallback` unchanged on any failure — an unreadable ledger must not change how a live
    session sizes, which is the same rule `_limits_for_session` already applies to settings.
    """
    from datetime import datetime

    stamp = str(session) if session else datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        from api.budget_store import distribute_unallocated, load_book
        from api.db.engine import session_factory

        async with session_factory() as session:
            # PUT PARKED CAPITAL TO WORK FIRST (#373). Idempotent and conservation-capped by
            # construction — `SleeveTransfer.fill_id` is uniquely indexed and each allocation gets a
            # deterministic id, so a replay lands once.
            try:
                allocations = await distribute_unallocated(
                    session, actor="session", run_id=f"session-{strategy_id}-{stamp}")
                # COMMIT EXPLICITLY. `distribute_unallocated` only FLUSHES, and `AsyncSession.__aexit__`
                # calls `close()`, which ROLLS BACK — the `/sleeves/distribute` route answered 200 with a
                # full allocation list and an untouched database until it learned this.
                await session.commit()
                if allocations:
                    _log.warning("%s: distributed %d allocation(s) before deciding: %s",
                                 strategy_id, len(allocations), allocations)
            except Exception as exc:  # noqa: BLE001 — a failed distribution must not stop the session
                await session.rollback()
                _log.warning("%s: distribution failed (%r) — sizing off the book as it stands",
                             strategy_id, exc)

            book = await load_book(session)

        sleeve = book.sleeves.get(str(strategy_id))
        if sleeve is None:
            _log.warning("%s: no sleeve in the ledger — keeping %s", strategy_id, fallback)
            return fallback

        actual, target = float(sleeve.actual), float(sleeve.target)
        # THE SAME RULE `Sleeve.deployable` APPLIES — unconditionally (#648). `target` stops a sleeve
        # growing past what it was granted; `actual` stops it spending capital still sitting in the
        # donor. The previous `if target else actual` read a REAL zero as "unset": the settings
        # schema defines zero as WIND DOWN, so a wound-down lane sized off its full holding — and on
        # a venue with no order gate, nothing downstream stops those entries.
        budget = min(actual, target)
        _log.warning("%s SIZING BASIS: platform=%s (actual=%s target=%s) was=%s",
                     strategy_id, budget, actual, target, fallback)
        return budget
    except Exception as exc:  # noqa: BLE001 — never take a session down (#377)
        _log.warning("%s: session budget unavailable (%r) — keeping %s", strategy_id, exc, fallback)
        return fallback
