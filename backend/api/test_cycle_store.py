"""Tests for the cycle envelope store (#74). The pure DTO→row mapping runs offline; the DB round-trip is a
`needs_services` integration test (real Postgres) run explicitly, not in the default suite."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from api.cycle_store import EnvelopeRow, envelope_from_dto
from api.models import TradeDTO


async def _clear(session_factory, *cycle_ids: str) -> None:
    """Hermetic setup: drop this test's own rows so the monotonic upsert guard can't reject a fresh insert
    because a prior run left a higher last_event_ts on the same cycle_id."""
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM trade_cycle WHERE cycle_id = ANY(:ids)"), {"ids": list(cycle_ids)}
        )
        await session.commit()


def _dto(state: str, **over) -> TradeDTO:
    base = dict(
        account_id="ACC",
        client_id="ALPACA",
        instrument_id="AAPL.XNAS",
        strategy_id="MANUAL-001",
        cycle_id="ACC:ALPACA:AAPL.XNAS:MANUAL-001:100",
        state=state,
        side="FLAT",
        quantity=0.0,
        is_capital_deployed=(state == "HELD"),
        is_engaged=(state != "CLOSED"),
        realized_pnl="1000.00 USD",
        leg_count=1,
        opened_ts=100,
        closed_ts=None,
        last_event_ts=5,
    )
    base.update(over)
    return TradeDTO(**base)


def test_envelope_from_dto_carries_identity_and_boundary():
    row = envelope_from_dto(_dto("HELD"))
    assert row.cycle_id == "ACC:ALPACA:AAPL.XNAS:MANUAL-001:100"
    assert row.account_id == "ACC" and row.client_id == "ALPACA"
    assert row.opened_ts == 100 and row.closed_ts is None
    assert row.state == "HELD"
    assert row.last_event_ts == 5
    # No P&L on the envelope — it is metadata, not a ledger.
    assert not hasattr(row, "realized_pnl")


def test_envelope_close_reason_defaults_to_walk_away_only_when_closed():
    assert envelope_from_dto(_dto("HELD")).close_reason is None
    assert envelope_from_dto(_dto("ARMED")).close_reason is None
    closed = envelope_from_dto(_dto("CLOSED", side="FLAT", closed_ts=200, last_event_ts=9))
    assert closed.close_reason == "manual_walk_away"
    assert closed.close_reason_source == "default"
    assert closed.closed_ts == 200


# -- integration (real Postgres) ------------------------------------------------------------------
pytestmark_url = os.environ.get("KUMO_DATABASE_URL")


@pytest.mark.needs_services
def test_store_upsert_is_idempotent_and_monotonic():
    """Round-trip against a live Postgres: insert an active cycle, prove a stale write is a no-op, a newer write
    applies, and closing it drops it from load_active(). Run with KUMO_DATABASE_URL set + `alembic upgrade head`."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.cycle_store import CycleEnvelopeStore

    url = os.environ["KUMO_DATABASE_URL"]
    engine = create_async_engine(url)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = CycleEnvelopeStore(session_factory_=sf)
    base = EnvelopeRow(
        cycle_id="IT:CLI:AAPL.XNAS:MANUAL-001:100",
        account_id="IT",
        client_id="CLI",
        instrument_id="AAPL.XNAS",
        strategy_id="MANUAL-001",
        opened_ts=100,
        closed_ts=None,
        state="HELD",
        last_event_ts=5,
    )

    async def run():
        await _clear(sf, base.cycle_id)
        await store.upsert(base)
        active = await store.load_active()
        assert any(a.cycle_id == base.cycle_id and a.state == "HELD" for a in active)
        # stale write ignored
        await store.upsert(EnvelopeRow(**{**base.__dict__, "state": "ARMED", "last_event_ts": 3}))
        active = await store.load_active()
        assert next(a for a in active if a.cycle_id == base.cycle_id).state == "HELD"
        # newer write applied
        await store.upsert(EnvelopeRow(**{**base.__dict__, "state": "ARMED", "last_event_ts": 7}))
        active = await store.load_active()
        assert next(a for a in active if a.cycle_id == base.cycle_id).state == "ARMED"
        # close → drops from active
        await store.upsert(
            EnvelopeRow(**{**base.__dict__, "state": "CLOSED", "closed_ts": 200, "last_event_ts": 9})
        )
        active = await store.load_active()
        assert all(a.cycle_id != base.cycle_id for a in active)
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.needs_services
def test_load_active_excludes_closed_with_null_closed_ts():
    """A no-fill ARMED cancel closes with state=CLOSED but no native closed_ts — load_active must still exclude
    it (state is the authoritative terminal signal, not closed_ts alone)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.cycle_store import CycleEnvelopeStore

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = CycleEnvelopeStore(session_factory_=sf)
    row = EnvelopeRow(
        cycle_id="NULLCLOSE:CLI:MSFT.XNAS:MANUAL-001:100",
        account_id="NC",
        client_id="CLI",
        instrument_id="MSFT.XNAS",
        strategy_id="MANUAL-001",
        opened_ts=100,
        closed_ts=None,  # no native close (ARMED cancel)
        state="CLOSED",
        last_event_ts=5,
    )

    async def run():
        await _clear(sf, row.cycle_id)
        await store.upsert(row)
        active = await store.load_active()
        assert all(a.cycle_id != row.cycle_id for a in active)  # excluded by the state != CLOSED filter
        await engine.dispose()

    asyncio.run(run())
