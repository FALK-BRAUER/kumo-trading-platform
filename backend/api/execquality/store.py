"""Persist execution-quality legs, and run the measurement once a day (#210).

Lives in the API process, not the engine. It is a read-only measurement — two Alpaca GETs and an
INSERT — and nothing here can submit, cancel, or influence an order. Putting it beside the engine
would mean a research job sharing a process with the thing holding real positions, for no benefit.

Runs AFTER THE CLOSE. Before that the day's fills may be incomplete, and the comparison would record
a partial picture as if it were the session. Idempotent on (session, symbol, side), so a repeat run,
a restart, or a manual invocation all converge on the same rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.db.models import ExecutionQualityLeg
from api.execquality.measure import Leg, summarise

_log = logging.getLogger("kumo.execquality")

#: How far back each run looks. Generous on purpose, and it is also how late-arriving data heals
#: itself: the first run after the close may see activities that are still settling, and tomorrow's
#: run re-reads the same week and inserts whatever was missing. A fixed "wait N minutes after the
#: close" delay would look more careful and would actually be more fragile — it has to be right on
#: the first attempt, and a restart during that window loses the day. The unique constraint makes
#: the overlap free.
LOOKBACK_DAYS = 7

_POLL_SECS = 300.0


async def record(session_factory, legs: list[Leg]) -> int:
    """Upsert legs. Returns the number of rows written or corrected.

    ON CONFLICT DO **UPDATE**, not DO NOTHING. The earlier version claimed the 7-day overlap would
    "heal late-arriving data"; it would not. DO NOTHING prevents duplicates and leaves a row computed
    from a partial set of fills wrong forever, which is worse than a duplicate — a silently incorrect
    measurement is indistinguishable from a correct one. Recomputing from the fuller picture is the
    whole point of re-reading the week.
    """
    if not legs:
        return 0
    rows = [{
        "session": x.session, "symbol": x.symbol, "side": x.side, "qty": x.qty,
        "fill_vwap": x.fill_vwap, "auction_px": x.auction_px,
        "auction_exchange": x.auction_exchange, "auction_size": x.auction_size,
        "first_fill_utc": x.first_fill_utc, "lag_minutes": x.lag_minutes,
        "drift_bps": x.drift_bps, "cost_usd": x.cost_usd,
    } for x in legs]
    async with session_factory() as db, db.begin():
        result = await db.execute(
            pg_insert(ExecutionQualityLeg).values(rows).on_conflict_do_update(
                index_elements=["session", "symbol", "side"],
                set_={c: pg_insert(ExecutionQualityLeg).excluded[c] for c in
                      ("qty", "fill_vwap", "auction_px", "auction_exchange", "auction_size",
                       "first_fill_utc", "lag_minutes", "drift_bps", "cost_usd")},
            ).returning(ExecutionQualityLeg.id))
        return len(result.fetchall())


class ExecutionQualityJob:
    """Once a day after the close, measure the drift and record it.

    Never raises out of `run`. A failure here must cost a research sample and nothing else — this is
    measurement infrastructure sitting in the same process as the API the operator relies on.
    """

    def __init__(self, session_factory, http, clock=None, recorder=None,
                 strategy_id: str = "MOMENTUM-002") -> None:
        self._sf = session_factory
        self._strategy_id = strategy_id
        self._http = http
        self._clock = clock or (lambda: datetime.now(UTC))
        # None until the first observation. Recording requires an OPEN→CLOSED transition, not merely
        # "the market is shut": a long-running process sees `is_open=false` all weekend and every
        # premarket, and keying off the date alone would let Monday premarket mark Monday done and
        # then skip Monday's actual close.
        self._was_open: bool | None = None
        # Injected so tests exercise the SCHEDULE without a database. Patching the module global
        # instead would leak into every test that ran afterwards.
        self._record = recorder or record
        self._done_for: str | None = None

    async def _collect(self) -> list[Leg]:
        from api.execquality.measure import compare

        since = (self._clock() - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
        # The PAGINATED helper, which follows Alpaca's page_token to completion. The first version
        # called the raw endpoint once with page_size=100: past 100 fills in a week it silently
        # dropped the rest, and a dropped partial fill does not just lose a leg — it corrupts the
        # VWAP and first-fill time of a leg that IS recorded, permanently.
        acts = await self._http.list_activities("FILL", after=since)
        if not acts:
            return []
        symbols = sorted({str(a["symbol"]).upper() for a in acts if a.get("symbol")})
        days = sorted({str(a["transaction_time"])[:10] for a in acts if a.get("transaction_time")})
        auctions = await self._auctions(symbols, days[0], days[-1])
        # Only legs THIS STRATEGY submitted. Without it a manual trade placed near the open lands in
        # the sample and is attributed to the strategy's execution — the original study had to
        # exclude exactly that by hand (CVS, PENG, and a manual FIG sell).
        #
        # Matched on (session, symbol) from STRUCTURED journal columns, not on parsed prose. Residual
        # limitation, stated rather than hidden: a manual trade in the SAME symbol on the SAME session
        # still passes. Narrow, and the alternative is reading the side out of a summary string.
        ours = await self._submitted_pairs()
        if not ours:
            return []
        return [leg for leg in compare(acts, auctions) if (leg.session, leg.symbol) in ours]

    async def _submitted_pairs(self) -> set[tuple[str, str]]:
        """(session, symbol) this strategy actually submitted, from the journal's structured fields."""
        if self._sf is None:
            return set()
        from sqlalchemy import text
        async with self._sf() as db:
            rows = await db.execute(text(
                "SELECT DISTINCT session, symbol FROM exec_action_log "
                "WHERE strategy_id = :sid AND kind = 'order' AND symbol IS NOT NULL "
                "AND detail->>'phase' = 'result' AND COALESCE((detail->>'ok')::boolean, false)"),
                {"sid": self._strategy_id})
            return {(r.session, str(r.symbol).upper()) for r in rows}

    async def _auctions(self, symbols: list[str], start: str, end: str) -> dict:
        """Auctions across a date range, following `next_page_token`.

        Alpaca's `limit` counts TOTAL data points, not points per symbol, so a week of eight symbols
        can exceed it. One page looked sufficient only because the activities bug was capping the
        symbol list first.
        """
        merged: dict[str, list] = {}
        params = {"symbols": ",".join(symbols), "start": start, "end": end, "feed": "sip",
                  "limit": 10000}
        while True:
            page = await self._http._get(self._http._data, "/v2/stocks/auctions", params) or {}
            for sym, rows in (page.get("auctions") or {}).items():
                merged.setdefault(sym, []).extend(rows or [])
            token = page.get("next_page_token")
            if not token:
                return {"auctions": merged}
            params = {**params, "page_token": token}

    async def tick(self) -> int:
        """One check. Returns rows written, or -1 when it was not yet time."""
        clock = await self._http.get_clock()
        is_open = bool(clock.get("is_open"))
        was, self._was_open = self._was_open, is_open
        # Only the moment the session ENDS. `was is None` is the first observation after a restart:
        # it establishes the baseline and records nothing, so a restart at 20:00 cannot manufacture a
        # session that never opened. The venue's own clock makes this holiday-correct for free.
        if is_open or was is not True:
            return -1
        marker = str(clock.get("timestamp", ""))[:10]
        if not marker or self._done_for == marker:
            return -1
        legs = await self._collect()
        written = await self._record(self._sf, legs)
        self._done_for = marker
        if legs:
            _log.info("execution quality: %s (+%d new rows)", summarise(legs), written)
        return written

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                # noqa: BLE001
                _log.warning("execution-quality run failed (will retry): %r", exc)
            await asyncio.sleep(_POLL_SECS)
