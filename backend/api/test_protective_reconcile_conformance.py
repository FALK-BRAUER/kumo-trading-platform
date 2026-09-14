"""The reconciler classified against REAL Nautilus enums, not against a double's idea of them.

WHY THIS FILE EXISTS SEPARATELY. `test_protective_reconcile.py` passed 14/14 against a module that
would have matched NOTHING in production: it compared `str(order.order_type)` to
`"TRAILING_STOP_MARKET"`, and `str(OrderType.TRAILING_STOP_MARKET)` is `'8'` — these are Cython enums
whose `__str__` is the ordinal. Every real protective order would have been classified as
non-protective, the reconciler would have found nothing, cancelled nothing, and reported a clean book
while twelve stops rested on one position. It was caught by hand in a running container, ten minutes
before deploying it, and not by any test.

`test_double_conformance.py` would not have caught it either: it checks that a double's attributes
EXIST on the real class. `order_type` existed. What differed was the VALUE SEMANTICS.

SO NOTHING HERE USES A DOUBLE FOR THE ENUMS. `nautilus_trader` is importable in this venv, so the
classification is driven by the actual members. A double cannot drift from a thing it is not
standing in for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderStatus, OrderType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, StrategyId

from api.protective_reconcile import (
    LIVE_STATUS,
    PROTECTIVE_TYPES,
    _is_live,
    _is_protective,
    reconcile_protection,
)

#: Every OrderType Nautilus defines, and whether a resting order of that type PROTECTS a long
#: position. Written out in full deliberately: a new order type added by an upgrade fails this test
#: rather than silently defaulting to "not protective", which is the direction that loses protection.
PROTECTS = {
    "MARKET": False,             # never rests
    "LIMIT": False,              # a target, not a floor — it secures nothing
    "STOP_MARKET": True,
    "STOP_LIMIT": True,
    "MARKET_TO_LIMIT": False,
    "MARKET_IF_TOUCHED": False,  # an entry trigger, not a protective floor
    "LIMIT_IF_TOUCHED": False,
    "TRAILING_STOP_MARKET": True,   # what `plan_protection` actually submits
    "TRAILING_STOP_LIMIT": True,    # equally protective, and it was MISSING from the set
}


def test_the_table_covers_every_order_type_nautilus_defines():
    """Vacuity guard, and an upgrade tripwire. If Nautilus adds a type, this fails here — where the
    answer is a decision — instead of silently classifying it as unprotective in production."""
    actual = {m.name for m in OrderType}
    assert actual == set(PROTECTS), (
        f"OrderType has changed: only-in-nautilus={sorted(actual - set(PROTECTS))}, "
        f"only-in-table={sorted(set(PROTECTS) - actual)}"
    )


@pytest.mark.parametrize("member", list(OrderType), ids=lambda m: m.name)
def test_every_REAL_order_type_classifies_as_intended(member):
    """Driven by the actual enum member — the exact object production hands the function."""
    order = SimpleNamespace(order_type=member, status=OrderStatus.ACCEPTED)
    assert _is_protective(order) is PROTECTS[member.name], (
        f"{member.name} classified as {'protective' if _is_protective(order) else 'not protective'}, "
        f"which is not what the table says. str() of it is {str(member)!r} — comparing that against a "
        f"NAME is the defect this file exists for."
    )


def test_PROTECTIVE_TYPES_contains_no_name_that_can_never_occur():
    """A set entry no enum member can produce is dead weight that reads as coverage.

    `TRAILING_STOP` and `STOP` were both in this set. Neither is a Nautilus OrderType. They looked
    like two more protective types being handled and matched nothing, ever.
    """
    real = {m.name for m in OrderType}
    phantom = sorted(PROTECTIVE_TYPES - real)
    assert not phantom, (
        f"{phantom} are in PROTECTIVE_TYPES and are not OrderType members — they can never match, "
        f"and they make the set look more complete than it is"
    )


def test_every_protective_type_in_the_table_IS_in_the_set():
    """The other direction, and how `TRAILING_STOP_LIMIT` was found missing. A real protective type
    absent from the set is a protective order the reconciler cannot see: never deduped, never
    cancelled when orphaned."""
    expected = {name for name, protects in PROTECTS.items() if protects}
    assert expected <= PROTECTIVE_TYPES, f"missing from PROTECTIVE_TYPES: {sorted(expected - PROTECTIVE_TYPES)}"


#: Whether an order in this state is still RESTING at the venue and can therefore be cancelled.
IS_LIVE = {
    "INITIALIZED": False,     # not sent
    "DENIED": False,
    "EMULATED": False,        # held by Nautilus locally, not resting at the venue
    "RELEASED": False,
    "SUBMITTED": True,        # sent, not yet acknowledged — cancellable, and it can still fill
    "ACCEPTED": True,
    "REJECTED": False,
    "CANCELED": False,
    "EXPIRED": False,
    "TRIGGERED": True,        # fired and working
    "PENDING_UPDATE": True,
    "PENDING_CANCEL": False,  # already on its way out; cancelling again is noise
    "PARTIALLY_FILLED": True,
    "FILLED": False,
}


def test_the_status_table_covers_every_status_nautilus_defines():
    actual = {m.name for m in OrderStatus}
    assert actual == set(IS_LIVE), (
        f"OrderStatus has changed: only-in-nautilus={sorted(actual - set(IS_LIVE))}, "
        f"only-in-table={sorted(set(IS_LIVE) - actual)}"
    )


@pytest.mark.parametrize("member", list(OrderStatus), ids=lambda m: m.name)
def test_every_REAL_status_classifies_as_intended(member):
    assert _is_live(SimpleNamespace(status=member)) is IS_LIVE[member.name], (
        f"{member.name} (str={str(member)!r}) classified wrongly — a terminal order treated as live "
        f"gets cancelled repeatedly, and a live one treated as terminal is never deduped"
    )


def test_LIVE_STATUS_contains_no_name_that_can_never_occur():
    real = {m.name for m in OrderStatus}
    phantom = sorted(LIVE_STATUS - real)
    assert not phantom, f"{phantom} are in LIVE_STATUS and are not OrderStatus members"


# ==================================================================================================
# The identifier types, which stringify the OTHER way — and that asymmetry is the trap
# ==================================================================================================
def test_the_IDENTIFIER_types_stringify_to_their_VALUE_not_an_ordinal():
    """Measured, because the module `str()`s these and `.name`s the enums, and the two conventions
    sitting side by side is exactly how one gets applied to the other."""
    assert str(StrategyId("BCTROT-004")) == "BCTROT-004"
    assert str(InstrumentId.from_str("HALO.XNAS")) == "HALO.XNAS"
    assert str(ClientOrderId("PROT-SELL-X")) == "PROT-SELL-X"


def test_the_WHOLE_PLAN_runs_on_REAL_nautilus_typed_values():
    """End to end with production's own types in every field — the HALO case as it actually was.

    Not a unit check on the classifier: the classifier being right is what the tests above assert.
    This asserts the PLAN comes out right when every value is the real thing, which is the claim that
    fourteen green tests did not support.
    """
    halo = InstrumentId.from_str("HALO.XNAS")
    bctrot, manual = StrategyId("BCTROT-004"), StrategyId("MANUAL-001")

    positions = [SimpleNamespace(instrument_id=halo, strategy_id=bctrot, quantity=55,
                                 signed_qty=55, is_open=True)]
    orders = [
        SimpleNamespace(client_order_id=ClientOrderId("orphan"), instrument_id=halo,
                        strategy_id=manual, quantity=19, ts_init=1,
                        order_type=OrderType.TRAILING_STOP_MARKET, status=OrderStatus.ACCEPTED),
        SimpleNamespace(client_order_id=ClientOrderId("keeper"), instrument_id=halo,
                        strategy_id=bctrot, quantity=55, ts_init=2,
                        order_type=OrderType.TRAILING_STOP_MARKET, status=OrderStatus.ACCEPTED),
        SimpleNamespace(client_order_id=ClientOrderId("dupe"), instrument_id=halo,
                        strategy_id=bctrot, quantity=55, ts_init=3,
                        order_type=OrderType.TRAILING_STOP_MARKET, status=OrderStatus.ACCEPTED),
    ]
    plan = reconcile_protection(positions, orders)
    assert plan.refused is None
    assert plan.keep == ("keeper",), f"kept {plan.keep}"
    assert sorted(c.client_order_id for c in plan.cancel) == ["dupe", "orphan"]


# ==================================================================================================
# The same rule in two languages
# ==================================================================================================
def _ui_set(name: str) -> set[str]:
    """Read a `new Set([...])` literal out of the UI's books.ts."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[2] / "ui/src/tiles/managed-portfolio/books.ts"
    text = src.read_text()
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\)", text, re.S)
    assert m, f"could not find `const {name} = new Set([...])` in {src}"
    return set(re.findall(r'"([A-Z_]+)"', m.group(1)))


