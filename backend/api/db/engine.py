"""Async engine + session factory for the app-data Postgres.

`KUMO_DATABASE_URL` selects the DB (compose injects the in-network URL; the local default targets a
localhost Postgres for dev). The engine connects lazily — importing this module opens no socket, so the
app boots even if Postgres is momentarily down (endpoints that use the DB surface the error themselves).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Local-dev default; overridden in every deployed stack via env.
_DEFAULT_URL = "postgresql+asyncpg://kumo:kumo@localhost:5432/kumo"


def database_url() -> str:
    return os.environ.get("KUMO_DATABASE_URL", _DEFAULT_URL)


#: CHOSEN, NOT INHERITED (#542). Without these two, SQLAlchemy defaults to 5 + 10 overflow — which is
#: exactly the ceiling that exhausted on 2026-08-25:
#:
#:     cycle-envelope write failed: TimeoutError('QueuePool limit of size 5 overflow 10 reached,
#:                                               connection timed out, timeout 30.00')
#:
#: The engine's concurrent DB users are not a mystery: five lanes, the reconciler, the trade-cycle
#: projection, the sleeve ledger, the journal and the alerting loop — each able to hold a connection
#: across an await. Five was never going to be enough, and nobody had weighed it because nobody had
#: written it down.
#:
#: RAISING IT IS HALF A FIX. Measured at rest after the exhaustion: `checked out: 0` — idle, so
#: nothing leaks permanently and the failure was a BURST under startup load. But a single idle reading
#: does not distinguish a burst from a slow leak, so `pool_stats()` below exists to make the next one
#: diagnosable rather than inferred. A bigger pool without that would hide a leak instead of fixing a
#: ceiling.
_POOL_SIZE = int(os.environ.get("KUMO_DB_POOL_SIZE", "20"))
_POOL_MAX_OVERFLOW = int(os.environ.get("KUMO_DB_POOL_MAX_OVERFLOW", "10"))

# `pool_pre_ping` recycles dead connections (a Postgres restart doesn't wedge the pool).
engine = create_async_engine(
    database_url(),
    pool_pre_ping=True,
    pool_size=_POOL_SIZE,
    max_overflow=_POOL_MAX_OVERFLOW,
)


def pool_stats() -> dict:
    """Current pool occupancy, for health. NEVER RAISES — unknown is reported as None.

    A pool that climbs monotonically is a LEAK; one that spikes at known moments is a SIZING problem.
    From outside those are identical, which is why #542 could not be answered when it was filed.

    Read from the health path, so it must not be able to take health down: an observability call that
    kills the thing it reports on is the #377 shape.
    """
    pool = engine.pool

    def _try(fn):
        try:
            return int(fn())
        except Exception:  # noqa: BLE001 — unknown is an answer; raising here would blank /health
            return None

    return {
        "size": _try(pool.size),
        "checked_out": _try(pool.checkedout),
        "overflow": _try(pool.overflow),
        "max_overflow": _POOL_MAX_OVERFLOW,
    }
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — one session per request, committed on success, rolled back on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
