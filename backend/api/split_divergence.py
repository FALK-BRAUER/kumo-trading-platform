"""Per-lane SPLIT reconciliation: the cache's attribution vs the claims ledger's (#692).

Totals-based checks (over_claimed #437, reconcile_drift #26) verify the SUM per symbol. BDX passed
both on 2026-08-28 — 55 shares either way — while the split disagreed (cache MOMENTUM 55/BCTROT 0,
claims 45/10), and BCTROT sized an exit off shares Nautilus says it does not hold. Under NETTING
that sell comes out of another lane's position; only a resting cross-lane stop happened to block it.
Agreement of the sums is exactly the condition under which a wrong split is invisible.

Scope is deliberately one-sided: a CLAIM that the cache contradicts. A cache position with no claim
is the adoption path's case (MANUAL, reconciled-in) and is not a defect here. SHORT cache rows count
signed — the #635 mirrors are shorts beside longs, and unsigned quantities would read a mirrored
lane as fully backed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_EPS = 1e-9


@dataclass(frozen=True)
class SplitDivergence:
    """`status` IS the point, not decoration — the same rule as `claims_endpoint.Breaches` (#817).

    An empty `pairs` means "computed, none found" ONLY when `status == "ok"`. Without a status, a
    failure to read either half returns exactly the same shape as a healthy book and every consumer
    reports green — which is how eleven divergent pairs and 1,504 shares stood on staging2 while
    nothing said so.
    """

    status: str
    pairs: list[dict] = field(default_factory=list)
    error: str | None = None


def build_split(cache_rows, claim_rows) -> SplitDivergence:
    """Fold the two halves and report the divergence, or report that a half could not be read (#817).

    `None` for either input means that read FAILED. It is reported as its own status rather than as
    an empty list: a check that could not see the claims ledger has not found zero divergences, it
    has found nothing. Three states, and the middle one is why this wrapper exists.

    IT DOES NOT REIMPLEMENT THE PREDICATE. `split_divergence` above is the single derivation, shared
    with `AlertsService._announce_split_divergence` — two derivations of one fact drift, and this
    module's whole subject is what happens when the sums agree and the split does not.

    `claim_rows` are SQLAlchemy rows read through `_mapping`, matching the query the alert already
    runs (`alerts.py:663`); a plain mapping is accepted too so a caller need not fabricate a Row.
    `cache_rows` are DICTS — `PositionDTO` is converted by the caller (`alerts.py:666` uses
    `_as_dict`), because this module must not import the API's DTOs.
    """
    if cache_rows is None:
        return SplitDivergence(status="cache_unreadable",
                               error="the engine's position cache could not be read — "
                                     "split divergence NOT computed")
    if claim_rows is None:
        return SplitDivergence(status="claims_unreadable",
                               error="the claims ledger could not be read — "
                                     "split divergence NOT computed")

    # SUMMED, NOT OVERWRITTEN. `(strategy_id, symbol)` is the ledger's primary key, so duplicates do
    # not occur in production today — but assignment would SILENTLY DROP the earlier quantity if that
    # ever changed, and a claims figure that is quietly too small reports a lane as under-claiming
    # when it is not (codex, implementation review). Summing is the arithmetic that stays correct
    # either way.
    from api.claims_invariant import claims_by_strategy

    diverged = split_divergence(cache_rows, claims_by_strategy(claim_rows))
    pairs = [
        {"symbol": sym, "strategy_id": sid, "claim": pair["claim"], "cache": pair["cache"]}
        for sym, lanes in sorted(diverged.items())
        for sid, pair in sorted(lanes.items())
    ]
    return SplitDivergence(status="ok", pairs=pairs)


def split_divergence(cache_rows: list[dict], claims_by_strategy: dict[str, dict[str, float]]) -> dict:
    """{symbol: {strategy_id: {"claim": x, "cache": y}}} where a claim and the cache's signed
    attribution for that (strategy, symbol) differ."""
    cache: dict[tuple[str, str], float] = {}
    for r in cache_rows or ():
        sid = str(r.get("strategy_id") or "")
        from api.claims_invariant import symbol_of
        sym = symbol_of(r.get("instrument_id"))
        qty = float(r.get("quantity") or 0.0)
        side = str(r.get("side") or "")
        signed = 0.0 if side == "FLAT" else (-qty if side == "SHORT" else qty)
        cache[(sid, sym)] = cache.get((sid, sym), 0.0) + signed

    out: dict[str, dict[str, dict[str, float]]] = {}
    for sid, claims in (claims_by_strategy or {}).items():
        for sym, claim in (claims or {}).items():
            have = cache.get((str(sid), str(sym)), 0.0)
            if abs(float(claim) - have) > _EPS:
                out.setdefault(str(sym), {})[str(sid)] = {"claim": float(claim), "cache": have}
    return out
