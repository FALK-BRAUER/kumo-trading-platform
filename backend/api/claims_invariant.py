"""Consumer for kumo-trading-strategies' claims invariant (#437, issue 66).

THE PREDICATE IS THEIRS AND IS NOT REIMPLEMENTED HERE. `over_claimed` landed on kumo-trading-strategies main
at c2358d1, pure and mutation-bitten, and had no caller — cockpit is the only side that holds both the
account book and the claims store. Two derivations of one fact is the defect class this whole ticket
exists to close, so this module is a CONSUMER and nothing else: it fetches, calls, and formats.

WHAT IT CATCHES THAT NOTHING ELSE DOES. Claims retire only when a symbol is ENTIRELY absent — a set
difference with no quantity comparison — so a PARTIAL close leaves the claim at full size. The account
falls, the claim does not, and the next exit sizes against shares that are already sold. It fires the
same session rather than twelve hours later, and it keeps firing if the retirement mechanism is
changed and got wrong again.

Live on 2026-08-22: BETA 156 claimed against 79 held, WHD 56 against 0, XLV 22 against 0.
"""

from __future__ import annotations


class PredicateAbsent(RuntimeError):
    """The installed kumo-trading-strategies pin does not carry the predicates (#903). Named, so the two
    consumers can say so: `/claims` reports `status: predicate_absent`, and the alerts check raises
    through `_checked` and is counted as broken — never an empty breaches map that reads as clean."""


def _predicates():
    """Imported AT CALL TIME, never copied (see the module docstring and
    `test_the_predicate_is_KUMO_STRATEGIES...`). At module scope this was the last module-scope
    import of the strategies package in cockpit production code: a pin predating either symbol
    turned into an ImportError while `api.app` imported `claims_endpoint`, taking the API down
    instead of degrading by name (#902's shape, #903)."""
    try:
        from kumo_strategies.runtime.executor.pgrunner import over_claimed, own_ceiling
    except Exception as exc:
        # Not only ImportError: a pin whose module imports but raises (a broken transitive dependency,
        # a SyntaxError) would otherwise escape raw as a 500 on /claims (review). Named the same way.
        raise PredicateAbsent(
            "kumo_strategies.runtime.executor.pgrunner (over_claimed, own_ceiling) is not importable "
            f"on this kumo-trading-strategies pin — the claims invariant CANNOT be computed: {exc!r}"
        ) from exc
    return over_claimed, own_ceiling

#: Claims are per (strategy, symbol) quantities from `exec_position_state` — the strategies' own
#: ledger, which is what an exit sizes against. NOT the Nautilus cache: the cache is the engine's view
#: and the two disagreeing is precisely the condition being detected.
CLAIMS_SQL = """
    SELECT strategy_id, symbol, qty FROM exec_position_state WHERE qty <> 0
"""


def claims_by_strategy(rows) -> dict[str, dict[str, float]]:
    """Claim rows -> `{strategy_id: {symbol: qty}}`. THE ONE FOLD (#843).

    Four copies of this existed — `claims_endpoint.build_breaches`, `split_divergence.build_split`,
    `alerts._announce_split_divergence` and a fourth the #843 fix would have added — and two of them
    disagreed: `build_breaches` OVERWROTE a duplicate `(strategy_id, symbol)` while `build_split`
    SUMMED it. The ledger's primary key makes duplicates impossible today, which is exactly why the
    disagreement was invisible. Summing is the arithmetic that stays correct if that ever changes; a
    claims figure that is quietly too small reads a lane as under-claiming when it is not.

    `rows` are SQLAlchemy rows read through `_mapping`, or plain mappings — a caller need not
    fabricate a Row.
    """
    claims: dict[str, dict[str, float]] = {}
    for r in rows or ():
        m = r._mapping if hasattr(r, "_mapping") else r
        by_symbol = claims.setdefault(str(m["strategy_id"]), {})
        sym = str(m["symbol"])
        by_symbol[sym] = by_symbol.get(sym, 0.0) + float(m["qty"])
    return claims


