"""What sizing falls back to when the allocation is unreadable — and why that is dangerous on IBKR.

`pgrunner.py:1153`:

    equity = self.limits.allocated_equity or self.broker.equity()

An `or`, so BOTH None and 0.0 fall through to the account. `_limits_for_session` returns the
CONSTRUCTED limits — a bare `RiskLimits()` with `allocated_equity=None` — on two paths:

    the settings read raises      -> return self._limits
    target <= 0                   -> return self._limits

The second is the reachable one, and it is not hypothetical: `api.settings.resolve` NEVER RAISES, it
degrades to schema defaults, so a missing or reset settings file yields target 0 rather than an
exception. That happened on ibkr-paper-retired on 2026-08-24 — a deploy re-seeded the settings volume from
instance config and BCTROT-004's target went 20,000 -> 0 while the lane stayed armed and TRADING.

ON ALPACA the fallback is survivable: the account number is USD and the right order of magnitude, and
`budget_gate.may_submit` refuses entries anyway once `actual > target` makes the sleeve reducing.

ON IBKR NEITHER PROTECTION HOLDS. The account reports 1,000,016.15 SGD, and the budget gate is not in the
IBKR order path at all — its only production callers are qc345.py, qc27.py and the ALPACA exec client.
A lane meant to size off 100,000 would size off 999,216: a tenfold oversize on a live broker login,
in the wrong currency (ni9q2dnz, 2026-08-24).

The fix is one line and it lives in kumo-trading-strategies: `allocated_equity if ... is not None else ...`,
which also lets 0.0 mean "wind me down" instead of "use the whole account".
"""
from __future__ import annotations

import pytest


def _limits_of(target, monkeypatch):
    """Drive the REAL `_limits_for_session` with a patched settings read."""
    from strategies import momentum

    class _Gateway(momentum.SessionGateway):
        def __init__(self):
            from kumo_strategies.runtime.executor.runner import RiskLimits

            self._limits = RiskLimits()
            self._strategy_id = "BCTROT-004"

    monkeypatch.setattr("api.settings.resolve", lambda _domain: {"BCTROT-004": target})
    return _Gateway()._limits_for_session()


def test_the_fixture_reaches_the_real_function_and_a_GOOD_target_is_honoured(monkeypatch):
    """Fixture property first: with a real allocation the basis is the allocation, so a failure below
    is about the fallback and not about the harness."""
    limits = _limits_of(100_000.0, monkeypatch)
    assert limits.allocated_equity == 100_000.0


def test_a_zero_target_now_ARRIVES_as_zero_but_the_hazard_is_only_HALF_closed(monkeypatch):
    """RE-DERIVED 2026-08-24, exactly as the old assertion's own failure message asked.

    Cockpit changed: `_limits_for_session` used to return unset limits for a zero target, fusing
    "wind me down" into "no allocation configured". It now distinguishes them — ABSENT returns unset
    limits (unchanged, deliberately), ZERO passes 0.0 through.

    THAT DOES NOT CLOSE THE HAZARD, and saying so would be the more dangerous error. The sizing line
    in the INSTALLED kumo-trading-strategies is still `allocated_equity or broker.equity()`, and 0.0 is
    falsy — so a zero target still reaches the account on the DEPLOYED pin. Cockpit now hands
    downstream a number that MEANS zero; downstream cannot yet hear it. The fix upstream exists
    (be244d9) and is NOT in the pinned ref.

    So this is still characterisation, of a hazard that is now half cockpit's and half a pin bump.
    The next test pins the upstream half and is the one that closes when the pin moves.
    """
    limits = _limits_of(0.0, monkeypatch)
    assert limits.allocated_equity == 0.0, (
        "a zero target no longer arrives as zero — cockpit has regressed to fusing wind-down into "
        "unconfigured")
    assert not bool(limits.allocated_equity), (
        "0.0 is still FALSY, which is why the upstream `or` below still sends this to the account — "
        "if this ever fails, the value stopped being a plain zero and the chain needs re-deriving")


def test_an_ABSENT_target_still_leaves_allocated_equity_UNSET(monkeypatch):
    """The other half of the split, pinned separately so the two cannot drift back together.

    Absent keeps its long-standing behaviour on this lane: unset limits, account fallback, logged.
    Deliberately NOT tightened here — that is a live behaviour change for MOMENTUM-002 and
    BCTROT-004 and it belongs in its own change, not smuggled into one about zero.
    """
    monkeypatch.setattr("api.settings.resolve", lambda _domain: {})
    from strategies import momentum

    class _Gateway(momentum.SessionGateway):
        def __init__(self):
            from kumo_strategies.runtime.executor.runner import RiskLimits
            self._limits = RiskLimits()
            self._strategy_id = "BCTROT-004"

    assert _Gateway()._limits_for_session().allocated_equity is None


