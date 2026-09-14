"""Platform-level budget enforcement: over budget means SELL-ONLY (#320).

Operator: "can you control the budget at platform level — only allow sells no buys when over budget?"

Yes, and the important word is PLATFORM. A strategy that must police its own budget is a strategy that
polices it correctly until the day it has a bug, and the failure is silent — it just quietly holds more
than it was granted. So the gate lives outside the strategy, on the path every order takes, and a
strategy cannot opt out of it by being wrong.

WHAT NAUTILUS ALREADY DOES, AND WHY IT IS NOT ENOUGH
----------------------------------------------------
`TradingState.REDUCING` (risk/engine.pyx:1150) does exactly this shape of thing: "only new orders or
updates which reduce an open position are allowed", enforced inside the RiskEngine, which sits between
the strategy and execution so nothing can bypass it. It denies on submit AND on modify.

Two reasons it cannot express a per-strategy budget:

  * `trading_state` is a SINGLE FIELD on the RiskEngine (engine.pyx:132) — node-wide. Setting REDUCING
    to wind one strategy down would freeze entries for every other strategy too.
  * It gates on `portfolio.is_net_long(instrument_id)` — per instrument DIRECTION, not per sleeve. It
    answers "would this increase exposure", not "has this strategy spent its allocation".

So REDUCING is the right mechanism for the NODE-WIDE case (the #108 kill switch: stop opening, keep
exiting, everywhere at once) and this module is the per-strategy one. They compose: the node-wide state
is checked first by Nautilus, and a strategy under its own budget still cannot open while the node is
REDUCING.

THE RULE
--------
Over budget denies ENTRIES ONLY. Exits are always allowed, and that asymmetry is the whole point — a
strategy over its budget needs to sell, and a gate that blocked its sells would trap it above its
target permanently, which is the opposite of what the operator asked for.

Denial is NORMAL, not an error. A strategy asked to open while over budget has not malfunctioned; it
has been told "not now". The contract in `docs/strategy-contract.md` says so explicitly, because a
strategy that treats a refusal as a failure will retry, log an error every session, or halt itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.budget import Sleeve

#: Sub-cent differences are rounding, not intent.
_EPS = 0.005


@dataclass(frozen=True)
class GateDecision:
    """Whether an order may proceed, and — when not — a reason meant for a human.

    The reason is part of the contract rather than a log line: it reaches the operator through the
    order's denial, and "denied" without "why" is indistinguishable from a broken strategy.
    """

    allowed: bool
    reason: str = ""
    #: THE NUMBERS THE DECISION WAS DERIVED FROM, not a restatement of the outcome.
    #:
    #: On 2026-08-31 MOMENTUM-002 refused four entries with "has 0 of budget left and this order
    #: needs 1,823" — a conclusion and one input. Answering whether the lane was overtrading or
    #: holding phantom positions then took a position query, a venue reconstruction out of the log,
    #: and arithmetic by hand, to find that 6,028 of 22,390 deployed was holdings the broker does not
    #: have. Every one of those numbers was in scope here at the moment of refusal.
    #:
    #: A FIELD, not a log line: the reason already reaches the operator through the order's denial,
    #: and a second channel carrying the same decision would drift from it.
    inputs: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


def is_entry(net_position: float, side: str, quantity: float) -> bool:
    """Does this order INCREASE absolute exposure? Delegates to kumo-strategies (#39).

    ONE RULE, ONE IMPLEMENTATION. Cockpit refuses entries and permits exits while a sleeve is over
    budget; the adapters decide what they are submitting. If those two answers were derived
    separately they would disagree — and this codebase has been bitten by exactly that often enough
    to treat a second derivation as a defect rather than a convenience.

    The formulation is "does absolute exposure grow", not "is it the same side as the position", and
    that is what makes the wind-down guarantee hold:

        reduce / flatten            EXIT, whatever the side — a BUY closing a short included, so a
                                    wind-down can never be blocked by the gate.
        flip that GROWS exposure    ENTRY. +10 SELL 1000 leaves -990, which is an over-budget
                                    strategy opening unbounded opposite exposure and calling it a
                                    wind-down.
        flip that SHRINKS exposure  EXIT. +10 SELL 11 leaves -1, strictly smaller than it started.
                                    Their judgement call and the right one: bounded by construction,
                                    and treating it as an entry would let the gate block an order
                                    that reduces risk.

    Falls back to "not an entry" if the shared rule is unavailable, because the alternative — failing
    closed — misclassifies a genuine wind-down as an entry and traps a strategy above its target.
    """
    try:
        from kumo_strategies.runtime.nautilus.contract import is_entry_order
    except Exception:  # noqa: BLE001 — a missing dependency must not block exits
        return False
    return bool(is_entry_order(net_position, side, quantity))


def may_submit(
    sleeve: Sleeve | None,
    *,
    is_entry: bool,
    notional: float,
    currently_deployed: float,
) -> GateDecision:
    """May this strategy submit this order?

    `is_entry` rather than a side, because side alone does not say which way exposure moves: a SELL is
    an exit on a long and an entry on a short. The caller knows which it is; this must not guess.

    An UNKNOWN strategy (no sleeve) is ALLOWED, deliberately. This gate exists to enforce an
    allocation, and a strategy nobody has allocated to has not breached one. Denying here would make
    the arrival of this feature silently stop every strategy that had not yet been configured — a gate
    whose default is "deny everything" gets switched off wholesale the first time it fires wrongly, and
    then protects nothing. Visibility for unbudgeted strategies belongs in the settings screen.
    """
    if not is_entry:
        # Exits are ALWAYS allowed. A strategy over its budget needs to sell; blocking that would trap
        # it above its target forever. Named as an EXEMPTION rather than a bare allow: otherwise an
        # exit is indistinguishable from an entry that happened to fit.
        return GateDecision(True, inputs={"exempt": "exit", "needs": float(notional)})
    if sleeve is None:
        # NOT `target: 0`. A strategy nobody has allocated to has not breached an allocation, and
        # reporting zeros for it would read as an allocation OF nothing — absence rendered as a value.
        return GateDecision(True, inputs={"sleeve": "unbudgeted", "needs": float(notional)})
    room = sleeve.deployable(currently_deployed)
    # Assembled ONCE and attached to every outcome below, so an allowed order carries the same
    # derivation as a refused one. The interesting moment is the one BEFORE a lane stops trading, and
    # a surface that only speaks on refusal cannot show it approaching the ceiling.
    facts = {
        "strategy_id": sleeve.strategy_id,
        "target": float(sleeve.target),
        "actual": float(sleeve.actual),
        "deployed": float(currently_deployed),
        "room": float(room),
        "needs": float(notional),
        "must_reduce": float(sleeve.must_reduce),
    }
    if sleeve.is_reducing:
        return GateDecision(
            False,
            f"{sleeve.strategy_id} is over its budget by {sleeve.must_reduce:,.0f} — "
            "entries are refused until it reduces; exits are unaffected",
            inputs=facts,
        )
    if notional > room + _EPS:
        return GateDecision(
            False,
            f"{sleeve.strategy_id} has {room:,.0f} of budget left and this order needs "
            f"{notional:,.0f}",
            inputs=facts,
        )
    return GateDecision(True, inputs=facts)


def over_budget_strategies(sleeves: dict[str, Sleeve]) -> list[str]:
    """Strategies currently refusing entries, for the operator surface.

    A gate that denies silently is a gate nobody can debug. This is what lets the UI say WHICH strategy
    is sell-only and why, rather than leaving an operator to infer it from orders that never appear.
    """
    return sorted(sid for sid, s in sleeves.items() if s.is_reducing)
