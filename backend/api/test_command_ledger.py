"""Tests for the command idempotency ledger (#78). Pure hash test offline; the reserve/dedup round-trip is a
`needs_services` integration test (real Postgres) — it NEVER submits an order, it only exercises the ledger."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from api.command_ledger import Reserve, payload_hash


def test_payload_hash_is_stable_and_order_independent():
    a = payload_hash({"x": 1, "y": 2})
    b = payload_hash({"y": 2, "x": 1})
    assert a == b
    assert payload_hash({"x": 1, "y": 3}) != a


@pytest.mark.needs_services
def test_reserve_dedups_by_command_id_and_client_order_id():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.command_ledger import CommandLedgerStore

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = CommandLedgerStore(session_factory_=sf)

    async def run():
        async with sf() as s:  # hermetic: drop this test's rows
            await s.execute(
                text("DELETE FROM command_ledger WHERE command_id = ANY(:ids)"),
                {"ids": ["cmd-1", "cmd-2", "cmd-3", "sr-1", "sr-2"]},
            )
            await s.commit()

        # First sight → RESERVED.
        assert (await store.reserve("cmd-1", "submit_order", "COID-1", "h1", "e1")).outcome is Reserve.RESERVED
        # Same command_id redelivered → DUPLICATE_COMMAND, mirroring the prior status (still RESERVED here).
        dup = await store.reserve("cmd-1", "submit_order", "COID-1", "h1", "e1")
        assert dup.outcome is Reserve.DUPLICATE_COMMAND and dup.existing_status == "RESERVED"
        assert dup.hash_mismatch is False
        # Same command_id, DIFFERENT payload → hash_mismatch flagged (an anomaly the caller rejects).
        assert (await store.reserve("cmd-1", "submit_order", "COID-1", "hX", "e1")).hash_mismatch is True
        # Different command_id, SAME client_order_id → DUPLICATE_CLIENT_ORDER_ID (economic dup).
        assert (
            await store.reserve("cmd-2", "submit_order", "COID-1", "h2", "e2")
        ).outcome is Reserve.DUPLICATE_CLIENT_ORDER_ID
        # A genuinely new order → RESERVED.
        assert (await store.reserve("cmd-3", "submit_order", "COID-2", "h3", "e3")).outcome is Reserve.RESERVED
        # Non-order commands (null client_order_id) never collide with each other.
        assert (await store.reserve("sr-1", "stream_request", None, "h4", "e4")).outcome is Reserve.RESERVED
        assert (await store.reserve("sr-2", "stream_request", None, "h5", "e5")).outcome is Reserve.RESERVED

        # Mark DONE → a later redelivery mirrors DONE (idempotent success, not a lost order).
        await store.mark("cmd-1", "DONE")
        done = await store.reserve("cmd-1", "submit_order", "COID-1", "h1", "e1")
        assert done.outcome is Reserve.DUPLICATE_COMMAND and done.existing_status == "DONE"
        await engine.dispose()

    asyncio.run(run())
