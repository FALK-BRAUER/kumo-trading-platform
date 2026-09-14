"""Tests for flatten decisions (#170 first slice, queue-until-open revision). Pure — this is where "an exit
must never reverse the position" and "don't fight a bad off-hours spread, queue instead" are pinned."""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.flatten import closing_side, decide


def _decide(**over):
    kwargs = dict(
        position_side="LONG",
        live_qty=Decimal(168),
        expected_side="LONG",
        expected_qty=Decimal(168),
        resting_reducing_qty=Decimal(0),
        market_open=True,
    )
    kwargs.update(over)
    return decide(**kwargs)


# --- direction: symmetric for long and short ------------------------------------------------------------

def test_a_long_is_closed_by_selling():
    assert closing_side("LONG") == "SELL"


def test_a_short_is_closed_by_buying():
    assert closing_side("SHORT") == "BUY"


def test_a_flat_side_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        closing_side("FLAT")


def test_flattening_a_short_buys_its_quantity():
    d = _decide(position_side="SHORT", expected_side="SHORT", live_qty=Decimal(23), expected_qty=Decimal(23))

    assert d.action == "EXECUTE"
    assert (d.order.side, d.order.quantity) == ("BUY", Decimal(23))


# --- staleness: the whole reason this is an engine decision ---------------------------------------------

def test_quantity_comes_from_live_state_not_the_request():
    d = _decide()

    assert d.order.quantity == Decimal(168)


def test_a_position_that_shrank_since_the_screen_rendered_is_refused():
    """Sending the stale 168 against a live 68 would sell 100 more than exists and open a short."""
    d = _decide(live_qty=Decimal(68))

    assert d.action == "REJECT"
    assert "not the 168 you confirmed" in d.reason


def test_a_position_that_flipped_side_is_refused():
    d = _decide(position_side="SHORT")

    assert d.action == "REJECT"
    assert "flipped" in d.reason


def test_an_already_closed_position_is_refused():
    d = _decide(live_qty=Decimal(0))

    assert d.action == "REJECT"
    assert "already closed" in d.reason


def test_resting_exit_orders_are_CANCELLED_not_a_reason_to_refuse():
    """This test previously asserted the opposite, and in doing so pinned the behaviour that left the
    operator unable to exit their own position — Operator, 2026-08-13, on WDAY: "I cannot even flatten it."

    The danger the refusal named is real: an exit left resting after the close fires against nothing and
    opens the opposite side. But refusing does not avoid it — it makes the human perform the same
    sequence by hand. The engine cancels them, waits for the venue to confirm, and then closes.
    """
    d = _decide(resting_reducing_qty=Decimal(168))

    assert d.action == "EXECUTE"
    assert d.cancel_resting_first is True
    assert d.order.quantity == Decimal(168)


def test_a_queued_flatten_also_carries_the_cancel_instruction():
    """Outside regular hours the close is queued — the resting exits still have to go first when it
    replays, or the queued close inherits exactly the same hazard."""
    d = _decide(resting_reducing_qty=Decimal(168), market_open=False)

    assert d.action == "QUEUE"
    assert d.cancel_resting_first is True


def test_a_clean_position_does_not_ask_for_cancels():
    assert _decide().cancel_resting_first is False


def test_expected_qty_is_optional_for_callers_that_cannot_supply_it():
    d = _decide(expected_qty=None)

    assert d.action == "EXECUTE"
    assert d.order.quantity == Decimal(168)


# --- session: two outcomes, not three ---------------------------------------------------------------------

def test_in_regular_hours_it_executes_now():
    d = _decide(market_open=True)

    assert d.action == "EXECUTE"


def test_outside_regular_hours_it_queues_rather_than_pricing_into_a_bad_spread():
    """No limit-pricing attempt outside RTH — queuing sidesteps the wide-spread problem entirely instead of
    fighting it."""
    d = _decide(market_open=False)

    assert d.action == "QUEUE"
    assert d.order.quantity == Decimal(168)  # what to submit once the open replay runs


def test_queue_decision_still_validates_first():
    """A stale/flipped/blocked request must not be silently queued — it should reject just as it would if
    the market were open."""
    d = _decide(market_open=False, live_qty=Decimal(68))

    assert d.action == "REJECT"


def test_a_short_queues_with_the_buy_to_cover_side():
    d = _decide(
        position_side="SHORT", expected_side="SHORT", live_qty=Decimal(23), expected_qty=Decimal(23),
        market_open=False,
    )

    assert d.action == "QUEUE"
    assert d.order.side == "BUY"


