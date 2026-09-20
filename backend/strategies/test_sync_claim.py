"""#829 — the claim follows the venue's answer. Both gateways accept the strategy's positional
`sync_claim(symbol, qty, px)` and turn it into exactly one ledger statement, or none.

Measured 2026-09-09 13:35:16 UTC: `BUY 260 LAND submitted via nautilus (initialized)` at .331,
`terminal: LAND rejected — MOMENTUM-002 has 0 of budget left` at .334, and `exec_position_state`
read MOMENTUM-002/LAND=260 for a position that never existed. The installed strategies
(issue 122) call `runner.sync_claim(sym, lane_qty, px)` from every terminal handler; the
denied entry arrives here as `(LAND, 0, None)` and must DELETE the row.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.sql.dml import Delete, Insert

from strategies.momentum import SessionGateway as Momentum
from strategies.qc345 import QC345SessionGateway as QC345


class _Journal:
    """Records every statement `record_claim`/`drop_claim` execute, the way PgJournal's sessionmaker
    would carry them to Postgres."""

    def __init__(self):
        self.executed = []

    def sessionmaker(self):
        journal = self

        class _S:
            @asynccontextmanager
            async def begin(self):
                yield

            async def execute(self, stmt, params=None):
                journal.executed.append(stmt)

        @asynccontextmanager
        async def _cm():
            yield _S()

        return _cm()


def _dml(executed):
    """The ledger statements minus the key lock `store.write_claim` takes first (issue 124):
    this file is about WHICH DML a terminal event becomes, and the lock is every write's preamble."""
    from sqlalchemy.sql.elements import TextClause
    return [s for s in executed if not (isinstance(s, TextClause) and "pg_advisory_xact_lock" in str(s))]


def _gateway(cls):
    g = object.__new__(cls)
    g._journal = _Journal()
    g._strategy_id = "MOMENTUM-002"
    return g


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_the_contract_is_three_positional_arguments_like_record_terminal(cls):
    params = [p for p in inspect.signature(cls.sync_claim).parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["symbol", "qty", "px"]
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in params)


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_denied_entry_DELETES_the_claim(cls):
    g = _gateway(cls)
    asyncio.run(g.sync_claim("LAND", 0, None))
    assert [type(s) for s in _dml(g._journal.executed)] == [Delete]
    stmt = _dml(g._journal.executed)[0]
    assert "strategy_id" in str(stmt) and "symbol" in str(stmt), "a DELETE missing either key clears other lanes"


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_fill_RECORDS_the_lanes_quantity(cls):
    g = _gateway(cls)
    asyncio.run(g.sync_claim("LAND", 260, 9.6))
    assert [isinstance(s, Insert) for s in _dml(g._journal.executed)] == [True]


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_rejection_while_the_lane_still_holds_some_leaves_the_claim_alone(cls):
    """No price to insert with; the submit-time claim stands (over-claim narrows OTHER lanes)."""
    g = _gateway(cls)
    asyncio.run(g.sync_claim("LAND", 5, None))
    assert g._journal.executed == []


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_broken_ledger_never_raises_into_the_event_handler(cls):
    g = _gateway(cls)

    class _Broken:
        def sessionmaker(self):
            raise RuntimeError("postgres away")

    g._journal = _Broken()
    asyncio.run(g.sync_claim("LAND", 0, None))          # must return, not raise
