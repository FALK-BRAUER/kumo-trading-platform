"""Apply app-data migrations at container start, tolerating a Postgres that isn't up yet.

`docker compose up` gates the api on `postgres: condition: service_healthy`, but Docker restarting a
container on its own — Docker Desktop relaunch, `docker start`, host reboot — ignores `depends_on`. The
api then boots with no Postgres and a bare `alembic upgrade head` dies on an unresolvable host with a raw
traceback and exit 1, which Docker turns into a silent crash-loop (2026-07-28: api at 14 restarts while
engine + ui sat up, so the UI rendered with a dead backend).

So wait explicitly: ONE clear log line per attempt, retry every 10s, migrate the moment Postgres answers.
A migration that fails on its own merits (bad SQL, multiple heads) still exits non-zero — only connection
failures are retried, because those are the ones that fix themselves.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from api.db.engine import database_url

_log = logging.getLogger("kumo.api.migrate")

RETRY_SECONDS = 10.0

# `alembic.ini` + the revision tree live at the backend root (/app in the image). Resolved from this file,
# not from the cwd, so the module runs the same from /app, from backend/, or from a test.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# "Postgres isn't there YET" — retry. OSError covers socket.gaierror (unresolvable compose hostname — the
# exact failure when the api starts without its postgres container) and connection-refused.
_UNREACHABLE = (OperationalError, InterfaceError, OSError)


def _safe_url() -> str:
    """DB URL with the password masked — this string goes to logs."""
    return make_url(database_url()).render_as_string(hide_password=True)


async def _ping() -> None:
    """One throwaway connection. NullPool so this short-lived process leaves nothing behind."""
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def wait_for_db(retry_seconds: float = RETRY_SECONDS) -> None:
    """Block until Postgres answers. Logs every failed attempt; never gives up (the container stays alive
    and recovers on its own the moment the DB appears, instead of crash-looping)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            asyncio.run(_ping())
            return
        except _UNREACHABLE as exc:
            _log.error(
                "postgres unreachable at %s (attempt %d): %s — retrying in %.0fs",
                _safe_url(),
                attempt,
                exc,
                retry_seconds,
            )
            time.sleep(retry_seconds)


def _alembic_config() -> Config:
    # `configure_logger=False`: env.py's fileConfig would otherwise disable the logger above mid-run.
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"), attributes={"configure_logger": False})
    # Absolute script_location — the relative default in alembic.ini resolves against the cwd.
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return cfg


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")
    wait_for_db()
    _log.info("postgres reachable at %s — applying migrations", _safe_url())
    command.upgrade(_alembic_config(), "head")
    _log.info("app-data migrations at head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