# --- the engine side: cancel, CONFIRM, then close (#170/#245) -----------------------------------------


def test_the_engine_cancels_resting_exits_and_waits_before_closing():
    """Operator, 2026-08-13, on WDAY: "I cannot even flatten it." The decision now says cancel-first; this
    pins that the engine does it, and — the part that matters — WAITS for the venue to confirm.

    A cancel is asynchronous. Alpaca frees the reserved shares only when it confirms, so a close sent
    straight after the request is rejected on `available: 0` while the cancel lands anyway. That is how
    five protective stops were lost on 2026-08-12.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._handle_flatten_command)
    cancel_at = src.index("_cancel_reducing_leg")
    wait_at = src.index("_await_reducing_orders_clear")
    available_at = src.index("_await_shares_available")
    assert cancel_at < wait_at < available_at, "cancel, then confirm, then check the shares are free"
    assert 'decision.action == "QUEUE"' in src[available_at:], "and the close after all of it"


def test_the_flatten_size_check_asks_the_BROKER_not_only_the_cache():
    """#269 — the reason the operator could not exit NBIS on 2026-08-17, with every guard above already in place.

    `cache.orders_open()` omits any order stuck in a terminal state, and eight protective stops were stuck
    exactly that way: submitted, accepted by Alpaca, recorded locally as REJECTED because the HTTP call
    failed afterwards. Nautilus treats REJECTED as terminal, so reconciliation's `OrderAccepted` is
    discarded forever (`InvalidStateTrigger: REJECTED -> ACCEPTED`, 81,128 times).

    `decide()` was therefore handed `resting_reducing_qty=0`, never set `cancel_resting_first`, and the
    cancel-and-confirm sequence pinned above NEVER RAN. The close went straight out and came back
    `insufficient qty available (requested: 29, available: 0)`.

    So the size check must not be the cache-only `_reducing_order_qty`. This pins the SEAM — which
    function the flatten path calls — because every part of the machinery it feeds was already correct.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._handle_flatten_command)
    assert "_reducing_qty_for_exit" in src, "the flatten path is back on cache-only truth"
    assert "self._reducing_order_qty(" not in src, (
        "the cache-only helper is being called directly again — it cannot see a terminal-state order that "
        "is still reserving shares at the broker"
    )


def test_a_cancel_timeout_QUEUES_the_close_rather_than_abandoning_it():
    """The Critical codex found. By the time the wait times out the cancels have ALREADY been requested
    and cannot be un-requested — if they land afterwards the position is left unprotected. Returning a
    plain error would leave it that way with no close pending either, which is the worst of both: the
    request evaporates while its side effects survive.

    So the close is queued and replays until it completes.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._handle_flatten_command)
    wait_at = src.index("_await_reducing_orders_clear")
    tail = src[wait_at:]
    timeout_branch = tail[: tail.index('if decision.action == "QUEUE"')]
    assert "deferred_flatten" in timeout_branch, (
        "a timeout must leave the exit PENDING, not abandon it after requesting the cancels"
    )
    # Presence is not enough — an early `return "error"` in front of the attach would leave the string
    # here while abandoning the close anyway. That exact injection passed the first version of this test.
    before_attach = timeout_branch[: timeout_branch.index("deferred_flatten")]
    assert 'return "error"' not in before_attach, (
        "nothing may return before the close is queued; the cancels are already in flight by then"
    )


def test_the_queued_replay_also_cancels_resting_exits_first():
    """The High. `_DeferredFlatten.apply()` calls the same `decide()`, so it gets the same
    `cancel_resting_first` — and must act on it. A close sent at the open while a bracket still rests
    hits exactly the hazard the immediate path guards against."""
    import inspect

    from api.engine_node import _DeferredFlatten

    src = inspect.getsource(_DeferredFlatten.apply)
    assert "cancel_resting_first" in src, "the replay must honour the cancel instruction"
    cancel_at = src.index("_cancel_reducing_leg")
    wait_at = src.index("_await_reducing_orders_clear")
    available_at = src.index("_await_shares_available")
    # `(order` not `(order)` — the self-submit now pins `position_id=pos.id` (#646); the property this
    # test defends is the ORDER of operations, not the argument list.
    submit_at = src.index("strategy._submit(order")
    assert cancel_at < wait_at < available_at < submit_at
    # The replay reaches the venue the same way the immediate path does. It used to cancel only what the
    # cache could see, which on 2026-08-17 was none of the eight resting stops.
    assert "strategy._reducing_qty_for_exit" in src, "the replay is back on cache-only truth"


