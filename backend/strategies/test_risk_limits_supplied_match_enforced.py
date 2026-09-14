"""Every RiskLimits field cockpit supplies must be READ, and every field READ must be supplied (#517).

THE DETECTOR, NOT THE INSTANCE. Three separate defects this session were one shape — a limit that
looks configured and is not connected to anything:

    TECHIVOL's allocation was `ALLOCATED_EQUITY = 20_000.0`, a constant. The settings knob
    `strategies.TECHIVOL-005 = 20000` AGREED with it, so no observation could contradict either, and
    a test asserting `== 20_000.0` passed. Dead for the lane's whole life (7761c92).

    `qc27_runner` reads 1 of 10 `RiskLimits` fields. Setting `max_positions=4` on TECHIVOL changes
    nothing and reports nothing, because the type DECLARES ten and the runner READS one (#517).

    QC345's gateway reads `max_position_notional` and `max_deployed_frac`, and cockpit constructs it
    with `RiskLimits(allocated_equity=...)` only — so both arrive as DATACLASS DEFAULTS. 20000.0 and
    0.8 became policy without anyone choosing them.

Each was invisible for the same reason: a declaration agreed with an intention while the wire between
them was cut. `agreement-is-not-connection`, three times.

WHAT THIS ASSERTS, per lane, measured against the INSTALLED package rather than remembered:

    SUPPLIED but not READ  -> a dead knob. An operator sets it, nothing happens, nothing says so.
    READ but not SUPPLIED  -> a dataclass default silently became policy.

Neither is automatically a bug — a default can be a deliberate choice — so the answer is RECORDED
here per lane and asserted as EQUALITY. A new field on the shared type, or a runner that starts or
stops reading one, fails this file until somebody decides which of the two it is. That is the point:
the shared `RiskLimits` grew fields a second runner never wired, and nothing noticed for months.

`pgrunner.py:517` records the same class happening once before, in kumo-strategies' own words:
"RiskLimits.daily_loss_frac said 'halts the strategy' and nothing read it."
"""

from __future__ import annotations

import ast
import dataclasses as dc
import inspect
import pathlib

import pytest
from kumo_strategies.runtime.executor.runner import RiskLimits

# (lane, module that CONSUMES the limits, cockpit module that CONSTRUCTS them)
LANES = [
    ("TECHIVOL-005", "kumo_strategies.runtime.executor.qc27_runner", "strategies/qc27.py"),
    ("MOMENTUM-002", "kumo_strategies.runtime.executor.pgrunner", "strategies/momentum.py"),
]

#: What each lane SUPPLIES today, and what its consumer READS. Recorded so a change has to be
#: deliberate. Update with a reason, never to make the suite green.
EXPECTED = {
    # qc27_runner now reads TWO. `daily_loss_frac` was added by kumo-strategies ea21f98/dfc8464
    # (#548): TECHIVOL can finally halt on a loss, and an UNARMED lane now JOURNALS that it cannot,
    # so "had no reason to halt" and "has no ability to halt" stop being the same empty journal.
    #
    # UPDATED DELIBERATELY, not loosened. This tripwire fired on the strategies bump and that is
    # exactly its job — a field that starts or stops being read is a limit that started or silently
    # stopped enforcing. The remaining eight are still accepted, rendered in a config dump and
    # enforced nowhere, which is #517 and still open.
    #
    # READ is not ARMED: the runner carries `daily_loss_armed: bool = False`, so reading the field
    # does not mean TECHIVOL is halting on 5% today. Arming is a separate decision and the operator's.
    "TECHIVOL-005": {"reads": {"allocated_equity", "daily_loss_frac"},
                     "supplies": {"allocated_equity"}},
    # pgrunner reads all ten. Cockpit now supplies ONE: `max_deployed_frac=1.0` (#806).
    #
    # UPDATED DELIBERATELY, not loosened — this tripwire fired on the change and that is its job.
    # NOT a dead knob: `pgrunner.py:1345` sizes every slot as
    # `equity * limits.max_deployed_frac / sizing_denominator(...)`, so the value is consumed on the
    # runner side even though cockpit's own lane code never reads it.
    #
    # WHY IT IS SET AT ALL. Operator, 2026-09-08: "It is 1.0. In reality it is lower because of math,
    # but only in reality, not per config. The strategy needs to take care to not over-deploy
    # because it will be stopped by the engine." The 0.80 that ran until today was a dataclass
    # default nobody chose — the exact condition the rest of this file exists to catch.
    #
    # WHAT 1.0 DOES NOT BUY. `max_deployed_frac` is applied PER SLOT, not as an aggregate:
    # kumo-strategies' own momentum_rotation/config.py:117 records that `_submit` has no aggregate
    # check and no cash check, so over-deployment is reachable from the strategy side. The only
    # aggregate ceiling is cockpit's `budget_allows`, which sums the lane's open positions — and it
    # FAILS OPEN on an unreadable book by design. At 0.80 the runner's own sizing left 20% of slack
    # absorbing that; at 1.0 there is none, so a fail-open budget read is now the whole margin.
    "MOMENTUM-002": {"reads": {f.name for f in dc.fields(RiskLimits)},
                     "supplies": {"max_deployed_frac"}},
}

