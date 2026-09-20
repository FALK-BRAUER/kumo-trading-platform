"""QC345 has NEVER placed an order, and this is why (measured 2026-08-23).

Its whole life in `exec_action_log`:

    2026-08-20  error     {}                       -- an empty detail; the reason was never written
    2026-08-21  decision  enter INTC MRVL DELL LRCX AMAT, and ZERO order rows

Lifecycle TRADING since 2026-08-19 by the operator. Not SHADOW, not halted. It decided to enter five names and
placed nothing, and the five refusals went into `SessionResult.blocked` and were journalled NOWHERE —
so the one sentence saying which of the five refusal branches fired does not exist anywhere.

TWO DEFECTS, and the first is the documented one arriving in the one place that never got the fix.

1. SIZING OFF THE WHOLE ACCOUNT. `_equity_per_position` reads `self._broker.equity()`, which is the
   ACCOUNT's ~103,466, and QC345's allocation is 20,000. Every other runner in kumo-trading-strategies reads
   `limits.allocated_equity or broker.equity()` — pgrunner:1125, qc27_runner:239, template_runner:81 —
   and `RiskLimits.allocated_equity` exists precisely for this, with a docstring that describes what
   happened here before it happened:

       "a strategy with a 20k target on a 100k account sized every entry as though it owned all 100k
        ... The budget gate then refused the order at submission. The result is not 'buys smaller' or
        'buys fewer', it is 'buys the same and gets rejected'."

   103,466 x 0.80 / 5 = 16,554 a name against a 20,000 sleeve. AT MOST ONE of five can fit. The
   researched five-name equally-weighted book cannot be built at any price.

2. THE STRATEGY IS BUILT WITH A BARE `RiskLimits()`. qc27 passes `allocated_equity=ALLOCATED_EQUITY`
   and momentum re-reads it per session in `_limits_for_session`. QC345 passes neither, so even after
   fixing (1) there is nothing for it to read.

Both halves are needed: honouring a field nobody sets changes nothing, and setting a field nobody reads
changes nothing. That is the same shape as every other defect found this session — a mechanism and a
caller that never meet.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

#: The live figures, so the assertions carry the reasoning rather than a bare number.
_ACCOUNT_EQUITY = 103466.29
_QC345_ALLOCATION = 20000.0
_PORTFOLIO_SIZE = 5
_MAX_DEPLOYED_FRAC = 0.80


def _gateway(allocated):
    from kumo_strategies.runtime.executor.runner import RiskLimits

    from strategies.qc345 import QC345SessionGateway

    g = QC345SessionGateway.__new__(QC345SessionGateway)
    g._broker = SimpleNamespace(equity=lambda: _ACCOUNT_EQUITY)
    g._limits = RiskLimits(allocated_equity=allocated, max_deployed_frac=_MAX_DEPLOYED_FRAC)
    g._cfg = SimpleNamespace(portfolio_size=_PORTFOLIO_SIZE)
    return g


def test_the_fixture_reproduces_the_live_mismatch() -> None:
    """Assert the fixture's own property first: the account must dwarf the allocation, or sizing off
    either gives the same answer and nothing below can discriminate."""
    assert _ACCOUNT_EQUITY > _QC345_ALLOCATION * 4, (
        "account and allocation too close — the two sizing bases would agree and the test is inert"
    )
    off_account = _ACCOUNT_EQUITY * _MAX_DEPLOYED_FRAC / _PORTFOLIO_SIZE
    assert off_account > _QC345_ALLOCATION / _PORTFOLIO_SIZE * 2
    assert off_account < _QC345_ALLOCATION * 1.0, (
        "one name must still fit under the sleeve — otherwise the failure is 'nothing ever fits', "
        "which is a different bug from 'only one of five fits'"
    )


def test_sizing_honours_the_allocation_when_it_has_one() -> None:
    """20,000 x 0.80 / 5 = 3,200 a name — five of which fit the sleeve, which is the researched book."""
    assert _gateway(_QC345_ALLOCATION)._equity_per_position() == 3200.0


def test_sizing_falls_back_to_the_account_when_it_has_none() -> None:
    """`allocated_equity or broker.equity()` — the SAME expression pgrunner, qc27_runner and
    template_runner use. An unallocated strategy must keep behaving exactly as it does today."""
    assert _gateway(None)._equity_per_position() == _ACCOUNT_EQUITY * _MAX_DEPLOYED_FRAC / _PORTFOLIO_SIZE


def test_a_zero_allocation_does_not_silently_become_the_whole_account() -> None:
    """0 is BOTH the schema default and an operator saying "wind down". `x or y` treats 0.0 as falsy,
    so a wound-down strategy would size off the entire account — which is the loudest possible
    direction to be wrong in."""
    assert _gateway(0.0)._equity_per_position() == 0.0


def test_the_strategy_is_BUILT_with_its_allocation() -> None:
    """Honouring a field nobody sets changes nothing. qc27 passes `allocated_equity=`; QC345 passed a
    bare `RiskLimits()`, so even a correct `_equity_per_position` would read None forever."""
    import ast

    from strategies import qc345

    src = inspect.getsource(qc345)
    # The CALL, not a substring: prose about `RiskLimits()` in a comment is not a construction, and a
    # test that a comment can satisfy is a test of nothing.
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "RiskLimits"]
    assert calls, "QC345 no longer constructs RiskLimits at all"
    for c in calls:
        assert any(k.arg == "allocated_equity" for k in c.keywords), (
            "QC345 is constructed with a bare RiskLimits — nothing supplies its allocation, so "
            "`_equity_per_position` reads None forever and the fix above changes nothing"
        )


def test_every_refusal_reaches_the_JOURNAL_not_just_the_return_value() -> None:
    """WHY THIS DEFECT SURVIVED TWO SESSIONS UNDIAGNOSED. `_submit_orders` builds a `refusals` list
    with a specific sentence per name — "no price", "sizing yielded 0 shares at X", the budget gate's
    own reason, the venue's rejection text — and returns it. On 2026-08-21 five entries were refused
    and `exec_action_log` recorded a decision row and nothing else.

    An operator at 09:31 cannot act on "it decided and bought nothing". The reason existed and was
    thrown away.
    """
    from strategies import qc345

    src = inspect.getsource(qc345.QC345SessionGateway)
    assert "REFUSED" in src, "no journal row carries the refusals; they still die in the return value"


def test_the_allocation_is_RE_READ_per_session_not_captured_at_build() -> None:
    """An operator edits the target from the UI while the node keeps running. Captured at build, that
    edit takes effect on the next restart — and the restart is the thing we are trying not to need."""
    from strategies import qc345

    run_src = inspect.getsource(qc345.QC345SessionGateway.run)
    assert "_limits_for_session()" in run_src, (
        "run() never re-reads the allocation — a UI edit needs a restart to take effect"
    )


def test_a_missing_allocated_equity_FIELD_does_not_kill_the_session() -> None:
    """This repo pins kumo-trading-strategies by revision. An unguarded `replace()` on a RiskLimits without
    the field raises TypeError AFTER the decision is journalled and BEFORE any order — the silent
    death that has already cost two sessions."""
    from dataclasses import dataclass

    from strategies.qc345 import QC345SessionGateway

    @dataclass
    class _Old:                      # a RiskLimits from before the field existed
        max_deployed_frac: float = 0.80

    g = QC345SessionGateway.__new__(QC345SessionGateway)
    g._limits = _Old()
    assert g._limits_for_session() is g._limits
