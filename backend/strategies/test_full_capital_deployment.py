"""Full capital deployment is CONFIGURED, not approximated (#806).

2026-09-08: "It is 1.0. In reality it is lower because of math, but only in reality, not per
config. The strategy needs to take care to not over-deploy because it will be stopped by the engine."

The rejected alternative was a fractional cap (0.97) chosen so a backtest would stop skipping
entries when cash ran short after spread. That bakes a guess about venue mechanics into policy and
hides the real constraint. 1.0 states the intent; the shortfall is a MEASUREMENT; and
`budget_allows` DENIES an over-reach before it reaches the venue (api/providers/gated_exec.py), so
over-deployment is refused rather than silently truncated.

These tests drive the real builders. A source grep would pass on a docstring, which is how two
guards in this repo were already earned falsely.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap

import pytest

from kumo_strategies.runtime.executor.runner import RiskLimits


def test_the_default_is_not_the_value_we_set_so_these_tests_can_fail():
    """FIXTURE PROPERTY FIRST. If the installed default were already 1.0, every assertion below
    would pass against a lane that sets nothing at all — the exact vacuity that let a 20_000.0
    constant agree with a 20000 setting for a lane's whole life. Pin the disagreement."""
    default = RiskLimits().max_deployed_frac
    assert default != 1.0, (
        f"installed kumo-trading-strategies default is already {default!r}; these tests can no longer "
        "distinguish 'cockpit set 1.0' from 'cockpit set nothing'"
    )
    assert default == pytest.approx(0.8)


def test_the_kwarg_exists_on_the_installed_class_or_construction_is_a_boot_kill():
    """momentum.py:200 already records that a RiskLimits field may not exist in the installed
    package. Passing a kwarg a dataclass does not declare raises TypeError AT GATEWAY
    CONSTRUCTION — which kills the lane before any order, the same shape as the `slot=` TypeError
    that took MOMENTUM and BCTROT down at the decision row."""
    fields = {f.name for f in dataclasses.fields(RiskLimits)}
    assert "max_deployed_frac" in fields, sorted(fields)
    RiskLimits(max_deployed_frac=1.0)  # must not raise


def _source_of(module, name):
    return textwrap.dedent(inspect.getsource(getattr(module, name)))


def _risklimits_call(module, name):
    """Resolve the RiskLimits() construction inside a builder, and REFUSE to pass if it is gone.

    The builders cannot be driven in a unit test — both reach Postgres through `asyncio.run` before
    they construct anything (momentum.py:904, qc345.py:965), so a live-infra harness would be the
    test, not the seam. This repo already answers that with an AST anchor plus a blindness guard
    (test_exit_release_reachable.py); the guard is the part that matters, because a matcher that
    silently finds nothing is how a `replace` that no-ops looks exactly like a fix.
    """
    tree = ast.parse(_source_of(module, name))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "RiskLimits"),
        None,
    )
    assert call is not None, (
        f"{module.__name__}.{name} no longer constructs RiskLimits — this test is blind, and the "
        "lane is silently back on the 0.8 default"
    )
    return call


def _kwarg_value(call, arg):
    kw = next((k for k in call.keywords if k.arg == arg), None)
    assert kw is not None, (
        f"RiskLimits is constructed without `{arg}` — omission is not neutral here, it inherits "
        f"the installed default ({RiskLimits().max_deployed_frac}), which is the value this change "
        "deliberately replaced"
    )
    return ast.literal_eval(kw.value)


def test_momentum_and_bctrot_deploy_the_whole_sleeve():
    """MOMENTUM-002 and BCTROT-004 both reach the cap through the RiskLimits handed to
    SessionGateway at momentum.py:932. Cockpit's lane code never READS the field — the runner
    enforces it — so the seam is what cockpit CONSTRUCTS."""
    from strategies import momentum

    assert _kwarg_value(_risklimits_call(momentum, "_build_rotation"), "max_deployed_frac") == 1.0


def test_qc345_constructs_the_whole_allocation():
    from strategies import qc345

    call = _risklimits_call(qc345, "build_qc345_strategy")
    assert _kwarg_value(call, "max_deployed_frac") == 1.0
    assert any(k.arg == "allocated_equity" for k in call.keywords), (
        "QC345 must keep sizing off its SLEEVE, not the account — 1.0 of the wrong base is the "
        "bug that had a lane sizing 16,554 a name against a 20,000 budget"
    )


def test_qc345_actually_spends_the_whole_allocation():
    """The reader half, driven for real. The AST tests above prove the value is CONSTRUCTED; this
    proves the value the reader is handed CHANGES THE NUMBER — two derivations of one fact, so a
    cap that stops being consulted cannot pass by agreeing with the default."""
    from strategies import qc345

    class _Cfg:
        portfolio_size = 4

    class _Sizer:
        _cfg = _Cfg()
        _broker = None

        def __init__(self, frac):
            self._limits = RiskLimits(allocated_equity=100_000.0, max_deployed_frac=frac)

    per_position = qc345.QC345SessionGateway._equity_per_position
    at_full = per_position(_Sizer(1.0))
    at_default = per_position(_Sizer(RiskLimits().max_deployed_frac))

    assert at_full == pytest.approx(25_000.0)  # 100k * 1.0 / 4 positions
    assert at_full > at_default, (
        "the cap is not reaching the sizing line — full deployment and the 0.8 default produce the "
        "same number, which is exactly how a dead knob looks"
    )


def test_qc27_is_deliberately_not_given_the_cap():
    """qc27/TECHIVOL does not READ max_deployed_frac — qc27_runner enforces one of the ten
    RiskLimits fields. Setting it there would be a knob wired to nothing, which is the defect that
    let a dead ALLOCATED_EQUITY agree with a live setting. If qc27 ever starts reading the field,
    this test must fail and force the decision rather than leaving a silent 0.8."""
    import pathlib

    src = pathlib.Path(__file__).with_name("qc27.py").read_text()
    assert "max_deployed_frac" not in src, (
        "qc27 now references max_deployed_frac — it is no longer a dead field, so cockpit must "
        "decide its value explicitly instead of inheriting the 0.8 default by omission"
    )
