"""Alembic environment (async). Uses the app's own DB URL + metadata so migrations never drift from the
models. Run: `alembic upgrade head` (from backend/, with the venv + KUMO_DATABASE_URL set)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from api.db import models  # noqa: F401 — import registers tables on Base.metadata
from api.db.base import Base
from api.db.engine import database_url

config = context.config
# `configure_logger=False` lets a programmatic caller (api.db.migrate) keep its own logging setup —
# fileConfig disables every logger not named in alembic.ini. The CLI leaves it unset and configures as usual.
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
