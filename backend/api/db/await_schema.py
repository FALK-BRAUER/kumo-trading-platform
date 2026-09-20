"""Block until the schema is at head, WITHOUT applying it (#205).

The api owns migrations — its command is `python -m api.db.migrate && exec python -m api`. The engine
has no such gate and starts in parallel, held back only by `postgres: condition: service_healthy`.
Healthy is not migrated. So `up -d --build` can start an engine whose code selects columns the api has
not created yet: 0011 shipped `sessions_held` / `sessions_since_high` / `quality` together with the
strategy package that reads them, and the failure would land mid-session as an `UndefinedColumn` on a
path holding real positions.

This deliberately does NOT migrate. Two containers racing `alembic upgrade head` is a worse problem
than the one being fixed — one writer, one waiter. The engine waits for the api to finish, then boots.

It also does not give up. A container that exits here would crash-loop; one that waits recovers by
itself the moment the api completes, and says once per attempt what it is waiting for. Same shape as
`migrate.wait_for_db`, and it reuses that function for the connection half.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from api.db.engine import database_url
from api.db.migrate import _alembic_config, _safe_url, wait_for_db

_log = logging.getLogger("kumo.api.await_schema")

RETRY_SECONDS = 5.0


def head_revision() -> str:
    """The revision this image's code expects — read from the revision tree it ships with."""
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def known_revisions() -> set[str]:
    """Every revision in this image's history, head back to base.

    Used to tell "the api has not migrated YET" from "the database is at a revision this image has
    never heard of". Only the first is worth waiting for.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    return {rev.revision for rev in script.walk_revisions()}


async def _current() -> str | None:
    """The revision the DATABASE is at, or None if it has never been migrated."""
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
            return row[0] if row else None
    finally:
        await engine.dispose()


def wait_for_head(retry_seconds: float = RETRY_SECONDS) -> None:
    head = head_revision()
    known = known_revisions()
    attempt = 0
    while True:
        attempt += 1
        try:
            at = asyncio.run(_current())
        except Exception as exc:                                        # noqa: BLE001
            # Includes the first-ever boot, where `alembic_version` does not exist yet. Indistinguishable
            # from "not migrated" for our purposes, and both resolve the same way: keep waiting.
            at = None
            if attempt == 1:
                _log.info("schema not readable yet (%s) — waiting for the api to migrate", exc)
        if at == head:
            _log.info("schema at head (%s) — starting", head)
            return
        if at is not None and at not in known:
            # The database is at a revision this image has never heard of — a rollback to an older image
            # against a newer database. Waiting cannot fix it: nothing moves a schema BACKWARDS, so the
            # gate would hold the engine down forever over a condition no operator action resolves.
            # Migrations here add columns rather than remove them, so older code against a newer schema
            # is the survivable direction. Start, and say so loudly enough to be found in the logs.
            _log.error("database is at %s, which this image does not know (its head is %s) — looks like "
                       "a rollback onto a newer schema. Starting anyway: waiting could never clear this, "
                       "and old code on a new schema is the survivable direction. VERIFY THIS IMAGE IS "
                       "THE ONE YOU MEANT TO RUN.", at, head)
            return
        _log.warning("schema is at %s, this image needs %s (attempt %d) — waiting %.0fs for the api "
                     "to apply it", at or "nothing", head, attempt, retry_seconds)
        time.sleep(retry_seconds)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")
    wait_for_db()
    _log.info("postgres reachable at %s — checking schema revision", _safe_url())
    wait_for_head()
    return 0


if __name__ == "__main__":
    sys.exit(main())
