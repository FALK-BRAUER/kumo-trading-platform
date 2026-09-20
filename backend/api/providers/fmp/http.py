"""Thin async Financial Modeling Prep REST client (aiohttp). Only the endpoints the watchlist fundamentals
KPIs need: quote, company profile, annual income statement — single-symbol only, no SDK dependency.

NOT a Nautilus adapter (no InstrumentProvider/DataClient/ExecutionClient) — fundamentals are slow-changing
display enrichment, not execution-grade market data, so they don't belong in Nautilus's subscription/
backtest machinery. This mirrors `providers/alpaca/http.py`'s shape (connect/close/`_get`) without any of
the Nautilus scaffolding around it; the engine calls it directly from a plain timer-driven poller.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

_BASE = "https://financialmodelingprep.com/stable"


class FmpHttpError(RuntimeError):
    """An FMP REST call failed — either an HTTP error status, or ANY other exception during the
    request/decode (connection failure, timeout, malformed response, `aiohttp.ContentTypeError`,
    `TooManyRedirects`, ...). Deliberately carries only `path`/`symbol`/`status`/`body` — NEVER the
    original exception's own message, and NEVER the request URL.

    FMP puts the API key in the query string (`apikey=...`), not a header like Alpaca's client does, so
    ANY exception whose `str()` embeds the request (most `aiohttp` exceptions include
    `request_info.real_url`, which carries the full query string) would leak the key into every log line
    an engine `_log.warning(...)` call touches. Confirmed live: a plain `resp.raise_for_status()` leaked
    the real key into engine logs on a 402 from FMP. A narrower first fix (only guard the status>=400
    branch) was still incomplete — codex review round 2 found `resp.json()`'s `ContentTypeError` and a
    hypothetical key-echoing response body both still leak. `_get` now: (1) never calls `resp.json()`
    directly, only `resp.text()` + our own `json.loads` (which can't raise `ContentTypeError`), (2) wraps
    the ENTIRE request in one `except Exception` that builds this error from ONLY known-safe strings
    (path/symbol/status/the exception's TYPE NAME, never `str(exc)`), and (3) redacts the configured API
    key out of any response BODY before it goes into this exception's own message, in case the body
    itself ever echoes the request back."""

    def __init__(self, path: str, symbol: str, status: int, body: str) -> None:
        self.path = path
        self.symbol = symbol
        self.status = status
        self.body = body
        super().__init__(f"FMP GET {path} symbol={symbol} → {status}: {body}")


class FmpHttpClient:
    """Minimal FMP REST client. `connect()` before use, `close()` when done.

    No batch/multi-symbol support: confirmed empirically (2026-08-01) that `/stable/quote` and
    `/stable/batch-quote` both 403 with "Restricted Endpoint... not available under your current
    subscription" for multi-symbol requests on the free tier. Every call here is single-symbol; the
    caller loops over the watchlist.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _redact(self, text: str) -> str:
        """Strip the configured API key out of a string before it can reach an exception message — a
        defensive last line, in case a response body ever echoes the request back (a WAF/proxy error page,
        for instance). See FmpHttpError's docstring."""
        return text.replace(self._api_key, "***REDACTED***") if self._api_key else text

    async def _get(self, path: str, symbol: str, **params: str) -> Any:
        assert self._session is not None, "call connect() first"
        query = {"symbol": symbol, "apikey": self._api_key, **params}
        try:
            async with self._session.get(f"{_BASE}{path}", params=query) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise FmpHttpError(path, symbol, resp.status, self._redact(body))
                return json.loads(body)  # NOT resp.json() — avoids aiohttp.ContentTypeError entirely
        except FmpHttpError:
            raise
        except Exception as exc:  # noqa: BLE001 — deliberately blanket: a timeout, connection failure,
            # TooManyRedirects, or a JSON decode error could ALL be raised here, and most `aiohttp`
            # exception types embed `request_info.real_url` (the full query string, key included) in their
            # own `str()`. Never let that surface — this is a hard boundary: nothing crosses it except an
            # FmpHttpError built from the exception's TYPE NAME only, never its message.
            raise FmpHttpError(path, symbol, 0, f"{type(exc).__name__} (request failed)") from None

    async def get_quote(self, symbol: str) -> dict | None:
        """Price/market-cap/day-range snapshot. Returns None if FMP has nothing for this symbol (an
        empty list is FMP's own "unknown symbol" signal, not an error)."""
        rows = await self._get("/quote", symbol)
        return rows[0] if rows else None

    async def get_profile(self, symbol: str) -> dict | None:
        """Company profile — carries `beta` and `lastDividend`, the two fields `/quote` doesn't have
        (confirmed empirically: `/quote` and `/profile` return genuinely different field sets, despite
        some FMP docs implying profile fields are folded into quote now — they aren't, as of 2026-08-01)."""
        rows = await self._get("/profile", symbol)
        return rows[0] if rows else None

    async def get_annual_eps(self, symbol: str) -> float | None:
        """Most recent fiscal year's EPS from the annual income statement — trailing, not forward (forward
        P/E needs picking a specific future fiscal-year analyst estimate row, deferred — see the
        fundamentals metric contract)."""
        rows = await self._get("/income-statement", symbol, period="annual", limit="1")
        if not rows:
            return None
        eps = rows[0].get("eps")
        return float(eps) if eps is not None else None
