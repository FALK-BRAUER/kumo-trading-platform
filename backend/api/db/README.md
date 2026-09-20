# api/db — app-data persistence (Postgres)

The storage-architecture **app-data** store. SQLAlchemy 2.0 async over asyncpg.

- `engine.py` — async engine + `session_factory` + `get_session` (FastAPI dependency). URL from
  `KUMO_DATABASE_URL` (local default = localhost Postgres).
- `base.py` — `Base` declarative base (Alembic reads `Base.metadata`).
- `models.py` — ORM models: `WatchlistItem` (runtime watchlist), `TradeCycleEnvelope` (#74 durable cycle
  boundary), `CommandLedgerEntry` (#78 order idempotency), `PositionTransferEvent` (#80 spin-off — internal
  position transfers between strategies), `Manager` + `ManagerEvent` (#55 — the generic attach/watch/trigger/
  act automation framework; see `api/managers.py`. Superseded the flatten-specific `queued_flatten` table,
  which the `0009_manager_framework` migration left in place, unused, for one release rather than dropping it
  mid-rollover).
- `migrate.py` — container start hook: wait for Postgres (retry every 10s, one log line per attempt),
  then `alembic upgrade head`. Run as `python -m api.db.migrate`.

## What goes here
APP data only: watchlists now; order history / notes / user prefs later. **NOT** trade state (Nautilus owns
it), bars (Parquet), or layout config (immutable TS/JSON — config-over-editor).

## Migrations (Alembic — `backend/alembic`)
```
cd backend && ./.venv/bin/alembic upgrade head          # apply
./.venv/bin/alembic revision --autogenerate -m "msg"    # new migration from model changes
```
The deployed api container runs `python -m api.db.migrate` on start (not bare `alembic upgrade head` —
Docker restarts a container without compose's `depends_on` gating, so the api can boot before Postgres and
must wait rather than crash-loop). `KUMO_DATABASE_URL` must be set.
