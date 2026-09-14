"""A refusal must carry the numbers it was derived from (#758).

MEASURED COST, 2026-08-31. MOMENTUM-002 fired at 23:00:01, produced four entries, and every one was
refused with:

    'MOMENTUM-002 has 0 of budget left and this order needs 1,823'

A conclusion and ONE input. Answering "is the strategy overtrading, or is protection feeding it bad
positions?" then took a position query, a venue-quantity reconstruction out of the durable log, and
arithmetic done by hand — to discover that of 22,390 deployed against a 20,000 sleeve, 6,028 was
holdings the broker does not have, and one position (BDX) was carrying five times the lane's own
sizing.

Every one of those numbers existed inside the gate at the moment it refused.

Operator: "I'm wondering why you needed to calculate. there should be logging or?"

SO THE DECISION CARRIES ITS DERIVATION. Not a log line — a field on the decision, because the reason
already reaches the operator through the order's denial and a second channel would drift from it.
"""

from __future__ import annotations

from api.budget import Sleeve
from api.budget_gate import may_submit


def _sleeve(target=20_000.0, actual=20_000.0):
    return Sleeve(strategy_id="MOMENTUM-002", target=target, actual=actual)


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: this must actually be a refusal, or the assertions below are about nothing."""
    d = may_submit(_sleeve(), is_entry=True, notional=1_823.0, currently_deployed=22_390.0)
    assert not d.allowed


def test_a_refusal_carries_THE_NUMBERS_IT_USED():
    """sleeve target, what it believes is deployed, the room that leaves, and what was asked for.
    All four, because three of them cannot be recovered from the outcome."""
    d = may_submit(_sleeve(), is_entry=True, notional=1_823.0, currently_deployed=22_390.0)
    assert d.inputs["strategy_id"] == "MOMENTUM-002"
    assert d.inputs["target"] == 20_000.0
    assert d.inputs["deployed"] == 22_390.0
    assert d.inputs["room"] == 0.0
    assert d.inputs["needs"] == 1_823.0


def test_the_REASON_still_reads_for_a_human():
    """The structured inputs are additive. The prose reaches an operator through the order denial and
    must not regress into a field dump."""
    d = may_submit(_sleeve(), is_entry=True, notional=1_823.0, currently_deployed=22_390.0)
    assert "0 of budget left" in d.reason and "1,823" in d.reason


def test_an_ALLOWED_order_also_carries_them():
    """Otherwise the only way to see how close a lane is to its ceiling is to wait for a refusal —
    and the interesting moment is the one BEFORE it stops trading."""
    d = may_submit(_sleeve(), is_entry=True, notional=100.0, currently_deployed=1_000.0)
    assert d.allowed and d.inputs["room"] == 19_000.0


def test_the_OVER_BUDGET_refusal_carries_them_too():
    """The other refusal path. A lane that is sell-only reports how far over it is, and by what."""
    d = may_submit(_sleeve(actual=25_000.0), is_entry=True, notional=100.0, currently_deployed=0.0)
    assert not d.allowed
    assert d.inputs["must_reduce"] == 5_000.0
    assert d.inputs["actual"] == 25_000.0


def test_an_UNBUDGETED_strategy_says_SO_rather_than_reporting_zeros():
    """A strategy with no sleeve is ALLOWED by design. Reporting `target=0` for it would read as an
    allocation of nothing — the absence-is-not-a-value rule this repo keeps paying for."""
    d = may_submit(None, is_entry=True, notional=100.0, currently_deployed=0.0)
    assert d.allowed
    assert d.inputs["sleeve"] == "unbudgeted"
    assert "target" not in d.inputs


def test_an_EXIT_says_why_it_was_waved_through():
    """Exits are always allowed. Recording that as a bare `allowed` makes an exit indistinguishable
    from an entry that happened to fit."""
    d = may_submit(_sleeve(), is_entry=False, notional=99_999.0, currently_deployed=99_999.0)
    assert d.allowed and d.inputs["exempt"] == "exit"
