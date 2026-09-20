"""Dependency health probes (#26 extension) — the api checks every system it depends on, at startup AND
live at runtime, so a down/degraded dependency is NAMED, never a silent half-broken cockpit.

Split by the #20 process boundary:
- **Directly probeable** by the api process: Redis (the UI bus) + Postgres (app data). Real connect+ping.
- **Only observable** (separate engine process): the engine + market-data feed — reported from the health
  frames the engine publishes over Redis (see `RedisConsumer.health()`), not probed here.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
from sqlalchemy import text

from api.db.engine import engine as _pg_engine


async def check_redis(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    """Connect + PING Redis. Returns (ok, detail)."""
    client = aioredis.Redis(host=host, port=port, socket_connect_timeout=timeout, socket_timeout=timeout)
    try:
        await asyncio.wait_for(client.ping(), timeout=timeout)
        return True, ""
    except Exception as exc:  # noqa: BLE001 — any failure = down, report it
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await client.aclose()


async def check_postgres(timeout: float = 2.0) -> tuple[bool, str]:
    """Open a connection + `SELECT 1` against the app-data DB. Returns (ok, detail)."""
    try:
        async with asyncio.timeout(timeout):
            async with _pg_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
