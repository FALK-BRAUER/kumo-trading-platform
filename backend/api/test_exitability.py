"""An over-claimed position is not over-SOLD. It is FROZEN — and that is the sentence to say (#437).

MEASURED on the live paper account, 2026-08-22 23:14 ET. The monitor said:

    OVER-CLAIMED BETA: claims 156 vs 79 held — BCTROT-004=79 MOMENTUM-002=77

I read that as two lanes about to sell the same shares. It is not, and `own_ceiling` is the reason:

    BCTROT-004    own_ceiling(79, my_claim=79, other=77) = max(0, min(79, 79-77)) =  2
    MOMENTUM-002  own_ceiling(79, my_claim=77, other=79) = max(0, min(77, 79-79)) =  0
                                                                         TOTAL       2

79 SHARES HELD, 2 SHARES SELLABLE. Each lane's ceiling subtracts the other's stale claim, so 97% of a
real position cannot be exited by anyone, on any path, including LIQUIDATING. BETA was also one of the
five unprotected positions going into Monday's open: no stop, and no exit either.

`own_ceiling` is RIGHT and must not be loosened — it exists because MOMENTUM-002 sold 28 WHD that
BCTROT-004 had bought (Operator: "strategies are not supposed to trade among each other"). The defect is
upstream, in retirement: `owned - set(account)` is a set difference with no quantity comparison, so a
position that goes flat INTRADAY and reopens keeps the old claim. BETA went flat at 15:35:22 on
2026-08-20 and reopened at 16:00:10, between two session-start reconciles.

WHY THIS LIVES IN COCKPIT AT ALL, when the repair is kumo-strategies': the alarm text is what an
operator acts on at 09:25, and "over-claimed 156 vs 79" reads as an accounting nit while "79 held, 2
sellable, unprotected" does not. The predicate is IMPORTED, never reimplemented — same rule as
`claim_breaches`.
"""

from __future__ import annotations

import inspect

from api.claims_endpoint import build_breaches
from api.claims_invariant import exit_ceilings
from api.db.engine import get_session

#: The live numbers, so the assertions carry the reasoning and not just a shape.
_ACCOUNT = {"BETA": 79.0, "AEM": 18.0}
_CLAIMS = {"BCTROT-004": {"BETA": 79.0}, "MOMENTUM-002": {"BETA": 77.0}}

#: The engine's own position legs, netted by `account_from_positions`. Per-strategy NETTING legs, which
#: is what the cache actually holds — one BETA leg per lane, summing to the broker net of 79.
class _Leg:
    def __init__(self, iid, qty, side="LONG"):
        self.instrument_id, self.quantity, self.side = iid, qty, side


_POSITIONS = [_Leg("BETA.XNYS", 79.0), _Leg("AEM.XNYS", 18.0)]


def test_the_fixture_is_actually_over_claimed() -> None:
    """Without a breach there is no shortfall to find, and every assertion below would pass empty."""
    assert sum(c.get("BETA", 0) for c in _CLAIMS.values()) > _ACCOUNT["BETA"]


def test_the_live_case_is_seventy_nine_held_and_two_sellable() -> None:
    out = exit_ceilings(_ACCOUNT, _CLAIMS)
    assert out["BETA"]["held"] == 79.0
    assert out["BETA"]["sellable"] == 2, "each lane's ceiling subtracts the other's stale claim"
    assert out["BETA"]["by_strategy"] == {"BCTROT-004": 2, "MOMENTUM-002": 0}
    assert out["BETA"]["stranded"] == 77, "the shares no lane can reach — this is the number to alarm on"


def test_a_consistent_ledger_strands_nothing() -> None:
    """Or the alarm fires on the normal path and is muted before the day it matters."""
    out = exit_ceilings({"BETA": 79.0}, {"A": {"BETA": 40.0}, "B": {"BETA": 39.0}})
    assert out["BETA"]["sellable"] == 79 and out["BETA"]["stranded"] == 0