def symbol_of(instrument_id) -> str:
    """`BRK.B.XNYS` -> `BRK.B`. The venue is the LAST dotted segment; `split(".")[0]` returned `BRK`
    and made a dotted ticker vanish from the account book (codex, #843 scope review). Same rule as
    `protection.py`."""
    iid = str(instrument_id or "")
    return iid.rsplit(".", 1)[0] if "." in iid else iid


def claim_breaches(account: dict, claims_by_strategy: dict) -> dict:
    """{symbol: (claimed_total, account_qty)} for each symbol claimed beyond what the account holds.

    A symbol ABSENT from `account` counts as zero held, not as unknown. Skipping it is the silencing
    direction and would have dropped two of the three live breaches — including WHD, which had already
    produced a phantom short.
    """
    over_claimed, _ = _predicates()
    return over_claimed(account, claims_by_strategy)


def format_breach(symbol: str, breach: tuple, holders: dict) -> str:
    """Name WHO claims what. "BETA over-claimed by 77" is not actionable; which strategy carries the
    stale claim is the entire content of the alarm."""
    claimed, held = breach
    who = " ".join(f"{k}={v:g}" for k, v in sorted(holders.items()))
    return f"OVER-CLAIMED {symbol}: claims {claimed:g} vs {held:g} held — {who}"


def exit_ceilings(account: dict, claims_by_strategy: dict) -> dict:
    """Per over-claimed symbol: what each lane can actually SELL, the total, and what is stranded.

    AN OVER-CLAIMED POSITION IS NOT OVER-SOLD. IT IS FROZEN. Measured live on 2026-08-22:

        BCTROT-004    own_ceiling(79, my_claim=79, other=77) = 2
        MOMENTUM-002  own_ceiling(79, my_claim=77, other=79) = 0

    79 held, 2 sellable, 77 stranded — unreachable by either lane on any path including LIQUIDATING,
    because each ceiling subtracts the other's stale claim. `own_ceiling` is correct and exists for a
    real incident; the defect is upstream in retirement, which compares PRESENCE and not quantity, so
    a position that goes flat intraday and reopens keeps the claim it had before.

    EVERY claimed symbol the account holds is computed, not only the breached ones. The claim that only
    an over-claim can strand shares is true — when the claims sum to no more than the account, each
    lane's `acct_qty - other_claims` is at least its own claim and the ceilings add back to the total —
    but it is a claim, and returning the consistent symbols too is what lets a test prove it instead of
    assuming it. Callers filter on `stranded`.

    `own_ceiling` is IMPORTED. If cockpit recomputed it, the alarm and the sizing could disagree and it
    would be the alarm that is wrong — which is the whole defect class #437 exists to close.
    """
    # THE PREDICATE IS CONSULTED BEFORE THE CLAIMS ARE, so an empty ledger on an absent pin raises
    # rather than returning {} — "nothing stranded" from a check that could not compute (#903).
    _, own_ceiling = _predicates()
    out: dict[str, dict] = {}
    claimed_symbols = {sym for c in claims_by_strategy.values() for sym in c}
    for sym in sorted(claimed_symbols & set(account)):
        held = account[sym]
        by_strategy = {}
        for sid, claims in sorted(claims_by_strategy.items()):
            mine = claims.get(sym) or 0
            if not mine:
                continue
            others = sum((c.get(sym) or 0) for s, c in claims_by_strategy.items() if s != sid)
            by_strategy[sid] = own_ceiling(int(held), int(mine), int(others))
        sellable = sum(by_strategy.values())
        out[sym] = {
            "held": held,
            "sellable": sellable,
            # What no lane can reach. This is the number an operator acts on before an open — not the
            # over-claim, which reads as an accounting nit.
            "stranded": max(0, int(held) - sellable),
            "by_strategy": by_strategy,
        }
    return out
