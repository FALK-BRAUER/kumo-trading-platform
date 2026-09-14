"""The realtime-budget warning promised "until a slot frees up" — a condition with no code behind
it (#652 item 4).

`_realtime_subscribed` is only ever ADDED to: nothing anywhere removes a key, so on a capped feed a
symbol denied a slot is denied for the LIFE OF THE SESSION. The old warning told the operator to
wait for a slot to free — prose describing a mechanism that does not exist is the most dangerous
kind of wrong (it reads as safety and survives review). Latent on sip/IBKR (budget inf), live on
any capped feed.

Two pins, deliberately coupled:
- the set stays add-only (exactly one mutation site) — if someone IMPLEMENTS slot freeing, this
  test fails and tells them the warning may promise it again;
- the warning must not promise freeing while that is true.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from api import engine_node
from api.engine_node import UiFeedStrategy


def test_the_realtime_subscription_set_is_add_only():
    """Fixture property for the honesty pin below: the promise is a lie ONLY while nothing frees a
    slot. Exactly one `.add(` and no removal verbs anywhere in the module."""
    src = Path(engine_node.__file__).read_text()
    assert src.count("_realtime_subscribed.add(") == 1
    for verb in (".remove(", ".discard(", ".clear(", ".pop(", ".difference_update("):
        assert f"_realtime_subscribed{verb}" not in src, (
            f"_realtime_subscribed{verb} exists now — slots CAN free up; restore the promise in the "
            "budget warning and retire this test pair"
        )


def test_the_budget_warning_does_not_promise_a_slot_will_free_up():
    src = inspect.getsource(UiFeedStrategy._after_definition)
    assert "frees up" not in src and "slot frees" not in src, (
        "the budget warning promises a slot can free up, but no code ever removes one — say what is "
        "true: the symbol stays bars/history-only for this session unless the plan is upgraded"
    )
