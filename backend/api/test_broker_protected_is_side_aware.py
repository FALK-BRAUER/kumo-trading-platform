"""A position is protected by an order on ITS OWN reducing side (#748 follow-up).

MEASURED LIVE ON PAPER, 2026-08-31, while both stacks were armed:

    CGAU.XNYS  MOMENTUM-002  SHORT   broker_protected=True   working orders: {SELL}
    HALO.XNAS  MOMENTUM-002  SHORT   broker_protected=True   working orders: {SELL}
    VCTR.XNAS  MOMENTUM-002  SHORT   broker_protected=True   working orders: {SELL}

A SHORT is reduced by BUYING. A resting SELL does not protect it — on trigger it ADDS to the short.
So all three reported protected while nothing was protecting them, and the badge an operator reads to
decide whether a position is covered said the opposite of the truth.

TWO ERRORS, IN OPPOSITE DIRECTIONS, from one cause. `_broker_protected` is a set of INSTRUMENT IDS
built from SELL orders only:

  false POSITIVE  any SELL stop on the instrument marks every position on it protected, including
                  shorts it cannot protect. Here the SELL belongs to the LONG leg of a mirror pair
                  held by another lane — so the phantom short is credited with the real long's stop.
  false NEGATIVE  a BUY stop correctly protecting a short is never counted at all, because the set
                  is built from sells. Staging's RDN.XNYS is SHORT 72 with protection armed today;
                  a correct BUY stop there would report the position NAKED.

The fix keys protection by (instrument, reducing side) and asks each position for its own side.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.engine_node import UiFeedStrategy


def _order(symbol, side, otype="trailing_stop", status="new"):
    return {"symbol": symbol, "side": side, "type": otype, "status": status}


def _dto(instrument_id, side):
    return SimpleNamespace(instrument_id=instrument_id, side=side, broker_protected=None,
                           working_orders=[])


def _mark(protected_map, dtos):
    fake = SimpleNamespace(_broker_protected=protected_map, _broker_stop_prices=None)
    UiFeedStrategy._mark_broker_stop_prices(fake, dtos)
    return dtos


def test_a_SHORT_is_NOT_protected_by_a_SELL_stop():
    """The live CGAU/HALO/VCTR shape. This is the assertion the old code fails."""
    dto = _dto("CGAU.XNYS", "SHORT")
    _mark({("CGAU.XNYS", "SELL")}, [dto])
    assert dto.broker_protected is False, (
        "a SHORT reported protected by a SELL stop — on trigger that order ADDS to the short"
    )


def test_a_SHORT_IS_protected_by_a_BUY_stop():
    """The other direction, and tonight's live path: staging's RDN.XNYS is SHORT 72 with protection
    armed. A correct BUY stop there must not report the position naked."""
    dto = _dto("RDN.XNYS", "SHORT")
    _mark({("RDN.XNYS", "BUY")}, [dto])
    assert dto.broker_protected is True


def test_a_LONG_is_protected_by_a_SELL_stop():
    """The common case must not regress — this is 20 of 25 paper instruments."""
    dto = _dto("BDX.XNYS", "LONG")
    _mark({("BDX.XNYS", "SELL")}, [dto])
    assert dto.broker_protected is True


def test_a_LONG_is_NOT_protected_by_a_BUY_stop():
    dto = _dto("BDX.XNYS", "LONG")
    _mark({("BDX.XNYS", "BUY")}, [dto])
    assert dto.broker_protected is False


def test_BOTH_LEGS_of_a_mirror_pair_are_judged_SEPARATELY():
    """The exact live defect. One instrument, a real LONG in one lane and a phantom SHORT in another,
    and a single SELL stop. The long is protected; the short is not. An instrument-level answer
    cannot express that, which is why it got the short wrong."""
    long_leg, short_leg = _dto("CGAU.XNYS", "LONG"), _dto("CGAU.XNYS", "SHORT")
    _mark({("CGAU.XNYS", "SELL")}, [long_leg, short_leg])
    assert long_leg.broker_protected is True
    assert short_leg.broker_protected is False


def test_NEVER_ASKED_stays_None_for_both_sides():
    """Three states, not two: None means the broker was never read, and must not render as NAKED.
    That distinction is already load-bearing here and must survive the side change."""
    longs, shorts = _dto("BDX.XNYS", "LONG"), _dto("BDX.XNYS", "SHORT")
    _mark(None, [longs, shorts])
    assert longs.broker_protected is None and shorts.broker_protected is None


def test_a_position_with_NO_READABLE_SIDE_is_UNKNOWN_not_protected():
    """A dto whose side this read cannot describe is not a protected position. Absence must not read
    as permission — and the old code answered `True` for it whenever any sell rested."""
    dto = SimpleNamespace(instrument_id="BDX.XNYS", side=None, broker_protected=None,
                          working_orders=[])
    _mark({("BDX.XNYS", "SELL")}, [dto])
    assert dto.broker_protected is None
