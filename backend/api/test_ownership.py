"""No strategy may hold a short (#437, rule 3).

WHD, 2026-08-21. BCTROT bought 28. MOMENTUM sold 28 — shares it did not own. Nautilus refused to open
the phantom and said so in the log, then the book sat at `+28 BCTROT / -28 MOMENTUM` against a broker
holding ZERO for twelve hours while every health check reported fine.

`_report_reconcile_drift` was right to report no drift: it sums signed_qty per SYMBOL and +28-28 == 0.
The account is genuinely flat. The SPLIT underneath is wrong and no reconciliation can ever see it,
because the broker has one net position per symbol and no opinion about whose it is.

So this is checked at the strategy level or not at all, and it is cheap: a strategy that never shorts
holding a negative quantity is wrong on its face, with no cross-referencing needed.
"""

from __future__ import annotations

import pytest

from api.ownership import SHORT_PERMITTED, short_violations, signed_qty_of


class _CachePosition:
    """Nautilus's `Position` where this code touches it: SIGNED qty, and `quantity` is UNSIGNED."""

    def __init__(self, sid, iid, signed):
        self.strategy_id, self.instrument_id, self.signed_qty = sid, iid, signed
        self.quantity = abs(signed)


class _Dto:
    """`PositionDTO` as /positions actually serves it — unsigned `quantity` plus a `side` string. This
    is the shape that made WHD invisible: 28 and 28, both positive, direction in a separate field."""

    def __init__(self, sid, iid, side, qty):
        self.strategy_id, self.instrument_id, self.side, self.quantity = sid, iid, side, qty


def test_the_two_fixtures_disagree_about_sign_the_way_production_does():
    # Fixture property first, and it is the whole reason `signed_qty_of` exists. The DTO's quantity is
    # POSITIVE for a short; only `side` carries the direction. Reading `quantity` off either shape and
    # comparing to zero finds nothing, which is exactly what happened for twelve hours.
    assert _CachePosition("M", "WHD.XNYS", -28).signed_qty == -28
    assert _Dto("M", "WHD.XNYS", "SHORT", 28).quantity == 28


def test_signed_qty_reads_BOTH_shapes():
    assert signed_qty_of(_CachePosition("M", "WHD.XNYS", -28)) == -28
    assert signed_qty_of(_Dto("M", "WHD.XNYS", "SHORT", 28)) == -28
    assert signed_qty_of(_Dto("B", "WHD.XNYS", "LONG", 28)) == 28
    assert signed_qty_of(_Dto("B", "X", "FLAT", 0)) == 0


def test_an_UNREADABLE_position_raises_rather_than_reporting_zero():
    """FAIL CLOSED. Returning 0.0 for a shape it does not understand would report "no shorts" for a
    book it could not read — a silent all-clear, which is the failure mode this whole ticket is about.
    A new DTO field name must break the check loudly, not quietly disarm it."""
    class _Alien:
        strategy_id, instrument_id = "M", "X"
    with pytest.raises(ValueError, match="signed quantity"):
        signed_qty_of(_Alien())


#: THE ACTUAL /trades ROW, copied from the live paper API on 2026-08-22. Dicts, not objects — the
#: seam this check is wired into serves JSON, and every fixture above is an object. That mismatch is
#: how a "tested" helper does nothing in production, five times in one day on 2026-08-14.
_LIVE_TRADE_ROWS = [
    {"instrument_id": "WHD.XNYS", "side": "LONG", "quantity": 28.0, "state": "HELD",
     "strategy_id": "BCTROT-004", "market_value": 1944.04},
    {"instrument_id": "WHD.XNYS", "side": "SHORT", "quantity": 28.0, "state": "HELD",
     "strategy_id": "MOMENTUM-002", "market_value": -1944.04},
]


def test_the_live_rows_are_MAPPINGS_and_carry_no_signed_qty():
    # Fixture property first. `getattr(row, "signed_qty")` on these returns None and `getattr(row,
    # "side")` returns None too — an attribute-only reader raises on every production row.
    assert isinstance(_LIVE_TRADE_ROWS[0], dict)
    assert getattr(_LIVE_TRADE_ROWS[0], "side", None) is None
    assert "signed_qty" not in _LIVE_TRADE_ROWS[0]


def test_the_check_runs_on_the_JSON_the_api_actually_serves():
    v = short_violations(_LIVE_TRADE_ROWS)
    assert [(x.strategy_id, x.signed_qty) for x in v] == [("MOMENTUM-002", -28.0)]


