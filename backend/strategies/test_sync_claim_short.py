"""#950 — the claim sync reads a lane's NEGATIVE netting quantity as flat and DROPS the claim.

The adapter hands the runner `qty = int(sum(p.signed_qty for p in cache.positions_open(...)))` —
SIGNED (kumo-trading-strategies `contract.py:231`; driven 2026-09-11: `protective_close` with a -1234 short
delivered `runner.sync_claim("XYZQ", -1234, 12.34)` to this boundary). Both cockpit gateways then do
`if qty > 0: record else: drop`, so a live 57-share short arrives as -57, takes the `else`, and the
claim on a REAL position is deleted. `_foreign_claims` then reports nothing for the symbol and
`reducible(acct, mine, other)` gives every long lane MORE room against shares that are the short
lane's — the freeze by omission with the sign flipped (l21, coordinator, 2026-09-11).

ASSERT THE NUMBER, not that a write happened: the two upstream tests that missed `abs(q)` for weeks
both checked "a statement was executed" and never looked at the value; this repo's own
`test_a_fill_RECORDS_the_lanes_quantity` has the same shape. Here the compiled statement's
parameters are read and the signed quantity must be IN them.

Seen red 2026-09-11 against b930bd2: `sync_claim("XYZQ", -57, 36.5)` executes a DELETE on both gateways.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from sqlalchemy.sql.dml import Delete, Insert

from strategies.momentum import SessionGateway as Momentum
from strategies.qc345 import QC345SessionGateway as QC345
from strategies.test_sync_claim import _dml, _gateway

SHORT = -57      # a magnitude no default produces; the sign is the subject
LONG = 260


def _params(stmt) -> list:
    """Every bound value on the statement, so the quantity can be found by VALUE."""
    return list(stmt.compile().params.values())


def _store_represents_short() -> bool:
    """issue 178's capability constant — the ONE authority on whether the ledger takes a
    signed claim. Read off the installed store, never a revision string."""
    from kumo_strategies.runtime.executor import store
    return getattr(store, "CLAIMS_REPRESENT_SHORT", False) is True


@pytest.mark.parametrize("cls", [Momentum, QC345])
@pytest.mark.xfail(not _store_represents_short(), strict=True,
                   reason="the installed store predates issue 178 and REFUSES a negative claim "
                          "('claim qty must be positive') — fails-by-passing on the bump")
def test_a_SHORT_position_RECORDS_the_signed_quantity_it_is_NOT_dropped(cls):
    g = _gateway(cls)
    asyncio.run(g.sync_claim("XYZQ", SHORT, 36.5))
    dml = _dml(g._journal.executed)
    assert [isinstance(s, Insert) for s in dml] == [True], [type(s).__name__ for s in dml] or "NOTHING was executed"
    assert SHORT in _params(dml[0]), f"-57 must reach the ledger; params were {_params(dml[0])}"


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_SHORT_position_is_NEVER_DROPPED_even_on_a_store_that_cannot_yet_record_it(cls, caplog):
    """Today's truth on a pre-#178 store: the ledger REFUSES the negative (`claim qty must be
    positive`), the gateway leaves the claim ALONE and says so — never a DELETE on a real position,
    never silence. The bug was the DELETE; the store's refusal is the honest interim answer."""
    import logging

    g = _gateway(cls)
    with caplog.at_level(logging.WARNING, logger="kumo.claims_sync"):
        asyncio.run(g.sync_claim("XYZQ", SHORT, 36.5))
    dml = _dml(g._journal.executed)
    assert not any(isinstance(s, Delete) for s in dml), "a live short's claim was DELETED"
    if not _store_represents_short():
        assert dml == [] and any("-57" in r.getMessage() for r in caplog.records), "the refusal must be LOUD"


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_LONG_fill_records_THE_NUMBER_not_merely_a_write(cls):
    g = _gateway(cls)
    asyncio.run(g.sync_claim("XYZQ", LONG, 9.6))
    dml = _dml(g._journal.executed)
    assert [isinstance(s, Insert) for s in dml] == [True]
    assert LONG in _params(dml[0]), f"260 must be IN the statement's parameters, not merely 'a write': {_params(dml[0])}"


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_ZERO_is_flat_and_nothing_else_is(cls):
    g = _gateway(cls)
    asyncio.run(g.sync_claim("XYZQ", 0, None))
    assert [type(s) for s in _dml(g._journal.executed)] == [Delete]


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_SHORT_with_no_price_is_left_alone_like_a_long_one(cls):
    """A rejection while the lane still holds a short: no price to insert with, the submit-time
    claim stands — the same rule as the long case, never a DELETE on a real position."""
    g = _gateway(cls)
    asyncio.run(g.sync_claim("XYZQ", SHORT, None))
    assert g._journal.executed == []


def test_both_gateways_share_ONE_predicate_and_neither_spells_the_sign_blind_test():
    """Two identical bodies is how the same defect ships twice; the predicate lives once
    (`api.claims_sync.sync_claim_from_book`) and the sync methods delegate to it."""
    from api import claims_sync

    for cls in (Momentum, QC345):
        src = inspect.getsource(cls.sync_claim)
        assert "qty > 0" not in src and "qty <= 0" not in src, f"{cls.__name__} still tests the sign"
        assert "sync_claim_from_book" in src
    src = inspect.getsource(claims_sync.sync_claim_from_book)
    assert "!= 0" in src and "qty > 0" not in src
