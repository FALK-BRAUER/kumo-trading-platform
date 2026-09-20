"""A trailing stop's trigger is the VENUE's, and ours goes stale the moment it ratchets (#289).

MEASURED on a live book, 2026-08-23, one resting stop:

    engine  working_orders[].trigger_price   176.356908
    venue   stop_price                       184.487823     hwm 192.295, trail_percent 4.06

Exact to the cent, both directions:

    192.295 * (1 - 0.0406) = 184.487823        <- the venue's number IS the ratcheted trail
    176.356908 / (1 - 0.0406) = 183.820        <- ours corresponds to an EARLIER high

So the engine is holding the trigger from when the stop was placed while Alpaca has ratcheted it up
$8.13. On 55 shares that is **$447.20 of protection the board does not show**.

WHY IT STICKS. `_mark_broker_stop_prices` only fills a trigger that is None:

    if getattr(order, "trigger_price", None) is not None:
        continue

and its docstring gives the reason — "A bracket leg submitted with an explicit trigger already carries
the right value, and the venue's echo of our own number should not overwrite it." That is CORRECT for a
fixed stop: we chose the number, the venue is echoing it back, and overwriting risks float drift for no
gain.

It is exactly WRONG for a trailing stop. We never submit a trigger at all — we submit an OFFSET, and
Alpaca computes the trigger and moves it up against its own high-water mark. Our value is a snapshot
that is stale from the first tick that makes a new high, and it is stale in the direction that
UNDERSTATES protection, which is the direction that makes a book look more exposed than it is.

This is #289's own finding arriving one layer in: "A trailing stop's ACTUAL stop price exists only at
the venue: we submit an offset, Alpaca computes the trigger and ratchets it as its high-water mark
rises, and none of that flows back into the order we hold." The fetch was fixed; the guard in front of
the assignment was not.

MONDAY: six trailing stops arm at 09:30 and every one of them ratchets through the session.
"""

from __future__ import annotations

from types import SimpleNamespace


def _dto(orders, iid="BDX.XNYS"):
    """`_mark_broker_stop_prices` also stamps `broker_protected` from `instrument_id`, so a double
    without one cannot represent a production DTO. Give it the field rather than loosen the method."""
    return SimpleNamespace(instrument_id=iid, broker_protected=None, working_orders=orders)


def _order(coid, trigger, order_type):
    return SimpleNamespace(client_order_id=coid, trigger_price=trigger, order_type=order_type)


def _host(prices):
    from api import engine_node

    h = engine_node.UiFeedStrategy.__new__(engine_node.UiFeedStrategy)
    h._broker_stop_prices = prices
    h._broker_protected = set()
    h._mark_broker_stop_prices = engine_node.UiFeedStrategy._mark_broker_stop_prices.__get__(h)
    return h


#: The live values. Named so an assertion that changes them has to change the reasoning too.
_COID = "PROT-SELL-BDX-XNYS-00000002"
_ENGINE_STALE = 176.356908
_VENUE_RATCHETED = 184.487823


def test_the_fixture_reproduces_a_RATCHET_and_not_a_rounding_difference() -> None:
    """Assert the fixture's own property first. If the two numbers were within float noise, every
    assertion below would pass with the fix deleted."""
    assert _VENUE_RATCHETED - _ENGINE_STALE > 8.0
    # And it must be the ratchet direction — the venue ABOVE ours — or this is a different bug.
    assert _VENUE_RATCHETED > _ENGINE_STALE


def test_a_TRAILING_stop_takes_the_venues_trigger_even_when_ours_is_set() -> None:
    """The live case. 55 shares x $8.13 = $447.20 of protection the board was not showing."""
    dto = _dto([_order(_COID, _ENGINE_STALE, "TRAILING_STOP_MARKET")])
    _host({_COID: _VENUE_RATCHETED})._mark_broker_stop_prices([dto])
    assert dto.working_orders[0].trigger_price == _VENUE_RATCHETED


def test_a_FIXED_stop_KEEPS_the_trigger_we_submitted() -> None:
    """The direction that must not regress, and the reason the guard exists. We chose that number; the
    venue is echoing it back, and overwriting it buys nothing and risks float drift."""
    dto = _dto([_order("PROT-SELL-X", 100.0, "STOP_MARKET")])
    _host({"PROT-SELL-X": 100.0000001})._mark_broker_stop_prices([dto])
    assert dto.working_orders[0].trigger_price == 100.0


