"""PREFLIGHT RUNNER (#438) — collect the strategy's observations, add cockpit's, judge, report.

Tests precede the module. The double is deliberate and NOT `kumo_strategies`: `preflight` exists in
the sibling CHECKOUT (00eb99c) and NOT in this venv, and a test importing the venv copy would pass or
skip for reasons unrelated to the code. The contract test at the bottom scans the checkout, which is
what `deploy/Dockerfile.backend` bakes.
"""

from __future__ import annotations

import pytest

from api.preflight import Outcome, Probe
from api.preflight_runner import run_preflight


class _Lane:
    """Shaped like RegistrationMixin where the runner touches it."""

    def __init__(self, probes=None, boom: Exception | None = None):
        self._probes, self._boom = probes or [], boom

    def preflight(self, broker):
        if self._boom:
            raise self._boom
        return self._probes


PLATFORM_OK = {"lifecycle": "TRADING", "budget": 20000.0, "slot_size": 183}


def test_the_fixture_yields_a_READY_lane():
    # Fixture property first: if nothing could be READY, every failure assertion below is vacuous.
    lane = _Lane([Probe("armed", True), Probe("equity", 103428.2), Probe("price", 90.32), Probe("owned", {}), Probe("universe", {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})])
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False)
    assert r.ready is True and r.failures == []


def test_a_lane_that_FAILS_A_PROBE_is_not_ready_and_the_report_NAMES_which():
    """"Not ready" without the reason sends someone diffing two repos. The whole point of the probe
    carrying a VALUE is that the report can say equity=None rather than equity=FAIL."""
    lane = _Lane([Probe("armed", True), Probe("equity", None), Probe("price", 90.32), Probe("owned", {}), Probe("universe", {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})])
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False)
    assert r.ready is False
    assert [f.name for f in r.failures] == ["equity"]
    assert "None" in r.failures[0].detail


def test_preflight_RAISING_makes_the_lane_not_ready_rather_than_taking_the_caller_down():
    """A lane whose probe collection explodes is the most broken case there is, and it must not be the
    one that escapes. #377 is the precedent: QC345 resolving its universe over HTTP at build time took
    MANUAL, MOMENTUM and BCTROT down with it."""
    r = run_preflight(_Lane(boom=RuntimeError("adapter exploded")), broker=object(),
                      platform=PLATFORM_OK, enabled=True, has_filled_this_session=False)
    assert r.ready is False
    assert any("adapter exploded" in f.detail for f in r.failures)


def test_the_PLATFORM_probes_are_added_by_cockpit_and_judged_alongside():
    """The boundary: lifecycle/budget/slot_size are cockpit's own state. A lane cannot report them and
    cannot be wrong about them."""
    lane = _Lane([Probe("armed", True), Probe("equity", 103428.2), Probe("price", 90.32), Probe("owned", {}), Probe("universe", {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})])
    # lifecycle stays TRADING here: a DISABLED row is the operator's decision and short-circuits to
    # its own verdict since #638 — this test's subject is that cockpit's OWN probes are judged
    # alongside the lane's, which budget=0 on a TRADING lane still demonstrates.
    r = run_preflight(lane, broker=object(),
                      platform={"lifecycle": "TRADING", "budget": 0.0, "slot_size": 0},
                      enabled=True, has_filled_this_session=False)
    assert r.ready is False
    assert "budget" in {f.name for f in r.failures}


def test_a_lane_that_CANNOT_BE_PROBED_AT_ALL_is_not_ready():
    """A strategy with no `preflight` satisfies every older Protocol while being unprobeable — and to
    anything reading the report, unprobeable must not be indistinguishable from probed-and-healthy.
    That is the exact shape of every defect this week."""

    class _Unprobeable:
        pass

    r = run_preflight(_Unprobeable(), broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False)
    assert r.ready is False
    assert any("preflight" in f.detail for f in r.failures)


def test_a_DISABLED_lane_is_ready_by_definition_and_probes_nothing():
    """A lane nobody switched on has not breached anything, and probing it would make enabling the
    feature light up every off strategy on day one."""
    lane = _Lane([Probe("equity", None)])
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=False,
                      has_filled_this_session=False)
    assert r.ready is True and r.failures == []


def test_the_alert_key_routes_to_notify_strategy_degraded():
    lane = _Lane([Probe("armed", True), Probe("equity", None), Probe("price", 90.32), Probe("owned", {}), Probe("universe", {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})])
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False, strategy_id="QC345-003")
    assert r.alert_key.split(":", 1)[0] == "strategy_degraded"
    assert "QC345-003" in r.alert_key


def test_the_kumo_strategies_CHECKOUT_declares_preflight_on_the_PROTOCOL():
    """Scans the SIBLING CHECKOUT, never the venv — that is what Dockerfile.backend bakes, and the venv
    copy here does not have `preflight` at all. Skips rather than asserting against the wrong tree; a
    check that fails for environmental reasons teaches people to switch it off."""
    import pathlib

    here = pathlib.Path(__file__).resolve()
    contract = next(
        (c for c in (p.parent / "kumo-trading-strategies" / "src" / "kumo_strategies" / "runtime"
                     / "nautilus" / "contract.py" for p in here.parents) if c.is_file()),
        None,
    )
    if contract is None:
        pytest.skip("the kumo-trading-strategies checkout is not beside this repo")
    src = contract.read_text()
    proto = src[src.index("class StrategyRegistration"):]
    proto = proto[:proto.index("\nclass ")] if "\nclass " in proto else proto
    assert "def preflight(" in proto, (
        "preflight is not on the PROTOCOL — on the mixin alone, a lane that does not use the mixin "
        "satisfies the contract while being unprobeable"
    )


def test_a_probe_the_lane_STOPPED_REPORTING_counts_against_readiness():
    """MISSING is not a pass.

    THE MUTATION THAT FOUND THIS GAP: narrowing the failure set to `Outcome.FAIL` alone left every
    other test green, because no fixture omitted a probe. The fixture could not violate the property
    it was asserting — the same shape as the ratchet test earlier today, and the reason a sweep is
    worth more than a review.

    A lane that quietly stops reporting `price` is indistinguishable from one that reports a good
    price, unless absence counts.
    """
    lane = _Lane([Probe("equity", 103428.2), Probe("owned", {})])          # no `price`
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False)
    assert r.ready is False
    assert any(f.name == "price" and f.outcome is Outcome.MISSING for f in r.failures)


def test_a_probe_COCKPIT_has_no_rule_for_counts_against_readiness():
    """UNKNOWN is not a pass either. A lane declaring a seventh capability must not have it silently
    ignored while believing it is checked — "nobody has a rule for this" is a reason to look, not a
    reason to proceed."""
    lane = _Lane([Probe("equity", 103428.2), Probe("price", 90.32), Probe("owned", {}),
                  Probe("liquidity_floor", 12)])
    r = run_preflight(lane, broker=object(), platform=PLATFORM_OK, enabled=True,
                      has_filled_this_session=False)
    assert r.ready is False
    assert any(f.name == "liquidity_floor" and f.outcome is Outcome.UNKNOWN for f in r.failures)