def test_the_UI_and_the_ENGINE_agree_on_what_PROTECTS():
    """One predicate, two languages, and they drift.

    The engine decides which resting orders to CANCEL; the UI decides which positions to show as
    UNPROTECTED. If those disagree, one of the two screens is making a safety claim that is wrong —
    and both copies of this set carried the same two names that match nothing and were missing the
    same real one, because the second was copied from the first.

    The API emits the enum NAME (measured: `TRAILING_STOP_MARKET`, `LIMIT`, `MARKET`), so the UI is
    comparing against the same vocabulary the tests above pin the engine to.
    """
    assert _ui_set("PROTECTIVE") == set(PROTECTIVE_TYPES), (
        f"only-in-ui={sorted(_ui_set('PROTECTIVE') - PROTECTIVE_TYPES)}, "
        f"only-in-engine={sorted(PROTECTIVE_TYPES - _ui_set('PROTECTIVE'))}"
    )


def test_the_UI_and_the_ENGINE_agree_on_what_is_LIVE():
    assert _ui_set("LIVE_STATUS") == set(LIVE_STATUS), (
        f"only-in-ui={sorted(_ui_set('LIVE_STATUS') - LIVE_STATUS)}, "
        f"only-in-engine={sorted(LIVE_STATUS - _ui_set('LIVE_STATUS'))}"
    )


def test_the_UI_SET_ALSO_contains_no_name_that_can_never_occur():
    """Aimed at the class, not the instance: the UI set must be checkable against the real enum too,
    or the next phantom entry is added there instead."""
    real = {m.name for m in OrderType}
    assert not sorted(_ui_set("PROTECTIVE") - real)
