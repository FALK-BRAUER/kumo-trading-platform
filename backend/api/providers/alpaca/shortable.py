"""Alpaca's shortability plane (#1102 d, umbrella #854): `feed.shortable_provider` from `/v2/assets`.

WHAT ALPACA CAN SAY. An asset row carries `tradable`, `shortable` and `easy_to_borrow` — and NO
fee. Measured 2026-09-18 across CRSISHORT's 130 names: 68 shortable, 62 not, 0 shortable-but-
hard-to-borrow; account `shorting_enabled: true`. So the answer per name is availability, never a
rate — exactly the protocol kumo-trading-strategies defined and the IB plane already speaks
(`ibkr_shortable.py`): `LOCATABLE` (identity) for "available, fee unknown", `None` for "no locate",
NEVER a number (0.0 would typecheck, pass the fee ceiling and read as free borrow).

THREE STATES PER NAME, never two. LOCATABLE only when the table is FRESH and the row is tradable
AND shortable AND easy_to_borrow. Everything else is None BY NAME: not shortable (HUBC), shortable-
but-HTB (the fee is unknown, and a name whose ceiling cannot be evaluated is refused rather than
submitted blind), unknown to the table, stale table, table never loaded. `health()` says which of
the last three, in the words the IB plane uses, plus the load time and row count so a readback can
say "table of N at HH:MM".

ONE LOAD, NOT ONE PER NAME. `subscribe` marks the instrument and makes sure the asset table is
loaded (`list_assets`, ~11k rows, one request); it reloads only when older than `max_age_ns` (26 h
— the flags are a daily fact; 130 subscriptions at boot must not be 130 requests against a
200/min limit). `subscribe` RAISES when the load fails: the engine's subscribe pass records and
retries a failed subscription (`_subscribe_short_lanes`), which is the loud path; a swallowed load
failure would read as "no name is shortable" with no line saying why.

THE SAME VERBS the engine already calls on IB's plane (engine_node.py `attach_shortable`,
`_subscribe_short_lanes`, health) — `subscribe(instrument)`, `borrow_rates(now_ns)`, `health()` —
so `_attach_shortable_plane` wires it with no engine change, and `/health.shortable` reads one
vocabulary on both instances.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from nautilus_trader.model.identifiers import InstrumentId

from api.providers.alpaca.http import AlpacaHttpClient

_log = logging.getLogger(__name__)

def _locatable():
    """The lane's contract token, or None when the installed kumo-trading-strategies has no CRSISHORT
    module. IMPORTED AT CALL TIME, AND SAID — the IB plane's reason (ibkr_shortable.py): on a pin
    without the module a module-level import is an engine crash-loop at boot. A NUMBER is refused
    outright: 0.0 would typecheck, pass the fee ceiling and read as free borrow."""
    try:
        from kumo_strategies.strategies.crsi_short import LOCATABLE
    except ImportError:
        return None
    if isinstance(LOCATABLE, (int, float)):  # pragma: no cover — upstream contract, asserted there too
        raise RuntimeError("LOCATABLE is a number in the installed kumo-trading-strategies — refusing to "
                           "publish availability as a fee")
    return LOCATABLE


#: A daily fact: Alpaca republishes the asset flags once a day. Older than this is stale, and a
#: stale yes is not a yes.
DEFAULT_MAX_AGE_NS = 26 * 3600 * 1_000_000_000


@dataclass(frozen=True)
class _Row:
    tradable: bool
    shortable: bool
    easy_to_borrow: bool

    @property
    def locatable(self) -> bool:
        return self.tradable and self.shortable and self.easy_to_borrow


class AlpacaShortablePlane:
    """What the engine holds: vendor-neutral verbs over the Alpaca asset table."""

    def __init__(self, http: AlpacaHttpClient, *, now_ns: Callable[[], int],
                 max_age_ns: int = DEFAULT_MAX_AGE_NS) -> None:
        self._http = http
        self._now_ns = now_ns
        self._max_age_ns = int(max_age_ns)
        self._rows: dict[str, _Row] = {}
        self._loaded_at_ns: int | None = None
        self._subscribed: set[str] = set()
        #: Whether the installed kumo-trading-strategies carries the LOCATABLE contract the plane speaks —
        #: read off the installed package, "present" | "absent" (the IB plane's words).
        self.contract: str = "absent" if _locatable() is None else "present"
        #: `_schedule_shortable` reads `plane.client._loop` as a fallback loop; Alpaca's plane has
        #: no client of its own — the engine's loop is what schedules the subscription.
        self.client = None

    # -- the table -------------------------------------------------------------------------------
    def _fresh(self, now: int) -> bool:
        return self._loaded_at_ns is not None and (now - self._loaded_at_ns) <= self._max_age_ns

    async def _ensure_loaded(self) -> None:
        now = self._now_ns()
        if self._fresh(now):
            return
        rows = await self._http.list_assets(status="active", asset_class="us_equity")
        table: dict[str, _Row] = {}
        for r in rows or []:
            sym = str(r.get("symbol") or "").strip()
            if not sym:
                continue
            table[sym] = _Row(tradable=bool(r.get("tradable")), shortable=bool(r.get("shortable")),
                              easy_to_borrow=bool(r.get("easy_to_borrow")))
        # REPLACED, NOT MERGED: a name that left the active list is gone from the table and reads
        # None (unknown) — a delisted holding must not keep yesterday's yes.
        self._rows = table
        self._loaded_at_ns = self._now_ns()
        _log.info("alpaca shortable table loaded: %d assets", len(table))

    # -- the engine's verbs ----------------------------------------------------------------------
    async def refresh_if_stale(self) -> bool:
        """Reload the table when it is older than `max_age_ns`; the engine's catch-up pass calls
        this every tick (it is the only periodic hook on the plane). True when a load happened."""
        if self._fresh(self._now_ns()):
            return False
        await self._ensure_loaded()
        return True

    async def subscribe(self, instrument) -> None:
        """Mark the name as asked and make sure the table is loaded. RAISES on a failed load."""
        iid = getattr(instrument, "id", instrument)
        sym = iid.symbol.value if isinstance(iid, InstrumentId) else str(iid).split(".")[0]
        self._subscribed.add(sym)
        await self._ensure_loaded()

    def borrow_rates(self, *, now_ns: Callable[[], int] | None = None) -> Callable[[list[str]], dict]:
        """`symbols -> {symbol: LOCATABLE | None}` in kumo-trading-strategies' protocol."""
        clock = now_ns or self._now_ns
        LOCATABLE = _locatable()
        if LOCATABLE is None:
            self.contract = "absent"

            def refusing(symbols) -> dict:
                raise RuntimeError(
                    "shortable provider called but the installed kumo-trading-strategies has no "
                    "kumo_strategies.strategies.crsi_short — no LOCATABLE contract to answer in. "
                    "The pin predates CRSISHORT; /health.shortable.contract says 'absent'.")

            return refusing
        self.contract = "present"

        def provider(symbols) -> dict:
            fresh = self._fresh(clock())
            out: dict = {}
            for sym in symbols:
                row = self._rows.get(str(sym))
                out[sym] = LOCATABLE if (fresh and row is not None and row.locatable) else None
            return out

        return provider

    def health(self, *, now_ns: int | None = None) -> dict:
        """Three states over the table, in the IB plane's words, plus the load time and row count."""
        now = self._now_ns() if now_ns is None else int(now_ns)
        subscribed = len(self._subscribed)
        loaded = self._loaded_at_ns is not None
        fresh = self._fresh(now)
        answered = sum(1 for s in self._subscribed if s in self._rows) if loaded else 0
        never = subscribed - answered
        stale = answered if (loaded and not fresh) else 0
        if subscribed == 0:
            state = "never_subscribed"
        elif not loaded:
            state = "never_loaded"
        elif not fresh:
            state = "stale"
        elif never:
            state = "partial"
        else:
            state = "complete"
        return {"contract": self.contract, "subscribed": subscribed, "answered": answered,
                "stale": stale, "never": never, "state": state,
                "loaded_at_ns": self._loaded_at_ns, "assets": len(self._rows)}


def build_plane(http: AlpacaHttpClient, now_ns: Callable[[], int]) -> AlpacaShortablePlane:
    """The spec's factory target. Never None: Alpaca always has the asset table; a failed load is
    reported per subscription and in `health()`, not by refusing to build."""
    return AlpacaShortablePlane(http, now_ns=now_ns)
