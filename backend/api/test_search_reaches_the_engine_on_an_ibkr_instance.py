"""Instrument search on an IBKR instance (#837) — the api seam.

MEASURED on ibkr-paper, 2026-09-09 15:15Z: typing `ibkr` into the search box showed "Search unavailable —
no instrument provider"; `GET /instruments/search?q=ibkr` → 503. `_build_search` built an index only
for `data_provider == "alpaca"` and this instance has no Alpaca, ever (CLAUDE.md 2026-09-09: the
broker supplies its own data). The engine already holds the connected IB client, so the api asks the
ENGINE over the command bus it already uses, and the engine asks IB (`reqMatchingSymbols`).

Three properties pinned here: the index exists on a non-Alpaca provider; a query is ONE command and
the reply's rows come back as `InstrumentMatch`; a silent engine is a REFUSAL, never an empty list
(an empty list reads as "the venue lists nothing", which is a different fact).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from api.models import InstrumentMatch

_ROWS = [{"instrument_id": "IBKR.XNAS", "symbol": "IBKR", "name": "IBKR", "venue": "XNAS"}]


class _Node:
    """The api-side bridge the way production exposes it: `send_command` mints an id, `command_status`
    answers the engine's ack (or None while it has not arrived)."""

    def __init__(self, ack: dict | None):
        self.sent: list[tuple[str, dict, str]] = []
        self._ack = ack
        self._acks: dict[str, dict] = {}

    async def send_command(self, ctype: str, payload: dict) -> str:
        cid = f"cmd-{len(self.sent) + 1}"
        self.sent.append((ctype, dict(payload), cid))
        if self._ack is not None:
            self._acks[cid] = dict(self._ack, id=cid, type=ctype)
        return cid

    def command_status(self, cid: str) -> dict | None:
        return self._acks.get(cid)


def _index(node, **kw):
    from api.instrument_search import EngineSearchIndex

    return EngineSearchIndex(node, timeout_s=kw.pop("timeout_s", 1.0), poll_s=0.005, **kw)


def test_build_search_returns_an_ENGINE_index_when_the_provider_is_not_alpaca(monkeypatch):
    """The seam: `_build_search` on an IBKR feed config. Before #837 this returned (None, None) and the
    endpoint 503'd for the life of the process."""
    import api.app as app_mod
    from api.instrument_search import EngineSearchIndex

    cfg = types.SimpleNamespace(data_provider="ibkr", provider_config={})
    monkeypatch.setattr("api.feed_config.load_feed_config", lambda: cfg)
    index, warm = asyncio.run(app_mod._build_search(node=_Node(ack=None)))
    assert isinstance(index, EngineSearchIndex), f"no search index on an IBKR instance: {index!r}"
    assert warm is None


def test_a_query_is_ONE_command_and_the_reply_rows_come_back_typed():
    node = _Node(ack={"status": "ok", "error": "", "results": _ROWS})
    idx = _index(node)
    out = asyncio.run(idx.search("ibk", 20))
    assert node.sent == [("search_instruments", {"q": "ibk", "limit": 20}, "cmd-1")], node.sent
    assert [m for m in out] == [InstrumentMatch(**_ROWS[0])]


def test_the_same_prefix_within_the_ttl_does_not_reach_the_bus():
    """The UI issues one request per keystroke with no debounce (GlobalSymbolSearch.tsx) and IB paces
    reqMatchingSymbols at ~1/s. Same normalised query → served from the api, no bus round trip."""
    node = _Node(ack={"status": "ok", "error": "", "results": _ROWS})
    idx = _index(node, ttl_seconds=60.0)
    asyncio.run(idx.search("ibk", 20))
    asyncio.run(idx.search(" IBK ", 20))
    assert len(node.sent) == 1, f"a repeat of the same prefix hit the bus again: {node.sent}"


def test_a_SILENT_engine_is_a_refusal_not_an_empty_list():
    node = _Node(ack=None)  # the ack never arrives
    idx = _index(node, timeout_s=0.03)
    with pytest.raises(RuntimeError):
        asyncio.run(idx.search("ibk", 20))
    assert len(node.sent) == 1, "fixture: the query was sent and simply never answered"


def test_an_engine_ack_without_rows_is_a_refusal_too():
    """Three states: rows, no rows, never told us. An `ok` ack that carries no `results` is the third."""
    node = _Node(ack={"status": "ok", "error": ""})
    with pytest.raises(RuntimeError):
        asyncio.run(_index(node).search("ibk", 20))


def test_known_symbols_is_None_because_the_venue_catalog_cannot_be_consulted():
    """Pool validation (#663) treats None as 'could not be consulted' — never as 'lists nothing'."""
    assert _index(_Node(ack=None)).known_symbols() is None
