"""Symbol-pool queries for the cockpit (#79 follow-on).

The pool is what MOMENTUM ranks each session: a union of sources (ledger_book, my_watchlist) with an
operator whitelist/blacklist layered on top. Both live in the executor's Postgres tables.

This module drives `PgSymbolPool` rather than querying `exec_pool_*` directly. The rules — a pin
survives every source refresh, an exclude beats every source, provenance is derived from which
sources carry a symbol — are strategy-side domain logic and belong in kumo-trading-strategies. Re-deriving
them here would be the same duplication that put a second web UI in the strategies repo: two
implementations of one rule, drifting apart the first time either changes.
"""

from __future__ import annotations

from api.db.engine import session_factory

PIN = "pin"
EXCLUDE = "exclude"


def _pool():
    """The pool bound to the cockpit's own session factory (its own pool, same database).

    Imported lazily, like `strategies.momentum` in the engine: `kumo_strategies` is installed in the
    deployed image but not in every local venv, and a module-level import would make the whole app
    unimportable — and every test uncollectable — on a checkout that only runs the API.
    """
    from kumo_strategies.runtime.executor.pgpool import PgSymbolPool

    return PgSymbolPool(session_factory)


async def list_pool(held: set[str] | None = None) -> list[dict]:
    """Every symbol the strategy can see, plus the blacklisted ones it deliberately cannot.

    Blacklisted symbols are appended rather than filtered out: a name you excluded is exactly the one
    you want to still SEE, otherwise the only evidence of the decision is its absence.
    """
    held = held or set()
    pool = _pool()
    effective = await pool.effective()

    rows = [
        {
            "symbol": symbol,
            "sources": sorted(entry.sources),
            "provenance": entry.provenance,
            "meta": entry.meta,
            "held": symbol in held,
            "override": PIN if entry.pinned else None,
            "reason": None,
        }
        for symbol, entry in effective.items()
    ]
    rows += [
        {
            "symbol": o["symbol"],
            "sources": [],
            "provenance": "blacklisted",
            "meta": {},
            "held": o["symbol"] in held,
            "override": EXCLUDE,
            "reason": o.get("reason") or None,
        }
        for o in await pool.overrides(EXCLUDE)
    ]
    return sorted(rows, key=lambda r: r["symbol"])


async def effective() -> dict:
    """The COMPOSED pool — source rows plus pins minus excludes — keyed by symbol (#723).

    A module-level delegate because `api.app` binds this MODULE (`from api import pool`), never the
    `PgSymbolPool` instance, and every other caller here reaches the instance through `_pool()`. The
    #723 sweep first called `pool.effective()` believing the name resolved to the instance; it does
    not, and the AttributeError raised on every poll — before the sweep's own catalog check, so the
    deliberately-silent third state never ran and every instance would have paged
    "the pool_unlisted check is broken" instead. Green tests hid it behind a double that HAD the
    method, which is the one thing a double must never do.

    Returns `PoolEntry` objects untouched. The sweep needs only the keys, but narrowing here would
    make this a second, lossier reader of the same composition rule — the duplication this module's
    own docstring exists to refuse.
    """
    return await _pool().effective()


async def set_override(symbol: str, kind: str, reason: str = "") -> None:
    """Pin or exclude a symbol. Takes effect at the next session, not immediately."""
    await _pool().set_override(symbol.upper(), kind, reason=reason, by="cockpit")


async def clear_override(symbol: str, kind: str) -> None:
    await _pool().clear_override(symbol.upper(), kind)


async def source_health() -> list[dict]:
    """Per-source freshness. A failed source is a hard block on deciding, so it belongs next to the
    pool it produced rather than buried in a log."""
    return [
        {
            "name": s.source,
            "status": s.status,
            "symbol_count": s.symbol_count,
            "refreshed_at": s.refreshed_at.isoformat() if s.refreshed_at else None,
            "stale": s.is_stale(),
            "detail": s.detail,
        }
        for s in await _pool().sources()
    ]


async def refresh_source(name: str, symbols, *, detail: str | None = None,
                         create: bool = False, allow_shrink: str | None = None) -> int:
    """Replace one source's whole symbol set. Returns the rows now held.

    DELEGATES; NEVER WRITES. `PgSymbolPool.refresh_source` is the only writer to `exec_pool_source`,
    and that is not a style preference: `PoolSource`'s own contract is "a refresh REPLACES a source's
    whole set", so two writers with replace semantics do not race — the loser's rows vanish. Cockpit
    owns the HTTP surface, the payload validation and the instance-specific script execution; the
    transaction stays in one place. Same consumer pattern as `own_ceiling` — imported, never copied.

    `create` DEFAULTS TO FALSE, DELIBERATELY, and that default is the whole reason this wrapper exists
    rather than the endpoint calling through directly. It is a PARAMETER rather than a constant because
    a fresh instance has to be able to create its first source: ibkr-paper-retired could not seed an empty
    pool at all, and an enabled rotation with no symbols takes the whole node down (2026-08-24). The
    caller must say so explicitly, which is exactly what upstream's own error message asks for. The upstream default is `True` because the scheduled refreshers
    are running against a live stack and a strict default would start failing any source lacking a
    health row — and a stale source is a HARD BLOCK on deciding, so that does not degrade a lane, it
    stops it deciding at all. A NEW surface has no such history, so it takes the strict rule from the
    first call: `refresh_source("typo_watchlist", …)` must not silently create a source that is fresh,
    real and feeds nothing while the one it was meant to refresh goes stale.

    `allow_shrink` is a REASON, never a flag. Upstream refuses a refresh losing more than half the
    previous set, which is what stops a truncated feed liquidating a book — and which would also block
    a followed trader GENUINELY going flat, the single most significant thing that source can say. So
    the escape exists, requires a stated reason, and lands in the health row's `detail`, where an
    unexplained empty set and an operator-justified one do not read the same afterwards.
    """
    return await _pool().refresh_source(
        name, symbols, detail=detail, create=create, allow_shrink=allow_shrink)
