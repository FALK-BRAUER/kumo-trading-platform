"""In-process instrument-search catalog for the api process (#25).

The api process runs no Nautilus node in `live` mode (only `RedisConsumer`, #20 split), so it can't query
the engine's instrument cache. It loads its OWN catalog via the native `AlpacaInstrumentProvider` — the
universe as Nautilus `Equity` domain objects (canonical `TICKER.MIC` ids, company name in `Equity.info`),
searched in-process. This is a metadata catalog only: `load_all_async()` subscribes to no market data;
live bars stay on-demand per focused symbol elsewhere.

Search over ~13k US equities is a flat linear scan over pre-lowercased fields (well under 50ms warm) — no
trie needed at this size. The catalog is TTL-refreshed from one hourly `/v2/assets` REST call.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from api.models import InstrumentMatch
from api.providers.alpaca.config import AlpacaDataClientConfig
from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.alpaca.providers import AlpacaInstrumentProvider

_KEY_ENV = "APCA_API_KEY_ID"
_SECRET_ENV = "APCA_API_SECRET_KEY"

_DEFAULT_TTL = 3600.0  # catalog refresh interval (s) — the tradable universe changes slowly
_FAIL_BACKOFF = 30.0  # after a reload failure, serve stale for this long before retrying (no retry storm)
_MAX_LIMIT = 50


@dataclass(frozen=True)
class _Record:
    """One catalog row, pre-lowercased for the hot scan."""

    instrument_id: str
    symbol: str
    symbol_lc: str
    name: str
    name_lc: str
    venue: str


def build_search_client(provider_config: dict) -> AlpacaHttpClient:
    """Build the (unconnected) Alpaca REST client for search, mirroring `data_client.build_data`.

    Env var NAMES come from the `[data.alpaca]` table (`key_env`/`secret_env`), resolved via `os.environ`;
    base URLs default from `AlpacaDataClientConfig` unless the table overrides them. Raises if keys are
    unset — the caller degrades that to a search-unavailable state (503), never a hard app-startup failure.
    """
    key_env = provider_config.get("key_env", _KEY_ENV)
    secret_env = provider_config.get("secret_env", _SECRET_ENV)
    api_key = os.environ.get(key_env)
    api_secret = os.environ.get(secret_env)
    if not api_key or not api_secret:
        raise RuntimeError(f"{key_env}/{secret_env} unset — instrument search unavailable")
    defaults = AlpacaDataClientConfig()
    return AlpacaHttpClient(
        key=api_key,
        secret=api_secret,
        trading_base=provider_config.get("trading_base_url", defaults.trading_base_url),
        data_base=provider_config.get("data_base_url", defaults.data_base_url),
    )


def _tier(q_lc: str, rec: _Record) -> int | None:
    """Ranking tier (lower = better), or None if `rec` doesn't match `q_lc`."""
    if rec.symbol_lc == q_lc:
        return 1
    if rec.symbol_lc.startswith(q_lc):
        return 2
    if q_lc in rec.symbol_lc:
        return 3
    if rec.name_lc == q_lc:
        return 4
    if rec.name_lc.startswith(q_lc):
        return 5
    if q_lc in rec.name_lc:
        return 6
    return None


def rank_matches(q: str, rows: list[dict]) -> list[dict]:
    """Rank plain match rows with the SAME tiers the Alpaca index uses, so two providers rank the same
    rows identically (#837). Rows that match no tier are dropped."""
    q_lc = (q or "").strip().lower()
    scored = []
    for row in rows:
        rec = _Record(instrument_id=row["instrument_id"], symbol=row["symbol"], symbol_lc=row["symbol"].lower(),
                      name=row["name"], name_lc=row["name"].lower(), venue=row["venue"])
        tier = _tier(q_lc, rec)
        if tier is not None:
            length = len(rec.symbol) if tier <= 3 else len(rec.name)
            scored.append((tier, length, rec.symbol_lc, rec.instrument_id, row))
    scored.sort(key=lambda s: s[:4])
    return [row for *_, row in scored]