ALL_FIELDS = {f.name for f in dc.fields(RiskLimits)}
ROOT = pathlib.Path(__file__).resolve().parent


def _reads(module_path: str) -> set[str]:
    import importlib

    src = inspect.getsource(importlib.import_module(module_path))
    return {f for f in ALL_FIELDS if f"limits.{f}" in src}


def _supplies(cockpit_file: str) -> set[str]:
    """Keywords passed to any `RiskLimits(...)` call in the file. AST, so a comment cannot fake it."""
    tree = ast.parse((ROOT.parent / cockpit_file).read_text())
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "RiskLimits":
            out |= {k.arg for k in n.keywords if k.arg}
    return out


def test_the_fixture_can_see_a_RiskLimits_construction_at_all():
    """THE FIXTURE'S OWN PROPERTY FIRST. If the AST found no `RiskLimits(...)` call the 'supplies'
    set would be empty for every lane and every assertion below would pass over a blind read."""
    assert _supplies("strategies/qc27.py"), (
        "no RiskLimits(...) construction found in qc27.py — this test is blind")
    assert ALL_FIELDS, "RiskLimits has no fields — the type moved"


@pytest.mark.parametrize("lane,consumer,cockpit_file", LANES, ids=[l[0] for l in LANES])
def test_what_is_READ_and_what_is_SUPPLIED_are_both_exactly_what_we_recorded(
        lane, consumer, cockpit_file):
    """Asserted as EQUALITY, not as a subset, so a field appearing OR disappearing both fail."""
    reads, supplies = _reads(consumer), _supplies(cockpit_file)

    assert reads == EXPECTED[lane]["reads"], (
        f"{lane}: {consumer} now reads {sorted(reads)}, recorded {sorted(EXPECTED[lane]['reads'])}.\n"
        f"  gained: {sorted(reads - EXPECTED[lane]['reads'])}\n"
        f"  lost:   {sorted(EXPECTED[lane]['reads'] - reads)}\n"
        f"A field that STOPPED being read is a limit that silently stopped enforcing."
    )
    assert supplies == EXPECTED[lane]["supplies"], (
        f"{lane}: cockpit now supplies {sorted(supplies)}, recorded "
        f"{sorted(EXPECTED[lane]['supplies'])}. Adding a field that the consumer does not read is a "
        f"DEAD KNOB — it will be set, nothing will happen, and nothing will say so."
    )


@pytest.mark.parametrize("lane,consumer,cockpit_file", LANES, ids=[l[0] for l in LANES])
def test_no_field_is_SUPPLIED_that_the_consumer_never_READS(lane, consumer, cockpit_file):
    """THE DEAD-KNOB DIRECTION, stated on its own so the failure names the defect.

    This is TECHIVOL's dead allocation exactly: cockpit set it, the runner ignored it, both agreed on
    20000, and no observation could tell the difference.
    """
    dead = _supplies(cockpit_file) - _reads(consumer)
    assert not dead, (
        f"{lane}: cockpit supplies {sorted(dead)}, which {consumer} never reads. Setting it changes "
        f"nothing and reports nothing — the shape that made `ALLOCATED_EQUITY = 20_000.0` invisible "
        f"for the lane's entire life"
    )


