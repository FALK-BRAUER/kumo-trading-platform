"""PYRAMID's two OFF-mid-apply outcome strings were SWAPPED (#652 item 3).

`_PyramidWatch.apply` has two chain-cancelled early returns, one per branch:

- the RESYNC branch has already replaced the trailing stop when it checks the toggle — its outcome
  must say the trail was resynced;
- the ADD branch has already SUBMITTED a real market BUY when it checks the toggle — its outcome
  must say a tranche was added.

Before the fix they said each other's sentence, so a durable manager event recorded a live market
BUY as "trail resynced" — the journal row an operator reads to know what the engine did to their
money said the wrong thing. Seen red with the strings swapped back (mutation bite).

Driving the full apply needs a strategy + DB + cache; the defect is purely WHICH string each branch
returns, so this pins the source per-branch (repo idiom: seam pins via inspect.getsource).
"""

from __future__ import annotations

import inspect

from api.engine_node import _PyramidWatch

_RESYNC_MSG = "trail resynced, but PYRAMID was turned off — chain stops here"
_ADD_MSG = "added, but PYRAMID was turned off — chain stops here"


def _branches() -> tuple[str, str]:
    src = inspect.getsource(_PyramidWatch.apply)
    # The add branch starts at the `_add_condition` gate; everything before it that mentions the
    # chain-cancel messages is the resync branch.
    marker = "self._add_condition("
    assert marker in src, "fixture property: the add gate must exist or this test splits nothing"
    cut = src.index(marker)
    return src[:cut], src[cut:]


def test_the_fixture_reaches_both_messages():
    """Both sentences must exist exactly once each, or the per-branch asserts below are vacuous."""
    src = inspect.getsource(_PyramidWatch.apply)
    assert src.count(_RESYNC_MSG) == 1
    assert src.count(_ADD_MSG) == 1


def test_the_resync_branch_reports_a_resync_not_an_add():
    resync_branch, _ = _branches()
    assert _RESYNC_MSG in resync_branch, (
        "the branch that replaced the trailing stop must SAY it resynced the trail"
    )
    assert _ADD_MSG not in resync_branch, (
        "the resync branch claims a tranche was added — no BUY was submitted in this branch"
    )


def test_the_add_branch_reports_the_market_buy_not_a_resync():
    _, add_branch = _branches()
    assert _ADD_MSG in add_branch, (
        "the branch that submitted a real market BUY must SAY it added"
    )
    assert _RESYNC_MSG not in add_branch, (
        "the add branch claims a trail resync — this branch deliberately leaves the trail untouched"
    )
