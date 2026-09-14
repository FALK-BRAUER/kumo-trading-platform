"""Turning stored observations into a per-lane window delta (#699 read path, #738).

WHAT THE OPERATOR ASKED FOR, before any of the rest of this: the strategy tile showing the DELTA for the
selected period. The tile shows `realized(W)` today. The missing half is `Δunrealized(W)` per lane,
which needs a per-lane mark at the window's START — the broker publishes only account-level curves,
which is why #734 built the observation table. This is the read side of it.

THE IDENTITY. `NET(W) = realized(W) + [unrealized_now − unrealized_at_window_start]`. It holds at the
LANE level because the table records per-lane unrealized per position per day: a position closed
during the window contributed its unrealized at the start and its result now sits in `realized(W)`,
so the two terms do not double-count.

VERSION SELECTION COMES FIRST, and #738 says so explicitly. `method_version` is in the base unique
key, deliberately, so a corrected re-derivation can stand BESIDE the old rows rather than overwrite
them. That means "exactly one base row per lane-instrument-day" is no longer a database guarantee,
and every reader must choose. One function chooses, or two call sites drift — which is the entire
subject of the tickets that produced this table.
"""

from __future__ import annotations

import pytest

from api.eod_read import lane_unrealized_at, select_version


def _row(lane, inst, day, unrealized, version="v1", kind="close"):
    """A row shaped like the TABLE, expressing a wanted unrealized through what is actually stored.

    THE FIRST VERSION CARRIED AN `unrealized_derived` KEY. No such column exists — the model says
    `qty x (mark - basis)` is computed at READ time, and the stored engine/venue figures are what it
    is checked against (#370). So the fixture asserted against a field production never supplies, and
    the code passed by reading it. A double that cannot represent production is the bug, and this one
    was inventing the very thing that made the read path look correct.

    `qty=1, basis=0` makes `mark` the unrealized directly, so the tests stay readable while going
    through the same arithmetic production does. An unpriced position is `mark=None`.
    """
    return {
        "session_date": day, "strategy_id": lane, "instrument_id": inst,
        "qty": None if unrealized is None else 1.0,
        "avg_px_engine": None if unrealized is None else 0.0,
        "mark_px": unrealized,
        "unrealized_engine": unrealized,
        "method_version": version, "capture_kind": kind,
    }


# ==================================================================================================
# VERSION SELECTION — one function, written down before anything reads
# ==================================================================================================
def test_a_single_version_is_selected_without_ceremony():
    assert select_version([_row("A", "X.XNYS", "2026-08-24", 10.0)]) == "v1"


def test_the_NEWEST_version_wins_when_a_re_derivation_stands_beside_the_old_rows():
    """That is what `method_version` in the key BUYS: a corrected derivation lands beside the
    original so the two can be diffed, instead of overwriting an audit trail. The reader must then
    pick, and the newest is the corrected one — that is the only reason a second version exists."""
    rows = [_row("A", "X.XNYS", "2026-08-24", 10.0, version="v1"),
            _row("A", "X.XNYS", "2026-08-24", 12.5, version="v2")]
    assert select_version(rows) == "v2"


def test_version_selection_orders_NUMERICALLY_because_lexical_gets_v10_wrong():
    """`v10` after `v9` is the trap, and it is nine corrections away rather than hypothetical — a
    plain string sort puts `v10` BEFORE `v9` and silently reads the older derivation forever.

    (This test was first named "...is_LEXICAL_and_says_so" while asserting the opposite. A name that
    contradicts its own assertion is worse than no name: the next reader trusts the sentence.)"""
    rows = [_row("A", "X.XNYS", "2026-08-24", 1.0, version="v9"),
            _row("A", "X.XNYS", "2026-08-24", 2.0, version="v10")]
    assert select_version(rows) == "v10", "v10 is newer than v9; a plain string sort gets this wrong"


