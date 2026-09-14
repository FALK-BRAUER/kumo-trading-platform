"""App-data persistence (Postgres) — the storage-architecture app-data store.

Scope: app data (watchlists) + engine-owned cycle ENVELOPE metadata (#74). NOT native trade state — Nautilus
owns positions/orders/fills — and NOT a P&L ledger (P&L stays derived from native). The envelope (`trade_cycle`)
is a distinct THIRD category: the cycle boundary/lifecycle Nautilus can't express, written by the engine so it
rebuilds identical TradeDTO state on restart. Bars stay in Parquet. SQLAlchemy 2.0 async over asyncpg; schema
via Alembic (`backend/alembic`).
"""

from api.db.base import Base
from api.db.engine import database_url, get_session, session_factory

__all__ = ["Base", "database_url", "get_session", "session_factory"]
