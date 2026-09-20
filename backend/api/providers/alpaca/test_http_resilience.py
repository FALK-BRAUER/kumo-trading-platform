"""A resolver blip must not cost the whole book, and a retry must never double an order.

WHY THIS FILE EXISTS
--------------------
2026-08-19. Startup reconciliation failed and the node ran with an EMPTY BOOK — `/positions` returned 0
while the broker held 35 — reporting RUNNING throughout:

    [ERROR] ExecClient-ALPACA: Cannot reconcile execution state
    ConnectionTimeoutError(Connection timeout to host https://paper-api.alpaca.markets/v2/positions)

One HTTP GET failed to connect, `asyncio.gather` in Nautilus's `generate_mass_status` propagated it, and
every position and every strategy was lost until a human noticed. The client had NO timeout configured
and NO retry: aiohttp's default is a five-minute total with no connect bound, which is the wrong shape
in both directions — too long to fail fast, and no second attempt when it does.

Docker's embedded resolver has bitten this container before (#354), so re-resolving on every request
turns one blip into a failed reconciliation.

THE ASYMMETRY THAT SHAPES THE RETRY
-----------------------------------
A GET is idempotent and safe to repeat. A POST is NOT: a request that timed out may still have reached
Alpaca, and re-sending it is how one intended order becomes two. Nautilus's duplicate-id guard does not
save us either — the retry reuses the same client_order_id and is DENIED LOCALLY, so the caller is told
the order failed while it is live at the venue. That is strictly worse than the timeout.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from api.providers.alpaca.http import AlpacaHttpClient


def _client() -> AlpacaHttpClient:
    c = AlpacaHttpClient("k", "s", "https://paper-api.alpaca.markets", "https://data.alpaca.markets")
    # `_request` asserts a session exists before doing anything — a real precondition, kept rather than
    # removed. These tests replace `_attempt`, so the session is never used; it only has to be present.
    c._session = object()
    return c


def test_a_request_is_bounded_at_all():
    """aiohttp's default has no connect bound; a stalled connect blocks startup for minutes."""
    t = AlpacaHttpClient._TIMEOUT
    assert t.total is not None and t.total <= 120, f"total is {t.total} — a stalled request blocks the boot"
    assert t.connect is not None and t.connect <= 30, (
        f"connect is {t.connect!r} — an unbounded connect is exactly what failed reconciliation"
    )


class _Recorder:
    """Counts attempts and fails the first `fail_times` with a connection-level error."""

    def __init__(self, fail_times: int, exc: Exception):
        self.calls: list[str] = []
        self._left, self._exc = fail_times, exc

    async def attempt(self, method, base, path, params=None, json=None):
        self.calls.append(method)
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return {"ok": True}


@pytest.mark.parametrize(
    "exc",
    [aiohttp.ClientConnectionError("Connector is closed."), TimeoutError()],
)
def test_a_read_survives_a_transient_connection_failure(exc):
    """Both of the exact exceptions seen on 2026-08-19, not a stand-in for them."""
    c = _client()
    rec = _Recorder(fail_times=1, exc=exc)
    c._attempt = rec.attempt

    got = asyncio.run(c._request("GET", "https://x", "/v2/positions"))
    assert got == {"ok": True}
    assert len(rec.calls) == 2, f"the read was not retried ({len(rec.calls)} attempt(s))"


def test_a_read_still_gives_up_eventually():
    """Retrying forever would hang the boot instead of failing it — the failure must still surface."""
    c = _client()
    rec = _Recorder(fail_times=99, exc=TimeoutError())
    c._attempt = rec.attempt

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c._request("GET", "https://x", "/v2/positions"))
    assert len(rec.calls) == AlpacaHttpClient._READ_RETRIES


def test_a_WRITE_IS_NEVER_RETRIED():
    """The assertion that matters most in this file.

    A POST that timed out may already have reached the venue. Retrying it is how one intended order
    becomes two — and because the retry reuses the same client_order_id, Nautilus denies it locally and
    reports failure for an order that is live. Strictly worse than the original timeout.
    """
    c = _client()
    rec = _Recorder(fail_times=1, exc=TimeoutError())
    c._attempt = rec.attempt

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c._request("POST", "https://x", "/v2/orders", json={"symbol": "AEM"}))
    assert rec.calls == ["POST"], (
        f"an order submission was retried {len(rec.calls)} times — a timed-out POST may already be live "
        f"at the venue, and re-sending it doubles the position"
    )


def test_an_http_error_is_not_retried_either():
    """A 4xx/5xx is an ANSWER, not a failed connection. Retrying it hammers the venue and changes nothing."""
    from api.providers.alpaca.http import AlpacaHttpError

    c = _client()
    rec = _Recorder(fail_times=1, exc=AlpacaHttpError("GET", "/v2/positions", 422, "nope"))
    c._attempt = rec.attempt

    with pytest.raises(AlpacaHttpError):
        asyncio.run(c._request("GET", "https://x", "/v2/positions"))
    assert len(rec.calls) == 1, "a 422 was retried — the venue already answered"
