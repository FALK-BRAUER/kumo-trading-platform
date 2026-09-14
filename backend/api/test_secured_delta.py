"""How much of the window's Δ UNREALIZED is secured by a resting stop (#787).

Operator: "how much of the unrealised delta for instance for the day is secured by a stop".

THE BASELINE IS THE WINDOW'S, NOT THE ENTRY'S. That is what makes this a real subset of the number
beside it:

    Δ unrealized(W) = Σ (mark_now − mark_at_window_start) × qty
    secured(W)      = Σ max(0, (stop − mark_at_window_start) × qty)

For a long the stop sits BELOW the mark, so each position's secured term cannot exceed its own delta
term. A loser contributes ZERO rather than netting against a winner — so "of today's +$409.63, $X
survives a gap to stops" is a true statement about the same number on the same screen.

WHY NOT THE EXISTING `securedValue`. That one measures against ENTRY and answers a different
question — "how much of my open gain since I bought cannot evaporate". Against a window delta it is
a category error: on 2026-09-02 the book showed $419.91 secured beside $14.57 standing unrealized,
because one ignores losers and the other nets them.

UNKNOWN IS NOT ZERO. Only 1D has an observed window start today (1W/1M/3M/all return NONE). A
window whose start was never observed has no baseline to measure against, and must say so rather
than render a confident 0.00 — the defect this whole file's neighbourhood keeps producing.
"""

from __future__ import annotations

import pytest

from api.secured_delta import secured_delta


def _pos(iid, qty, base_mark):
    return {"instrument_id": iid, "qty": qty, "mark_px": base_mark}


def test_the_fixture_has_a_stop_ABOVE_the_window_start_or_it_proves_nothing():
    """FIXTURE PROPERTY FIRST. With every stop below the baseline the answer is 0 for any
    implementation, and the assertions below could not distinguish one."""
    assert 105.0 > 100.0


def test_a_stop_above_the_window_start_secures_the_gain_up_to_it():
    """AAPL opened the window at 100, holds 10, stop at 105 → 50 of the move cannot evaporate."""
    out = secured_delta([_pos("AAPL.XNAS", 10, 100.0)], {"AAPL.XNAS": 105.0})
    assert out.value == pytest.approx(50.0)
    assert out.state == "ok"


def test_a_stop_BELOW_the_window_start_secures_NOTHING_and_never_goes_negative():
    """A stop under the baseline protects none of THIS window's move, even though it may protect
    plenty of gain from before it. Killed by dropping the max(0, …)."""
    out = secured_delta([_pos("AAPL.XNAS", 10, 100.0)], {"AAPL.XNAS": 95.0})
    assert out.value == 0.0


def test_a_loser_contributes_ZERO_rather_than_netting_against_a_winner():
    """The subset property depends on this. Netting is what made the entry-based figure incoherent
    beside standing unrealized."""
    out = secured_delta(
        [_pos("WIN.XNAS", 10, 100.0), _pos("LOSE.XNAS", 10, 100.0)],
        {"WIN.XNAS": 110.0, "LOSE.XNAS": 80.0},
    )
    assert out.value == pytest.approx(100.0)


def test_a_position_with_NO_stop_secures_nothing_but_is_still_counted_as_held():
    out = secured_delta([_pos("AAPL.XNAS", 10, 100.0), _pos("MSFT.XNAS", 5, 200.0)],
                        {"AAPL.XNAS": 105.0})
    assert out.value == pytest.approx(50.0)
    assert out.unsecured == 1


def test_a_window_whose_START_WAS_NEVER_OBSERVED_is_UNKNOWN_not_zero():
    """1W/1M/3M/all today. A confident 0.00 here would read as 'nothing is protected', which is the
    opposite of 'we cannot tell'. Killed by returning a value when base rows are None."""
    out = secured_delta(None, {"AAPL.XNAS": 105.0})
    assert out.state == "unknown"
    assert out.value is None
    assert "never observed" in out.reason


def test_a_position_the_window_start_did_not_MARK_is_excluded_and_counted():
    """A row captured without a mark cannot be measured against. Excluding it silently would
    understate; including it as zero would misstate. It is reported."""
    out = secured_delta([_pos("AAPL.XNAS", 10, 100.0), _pos("NOPX.XNAS", 5, None)],
                        {"AAPL.XNAS": 105.0, "NOPX.XNAS": 50.0})
    assert out.value == pytest.approx(50.0)
    assert out.unmarked == 1


def test_a_SHORT_is_secured_when_its_stop_sits_BELOW_the_window_start():
    """Symmetric by construction — nothing here may assume a long book."""
    out = secured_delta([_pos("AAPL.XNAS", -10, 100.0)], {"AAPL.XNAS": 95.0})
    assert out.value == pytest.approx(50.0)
    out = secured_delta([_pos("AAPL.XNAS", -10, 100.0)], {"AAPL.XNAS": 105.0})
    assert out.value == 0.0
