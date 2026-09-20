"""A lifecycle row saying DISABLED is an OPERATOR DECISION, not a degradation (#638).

Measured on ibkr-paper-retired: MOMENTUM-002 carries `exec_strategy_state = DISABLED` (operator note:
"staging runs ONE strategy") while the container carries `KUMO_MOMENTUM_ENABLED=true`. The env gate
says build it; the lifecycle row says do not run it. Nothing reconciled them, so preflight compared
a built lane against its deliberate zero allocation and reported

    [ERROR] PREFLIGHT DEGRADED MOMENTUM-002: budget: budget=0.0 while the lane is enabled

— which trained the operator to ignore ERRORs, and got read as a real defect during the #633
investigation. The disagreement is the bug, not either value: DISABLED must be a THIRD verdict
(ready / degraded / disabled-by-operator), derived in ONE place.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.preflight import Outcome, Probe
from api.preflight_runner import run_preflight


def _lane(probes):
    return SimpleNamespace(preflight=lambda broker: probes)


_BROKER = SimpleNamespace(equity=lambda: 100_000.0)


def _healthy_probes():
    # What a built-but-disabled lane actually reports: the broker answers fine.
    return [Probe("armed", True), Probe("equity", 100_000.0),
            Probe("price", 10.0), Probe("owned", {})]


def test_the_fixture_reproduces_the_staging_spam_without_the_fix_shape():
    """FIXTURE PROPERTY: with lifecycle TRADING and budget 0 this platform dict DOES degrade —
    proving the probes and platform shape can express the failure, so the DISABLED case below
    passes for its own reason and not because the fixture is inert."""
    report = run_preflight(
        _lane(_healthy_probes()), broker=_BROKER,
        platform={"lifecycle": "TRADING", "budget": 0.0, "slot_size": 0},
        enabled=True, has_filled_this_session=True, strategy_id="MOMENTUM-002",
    )
    assert not report.ready
    assert any(r.name == "budget" for r in report.failures)


def test_DISABLED_by_operator_is_not_degraded():
    """The staging shape verbatim: lifecycle row DISABLED, budget 0, env gate enabled. This must be
    the lane's OWN verdict — no failures, nothing for the boot gate to page about every 2s."""
    report = run_preflight(
        _lane(_healthy_probes()), broker=_BROKER,
        platform={"lifecycle": "DISABLED", "budget": 0.0, "slot_size": 0},
        enabled=True, has_filled_this_session=True, strategy_id="MOMENTUM-002",
    )
    assert report.failures == [], (
        "a deliberate DISABLED row is reported as degradation — the every-2s ERROR spam of #638"
    )
    assert report.ready


def test_disabled_is_LOUD_in_the_report_not_absent():
    """Three states, never two: the report must SAY disabled-by-operator — an empty report would be
    indistinguishable from probed-and-healthy, which is the absence-read-as-agreement class."""
    report = run_preflight(
        _lane(_healthy_probes()), broker=_BROKER,
        platform={"lifecycle": "DISABLED", "budget": 0.0, "slot_size": 0},
        enabled=True, has_filled_this_session=True, strategy_id="MOMENTUM-002",
    )
    assert getattr(report, "disabled", False) is True
    assert any("DISABLED" in r.detail for r in report.results), (
        "the verdict does not name the operator decision anywhere"
    )


def test_a_TRADING_lane_with_zero_budget_still_degrades():
    """The other direction must survive: the fix must not disarm the budget detector for lanes the
    operator did NOT disable — QC345's budget=0 while TRADING was a real defect (#568 family)."""
    report = run_preflight(
        _lane(_healthy_probes()), broker=_BROKER,
        platform={"lifecycle": "TRADING", "budget": 0.0, "slot_size": 0},
        enabled=True, has_filled_this_session=True, strategy_id="QC345-003",
    )
    assert not report.ready
    assert any(r.name == "budget" and r.outcome is Outcome.FAIL for r in report.failures)


def test_an_ABSENT_lifecycle_row_is_not_disabled():
    """An absent row means TRADING (the staging runbook had this inverted, 2026-08-24). Absence must
    not be read as the operator having decided anything — the lane is judged normally."""
    report = run_preflight(
        _lane(_healthy_probes()), broker=_BROKER,
        platform={"budget": 0.0, "slot_size": 0},
        enabled=True, has_filled_this_session=True, strategy_id="BCTROT-004",
    )
    assert getattr(report, "disabled", False) is False
    assert not report.ready  # budget=0 still judged