class InstrumentSearchIndex:
    """TTL-refreshed instrument catalog + ranked search. Owns one connected `AlpacaHttpClient`.

    Lifecycle: `await start()` (connects the client) → `warm()` as a background task (non-blocking; a load
    failure just leaves search 503 until a later request succeeds) → `await close()` on shutdown. `search()`
    lazily (re)loads if the catalog is missing/stale, so it works even if the warm task hasn't finished.
    """

    def __init__(
        self,
        client: AlpacaHttpClient,
        ttl_seconds: float = _DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._records: list[_Record] | None = None  # None = never successfully loaded
        self._loaded_at: float = 0.0
        self._last_fail_at: float | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        await self._client.close()

    def _fresh(self) -> bool:
        return self._records is not None and (self._clock() - self._loaded_at) <= self._ttl

    async def _reload(self) -> None:
        """Rebuild the catalog from a FRESH provider. A provider only `add()`s and never clears, so reusing
        one would retain delisted/moved assets — build a new one each time and swap the derived index in."""
        provider = AlpacaInstrumentProvider(self._client)  # config slot = Nautilus config; leave default
        await provider.load_all_async()
        records = [
            _Record(
                instrument_id=str(instr.id),
                symbol=str(instr.raw_symbol),
                symbol_lc=str(instr.raw_symbol).lower(),
                name=(instr.info or {}).get("name") or str(instr.raw_symbol),
                name_lc=((instr.info or {}).get("name") or str(instr.raw_symbol)).lower(),
                venue=str(instr.id.venue),
            )
            for instr in provider.get_all().values()
        ]
        self._records = records  # atomic swap — readers see the old list until this assignment
        self._loaded_at = self._clock()

    async def _ensure_loaded(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():  # double-check inside the lock: concurrent waiters don't re-reload
                return
            now = self._clock()
            if (
                self._records is not None
                and self._last_fail_at is not None
                and now - self._last_fail_at < _FAIL_BACKOFF
            ):
                return  # recent failure + we have stale data → serve stale, don't hammer upstream
            try:
                await self._reload()
                self._last_fail_at = None
            except Exception:
                self._last_fail_at = self._clock()
                if self._records is None:
                    raise  # first-ever load failed, no data to serve → endpoint returns 503
                # else: keep the stale catalog

    async def warm(self) -> None:
        """Background pre-load. Swallows failures — search 503s until a later request reloads successfully."""
        try:
            await self._ensure_loaded()
        except Exception:
            pass

    def known_symbols(self) -> set[str] | None:
        """Every symbol the venue lists, or None if the catalog has never loaded (#663).

        SYNCHRONOUS AND NON-LOADING on purpose. Pool validation runs on a request path that must not
        block on a REST call, and `None` is a real answer there: "the catalog could not be consulted"
        must stay distinguishable from "the venue does not list this", or a failed reload would
        refuse an entire pool and take every lane out of the market.
        """
        records = self._records
        if records is None:
            return None
        return {r.symbol.upper() for r in records}

    async def search(self, q: str, limit: int) -> list[InstrumentMatch]:
        """Ranked matches for `q`, best first, capped at `limit` (clamped 1..50). Empty/blank `q` → []."""
        q = (q or "").strip()
        if not q:
            return []
        await self._ensure_loaded()
        records = self._records or []
        q_lc = q.lower()
        scored: list[tuple[int, int, str, str, _Record]] = []
        for rec in records:
            tier = _tier(q_lc, rec)
            if tier is not None:
                # tie-break: shorter symbol (symbol tiers) / shorter name (name tiers) → symbol alpha → id
                length = len(rec.symbol) if tier <= 3 else len(rec.name)
                scored.append((tier, length, rec.symbol_lc, rec.instrument_id, rec))
        scored.sort(key=lambda t: t[:4])
        limit = max(1, min(limit, _MAX_LIMIT))
        return [
            InstrumentMatch(instrument_id=r.instrument_id, symbol=r.symbol, name=r.name, venue=r.venue)
            for *_, r in scored[:limit]
        ]


_ENGINE_SEARCH_TTL = 60.0      # per-prefix result cache: the UI asks once per keystroke, IB paces ~1/s
_ENGINE_SEARCH_TIMEOUT = 4.0   # IB answers reqMatchingSymbols in ~0.3-1 s; past this the engine is not answering
_ENGINE_SEARCH_POLL = 0.05


class EngineSearchIndex:
    """Instrument search that asks the ENGINE, for every provider that is not Alpaca (#837).

    The broker supplies its own data (CLAUDE.md 2026-09-09), and the engine already holds the
    connected IB client. So a query is one `search_instruments` command over the bus every other
    UI command already uses, and the engine's ack carries the rows. Same surface as
    `InstrumentSearchIndex`, so the endpoint does not know which one it holds.

    THREE STATES. Rows, no rows, and never-answered are three different facts: a silent engine or an
    ack without `results` RAISES, which the endpoint renders as 503 — never as an empty list, which
    the UI would read as "the venue lists nothing like that".
    """

    def __init__(
        self,
        node,
        ttl_seconds: float = _ENGINE_SEARCH_TTL,
        timeout_s: float = _ENGINE_SEARCH_TIMEOUT,
        poll_s: float = _ENGINE_SEARCH_POLL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._node = node
        self._ttl = ttl_seconds
        self._timeout = timeout_s
        self._poll = poll_s
        self._clock = clock
        self._cache: dict[tuple[str, int], tuple[float, list[InstrumentMatch]]] = {}

    async def start(self) -> None:  # nothing to connect: the engine owns the venue session
        return None

    async def close(self) -> None:
        return None

    async def warm(self) -> None:
        return None

    def known_symbols(self) -> set[str] | None:
        """None: the venue catalog cannot be consulted this way (IB has no list-all). Pool validation
        (#663) treats None as "could not be consulted", never as "the venue lists nothing"."""
        return None

    async def search(self, q: str, limit: int) -> list[InstrumentMatch]:
        q_norm = (q or "").strip().lower()
        if not q_norm:
            return []
        limit = max(1, min(int(limit), _MAX_LIMIT))
        key = (q_norm, limit)
        hit = self._cache.get(key)
        if hit is not None and (self._clock() - hit[0]) <= self._ttl:
            return list(hit[1])
        cid = await self._node.send_command("search_instruments", {"q": q_norm, "limit": limit})
        deadline = self._clock() + self._timeout
        ack = self._node.command_status(cid)
        while ack is None and self._clock() < deadline:
            await asyncio.sleep(self._poll)
            ack = self._node.command_status(cid)
        if ack is None:
            raise RuntimeError(f"engine did not answer search {cid} within {self._timeout:.1f}s")
        if ack.get("status") != "ok":
            raise RuntimeError(f"engine refused search: {ack.get('error') or 'no reason given'}")
        rows = ack.get("results")
        if rows is None:
            raise RuntimeError("engine ack carried no results — not the same as no matches")
        out = [InstrumentMatch(**row) for row in rows][:limit]
        self._cache[key] = (self._clock(), out)
        return list(out)
