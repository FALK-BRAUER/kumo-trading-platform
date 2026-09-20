"""Runtime watchlist queries (Postgres app-data). The watchlist is USER data — add/remove at runtime,
persisted — distinct from the immutable layout config (config-over-editor stays true for layout)."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import WatchlistItem


async def list_symbols(session: AsyncSession) -> list[str]:
    """Instrument ids on the watchlist, oldest-added first."""
    result = await session.execute(select(WatchlistItem).order_by(WatchlistItem.added_at))
    return [row.instrument_id for row in result.scalars().all()]


async def add_symbol(session: AsyncSession, instrument_id: str) -> None:
    """Add a symbol (idempotent — the unique index means re-adding is a no-op, not an error)."""
    existing = await session.execute(
        select(WatchlistItem.id).where(WatchlistItem.instrument_id == instrument_id)
    )
    if existing.scalar_one_or_none() is None:
        session.add(WatchlistItem(instrument_id=instrument_id))


async def remove_symbol(session: AsyncSession, instrument_id: str) -> None:
    await session.execute(delete(WatchlistItem).where(WatchlistItem.instrument_id == instrument_id))
