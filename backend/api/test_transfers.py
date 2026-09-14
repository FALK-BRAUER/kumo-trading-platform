"""Tests for internal position transfers (#80 spin-off). Pure/offline — the Nautilus-side mechanism is pinned
separately in test_transfer_harness.py (marked `engine`)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.transfers import (
    TransferRequest,
    fold,
    incomplete,
    leg_sides,
    resolve_transfer_px,
    validate,
)


def _req(transfer_id="T1", qty="100", mode="CARRY_OVER") -> TransferRequest:
    return TransferRequest(
        transfer_id=transfer_id,
        command_id=transfer_id,
        account_id="ALPACA-1",
        client_id="ALPACA",
        instrument_id="CVS.XNYS",
        source_strategy_id="EXTERNAL",
        target_strategy_id="MANUAL-001",
        side="LONG",
        quantity=Decimal(qty),
        pricing_mode=mode,
        transfer_px=Decimal("106.63"),
        source_avg_px=Decimal("106.63"),
        source_ts_last=1_785_000_000_000_000_000,
        reason_code="manual",
    )


def _ok(**over):
    kwargs = dict(
        quantity=Decimal(100),
        source_qty=Decimal(190),
        source_ts_last=1,
        current_ts_last=1,
        source_strategy_id="EXTERNAL",
        target_strategy_id="MANUAL-001",
        manageable_strategies={"MANUAL-001"},
        source_reducing_qty=Decimal(0),
        target_opposite_qty=Decimal(0),
        transfer_px=Decimal("106.63"),
    )
    kwargs.update(over)
    return validate(**kwargs)


# --- leg direction: getting this backwards doubles internal exposure -----------------------------------

def test_transferring_a_long_sells_the_source_and_buys_the_target():
    assert leg_sides("LONG") == ("SELL", "BUY")


def test_transferring_a_short_is_the_mirror():
    assert leg_sides("SHORT") == ("BUY", "SELL")


def test_unknown_side_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        leg_sides("FLAT")


# --- pricing ------------------------------------------------------------------------------------------

def test_carry_over_uses_the_source_basis_so_nothing_is_realized():
    assert resolve_transfer_px("CARRY_OVER", Decimal("106.63"), Decimal("108.00")) == Decimal("106.63")


def test_market_uses_the_live_mark():
    assert resolve_transfer_px("MARKET", Decimal("106.63"), Decimal("108.00")) == Decimal("108.00")


def test_market_without_a_mark_refuses_rather_than_falling_back_to_basis():
    """Falling back would silently turn a MARKET transfer into CARRY_OVER and misstate both strategies."""
    assert resolve_transfer_px("MARKET", Decimal("106.63"), None) is None


def test_carry_over_without_a_basis_refuses():
    assert resolve_transfer_px("CARRY_OVER", Decimal(0), Decimal("108.00")) is None


# --- validation ---------------------------------------------------------------------------------------

def test_valid_transfer_passes():
    assert _ok() is None


def test_cannot_transfer_more_than_the_source_holds():
    assert "source holds" in _ok(quantity=Decimal(500))


def test_cannot_transfer_from_an_empty_source():
    assert "no position" in _ok(source_qty=Decimal(0))


def test_zero_or_negative_quantity_is_refused():
    assert _ok(quantity=Decimal(0)) is not None
    assert _ok(quantity=Decimal(-5)) is not None


def test_stale_read_is_refused():
    """The position moved between the api reading it and the engine applying — re-read rather than book."""
    assert "moved since it was read" in _ok(current_ts_last=999)


def test_transfer_into_an_unregistered_strategy_is_refused():
    """Otherwise the position lands with no owner able to manage it."""
    assert "not a registered strategy" in _ok(target_strategy_id="MOMENTUM-001")


def test_reducing_orders_the_source_could_not_cover_block_the_transfer():
    """EXTERNAL holds 190 with a 190 stop resting; moving 100 away would leave 90 behind a 190 sell, which on
    trigger sells more than it holds and flips it short."""
    assert "flip it" in _ok(source_reducing_qty=Decimal(190))


def test_reducing_orders_the_source_can_still_cover_are_allowed():
    """90 resting against 90 remaining is exactly covered — nothing unsafe about it."""
    assert _ok(source_reducing_qty=Decimal(90)) is None


def test_opening_orders_on_the_source_do_not_block():
    """Only REDUCING orders matter; a resting BUY on a long source is unaffected by losing quantity."""
    assert _ok(source_reducing_qty=Decimal(0)) is None


def test_orders_on_the_target_never_block():
    """The target only GAINS quantity, so nothing resting against it can be invalidated dangerously — the
    guard doesn't even look at the target side."""
    assert _ok() is None


def test_missing_price_is_refused():
    assert "transfer price" in _ok(transfer_px=None)


