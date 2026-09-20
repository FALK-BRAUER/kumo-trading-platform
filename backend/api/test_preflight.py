"""PREFLIGHT JUDGEMENT (#438) — the platform decides; the strategy only observes.

2026-08-22: "I need to be able to reliably deploy strategies. Which is not the case right now."

`build_optional_strategy` (engine_node.py:5216) verifies a strategy CONSTRUCTS. `RUNNING` means it
REGISTERED. Neither says it can do anything, and everything between construction and a filled order
went unverified — which is how twelve of nineteen decided slots since 2026-07-31 ended in nothing or
failure with nobody noticing one of them.

WHY THE STRATEGY MAY NOT JUDGE ITSELF. kumo-trading-strategies' own contract.py says it:

    "THE STRATEGY DECLARES, THE PLATFORM DECIDES ... There is deliberately no `check_your_own_budget`
     here and there must never be one: a strategy polices its own allocation correctly right up until
     the day it has a bug, and then holds more than it was granted, silently."

So a probe carries an OBSERVED VALUE and an error, never a verdict. Every defect of the last 48 hours
was a component wrong about itself while reporting fine; asking it for a boolean asks the broken thing
whether it is broken.

These tests are written BEFORE the module.
"""

from __future__ import annotations

from api.preflight import Outcome, Probe, judge_preflight

ENABLED = True


def p(name, value=None, error=None) -> Probe:
    return Probe(name=name, value=value, error=error)


def _fails(probes, name: str) -> bool:
    res = judge_preflight(probes, enabled=ENABLED, has_filled_this_session=False)
    return any(r.name == name and r.outcome is Outcome.FAIL for r in res)


def test_the_fixture_can_express_a_HEALTHY_strategy():
    # Fixture property first: if no input passed, every FAIL assertion below would be vacuous.
    healthy = [p("armed", True), p("equity", 103428.2), p("price", 90.32), p("slot_size", 183),
               p("budget", 20000.0), p("lifecycle", "TRADING"), p("owned", {}), p("universe", {"source": "symbols", "requested": 2, "resolved": 2, "unresolved": [], "ambiguous": {}})]
    res = judge_preflight(healthy, enabled=ENABLED, has_filled_this_session=False)
    assert all(r.outcome is Outcome.PASS for r in res), [r for r in res if r.outcome is not Outcome.PASS]


def test_equity_that_RAISED_fails():
    """kumo-trading-strategies ccea7b4 — `broker_equity` was a @property, so `equity()` called its RESULT and
    raised `'NoneType' object is not callable`. QC345 died mid-session on this."""
    assert _fails([p("equity", None, error="TypeError('NoneType' object is not callable)")], "equity")


def test_equity_that_returned_NONE_fails_and_is_NOT_the_same_diagnosis_as_a_raise():
    """kumo-trading-strategies b0593af — both QC lanes read `portfolio_value` from a message that publishes it
    as `equity`, so the call SUCCEEDED and returned None. A pass/fail probe reports these two identically
    and they are different bugs: one is a broken call, one is a broken key."""
    raised = [p("equity", None, error="TypeError(...)")]
    returned_none = [p("equity", None)]
    assert _fails(raised, "equity") and _fails(returned_none, "equity")
    r1 = next(r for r in judge_preflight(raised, enabled=ENABLED, has_filled_this_session=False) if r.name == "equity")
    r2 = next(r for r in judge_preflight(returned_none, enabled=ENABLED, has_filled_this_session=False) if r.name == "equity")
    assert r1.detail != r2.detail, "a raise and a None must not read the same in the report"


def test_zero_equity_fails():
    assert _fails([p("equity", 0.0)], "equity")


def test_SIZING_ZERO_SHARES_on_a_priced_symbol_fails():
    """QC345 2026-08-21: `enter INTC: sizing yielded 0 shares at 90.32` — a $90 stock against $103,428
    of equity. The call did not raise and the strategy reported RUNNING."""
    probes = [p("equity", 103428.2), p("price", 90.32), p("slot_size", 0), p("budget", 20000.0)]
    assert _fails(probes, "slot_size")


def test_a_zero_BUDGET_fails_while_the_lane_is_enabled():
    """TECHIVOL-005 was enabled with its budget still 0."""
    assert _fails([p("budget", 0.0)], "budget")


def test_a_DISABLED_lifecycle_fails_while_the_lane_is_enabled():
    """A lifecycle PROBE reporting DISABLED must fail preflight — regardless of how it got there.

    The assertion was always right; this docstring was not. It said "enabling QC27 without a lifecycle
    row leaves it DISABLED", and an absent row reads as TRADING (2026-08-19). A lane with no row
    is not caught by this probe at all — it is trading. [absent-row: historical]
    """
    assert _fails([p("lifecycle", "DISABLED")], "lifecycle")