def test_the_LIVE_WHD_book_is_reported():
    """The actual 2026-08-21 book. If this ever passes silently the check has stopped working."""
    book = [_CachePosition("BCTROT-004", "WHD.XNYS", 28),
            _CachePosition("MOMENTUM-002", "WHD.XNYS", -28)]
    v = short_violations(book)
    assert [(x.strategy_id, x.instrument_id, x.signed_qty) for x in v] == [
        ("MOMENTUM-002", "WHD.XNYS", -28)]


def test_the_LONG_leg_of_the_same_symbol_is_NOT_reported():
    """Discriminating half. BCTROT's +28 is a legitimate holding; a check that flags both legs of a
    netted pair reports the victim alongside the culprit and teaches an operator to ignore it."""
    book = [_CachePosition("BCTROT-004", "WHD.XNYS", 28),
            _CachePosition("MOMENTUM-002", "WHD.XNYS", -28)]
    assert all(x.strategy_id != "BCTROT-004" for x in short_violations(book))


def test_a_flat_book_is_silent():
    assert short_violations([_CachePosition("M", "X", 0), _Dto("B", "Y", "FLAT", 0)]) == []


def test_ONE_lane_is_exempt_by_name_and_the_default_is_still_DENY():
    """Opt-in only, per the project's standing rule that new gates default to False. Exactly one
    strategy in this cockpit shorts — CRSISHORT-006 (#858), a deliberate, named, reviewed edit —
    so a short in any OTHER lane is still a defect. The set is pinned literally: a second name here
    must arrive through a review, not through a default that grew."""
    assert SHORT_PERMITTED == frozenset({"CRSISHORT-006"})


def test_an_EXEMPT_strategy_is_silent_but_only_that_one():
    """The exemption is named to the STRATEGY, and it must not leak to its neighbours holding the
    same symbol — the exemption mechanism itself being too broad is how a guard stops guarding."""
    book = [_CachePosition("HEDGE-009", "WHD.XNYS", -28),
            _CachePosition("MOMENTUM-002", "WHD.XNYS", -28)]
    v = short_violations(book, permitted=frozenset({"HEDGE-009"}))
    assert [x.strategy_id for x in v] == ["MOMENTUM-002"]


def test_an_explicit_permitted_set_REPLACES_the_module_default_and_does_not_union(monkeypatch):
    """Survived the first sweep: with `SHORT_PERMITTED` empty, `A | B` and `B` are the same value, so
    a union could not be told from a replacement. It matters the day a name is added — every caller
    passing an explicit set would silently inherit the module's exemptions on top of its own, and a
    test asserting "MOMENTUM is reported" would start passing for the wrong reason.

    Patched non-empty so the two implementations can actually disagree. A fixture that cannot violate
    the property it asserts proves nothing."""
    monkeypatch.setattr("api.ownership.SHORT_PERMITTED", frozenset({"MOMENTUM-002"}))
    book = [_CachePosition("MOMENTUM-002", "WHD.XNYS", -28)]
    # The module default would exempt MOMENTUM; an explicit set that names someone else must not.
    assert [x.strategy_id for x in short_violations(book, permitted=frozenset({"HEDGE-009"}))] == [
        "MOMENTUM-002"]
    # ...and with no explicit set, the module default still applies.
    assert short_violations(book) == []


def test_the_DISPLAY_predicate_and_the_DETECTOR_answer_LONG_minus_28_DIFFERENTLY():
    """Two questions, two predicates (#855 scope review). The detector reads the raw sign as
    evidence — a LONG carrying -28 is exactly the WHD shape it exists to see — while a mark treats
    `side` as the authority and the number as a size. Collapsing them into one abs-first predicate
    silences the detector; collapsing them into the raw one blanks the book on a signed spelling."""
    from api.ownership import display_signed_qty, signed_qty_of

    assert signed_qty_of({"side": "LONG", "quantity": -28}) == -28.0, "the detector keeps the raw sign"
    assert display_signed_qty("LONG", -28) == 28.0, "the display trusts the side"
    assert display_signed_qty("SHORT", 28) == -28.0 and display_signed_qty("SHORT", -28) == -28.0
    assert display_signed_qty("FLAT", 5) == 0.0 and display_signed_qty("LONG", 0) == 0.0
    # RETURNS, never raises: it runs inside the publisher's try, where a raise blanks the book.
    assert display_signed_qty(None, "x") == 0.0 and display_signed_qty("SHORT", None) == 0.0