def test_a_version_that_cannot_be_ORDERED_is_REFUSED_rather_than_guessed():
    """A stored conclusion nobody can order is not a version. Refusing names the bad row; guessing
    silently reports a number derived from whichever string sorted highest."""
    rows = [_row("A", "X.XNYS", "2026-08-24", 1.0, version="v1"),
            _row("A", "X.XNYS", "2026-08-24", 2.0, version="experimental")]
    with pytest.raises(ValueError, match="cannot be ordered"):
        select_version(rows)


def test_NO_rows_means_UNKNOWN_not_a_default_version():
    assert select_version([]) is None


# ==================================================================================================
# THE LANE'S UNREALIZED AT A DATE
# ==================================================================================================
def test_a_lane_total_sums_only_ITS_OWN_rows_for_that_day():
    rows = [_row("MOMENTUM-002", "AEM.XNYS", "2026-08-24", 100.0),
            _row("MOMENTUM-002", "WPM.XNYS", "2026-08-24", 50.0),
            _row("BCTROT-004", "AEM.XNYS", "2026-08-24", 999.0),
            _row("MOMENTUM-002", "AEM.XNYS", "2026-08-25", 777.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "MOMENTUM-002", "v1") == 150.0


def test_ONE_unpriced_position_makes_the_LANE_total_UNKNOWN_not_partial():
    """The rule `observe_lanes` and `_standing_unrealized_total` both already follow. A lane total
    missing one leg is not a smaller total, it is an unknown one — and a partial sum presented as a
    total is the defect #596 was about."""
    rows = [_row("A", "X.XNYS", "2026-08-24", 100.0),
            _row("A", "Y.XNYS", "2026-08-24", None)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") is None


def test_a_day_with_NO_ROWS_for_that_lane_is_UNKNOWN_never_zero():
    """THE WHOLE POINT OF THE TABLE. A lane that was not captured and a lane that held nothing must
    not read the same. Zero here would render a confident $0.00 delta for a window nobody observed —
    which is the em dash this feature exists to replace with a real number, not with a lie."""
    assert lane_unrealized_at([], "2026-08-24", "A", "v1") is None


def test_rows_from_ANOTHER_method_version_are_not_mixed_in():
    """Mixing derivations is how a delta comes out of two different rules. The selected version is
    the only one read."""
    rows = [_row("A", "X.XNYS", "2026-08-24", 100.0, version="v1"),
            _row("A", "X.XNYS", "2026-08-24", 999.0, version="v2")]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") == 100.0


# THE DELTA ITSELF MOVED TO TYPESCRIPT. `window_delta_unrealized` lived here with its
# None-propagation rules pinned and no production caller; the subtraction belongs where the "now"
# term is, which is the live frame. The rules were ported to `ui/src/lib/framework/windowBase.test.ts`
# in the SAME commit that deleted the function — keeping tested-but-dead code is how the phantom
# `unrealized_derived` key survived in this very chain, and deleting first would have orphaned the
# rules.

# ==================================================================================================
# THE DERIVED FIGURE IS COMPUTED, NOT STORED — and reading a phantom column hid that
# ==================================================================================================
def _stored(lane, inst, day, qty, basis, mark, engine=None, version="v1", kind="close"):
    """A row as the TABLE actually holds it. There is no `unrealized_derived` column: the model says
    `qty x (mark - basis)` is computed at READ time and the stored engine/venue figures are what it
    is checked against (#370). A fixture carrying a phantom column cannot represent production."""
    return {"session_date": day, "strategy_id": lane, "instrument_id": inst,
            "qty": qty, "avg_px_engine": basis, "mark_px": mark,
            "unrealized_engine": engine, "method_version": version, "capture_kind": kind}


def test_the_lane_total_is_COMPUTED_from_qty_mark_and_basis():
    """THE DEFECT THIS PINS. `eod_read` asked for an `unrealized_derived` key, and the store's read
    listed it as a column. It does not exist — `getattr(row, col, None)` returned None silently, and
    the code fell through to `unrealized_engine`.

    That works for a LIVE capture and is empty for a RECONSTRUCTED day, where `unrealized_engine` is
    deliberately NULL because Nautilus has no reading for a past day. So every backfilled row would
    have read back UNKNOWN, and the backfill — whose entire purpose is giving 1W/1M/3M a base
    immediately instead of waiting weeks for captures — would have produced nothing usable.

    A phantom column, silently None, is why: absence read as a value."""
    rows = [_stored("A", "X.XNYS", "2026-08-24", qty=10.0, basis=100.0, mark=110.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") == 100.0


def test_a_SHORT_position_derives_a_negative_correctly():
    """Signed quantity, so the arithmetic must not assume long. A short of 10 opened at 100 with the
    mark at 110 has LOST 100."""
    rows = [_stored("A", "X.XNYS", "2026-08-24", qty=-10.0, basis=100.0, mark=110.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") == -100.0


def test_an_UNPRICED_row_is_UNKNOWN_even_when_the_ENGINE_figure_is_present():
    """The engine's reading is a CROSS-CHECK, not a substitute. Falling back to it when the mark is
    missing would silently mix two derivations inside one total — and the reason both are stored is
    that they can disagree (#370: +263.84 against the broker's +9.52)."""
    rows = [_stored("A", "X.XNYS", "2026-08-24", qty=10.0, basis=100.0, mark=None, engine=55.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") is None


def test_a_row_with_no_BASIS_is_UNKNOWN_rather_than_treated_as_zero_cost():
    """A missing basis would make the derived figure the position's whole market value — a number
    that looks like a gain and is actually the notional."""
    rows = [_stored("A", "X.XNYS", "2026-08-24", qty=10.0, basis=None, mark=110.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") is None


def test_a_PARTIAL_re_derivation_is_UNKNOWN_not_a_confident_smaller_total():
    """Found by review, dormant today and armed the moment a second `method_version` exists.

    A corrected version may legitimately cover only SOME instruments — `reconstruct_lanes` skips
    symbols it cannot price, and the backfill runner's own comment anticipates "a re-run after a
    method_version change that only some rows carry". Reading the newest version alone then returns a
    total missing whatever it did not re-derive: here 11.0 where the truth is 31.0.

    That is the one path in this module that produced a CONFIDENT WRONG NUMBER rather than an em
    dash, which contradicts its whole charter."""
    rows = [
        _stored("A", "AAA.XNYS", "2026-08-24", qty=1.0, basis=0.0, mark=20.0, version="v1"),
        _stored("A", "BBB.XNYS", "2026-08-24", qty=1.0, basis=0.0, mark=11.0, version="v1"),
        _stored("A", "AAA.XNYS", "2026-08-24", qty=1.0, basis=0.0, mark=11.0, version="v2"),
    ]
    # FIXTURE PROPERTY FIRST: v2 must genuinely cover FEWER instruments than v1, or there is no
    # partial re-derivation to refuse.
    v2 = {r["instrument_id"] for r in rows if r["method_version"] == "v2"}
    v1 = {r["instrument_id"] for r in rows if r["method_version"] == "v1"}
    assert v2 < v1

    assert select_version(rows) == "v2"
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v2") is None, (
        "a version covering only part of the lane must be UNKNOWN, not its own partial sum"
    )


def test_a_NON_FINITE_leg_is_UNKNOWN_rather_than_serialised_as_literal_NaN():
    """NaN survives every comparison written for numbers, and `json.dumps` emits a bare `NaN` that
    `JSON.parse` REJECTS — so one poisoned row would break the entire payload rather than one cell.
    Same family as the non-finite mark guard in `reconstruct_lanes`."""
    rows = [_stored("A", "X.XNYS", "2026-08-24", qty=float("inf"), basis=0.0, mark=1.0)]
    assert lane_unrealized_at(rows, "2026-08-24", "A", "v1") is None