def test_an_UNSET_trigger_is_still_filled_for_either_type() -> None:
    for kind in ("TRAILING_STOP_MARKET", "STOP_MARKET"):
        dto = _dto([_order("C", None, kind)])
        _host({"C": 42.0})._mark_broker_stop_prices([dto])
        assert dto.working_orders[0].trigger_price == 42.0, kind


def test_a_trailing_stop_the_broker_cannot_price_keeps_what_it_has() -> None:
    """No venue answer must never BLANK a trigger we already show — that reads as no protection at
    all, which is worse than a stale number."""
    # A NON-EMPTY MAP THAT LACKS THIS COID. An empty one returns early and never reaches the
    # assignment, so the fixture could not reach the bug: blanking the trigger on a missing lookup
    # survived this test until the map had something else in it.
    dto = _dto([_order(_COID, _ENGINE_STALE, "TRAILING_STOP_MARKET")])
    other = {"PROT-SELL-SOMETHING-ELSE": 99.0}
    assert _COID not in other, "the fixture must miss THIS order, not every order"
    _host(other)._mark_broker_stop_prices([dto])
    assert dto.working_orders[0].trigger_price == _ENGINE_STALE


# ==================================================================================================
# WHY THE PREDICATE COMPARES NAMES AND NOT THE ENUM (59sh1zl1 review of #483).
#
# The review asked for `order_type in (OrderType.TRAILING_STOP_MARKET, OrderType.TRAILING_STOP_LIMIT)`
# instead of a substring, on the sound general ground that a string predicate standing in for an enum
# is the shape that has bitten this repo through `type` vs `order_type` and through status strings.
#
# MEASURED IN THE RUNNING IMAGE BEFORE TAKING IT:
#
#     OrderType.TRAILING_STOP_MARKET == "TRAILING_STOP_MARKET"   ->  False
#     models.py                                                  ->  order_type: str
#     the live DTO                                               ->  'TRAILING_STOP_MARKET'
#
# Nautilus's `OrderType` is a Cython enum, not a str-enum, and the DTO field is declared `str`. So the
# suggested comparison is False for every order and would have SILENTLY DISABLED the fix — reverting
# every trailing trigger to the stale value with no test failing and no log line. The hardening would
# have introduced precisely the failure it was proposed to prevent.
#
# What IS available is exactness without the drift: derive the name set FROM the enum. Membership
# rather than substring, and it tracks a Nautilus upgrade that adds a member.
# ==================================================================================================

def test_the_trailing_set_is_DERIVED_from_the_nautilus_enum() -> None:
    from nautilus_trader.model.enums import OrderType

    from api.engine_node import _TRAILING_ORDER_TYPES

    assert _TRAILING_ORDER_TYPES == frozenset(t.name for t in OrderType if "TRAILING" in t.name)
    assert _TRAILING_ORDER_TYPES == {"TRAILING_STOP_MARKET", "TRAILING_STOP_LIMIT"}


def test_every_OTHER_order_type_keeps_the_trigger_we_chose() -> None:
    """Enumerated, not reasoned about. The four unmatched types that CAN carry a trigger carry one WE
    chose and the venue echoes back; only the two trailing ones carry a trigger the venue COMPUTES."""
    from nautilus_trader.model.enums import OrderType

    from api.engine_node import _TRAILING_ORDER_TYPES

    for t in OrderType:
        if t.name in _TRAILING_ORDER_TYPES:
            continue
        dto = _dto([_order("C", 100.0, t.name)])
        _host({"C": 999.0})._mark_broker_stop_prices([dto])
        assert dto.working_orders[0].trigger_price == 100.0, f"{t.name} took the venue's number"


def test_comparing_the_ENUM_to_the_STRING_would_silently_disable_this() -> None:
    """Pinned so the next reader does not 'harden' the predicate into a no-op.

    This is not a hypothetical: it was proposed in review, it is the more idiomatic-looking code, and
    it is False for every order the DTO carries.
    """
    from nautilus_trader.model.enums import OrderType

    assert OrderType.TRAILING_STOP_MARKET != "TRAILING_STOP_MARKET", (
        "OrderType became comparable to its own name — the name-based predicate can now be replaced "
        "by the enum, and this test should be deleted along with the comment above it"
    )
