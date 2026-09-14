"""Claims-vs-account breaches, assembled where both halves are visible (#437).

Claims are rows in Postgres (`exec_position_state`, the strategies' own ledger — the thing an exit
sizes against). The account is the broker's. Only the API process holds both, which is why this lives
here and not in `session_watch`, and why the ad-hoc version of it lived in a terminal until now.

The predicate itself is kumo-strategies' `over_claimed`, reached through `api.claims_invariant`. Not
reimplemented: two derivations of one fact is the defect class this ticket exists to close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: `CLAIMS_SQL` is DEFINED in `claims_invariant` and only re-exported here (#843): one query, one
#: fold, every consumer. `qty <> 0` because a retired claim is a zero row, not a deleted one.
from api.claims_invariant import (  # noqa: F401 — CLAIMS_SQL is part of this module's surface
    CLAIMS_SQL,
    PredicateAbsent,
    claim_breaches,
    claims_by_strategy,
    exit_ceilings,
    symbol_of,
)


@dataclass(frozen=True)
class Breaches:
    """`status` IS the point, not decoration.

    An empty `breaches` map means "no symbol is over-claimed" ONLY when `status == "ok"`. Without a
    status, a failure to read the account returns exactly the same shape as a healthy ledger and every
    consumer reports green — which is how an empty tile stood in for eight held positions on
    2026-08-14, and why the trades plane grew the same field (#298).
    """

    status: str
    breaches: dict = field(default_factory=dict)
    error: str | None = None


def build_breaches(rows, account: dict | None) -> Breaches:
    """Fold claim rows against the account book.

    `account is None` means the broker could not be read. That is reported as an ERROR rather than as a
    clean ledger: a check that cannot see the account has not found zero breaches, it has found
    nothing.
    """
    if account is None:
        return Breaches(status="account_unreadable",
                        error="broker positions could not be read — breaches NOT computed")
    claims = claims_by_strategy(rows)
    # WHAT THE BREACH COSTS, computed here rather than left for each consumer to rediscover. An
    # over-claim does not over-sell — `own_ceiling` prevents that — it FREEZES: BETA was 79 held and 2
    # sellable on 2026-08-22, with 77 shares no lane could reach on any path.
    #
    # THE THIRD STATE (#903): a kumo-strategies pin without the predicates. Named here, at the one
    # place `/claims` reads, rather than an ImportError at import time of `api.app`. `PredicateAbsent`
    # is imported at MODULE scope above on purpose — a call-time import would bind the class from a
    # reloaded module while a stale function raises the original, and the except would miss.
    try:
        ceilings = exit_ceilings(account, claims)
        breached = claim_breaches(account, claims)
    except PredicateAbsent as exc:
        return Breaches(status="predicate_absent", error=str(exc))
    out = {}
    for sym, (claimed, held) in sorted(breached.items()):
        # WHO claims it, not just how much. "BETA over-claimed by 77" is not actionable; the holder is
        # what tells an operator whose exit will size against shares it does not own.
        out[sym] = {
            "claimed": claimed,
            "held": held,
            "holders": {s: c[sym] for s, c in sorted(claims.items()) if sym in c},
            "sellable": ceilings.get(sym, {}).get("sellable", 0),
            "stranded": ceilings.get(sym, {}).get("stranded", 0),
            "sellable_by_strategy": ceilings.get(sym, {}).get("by_strategy", {}),
        }
    return Breaches(status="ok", breaches=out)


def account_from_positions(positions) -> dict:
    """Net the engine's per-strategy legs into an account book, one entry per symbol.

    The engine's net and the broker's net were verified identical on 2026-08-22 across all six held
    symbols, and CLAUDE.md names broker net as the only hard reconciliation anchor — so netting the
    legs is the same number without a second HTTP call to a venue that rate-limits.

    SIGNED. A DTO's `quantity` is unsigned with the direction in `side`, and summing unsigned
    quantities turns a long and an equal short into double the position instead of zero. That is the
    WHD shape exactly: +28 and -28 would read as 56 held, and the breach would vanish.
    """
    out: dict[str, float] = {}
    for p in positions or ():
        symbol = symbol_of(getattr(p, "instrument_id", ""))
        if not symbol:
            continue
        qty = float(getattr(p, "quantity", 0) or 0)
        side = str(getattr(p, "side", "") or "").upper()
        if side == "SHORT":
            qty = -qty
        elif side not in ("LONG", "FLAT"):
            continue
        out[symbol] = out.get(symbol, 0.0) + qty
    return out