def test_OWNED_is_judged_against_WHETHER_THE_STRATEGY_HAS_EVER_FILLED():
    """THE ONE A SELF-REPORTED BOOLEAN CANNOT CATCH, and the most dangerous of the eight.

    kumo-trading-strategies 578fbb6: TECHIVOL read `broker.positions()` — the ACCOUNT book — as its own. It
    returned eight symbols and NO error. It looked healthy, and went on to form eight liquidation
    orders against BCTROT's and MOMENTUM's positions.

    `owned=[8 symbols]` is only wrong in the light of "this strategy has never filled anything", which
    the strategy does not know and the platform does. That is the whole argument for the value/verdict
    split, in one probe.
    """
    owned_eight = [p("owned", {"AEM": 18, "AMGN": 8, "BDX": 65, "BETA": 79,
                               "CGAU": 174, "WHD": 56, "WPM": 26, "XLV": 11}), p("universe", {"source": "symbols", "requested": 2, "resolved": 2, "unresolved": [], "ambiguous": {}})]
    never_filled = judge_preflight(owned_eight, enabled=ENABLED, has_filled_this_session=False)
    has_filled = judge_preflight(owned_eight, enabled=ENABLED, has_filled_this_session=True)

    assert any(r.name == "owned" and r.outcome is Outcome.FAIL for r in never_filled)
    assert all(r.outcome is not Outcome.FAIL for r in has_filled), (
        "the SAME observation must pass once the strategy has actually filled — otherwise this is not "
        "judging against expectation, it is just banning non-empty books"
    )


def test_a_DISABLED_lane_is_not_judged_at_all():
    """A lane nobody has switched on has not breached anything. Failing it would make the arrival of
    this feature light up every strategy that is off, and an alarm that fires on the normal state gets
    switched off wholesale — the same reasoning budget_gate.py already applies to an unknown sleeve."""
    res = judge_preflight([p("equity", None), p("budget", 0.0)], enabled=False,
                          has_filled_this_session=False)
    assert all(r.outcome is Outcome.SKIPPED for r in res)


def test_an_UNKNOWN_probe_name_is_reported_not_silently_dropped():
    """If a lane declares a seventh capability, the judge does not know its rule — but dropping it would
    hide a probe the strategy thought was being checked. UNKNOWN is visible and is not a pass."""
    res = judge_preflight([p("liquidity_floor", 12)], enabled=ENABLED, has_filled_this_session=False)
    out = next(r for r in res if r.name == "liquidity_floor")
    assert out.outcome is Outcome.UNKNOWN


def test_a_MISSING_probe_is_a_failure_of_the_STRATEGY_not_a_pass():
    """A lane that returns no `equity` probe at all must not read as healthy. This is the shape that
    let `test_broker_equity_is_a_METHOD_on_every_strategy` miss QC27: absence read as agreement."""
    res = judge_preflight([p("equity", 100.0)], enabled=ENABLED, has_filled_this_session=False)
    missing = [r for r in res if r.outcome is Outcome.MISSING]
    assert {r.name for r in missing} >= {"price", "slot_size", "budget", "lifecycle", "owned"}


def test_the_two_halves_of_the_probe_set_are_DISJOINT_and_complete():
    """BOUNDARY, set with kumo-trading-strategies 2026-08-22.

    The strategy reports only what it learns through the BROKER. `lifecycle` and `budget` are cockpit's
    own state — asking a strategy to attest to them is the `check_your_own_budget` anti-pattern one
    notch further out. `slot_size` is arithmetic over equity and price, computed cockpit-side so the
    strategy cannot be wrong about it.
    """
    from api.preflight import REQUIRED_FROM_STRATEGY, REQUIRED_PROBES, SUPPLIED_BY_PLATFORM

    # `armed` joined 2026-08-22 with kumo-trading-strategies 7de760a. It belongs on the STRATEGY side for the
    # boundary's own reason: cockpit cannot observe it from outside. A lane whose calendar never
    # resolved is running, counted, and unable to decide — invisible to every probe the platform can
    # take for itself.
    # `universe` joined 2026-08-29 with issue 80 / a456c26, on the STRATEGY side by the
    # boundary's own reason: symbol resolution happens in the strategy's on_start (#622), so only
    # the strategy can attest to what resolved, what did not, and what was ambiguous. Cockpit
    # observing it from outside required grepping a container (#625).
    assert set(REQUIRED_FROM_STRATEGY) == {"armed", "equity", "price", "owned", "universe"}
    assert set(SUPPLIED_BY_PLATFORM) == {"lifecycle", "budget", "slot_size"}
    assert not set(REQUIRED_FROM_STRATEGY) & set(SUPPLIED_BY_PLATFORM), "a probe may have ONE owner"
    assert set(REQUIRED_PROBES) == set(REQUIRED_FROM_STRATEGY) | set(SUPPLIED_BY_PLATFORM)


def test_a_MISSING_probe_says_WHOSE_bug_it_is():
    """A lane that stopped reporting `equity` is kumo-trading-strategies' problem; a missing `budget` is cockpit
    failing to supply its own state. One message for both sends the reader to the wrong repo — and with
    two repos and two sessions, that is not hypothetical."""
    res = judge_preflight([p("equity", 100.0)], enabled=ENABLED, has_filled_this_session=False)
    by = {r.name: r for r in res}
    assert "strategy reported no such probe" in by["price"].detail
    assert "platform-side" in by["budget"].detail