def test_the_QC345_gateway_reads_limits_cockpit_does_not_supply():
    """THE OTHER DIRECTION, and it is live: a DEFAULT quietly became policy.

    `QC345SessionGateway` is cockpit's own code. It reads `max_position_notional` and
    `max_deployed_frac`, and `qc345.py:752` constructs `RiskLimits(allocated_equity=...)` — so both
    arrive as dataclass defaults, 20000.0 and 0.8. Nobody chose them and no settings domain exposes
    them: `settings/qc345` is the STRATEGY config (lookback, portfolio_size), not risk limits.

    Today `max_position_notional` cannot even bind — the per-position slot is roughly
    `allocated_equity * max_deployed_frac / portfolio_size`, far below 20000 — so it is a declared
    cap doing no work, which is the dead-mechanism signature: identical results whether or not it
    exists. It starts binding, invisibly, the day the allocation grows.

    Recorded rather than fixed: choosing those numbers is a risk decision, not a refactor. This fails
    the moment the set changes, so the decision cannot be lost.
    """
    src = (ROOT / "qc345.py").read_text()
    reads = {f for f in ALL_FIELDS if f"_limits.{f}" in src}
    supplies = _supplies("strategies/qc345.py")

    assert reads == {"max_position_notional", "max_deployed_frac"}, (
        f"QC345's gateway now reads {sorted(reads)} — re-derive which of them are defaults")
    # `max_deployed_frac` moved from DEFAULT to CONFIGURED on 2026-09-08 (#806): QC345 is the one
    # cockpit lane that reads the cap in its own code (qc345.py:879), so here it is both supplied
    # and read, and `max_position_notional` is the only remaining unchosen default.
    assert supplies == {"allocated_equity", "max_deployed_frac"}, (
        f"cockpit now supplies {sorted(supplies)} to QC345 — if a former default is now configured, "
        f"record it here")

    from_defaults = reads - supplies
    assert from_defaults == {"max_position_notional"}, (
        f"the set of QC345 limits coming from DATACLASS DEFAULTS changed to {sorted(from_defaults)}. "
        f"Every one of these is policy nobody chose"
    )


def test_QC345s_notional_cap_CANNOT_BIND_at_todays_allocation():
    """A cap that cannot bind is a cap doing no work — say so, and notice the day it changes.

    Suggested by the kumo-strategies peer, whose own `test_at_STAGING_SIZE...` does this for the
    notional cap on their side and has already earned itself once.

    QC345 sizes each entry as:

        qty = int(min(limits.max_position_notional, equity_per_position) // price)

    with `equity_per_position ~= allocated_equity * max_deployed_frac / portfolio_size`. At today's
    numbers that slot sits FAR below the 20000 default, so `max_position_notional` never enters the
    `min()`. It produces identical results whether or not it exists — the dead-mechanism signature
    from CLAUDE.md, and the reason nobody noticed it was never configured.

    THE HAZARD IS NOT TODAY, IT IS THE DAY THE ALLOCATION GROWS. At an allocation where the slot
    crosses 20000, a limit nobody chose silently starts shaping the book. This test fails then, which
    is the whole point: the change becomes a decision instead of a surprise.
    """
    import dataclasses as _dc

    from kumo_strategies.runtime.executor.runner import RiskLimits

    defaults = {f.name: f.default for f in _dc.fields(RiskLimits)}
    notional_cap = defaults["max_position_notional"]
    deployed_frac = defaults["max_deployed_frac"]

    # The allocation cockpit actually configures for this lane, and the book size it sizes against.
    from strategies import qc345

    allocated = 20_000.0                     # settings `strategies.QC345-003`
    portfolio_size = qc345._live_config().portfolio_size

    slot = allocated * deployed_frac / portfolio_size

    assert portfolio_size > 0, "portfolio_size is 0 — the slot arithmetic below is meaningless"
    assert slot < notional_cap, (
        f"QC345's per-position slot is {slot:,.0f} and the notional cap is {notional_cap:,.0f} — the "
        f"cap now BINDS. It arrives as a dataclass default that no settings domain exposes, so a "
        f"number nobody chose has started shaping the book. Decide it, then update this test with "
        f"the reason"
    )

    # THE NUMBER, NOT JUST THE VERDICT. Measured 2026-08-25: portfolio_size=5, deployed_frac=0.8,
    # allocation 20,000 -> slot 3,200 against a 20,000 cap, a 6.2x margin. The cap begins to bind at
    # an allocation of 125,000:
    #
    #     allocation    20,000 -> slot  3,200   binds=False   <- today
    #     allocation    62,500 -> slot 10,000   binds=False
    #     allocation   125,000 -> slot 20,000   binds=True    <- here
    #
    # Pinning the reasoning rather than the outcome, per CLAUDE.md: an assertion that merely passes
    # teaches nothing to whoever breaks it next.
    binds_at = notional_cap * portfolio_size / deployed_frac
    assert allocated < binds_at / 2, (
        f"the allocation is {allocated:,.0f} and the unconfigured cap begins to bind at "
        f"{binds_at:,.0f} — within 2x. Still not binding, but one increase away from a number nobody "
        f"chose shaping the book. Decide it before it decides itself"
    )
