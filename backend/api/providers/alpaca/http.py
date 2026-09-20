"""Thin async Alpaca REST client (aiohttp). Only the endpoints the cockpit needs:
account (validation), assets (instrument universe), historical stock bars (chart backfill).

No Alpaca SDK dependency — direct HTTP, following the Nautilus adapter convention of owning the wire.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

_log = logging.getLogger(__name__)


#: Page bound for the order walk. 20 x 500 = 10,000 orders; the paper account was at 269 on 2026-08-20.
#: A backstop against a cursor that fails to advance, not an expected limit — reaching it raises.
_MAX_ORDER_PAGES = 20


class AlpacaHttpError(RuntimeError):
    """An Alpaca REST call returned status >= 400.

    Carries the parsed error body ({"code","message","market_price",…}) so callers can surface a clean,
    human `reason` instead of the raw JSON envelope. `str(exc)` keeps the full wire context for logs; the
    `reason` property is the UI-ready line.
    """

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        self.code: int | None = None
        self.message: str | None = None
        self.market_price: str | None = None
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            code = parsed.get("code")
            self.code = code if isinstance(code, int) else None
            msg = parsed.get("message")
            self.message = msg.strip() if isinstance(msg, str) and msg.strip() else None
            mp = parsed.get("market_price")
            self.market_price = str(mp) if mp is not None else None
        super().__init__(f"Alpaca {method} {path} → {status}: {self.message or body}")

    @property
    def reason(self) -> str:
        """Human, UI-ready reason: capitalized message (+ market price when Alpaca provides it), else raw."""
        if not self.message:
            return self.body
        base = self.message[0].upper() + self.message[1:]
        return f"{base} (mkt ${self.market_price})" if self.market_price else base


#: The two price series this door will serve (#1124). Declared here — the component that pulls the
#: bars — and re-declared on the DataClientSpec (`price_adjustments`); a test pins the two equal.
PRICE_ADJUSTMENTS: frozenset[str] = frozenset({"raw", "split"})


class AlpacaHttpClient:
    """Minimal Alpaca REST client. `connect()` before use, `close()` when done."""

    def __init__(self, key: str, secret: str, trading_base: str, data_base: str) -> None:
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._trading = trading_base.rstrip("/")
        self._data = data_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    #: Bounds for one HTTP attempt. aiohttp's default is a 5-MINUTE total and NO connect bound, which
    #: sounds generous and is the wrong shape: a stalled connect blocks startup reconciliation for
    #: minutes, and Nautilus gives up on the whole book long before the request does.
    _TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15, sock_connect=15, sock_read=45)

    #: Idempotent reads are retried; nothing else is. See `_request`.
    _READ_RETRIES = 3

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=self._TIMEOUT,
                # KEEP DNS. On 2026-08-19 startup reconciliation failed with
                # `ConnectionTimeoutError(Connection timeout to host .../v2/positions)` and the node ran
                # with an EMPTY BOOK — 0 positions against 35 held — while reporting RUNNING. Docker's
                # embedded resolver has bitten this container before (#354), and re-resolving on every
                # request turns one blip into a failed reconciliation.
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=32),
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", base, path, params=params)

    async def _request(
        self, method: str, base: str, path: str, params: dict | None = None, json: dict | None = None
    ) -> Any:
        assert self._session is not None, "call connect() first"
        # RETRY READS, NEVER WRITES.
        #
        # A GET is idempotent, so a connection that never completed can be tried again safely. A POST is
        # NOT: a request that timed out may still have reached Alpaca, and re-sending it is how one
        # intended order becomes two. Nautilus's own duplicate-id guard would not save us either, since
        # the retry reuses the same client_order_id and would be DENIED LOCALLY — reporting failure for
        # an order that is live at the venue, which is worse than the timeout.
        #
        # So: only the connection-level failures, only on GET, and the last attempt raises as before.
        attempts = self._READ_RETRIES if method == "GET" else 1
        for attempt in range(attempts):
            try:
                return await self._attempt(method, base, path, params=params, json=json)
            except (TimeoutError, aiohttp.ClientConnectionError) as exc:
                if attempt == attempts - 1:
                    raise
                _log.warning(
                    "alpaca %s %s: %s — retrying (%d/%d)",
                    method, path, type(exc).__name__, attempt + 1, attempts - 1,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _attempt(
        self, method: str, base: str, path: str, params: dict | None = None, json: dict | None = None
    ) -> Any:
        async with self._session.request(method, f"{base}{path}", params=params, json=json) as resp:
            if resp.status >= 400:
                # Surface Alpaca's error body ({"code","message"}) as a typed error — raise_for_status drops
                # it, and a bare RuntimeError forces callers to string-parse. AlpacaHttpError.reason is clean.
                body = await resp.text()
                raise AlpacaHttpError(method, path, resp.status, body)
            if resp.status == 204 or resp.content_length == 0:
                return None
            return await resp.json()

    async def get_account(self) -> dict:
        """Account snapshot — used to validate the key + reachability."""
        return await self._get(self._trading, "/v2/account")

    async def get_portfolio_history(
        self, period: str = "1M", timeframe: str = "1D", extended_hours: bool = False
    ) -> dict:
        """The account's own equity curve (#243) — `{timestamp[], equity[], profit_loss[],
        profit_loss_pct[], base_value, timeframe}`.

        The broker's accounting, not ours. Equity over time is not something Nautilus retains across
        sessions (its Cache holds current state, and `AccountState` events are not persisted as a
        series), so the alternative would be snapshotting equity ourselves — a parallel ledger that
        would drift from the statement. This reconciles by construction.

        It also answers #233's account half for free: `profit_loss` over a period IS the period P&L,
        so the day/week/month figures and the chart are one call, not two features.

        `pnl_reset` is deliberately NOT sent. It resets P&L each session, which is right for a single
        intraday view but wrong for every period here: on a 1W chart the last `profit_loss` would then be
        TODAY's P&L, not the week's, and the number printed beside the chart would contradict the line
        above it. The default accumulates from the window start, which is the period P&L we want.
        (codex review, Medium.)
        """
        return await self._get(
            self._trading,
            "/v2/account/portfolio/history",
            {
                "period": period,
                "timeframe": timeframe,
                "intraday_reporting": "extended_hours" if extended_hours else "market_hours",
            },
        )

    async def get_clock(self) -> dict:
        """`{timestamp, is_open, next_open, next_close}` — the venue's own session state.

        Used instead of a local calendar because it is HOLIDAY-AWARE and authoritative. A weekday
        time check would fire a daily digest on Thanksgiving reporting a session that never happened,
        and half-days (the 13:00 ET closes) would be wrong by three hours.
        """
        return await self._get(self._trading, "/v2/clock")

    async def get_calendar(self, start: str, end: str) -> list[dict]:
        """Trading sessions in `[start, end]` (YYYY-MM-DD, inclusive) — `[{date, open, close}, ...]`.

        Rows exist ONLY for trading days: a weekend or holiday is ABSENT, not marked closed. Any
        caller inferring "closed" from absence must therefore widen the window until an empty
        answer is impossible on a healthy read, and treat empty as broken (#645) — otherwise a
        failed read disarms whatever the caller guards.
        """
        return await self._get(self._trading, "/v2/calendar", {"start": start, "end": end})

    async def list_assets(self, status: str = "active", asset_class: str = "us_equity") -> list[dict]:
        """The tradable US-equity universe → source for the instrument provider."""
        return await self._get(
            self._trading, "/v2/assets", {"status": status, "asset_class": asset_class}
        )

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str | None = None,
        limit: int = 10000,
        feed: str = "sip",
        page_token: str | None = None,
        adjustment: str = "raw",
    ) -> dict:
        """One page of historical OHLCV bars for `symbol`. `timeframe` = Alpaca form ('1Min','1Hour','1Day').

        Returns the raw Alpaca payload: {"bars": [{t,o,h,l,c,v,...}, ...], "next_page_token": ...}.
        Pass `page_token` (a prior response's `next_page_token`) to fetch the next page.

        `adjustment` is the DATA-POLICY door (test_http.py): `raw` by default for every caller —
        backtests run on executable prices — and `split` for the one lane measured on split-adjusted
        bars (#1124: NVDA 2024-06-07 = 120.89 under `split`, == IB; 1208.88 raw). Nothing else:
        `all` folds dividends in (120.54 on the same day) and is a third series nobody measured.
        """
        if adjustment not in PRICE_ADJUSTMENTS:
            raise ValueError(
                f"adjustment must be one of {sorted(PRICE_ADJUSTMENTS)}, got {adjustment!r} — "
                f"`all`/`dividend` are refused by policy (#1124), not degraded to raw")
        params: dict = {
            "timeframe": timeframe,
            "start": start,
            "limit": limit,
            "feed": feed,
            "adjustment": adjustment,
        }
        if end:
            params["end"] = end
        if page_token:
            params["page_token"] = page_token
        return await self._get(self._data, f"/v2/stocks/{symbol}/bars", params)

    async def get_stock_snapshots(self, symbols: list[str], feed: str = "sip") -> dict:
        """Batch snapshot (latest trade/quote/minute-bar/daily-bar/prev-daily-bar) for every symbol in
        ONE request — the watchlist KPI Phase 3 source for today's range + prior close (#182 follow-up).
        Returns the raw payload keyed by bare ticker (Alpaca's shape, not wrapped in a `snapshots` key); a
        symbol Alpaca has no data for is simply absent from the response, never an error for the batch.
        """
        if not symbols:
            return {}
        return await self._get(
            self._data, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed}
        ) or {}

    # -- execution / account (trading API) --------------------------------------------------------
    async def get_order_by_client_order_id(self, client_order_id: str) -> dict:
        """ONE order by OUR id (`GET /v2/orders:by_client_order_id`) — an ANSWER in one call (#354).

        Raises `AlpacaHttpError(status=404)` when the venue says there is no such order: that is an
        answer, and the caller may act on it. A transport failure (after the GET retries) raises the
        transport's own error: that is NOT an answer, and the caller must not read it as one.
        """
        return await self._get(self._trading, "/v2/orders:by_client_order_id", {"client_order_id": client_order_id})

    async def get_order(self, venue_order_id: str) -> dict:
        """ONE order by ALPACA's id (`GET /v2/orders/{id}`) — the id a bracket leg carries when its
        client id was never ours (#242). Same answered / unanswered contract as above."""
        return await self._get(self._trading, f"/v2/orders/{venue_order_id}")

    async def list_orders(
        self, status: str = "all", limit: int = 500, paginate: bool = False
    ) -> list[dict]:
        """Orders for reconciliation (status: open/closed/all). Alpaca defaults to 50 → raise the cap.

        `paginate=True` follows the cursor to completion. WITHOUT IT THIS SILENTLY TRUNCATES (#387
        review, Critical): 500 is Alpaca's hard per-page cap and the default ordering is newest-first,
        so once an account crosses 500 orders the OLDEST rows vanish from the response — and a long-lived
        GTC protective stop is exactly the oldest thing in the list. The paper account was at 269 on
        2026-08-20 and climbing.

        That truncation is worse than a missed alarm. The protection reconciler uses this one read as
        the coverage oracle for BOTH `_broker_protected` AND `plan_protection`, so a dropped stop yields
        `protective_quantity=0` and `reserved_quantity=0` together, and the reconciler places a SECOND
        stop on top of a resting one — the over-coverage case the oversize path calls worse than a
        missing stop.

        `list_activities` directly below has paged to completion since it was written. This one did not,
        which is the whole finding: the pattern was already in this file.

        Cursor is `until` on `submitted_at`, descending, deduped by order id. Dedup rather than a strict
        timestamp walk because orders submitted in the same instant would otherwise loop forever; a page
        that yields no new ids ends the walk. `_MAX_ORDER_PAGES` bounds it and RAISES rather than
        returning a short list, because the caller's whole job is to distinguish "no stop" from "a stop
        I could not see", and a quietly-short list is indistinguishable from the first.
        """
        params: dict = {"status": status, "limit": limit}
        if not paginate:
            return await self._get(self._trading, "/v2/orders", params) or []

        params["direction"] = "desc"
        out: list[dict] = []
        seen: set[str] = set()
        for _ in range(_MAX_ORDER_PAGES):
            page = await self._get(self._trading, "/v2/orders", params) or []
            fresh = [o for o in page if str(o.get("id") or "") not in seen]
            for o in fresh:
                seen.add(str(o.get("id") or ""))
            out.extend(fresh)
            if len(page) < limit or not fresh:
                return out
            oldest = min((str(o.get("submitted_at") or "") for o in page if o.get("submitted_at")), default="")
            if not oldest:
                return out
            params = {**params, "until": oldest}
        raise RuntimeError(
            f"list_orders(status={status!r}) exceeded {_MAX_ORDER_PAGES} pages of {limit} — refusing to "
            "return a truncated order list, because a caller cannot tell it from a complete one"
        )

    async def list_positions(self) -> list[dict]:
        """Open positions → position status reports."""
        return await self._get(self._trading, "/v2/positions")

    async def list_activities(
        self,
        activity_type: str | None = "FILL",
        after: str | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Account activities of one type (FILL → per-execution trade activities) → fill reports.

        `after` (ISO8601) is a server-side LOWER bound on the activity's datetime. No `until`: Alpaca's
        server bounds filter on activity-creation time, but the caller reconciles on `transaction_time`
        (execution time), so the upper bound is enforced client-side to avoid dropping a fill created just
        after the window. Follows Alpaca's `page_token` cursor to completion — nothing is silently truncated.
        `page_size` is Alpaca's max per page (100). Each activity:
        {id, transaction_time, type, price, qty, side, symbol, order_id, …}.
        """
        # `activity_type=None` fetches EVERY type in one sweep (#345 item 1). The fill record alone
        # cannot reconcile against equity — a fee or a withholding moves cash with no fill behind it,
        # and on this account that was $999.09 nobody could see. One sweep rather than two: the types
        # nest in a single cursor, and two sweeps can disagree if a row posts between them.
        params: dict = {"page_size": page_size, "direction": "asc"}
        if activity_type is not None:
            params["activity_types"] = activity_type
        if after:
            params["after"] = after
        out: list[dict] = []
        while True:
            page = await self._get(self._trading, "/v2/account/activities", params) or []
            if not page:
                break
            out.extend(page)
            # Alpaca paginates activities by page_token = the last row's id; a short page is the last page.
            if len(page) < page_size:
                break
            params = {**params, "page_token": page[-1]["id"]}
        return out

    async def submit_order(self, payload: dict) -> dict:
        """POST a new order. `payload` is the Alpaca order request (symbol/qty/side/type/…)."""
        return await self._request("POST", self._trading, "/v2/orders", json=payload)

    async def replace_order(self, venue_order_id: str, payload: dict) -> dict:
        """PATCH an existing order (modify qty/limit/stop)."""
        return await self._request("PATCH", self._trading, f"/v2/orders/{venue_order_id}", json=payload)

    async def cancel_order(self, venue_order_id: str) -> None:
        """DELETE (cancel) one order by its Alpaca id."""
        await self._request("DELETE", self._trading, f"/v2/orders/{venue_order_id}")

    async def cancel_all_orders(self) -> None:
        """DELETE all open orders."""
        await self._request("DELETE", self._trading, "/v2/orders")
