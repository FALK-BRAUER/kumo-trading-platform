"""The doubles in test_market_aware_fold.py claim the shape of kumo-trading-strategies' `Verdict` and
`Assessment` (f5895d1: `.state`, `.acts`, `.reasons`, `evidence_sufficient`). This is the check
that the claim is true against the INSTALLED package — a double that drifts from production is the
bug (CLAUDE.md, six doubles in one session).

On a pin that predates f5895d1 the import fails; that is reported as a SKIP naming the rev needed,
not as green, and the fold tests still run on the doubles. `read_hook` never imports these classes
— it duck-types `.acts`/`.state`/`.reasons` — so the poller itself does not depend on the pin.
"""

from __future__ import annotations

import pytest

mv = pytest.importorskip("kumo_strategies.strategies.market_view")
if not all(hasattr(mv, n) for n in ("YES", "NO", "UNKNOWN", "IN_ENVELOPE", "OUT_OF_ENVELOPE")):
    pytest.skip("installed kumo-trading-strategies predates f5895d1 (three-state Verdict/Assessment, PR #156)",
                allow_module_level=True)

from api.market_aware import FAULT, NO, UNKNOWN, YES as R_YES, read_hook  # noqa: E402


class _Lane:
    def __init__(self, **hooks):
        for k, v in hooks.items():
            setattr(self, k, (lambda _v=v: _v))


def test_the_real_verdict_reads_through_read_hook_exactly_as_the_double_does():
    assert read_hook(_Lane(emergency_exit=mv.Verdict(state=mv.YES, reasons=("below",))),
                     "emergency_exit", ts_ns=1).state == R_YES
    assert read_hook(_Lane(emergency_exit=mv.Verdict(state=mv.NO, reasons=("above",))),
                     "emergency_exit", ts_ns=1).state == NO
    r = read_hook(_Lane(emergency_exit=mv.Verdict(state=mv.UNKNOWN, reasons=("no panel",))),
                  "emergency_exit", ts_ns=1)
    assert r.state == UNKNOWN and r.reasons == ("no panel",)


def test_the_real_assessment_acts_only_with_sufficient_evidence():
    thin = mv.Assessment(state=mv.OUT_OF_ENVELOPE, reasons=("3 windows",), evidence_sufficient=False)
    assert read_hook(_Lane(self_assessment=thin), "self_assessment", ts_ns=1).state == NO
    full = mv.Assessment(state=mv.OUT_OF_ENVELOPE, reasons=("9 windows",))
    assert read_hook(_Lane(self_assessment=full), "self_assessment", ts_ns=1).state == R_YES
    ok = mv.Assessment(state=mv.IN_ENVELOPE, reasons=("fine",))
    assert read_hook(_Lane(self_assessment=ok), "self_assessment", ts_ns=1).state == NO


def test_the_old_two_state_verdict_shape_is_a_FAULT_not_a_yes():
    """`Verdict(answer=False)` from 513d813 is a truthy dataclass with no `.acts`."""
    class _Old:
        answer = False
        reasons = ()
    assert read_hook(_Lane(emergency_exit=_Old()), "emergency_exit", ts_ns=1).state == FAULT
