"""Stamp a protective stop with the lane that holds the shares, where that lane is unambiguous (#748).

THE MINT. Under NETTING a fill's position is derived `{instrument}-{fill.strategy_id}` from the
ORDER's strategy_id. Protective stops are built by the display strategy's factory, so they carry
MANUAL-001; on an instrument MANUAL-001 does not hold, the fill resolves to a position that has never
existed, reduce-only applies it to NO position, and the ~10s poll fabricates the difference as a
synthetic sell at a price that never traded. That is where paper's 8 mirror pairs and 262 fabricated
shares came from.

WHY THIS IS THE SMALL CUT AND NOT THE FULL SPLIT. #748's per-lane split changes what is PLANNED — one
stop per lane instead of one per leg — and that cannot be done alone: instrument-level oversize would
shrink the wrong lane's correctly-sized stop, and pro-rata coverage would tell a fully covered lane it
is naked and rest a duplicate that flips it short on trigger. Both fixes exist and neither is wired.

This changes only the STAMP. Where exactly one lane holds the instrument, the aggregate stop's
quantity ALREADY equals that lane's quantity — so naming that lane is not an attribution guess, it is
the only answer consistent with the size already being placed. No coverage or oversize arithmetic
moves. Where more than one lane holds it, nothing changes and today's behaviour is kept, because
stamping a 56-share stop with a lane holding 54 would over-close it by 2 and flip it short — trading a
bookkeeping defect for a real one.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.protective_stamp import stamp_for


@dataclass
class _Pos:
    instrument_id: str
    strategy_id: str
    signed_qty: float
    is_open: bool = True


def test_ONE_holder_gets_the_stop_stamped_with_that_lane():
    """The common case, and the whole point: 20 of 25 paper instruments have a single holder."""
    book = [_Pos("BDX.XNYS", "MOMENTUM-002", 55.0)]
    assert stamp_for("BDX.XNYS", book) == "MOMENTUM-002"


def test_TWO_holders_are_left_ALONE_rather_than_guessed():
    """A 56-share stop stamped with a lane holding 54 over-closes it by 2 and flips it SHORT. That is
    a real position error traded for a bookkeeping one — strictly worse. Needs the full split."""
    book = [_Pos("AEM.XNYS", "MOMENTUM-002", 54.0), _Pos("AEM.XNYS", "MANUAL-001", 2.0)]
    assert stamp_for("AEM.XNYS", book) is None


def test_a_FLAT_sibling_does_not_make_it_ambiguous():
    """The durable cache retains CLOSED positions, so nearly every instrument shows a sibling lane at
    zero. Counting those would make almost the whole book ambiguous and leave the mint in place —
    which is the failure that would make this change do nothing while looking like it worked."""
    book = [_Pos("BDX.XNYS", "MOMENTUM-002", 55.0), _Pos("BDX.XNYS", "BCTROT-004", 0.0)]
    assert stamp_for("BDX.XNYS", book) == "MOMENTUM-002"


def test_a_CLOSED_sibling_does_not_make_it_ambiguous():
    book = [_Pos("BDX.XNYS", "MOMENTUM-002", 55.0),
            _Pos("BDX.XNYS", "BCTROT-004", 40.0, is_open=False)]
    assert stamp_for("BDX.XNYS", book) == "MOMENTUM-002"


def test_EXTERNAL_is_not_a_holder():
    """EXTERNAL is the record that attribution FAILED, not a lane. Counting it as a holder would make
    every phantom-carrying instrument ambiguous — and those are exactly the ones already damaged."""
    book = [_Pos("RBRK.XNYS", "TECHIVOL-005", 14.0), _Pos("RBRK.XNYS", "EXTERNAL", -14.0)]
    assert stamp_for("RBRK.XNYS", book) == "TECHIVOL-005"


def test_a_SHORT_holder_is_not_stamped():
    """A short's protection reduces by BUYING, and this cut does not reason about side. Leaving it
    unstamped keeps today's behaviour rather than aiming a BUY at a position by guess."""
    book = [_Pos("RDN.XNYS", "MANUAL-001", -72.0)]
    assert stamp_for("RDN.XNYS", book) is None


def test_NOBODY_holds_it_is_None():
    assert stamp_for("AEM.XNYS", []) is None


def test_a_position_that_CANNOT_SAY_whether_it_is_open_makes_it_AMBIGUOUS():
    """Absence must not read as permission. A shape this read cannot describe is not a confirmation
    that one lane holds the instrument."""
    class _Mute:
        instrument_id = "BDX.XNYS"
        strategy_id = "MOMENTUM-002"
        signed_qty = 55.0

    assert not hasattr(_Mute(), "is_open")
    assert stamp_for("BDX.XNYS", [_Mute()]) is None


def test_a_NaN_quantity_makes_it_AMBIGUOUS():
    """NaN survives every comparison written for numbers; `nan != 0` is True, so it would otherwise
    read as a holder."""
    book = [_Pos("BDX.XNYS", "MOMENTUM-002", float("nan"))]
    assert stamp_for("BDX.XNYS", book) is None


def test_a_MUTE_position_BESIDE_A_VALID_HOLDER_still_makes_it_ambiguous():
    """The unreadable-openness guard must REFUSE, not skip — and only this fixture can tell them apart.

    With a single mute position, skipping and refusing both yield "no holder", so a mutant replacing
    `return None` with `continue` survived. Put a readable holder beside it and they diverge: skipping
    leaves one holder and stamps, refusing leaves the instrument alone. Skipping is wrong because a
    position this read cannot describe might be a second holder — and stamping then aims a fill at a
    lane that owns only part of the quantity.
    """
    class _Mute:
        instrument_id = "BDX.XNYS"
        strategy_id = "BCTROT-004"
        signed_qty = 40.0

    book = [_Pos("BDX.XNYS", "MOMENTUM-002", 55.0), _Mute()]
    assert stamp_for("BDX.XNYS", book) is None


def test_a_NaN_position_BESIDE_A_VALID_HOLDER_still_makes_it_ambiguous():
    """Same boundary, one field over. A lone NaN position collapses to "no holder" either way, so the
    mutant survived; beside a real holder, ignoring the NaN would stamp on a book containing a
    quantity nobody can read."""
    book = [_Pos("BDX.XNYS", "MOMENTUM-002", 55.0), _Pos("BDX.XNYS", "BCTROT-004", float("nan"))]
    assert stamp_for("BDX.XNYS", book) is None