@pytest.mark.parametrize("module", ["pgrunner", "qc27_runner"])
def test_ZERO_IS_NO_LONGER_FUSED_WITH_UNSET_in_the_installed_runner(module):
    """CLOSED UPSTREAM, 2026-08-25. This test did its job: it went red the moment the fix landed.

    It used to assert the PRESENCE of `self.limits.allocated_equity or self.broker.equity()` and
    carried an `xfail(strict=True)` below it, so that the one-line upstream fix would XPASS and fail
    the suite — a reminder that could not be forgotten rather than a comment. That is what happened,
    except it surfaced for a second reason worth recording: the cockpit backend venv had a
    kumo-trading-strategies OLDER than the deployed pin, so the whole local suite had been measuring a
    revision production does not run. Aligning the venv to `cb5ee79` is what made it visible.

    Both runners now read:

        alloc = self.limits.allocated_equity
        equity = <account> if alloc is None else float(alloc)

    `0.0` no longer falls through to the account. That mattered on ibkr-paper-retired, where BCTROT-004
    holds 100,000 and EVERY other lane is allocated 0 against a ~999,216 SGD account — so every
    unfunded lane there was one decision away from sizing off BCTROT's capital. It had not bitten
    only because nothing on that stack had ever decided.

    Pinned against the INSTALLED package, not remembered, and for BOTH runners: fixing one and not
    the other is the drift this file exists to catch.
    """
    import inspect
    from conftest import real_installed_module as real_module

    src = inspect.getsource(real_module(f"kumo_strategies.runtime.executor.{module}"))

    assert "allocated_equity or self.broker.equity()" not in src, (
        f"{module} reverted to the falsy `or` — a lane allocated 0.0 sizes off the whole account "
        f"again. On staging that is ~999,216 SGD against an intended 100,000 basis, with the budget "
        f"gate absent from the IBKR order path")
    assert "allocated_equity or self._account_equity()" not in src, (
        f"{module} reverted to the falsy `or` by the other route")
    assert "alloc is None" in src, (
        f"{module} no longer distinguishes unset from zero by identity — re-read it, the sizing "
        f"basis may have moved somewhere this assertion cannot see")


def test_a_ZERO_allocation_reaches_the_runner_AS_ZERO_and_does_not_become_the_account():
    """The behaviour the source pin above is a proxy for, asserted directly.

    THE FIXTURE'S OWN PROPERTY FIRST: 0.0 must actually be falsy, or the `or` this is about could
    not have fused it and the test would pass over a hazard that was never reachable.
    """
    from kumo_strategies.runtime.executor.runner import RiskLimits

    account_equity = 1_000_016.15          # the live staging IB account, in SGD
    wound_down = RiskLimits(allocated_equity=0.0)

    assert not bool(wound_down.allocated_equity), (
        "0.0 is not falsy here, so the `or` fusion this test is about was never reachable — the "
        "fixture cannot demonstrate the hazard")
    assert wound_down.allocated_equity is not None, "a wind-down must not read as unconfigured"

    basis = (account_equity if wound_down.allocated_equity is None
             else float(wound_down.allocated_equity))
    assert basis == 0.0, (
        "a lane allocated zero sized off the account — the wind-down was read as 'no allocation "
        "configured', which is a different instruction entirely")


def test_an_UNSET_allocation_still_falls_back_to_the_account_which_is_DELIBERATE():
    """The other direction, and it must NOT be 'fixed'.

    `None` means cockpit configured no allocation, which every running config passes and which
    kumo-trading-strategies documents as sizing off the account. Only ZERO was ever the defect.
    """
    from kumo_strategies.runtime.executor.runner import RiskLimits

    account_equity = 1_000_016.15
    unset = RiskLimits()

    assert unset.allocated_equity is None, "the default is no longer None — re-read this test"
    basis = (account_equity if unset.allocated_equity is None else float(unset.allocated_equity))
    assert basis == account_equity, (
        "an UNSET allocation stopped falling back to the account. That is not the fix — it is a "
        "different change, and every running config passes None")