def test_full_transfer_of_the_whole_source_is_allowed():
    assert _ok(quantity=Decimal(190)) is None


def test_full_transfer_is_blocked_by_any_reducing_order():
    """Move everything away and nothing remains, so even a small resting exit has nothing left to sell."""
    assert "flip it" in _ok(quantity=Decimal(190), source_reducing_qty=Decimal(1))


def test_transfer_to_the_same_strategy_is_refused():
    assert "same strategy" in _ok(source_strategy_id="MANUAL-001", target_strategy_id="MANUAL-001")


def test_target_holding_the_opposite_side_is_refused():
    """Under NETTING the destination leg would net against that position rather than receive the transfer —
    a change in exposure disguised as bookkeeping."""
    assert "OPPOSITE side" in _ok(target_opposite_qty=Decimal(23))


def test_short_source_is_handled_symmetrically():
    """A SHORT source is reduced by BUYs; the quantity math is side-agnostic since quantities are absolute."""
    assert _ok(source_qty=Decimal(100), quantity=Decimal(40), source_reducing_qty=Decimal(60)) is None
    assert "flip it" in _ok(source_qty=Decimal(100), quantity=Decimal(40), source_reducing_qty=Decimal(61))


# --- outbox fold --------------------------------------------------------------------------------------

class _Row:
    """Stand-in for a PositionTransferEvent row — fold only reads these fields."""

    def __init__(self, transfer_id, event_type, req: TransferRequest):
        self.transfer_id = transfer_id
        self.event_type = event_type
        for f in (
            "command_id", "account_id", "client_id", "instrument_id", "source_strategy_id",
            "target_strategy_id", "side", "quantity", "pricing_mode", "transfer_px", "source_avg_px",
            "source_ts_last", "reason_code", "reversal_of_transfer_id",
        ):
            setattr(self, f, getattr(req, f))


def _rows(transfer_id, *states):
    req = _req(transfer_id)
    return [_Row(transfer_id, st, req) for st in states]


def test_fold_reports_the_furthest_state_reached():
    [p] = fold(_rows("T1", "PREPARED", "SOURCE_APPLIED", "DEST_APPLIED", "COMPLETED"))

    assert p.state == "COMPLETED"


def test_fold_is_independent_of_row_order():
    forward = fold(_rows("T1", "PREPARED", "SOURCE_APPLIED"))
    reverse = fold(list(reversed(_rows("T1", "PREPARED", "SOURCE_APPLIED"))))

    assert forward[0].state == reverse[0].state == "SOURCE_APPLIED"


def test_a_failure_after_the_source_leg_is_still_recoverable():
    """The state that used to hide: quantity left the source, never reached the target, and FAILED made
    recovery skip it — leaving the internal books short against the broker net permanently."""
    [p] = incomplete(fold(_rows("T1", "PREPARED", "SOURCE_APPLIED", "FAILED")))

    assert p.transfer_id == "T1"


def test_a_transfer_stuck_after_the_source_leg_is_flagged_incomplete():
    """The dangerous state: quantity left one strategy and never reached the other, so the internal books
    don't sum to the broker net until it's finished."""
    [p] = incomplete(fold(_rows("T1", "PREPARED", "SOURCE_APPLIED")))

    assert p.state == "SOURCE_APPLIED"


def test_completed_transfers_need_no_recovery():
    assert incomplete(fold(_rows("T1", "PREPARED", "COMPLETED"))) == []


def test_multiple_transfers_are_folded_independently():
    rows = _rows("T1", "PREPARED", "COMPLETED") + _rows("T2", "PREPARED", "SOURCE_APPLIED")

    assert {p.transfer_id for p in incomplete(fold(rows))} == {"T2"}


def test_leg_order_ids_are_deterministic_so_replay_collides_instead_of_duplicating():
    a, b = _req("T9"), _req("T9")

    assert a.source_order_id == b.source_order_id
    assert a.dest_order_id == b.dest_order_id
    assert a.source_order_id != a.dest_order_id


def test_leg_ids_are_hashed_not_prefixed_so_similar_ids_do_not_collide():
    """Time-ordered ids share long prefixes; truncation would map unrelated transfers to the same legs."""
    a = _req("0199a1b2-c3d4-7000-8000-000000000001")
    b = _req("0199a1b2-c3d4-7000-8000-000000000002")

    assert a.source_order_id != b.source_order_id


def test_leg_ids_fit_the_nautilus_identifier_limit():
    """Identifiers cap at 36 chars and the fill adds a `-T` suffix, so a raw uuid transfer id overflows."""
    req = _req("23b2d7db-87c5-40b0-ba7e-df70f5d2d4f5")

    assert len(req.source_order_id) + 2 <= 36
    assert len(req.dest_order_id) + 2 <= 36
