"""Durable sleeves, and the fill-driven transfer that moves capital between them (#320).

`api.budget` is the arithmetic and knows nothing about a database. This is the part that makes it real:
it loads the book, asks the pure layer what should move when a strategy sells, and writes the answer
down exactly once.

IDEMPOTENCY LIVES IN THE DATABASE, NOT IN A SET
-----------------------------------------------
`sleeve_transfer.fill_id` is UNIQUE, and that constraint is the guard. Fills are not delivered exactly
once — a reconciliation pass re-reports them, a reconnect replays them — and applying one twice pushes
the donor below its target and the recipient above its headroom while each application looks
individually correct. An in-memory set forgets across a restart, which is precisely when
reconciliation replays hardest. Here the second insert simply fails and the transfer is skipped.

The insert comes FIRST, before the balances move. A crash between them then leaves a recorded transfer
whose effect never landed — visible, reconcilable, and detectable by comparing the log against the
balances. The other order leaves balances that moved with nothing saying why, which is neither.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from api.budget import UNALLOCATED, Book, Sleeve, Transfer, plan_transfer
from api.db.models import SleeveTransfer, StrategySleeve

_log = logging.getLogger("kumo.budget")


def _f(value) -> float:
    return float(value) if value is not None else 0.0


def _targets() -> dict[str, float]:
    """Operator intent, from the `strategies` SETTINGS domain.

    Target is CONFIG and actual is RUNTIME STATE, so they live in different places on purpose. Intent is
    something a person types and a schema validates; reality is something fills produce. Keeping target
    in the settings domain also means it renders in the existing settings UI with no per-setting code —
    "add a schema on the backend and it appears here automatically" — instead of needing a bespoke
    screen.

    ONE WRITER EACH, which is the point: settings owns target, the sleeve table owns actual. The
    `strategy_sleeve.target` column is now vestigial and is NOT read; it stays only because dropping a
    column is a migration in its own right, and a half-applied one is worse than an unused column.
    """
    try:
        from api import settings

        values = settings.resolve("strategies") or {}
    except Exception:  # noqa: BLE001 — a missing/unreadable domain must not stop the book loading
        return {}
    # ONLY REGISTERED STRATEGIES. The `strategies` domain holds knobs as well as budgets —
    # `TRANSFER_TO`, the `*_SLOTS` schedules, `QC345_ENABLED`, `QC345_UNIVERSE_REFRESH` — and
    # `float(False)` is 0.0, so every boolean flag arrived here as a zero-target "strategy". That was
    # harmless only while the flags were off: with `QC345_ENABLED` true, `plan_distribution` allocated
    # $1 to a sleeve named `QC345_ENABLED`, `distribute_unallocated` CREATED the row, and the capital
    # left UNALLOCATED for an address `/strategies` does not render and no strategy can spend from.
    # The next numeric knob added to this domain would have been funded at its full value.
    #
    # The registry is the existing allowlist for exactly this question, so the answer comes from there
    # rather than from a naming convention that the next key can break.
    from api.strategy_registry import REGISTRY

    known = {e.strategy_id for e in REGISTRY}
    out: dict[str, float] = {}
    for sid, value in values.items():
        if sid not in known:
            continue
        try:
            out[sid] = float(value)
        except (TypeError, ValueError):
            continue
    return out


async def load_book(session) -> Book:
    """Every sleeve: `target` from settings (intent), `actual` from the table (reality).

    A strategy configured with a target but holding nothing still gets a sleeve, so a freshly funded
    strategy is visible before its first fill rather than appearing only once it trades.
    """
    rows = (await session.execute(select(StrategySleeve))).scalars().all()
    actual = {r.strategy_id: _f(r.actual) for r in rows}
    targets = _targets()
    return Book({
        sid: Sleeve(sid, targets.get(sid, 0.0), actual.get(sid, 0.0))
        for sid in set(actual) | set(targets)
    })


async def ensure_sleeves(session, strategy_ids: list[str]) -> None:
    """Create a zeroed sleeve for any declared strategy that has none.

    Zero target AND zero actual, deliberately: a strategy that appears in the registry has not thereby
    been granted capital. Defaulting a new sleeve to anything else would let adding a line to the
    registry allocate money, which is an operator decision and must stay one.
    """
    existing = {r.strategy_id for r in (await session.execute(select(StrategySleeve))).scalars().all()}
    for sid in strategy_ids:
        if sid not in existing:
            session.add(StrategySleeve(strategy_id=sid, target=Decimal(0), actual=Decimal(0)))
    await session.flush()


# `set_target` WAS HERE AND IS DELETED (#463, 2026-08-23).
#
# It validated its input, wrote `strategy_sleeve.target`, and NOTHING READ THAT COLUMN — targets come
# from the `strategies` settings domain via `_targets()`. Measured live: every target column on the
# paper database was 0.0000 while settings said 20,000 for all five lanes, and `GET /strategies`
# served the settings number. A public, exported, input-validating function that writes a column
# nobody reads is worse than a missing one: it looks like it worked, and it validated carefully.
#
# NOT rewritten to write settings. That would give `target` two writers and recreate the disagreement
# the split exists to prevent — see `_targets()`: settings owns target, this table owns actual.
#
# `test_one_writer_per_target.py` pins the invariant rather than the name, because a replacement under
# a different name would satisfy a name-based test and reintroduce the defect.


async def on_sell_fill(
    session,
    *,
    seller_id: str,
    proceeds: float,
    fill_id: str,
    recipient_id: str | None,
) -> Transfer | None:
    """A strategy sold. Move capital if — and only if — it is over its target.

    Returns the transfer that was APPLIED, or None when nothing moved. A strategy at or under its target
    that sells is rotating, not shrinking: it turned stock into cash inside its own sleeve and will buy
    something else. Taking that away would break a strategy behaving exactly as intended.

    `fill_id` is required and must identify the venue fill. Passing a synthetic or reused id defeats the
    uniqueness guard, so this refuses an empty one rather than silently accepting a transfer that can be
    applied twice.
    """
    if not fill_id:
        raise ValueError("on_sell_fill requires a fill_id — it is the idempotency key")

    book = await load_book(session)
    seller = book.sleeves.get(seller_id)
    if seller is None:
        # A fill for a strategy with no sleeve. Not an error — a strategy can trade before anyone
        # allocates to it — but it must be visible, because its capital is unaccounted for.
        _log.warning("budget: sell fill for %s which has no sleeve — nothing transferred", seller_id)
        return None

    recipient = book.sleeves.get(recipient_id) if recipient_id else None
    transfer = plan_transfer(seller, proceeds, recipient=recipient, fill_id=fill_id)
    if transfer is None:
        return None

    # Record BEFORE moving the balances. A crash between the two then leaves a transfer that is written
    # but not applied — detectable by comparing the log against the balances. The reverse leaves
    # balances that moved with no record of why, which is undetectable.
    session.add(SleeveTransfer(
        fill_id=transfer.fill_id, from_strategy=transfer.from_strategy,
        to_strategy=transfer.to_strategy, amount=Decimal(str(transfer.amount)),
        reason=transfer.reason[:128],
    ))
    try:
        await session.flush()
    except IntegrityError:
        # The unique constraint did its job: this fill has already been applied. Not an error.
        await session.rollback()
        _log.info("budget: fill %s already transferred — skipping duplicate", fill_id)
        return None

    donor = await session.get(StrategySleeve, transfer.from_strategy)
    donor.actual = Decimal(str(_f(donor.actual) - transfer.amount))

    target_row = await session.get(StrategySleeve, transfer.to_strategy)
    if target_row is None:
        # UNALLOCATED, or a recipient with no sleeve yet. It must be credited SOMEWHERE or the book
        # loses capital and the conservation law fails on exactly the path it exists to cover.
        session.add(StrategySleeve(strategy_id=transfer.to_strategy, target=Decimal(0),
                                   actual=Decimal(str(transfer.amount))))
    else:
        target_row.actual = Decimal(str(_f(target_row.actual) + transfer.amount))

    await session.flush()
    _log.info("budget: %s -> %s %.2f (%s)", transfer.from_strategy, transfer.to_strategy,
              transfer.amount, transfer.reason)
    return transfer


async def distribute_unallocated(
    session, *, available: float | None = None, actor: str = "operator", run_id: str | None = None
) -> list:
    """Hand unallocated capital to the sleeves that are short, in proportion to how short each one is.

    THE GAP THIS CLOSES (#373). `on_sell_fill` is the only other writer of `actual`, and it moves capital
    exactly one way: from a seller that is over target, to the single `TRANSFER_TO` recipient, when a
    fill happens. Nothing could put capital that is ALREADY unallocated to work. So BCTROT-004 and
    QC345-003 held `target 20000, actual 0` on 2026-08-19 — `deployable` pinned at 0, unable to open a
    position — while $44,889.50 sat unallocated and BCTROT's close-20m slot was two hours out.

    IDEMPOTENT BY CONSTRUCTION, like every other transfer here. `SleeveTransfer.fill_id` is uniquely
    indexed and each allocation gets a deterministic id derived from the run and the recipient, so a
    retried or replayed request lands once. Without that, a double-submitted distribution funds every
    sleeve twice and the caps cannot catch it: each application looked individually correct, which is
    the same argument `Transfer` already makes for fills.

    ONE TRANSACTION. Either the whole distribution applies or none of it does. A partial distribution
    leaves the book unbalanced against the account with no record of how far it got.

    `available` defaults to whatever UNALLOCATED holds. Passing it explicitly is for the caller that has
    a better number — notably broker cash, which is the fact that is actually true; `actual` was wrong
    by $24,614.85 on 2026-08-19 because `mark_to_market` has no production caller and nothing noticed
    until the book was compared with the account.
    """
    from api.budget import plan_distribution

    book = await load_book(session)
    source = book.sleeves.get(UNALLOCATED)
    if source is None:
        _log.warning("budget: no %s sleeve — nothing to distribute", UNALLOCATED)
        return []

    held = float(source.actual)
    requested = held if available is None else float(available)
    # NEVER DISTRIBUTE MORE THAN THE SOURCE HOLDS. `available` reaches here from a request body, and
    # without this cap a caller could credit sleeves with capital the account does not have and drive
    # UNALLOCATED negative — the book stays "conserved" on paper while every funded sleeve's
    # `deployable` authorises notional backed only by margin. Alpaca reports buying power near 3x
    # equity, so those orders FILL rather than reject: the budget cap is the one mechanism bounding
    # automated buying, and a request body could raise it past the account.
    if requested > held + 1e-9:
        _log.warning(
            "budget: refusing to distribute %.2f — %s holds %.2f", requested, UNALLOCATED, held
        )
        raise ValueError(
            f"cannot distribute {requested:.2f}: {UNALLOCATED} holds {held:.2f}"
        )
    pot = max(0.0, min(requested, held))
    allocations = plan_distribution(dict(book.sleeves), pot)
    if not allocations:
        _log.info("budget: nothing to distribute (available %.2f)", pot)
        return []

    total = sum(a.amount for a in allocations)
    if total > pot + 1e-9:  # pragma: no cover — plan_distribution caps this; belt and braces
        raise ValueError(f"distribution planned {total:.2f} against {pot:.2f} available")

    # Deterministic per run AND per recipient: a replay of the same run is refused by the unique index,
    # while a genuinely new distribution gets fresh ids.
    # Injectable so a test can replay the SAME run and prove the unique index refuses it. A wall-clock
    # default is fine in production — two distributions a second apart are genuinely different runs.
    run = run_id or f"dist-{int(datetime.now(UTC).timestamp())}"
    for alloc in allocations:
        session.add(SleeveTransfer(
            fill_id=f"{run}-{alloc.to_strategy}",
            from_strategy=UNALLOCATED,
            to_strategy=alloc.to_strategy,
            amount=Decimal(str(alloc.amount)),
            # Truncated like `on_sell_fill` does. `reason` is String(128) and `actor` arrives from a
            # request body unbounded; an over-long one raises DataError mid-flush, which is NOT an
            # IntegrityError, so it escapes the duplicate handler and aborts the caller's transaction.
            reason=f"proportional distribution of unallocated capital ({actor})"[:128],
        ))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        _log.info("budget: distribution %s already applied — skipping duplicate", run)
        return []

    # RELATIVE, NOT ABSOLUTE. Writing a literal computed from the `load_book` snapshot loses any
    # transfer that committed in between: `on_sell_fill` credits UNALLOCATED from a MOMENTUM sale, this
    # transaction then writes `snapshot - total` and the sale's proceeds vanish. The engine runs at
    # READ COMMITTED with no row lock, and `session.get` returns the identity-mapped object from that
    # same snapshot rather than re-reading. `StrategySleeve.actual + x` renders as
    # `actual = strategy_sleeve.actual + :x`, which the database evaluates against the CURRENT row.
    src_row = await session.get(StrategySleeve, UNALLOCATED)
    src_row.actual = StrategySleeve.actual - Decimal(str(total))
    for alloc in allocations:
        row = await session.get(StrategySleeve, alloc.to_strategy)
        if row is None:
            row = StrategySleeve(strategy_id=alloc.to_strategy, target=Decimal(0), actual=Decimal(0))
            session.add(row)
            await session.flush()
        row.actual = StrategySleeve.actual + Decimal(str(alloc.amount))
    await session.flush()
    _log.info("budget: distributed %.2f across %d sleeve(s): %s", total, len(allocations),
              ", ".join(f"{a.to_strategy} {a.amount:.2f}" for a in allocations))
    return allocations


async def mark_to_market(session, strategy_id: str, net_asset_value: float) -> None:
    """Set a sleeve's `actual` to its measured net asset value.

    `actual` is defined as net asset value, so P&L must reach it or a profitable sleeve would show the
    same number forever. This is the only writer besides transfers, and it is deliberately a SET rather
    than an adjustment: computing a delta here would create a second derivation of the same quantity,
    and two derivations of one fact disagree.
    """
    row = await session.get(StrategySleeve, strategy_id)
    if row is None:
        session.add(StrategySleeve(strategy_id=strategy_id, target=Decimal(0),
                                   actual=Decimal(str(net_asset_value))))
        return
    row.actual = Decimal(str(net_asset_value))


async def transfers_for(session, strategy_id: str, limit: int = 50) -> list[SleeveTransfer]:
    """Recent transfers touching this strategy, newest first — the audit an operator needs to answer
    'where did my capital go'."""
    stmt = (
        select(SleeveTransfer)
        .where((SleeveTransfer.from_strategy == strategy_id)
               | (SleeveTransfer.to_strategy == strategy_id))
        .order_by(SleeveTransfer.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "UNALLOCATED",
    "ensure_sleeves",
    "load_book",
    "mark_to_market",
    "on_sell_fill",
    "transfers_for",
]


async def lifecycle_states(session) -> dict[str, str]:
    """Each strategy's operator lifecycle row: {strategy_id: state} from exec_strategy_state.

    THE TABLE THE #638 SHORT-CIRCUIT ACTUALLY KEYS ON. The platform probe resolver used to ask
    `last_decisions` for a "lifecycle" key that function has never returned — always None, always
    defaulted to TRADING — so a DISABLED row was read by NOTHING on the preflight path and staging
    kept paging budget=0 for a lane the operator had switched off. Absent rows are absent here too:
    an absent row means TRADING (Operator, 2026-08-19) and that default belongs to the CALLER, stated,
    not hidden in a falsy `or`.
    """
    rows = (await session.execute(
        text("select strategy_id, state from exec_strategy_state")
    )).all()
    return {str(r[0]): str(r[1]) for r in rows}


async def last_decisions(session) -> dict[str, dict]:
    """Each strategy's most recent DECISION row: `{strategy_id: {"session": str, "slot": str}}`.

    Reads `kind='decision'` only. A `risk` row saying a session FAILED is not a decision — conflating
    them would report a dead strategy as alive, which is the exact confusion #349 exists to end.

    Strategies with no decision row are ABSENT from the mapping rather than mapped to a null, so the
    caller has to handle never-decided deliberately instead of receiving a falsy value that reads like
    "recently, sort of".
    """
    rows = (await session.execute(
        text("""
            select distinct on (strategy_id) strategy_id, session, slot
            from exec_action_log
            where kind = 'decision'
            order by strategy_id, ts desc
        """)
    )).all()
    return {r[0]: {"session": r[1], "slot": r[2]} for r in rows}