def test_a_sole_claimant_can_still_exit_everything_it_opened() -> None:
    """The direction that must NOT regress: one lane, no rival claim, full exit."""
    out = exit_ceilings({"BETA": 79.0}, {"A": {"BETA": 79.0}})
    assert out["BETA"]["sellable"] == 79 and out["BETA"]["stranded"] == 0


def test_a_claim_on_a_symbol_the_account_does_not_hold_strands_nothing() -> None:
    """WHD: 56 claimed, 0 held. Nothing is stranded because nothing is there — it retires at the next
    session-start reconcile, and calling it stranded would bury the one symbol that is."""
    out = exit_ceilings({}, {"A": {"WHD": 28.0}, "B": {"WHD": 28.0}})
    assert out.get("WHD", {}).get("stranded", 0) == 0


def test_the_predicate_is_KUMO_STRATEGIES_own_and_is_not_reimplemented() -> None:
    """Two derivations of one fact is the defect class #437 exists to close. If cockpit computed its
    own ceiling, the alarm and the sizing could disagree and the alarm would be the wrong one."""
    from api import claims_invariant

    src = inspect.getsource(claims_invariant)
    assert "from kumo_strategies.runtime.executor.pgrunner import" in src
    assert "own_ceiling" in src
    assert "max(0, min(" not in src, "cockpit is recomputing the ceiling instead of importing it"


def test_the_breach_payload_carries_the_actionable_number() -> None:
    """A consumer that only gets `claimed` and `held` has to rediscover the ceiling to know what it
    means — and the last five defects in this repo were all a mechanism nothing asked."""
    rows = [{"strategy_id": s, "symbol": k, "qty": v} for s, c in _CLAIMS.items() for k, v in c.items()]
    b = build_breaches(rows, _ACCOUNT)
    assert b.status == "ok"
    assert b.breaches["BETA"]["sellable"] == 2
    assert b.breaches["BETA"]["stranded"] == 77
    assert b.breaches["BETA"]["holders"] == {"BCTROT-004": 79.0, "MOMENTUM-002": 77.0}


def test_the_claims_check_is_REACHABLE(monkeypatch) -> None:
    """`/claims` returned 404 while `build_breaches` had no production caller — the SIXTH mechanism in
    this session built, tested, and driven by nothing (#439 detector, #462 caller argument, #467
    acceptor, #440 boot gate, #345's `open_lots_after`, this).

    The alarm that found BETA came from an ad-hoc monitor script, not from the platform, which means
    the platform's own answer to "is any position frozen" was unreachable from the UI, from
    `session_watch`, and from anyone who does not have my terminal open.
    """
    from fastapi.testclient import TestClient

    from api import app as app_module

    routes = {getattr(r, "path", None) for r in app_module.app.routes}
    assert "/claims" in routes, "the claims plane is not served — nothing outside a test can reach it"

    class _Node:
        def positions(self):
            return _POSITIONS

    class _Result:
        def all(self):
            return [{"strategy_id": s, "symbol": k, "qty": v}
                    for s, c in _CLAIMS.items() for k, v in c.items()]

    class _Session:
        async def execute(self, _sql):
            return _Result()

    monkeypatch.setattr(app_module.app.state, "node", _Node(), raising=False)
    # No lifespan: it opens redis and postgres, and this asserts about a route, not about a stack.
    app_module.app.dependency_overrides[get_session] = lambda: _Session()
    try:
        r = TestClient(app_module.app).get("/claims")
    finally:
        app_module.app.dependency_overrides.pop(get_session, None)
    assert r.status_code == 200, r.text
    body = r.json()
    # THE FOUR HOPS (#233/#322/#336): the endpoint must not be a DTO that eats undeclared keys. This
    # payload has lost a published field three times; `sellable` and `stranded` are the next two.
    assert body["breaches"]["BETA"]["stranded"] == 77, "the route dropped the actionable number"
    assert body["breaches"]["BETA"]["sellable"] == 2
    # STATUS IS THE POINT: an empty `breaches` with no status is indistinguishable from a check that
    # could not run, which is how an empty tile stood in for eight held positions on 2026-08-14.
    assert "status" in body and "breaches" in body
