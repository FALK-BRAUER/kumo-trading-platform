"""`list_orders` must not return a truncated order list without saying so (#387 review, Critical).

WHY THIS EXISTS. Alpaca's `/v2/orders` caps at 500 rows per page and defaults to newest-first. The
protection reconciler uses ONE such read as the coverage oracle for both `_broker_protected` (the
SECURED badge) and `plan_protection` (which decides whether to PLACE a stop). So a row that falls off
the end yields `protective_quantity=0` and `reserved_quantity=0` together, and the reconciler puts a
SECOND stop on top of a resting one — the over-coverage case the oversize path calls worse than a
missing stop. And the row most likely to fall off is the worst one to lose: the list is ordered
newest-first, a long-lived GTC protective stop is the oldest thing in it.

Under the old `status="open"` fetch this could not happen — that filter returned 9 rows. Widening to
`all` (269 rows on 2026-08-20, and climbing) is what made paging load-bearing, which is why the two
changes belong in one commit.

`list_activities` in the same file has paged to completion since it was written. The pattern was
already here.
"""

from __future__ import annotations

import asyncio

import pytest

from api.providers.alpaca.http import _MAX_ORDER_PAGES, AlpacaHttpClient


class _Recorder(AlpacaHttpClient):
    """The REAL `list_orders` over a faked transport. Only `_get` is replaced — the paging walk, the
    cursor, the dedup and the raise are all production code."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = pages
        self.calls: list[dict] = []
        self._trading = "https://paper-api.example"

    async def _get(self, base, path, params=None):
        self.calls.append(dict(params or {}))
        i = len(self.calls) - 1
        return self._pages[i] if i < len(self._pages) else []


def _order(n: int, submitted: str) -> dict:
    return {"id": f"o{n}", "symbol": "APA", "status": "held", "submitted_at": submitted}


def test_a_short_page_ends_the_walk_and_returns_everything():
    page = [_order(i, f"2026-08-20T13:{i:02d}:00Z") for i in range(3)]
    http = _Recorder([page])

    out = asyncio.run(http.list_orders(status="all", limit=500, paginate=True))

    assert [o["id"] for o in out] == ["o0", "o1", "o2"]
    assert len(http.calls) == 1, "a page shorter than the limit is the last page — do not ask again"


def test_a_FULL_page_is_followed_and_nothing_is_lost():
    """The defect itself: at limit=2 the old code returned 2 of 4 orders and reported no problem.

    The oldest order — `o3`, the one standing in for a long-lived GTC stop — is the one that vanished.
    """
    full = [_order(0, "2026-08-20T13:00:00Z"), _order(1, "2026-08-20T12:00:00Z")]
    rest = [_order(2, "2026-08-20T11:00:00Z"), _order(3, "2026-08-20T10:00:00Z")]
    http = _Recorder([full, rest])

    out = asyncio.run(http.list_orders(status="all", limit=2, paginate=True))

    assert [o["id"] for o in out] == ["o0", "o1", "o2", "o3"]
    assert http.calls[1].get("until") == "2026-08-20T12:00:00Z", (
        "the cursor must be the OLDEST timestamp on the page just read — descending order means the "
        "next page continues below it"
    )
    assert http.calls[0].get("direction") == "desc"


def test_orders_sharing_one_TIMESTAMP_cannot_loop_forever():
    """Ties are why the walk dedups on id instead of trusting the timestamp to advance.

    Two orders submitted in the same instant make `until` return the same page again. Without the dedup
    this walk would spin to `_MAX_ORDER_PAGES` and RAISE on a perfectly healthy account — turning a
    truncation guard into an outage.
    """
    same = [_order(0, "2026-08-20T13:00:00Z"), _order(1, "2026-08-20T13:00:00Z")]
    http = _Recorder([same, list(same), list(same)])

    out = asyncio.run(http.list_orders(status="all", limit=2, paginate=True))

    assert [o["id"] for o in out] == ["o0", "o1"], "a repeated page yields no new ids and ends the walk"
    assert len(http.calls) == 2


def test_a_cursor_that_never_advances_RAISES_rather_than_returning_short():
    """The whole point. The caller's job is to tell "no stop" from "a stop I could not see", and a
    quietly-short list is indistinguishable from the first — so it must not be returned at all.
    """
    pages = [[_order(i * 2 + j, f"2026-08-20T13:{i:02d}:{j:02d}Z") for j in range(2)]
             for i in range(_MAX_ORDER_PAGES + 2)]
    http = _Recorder(pages)

    with pytest.raises(RuntimeError, match="refusing to return a truncated order list"):
        asyncio.run(http.list_orders(status="all", limit=2, paginate=True))


def test_without_paginate_the_behaviour_is_unchanged():
    """Every other caller still gets exactly one request. Paging is opt-in, not a global change to how
    orders are fetched."""
    page = [_order(i, f"2026-08-20T13:{i:02d}:00Z") for i in range(2)]
    http = _Recorder([page, page])

    out = asyncio.run(http.list_orders(status="all", limit=2))

    assert [o["id"] for o in out] == ["o0", "o1"]
    assert len(http.calls) == 1
    assert "direction" not in http.calls[0]
