"""Instrument search on an IBKR instance (#837) — the engine seam.

Driven through `_handle_command`, the real entry point: a `search_instruments` command asks the IB
client the engine already holds (`get_matching_contracts` = `reqMatchingSymbols`) and the ack carries
the rows. Only STK/USD rows, ids built the way this book already names IB instruments (`IBKR.XNAS`),
and a node with no IB client REFUSES rather than answering nothing.
"""

from __future__ import annotations

import asyncio
import json

from nautilus_trader.adapters.interactive_brokers.common import IBContract

from api import engine_node as engine_mod
from api.engine_node import UiFeedStrategy


class _IBClient:
    def __init__(self, contracts):
        self.contracts = contracts
        self.patterns: list[str] = []

    async def get_matching_contracts(self, pattern: str):
        self.patterns.append(pattern)
        return list(self.contracts)


def _probe(client):
    """A real UiFeedStrategy with ONLY the bus and the IB client handle replaced."""
    s = UiFeedStrategy.__new__(UiFeedStrategy)
    s.published: list[tuple[str, dict]] = []
    s._publish = lambda kind, payload, **k: s.published.append((kind, payload))
    s._redis = None
    s._loaded = set()
    s._ib_client_ref = lambda: client
    s._search_timeout_s = engine_mod._SEARCH_VENUE_TIMEOUT_S  # production sets it in __init__
    return s


def _ask(s, q="ibk", limit=20):
    async def _drive():
        await s._handle_command("1-0", {"id": "c1", "type": "search_instruments",
                                        "payload": json.dumps({"q": q, "limit": limit})})
        await asyncio.sleep(0.05)  # the ack comes from the search's own task, off the command lane
    asyncio.run(_drive())
    acks = [p for k, p in s.published if k == "command_ack"]
    assert len(acks) == 1, s.published
    return acks[0]


_STK = IBContract(symbol="IBKR", secType="STK", primaryExchange="NASDAQ", currency="USD", exchange="SMART")
_OPT = IBContract(symbol="IBKR", secType="OPT", primaryExchange="NASDAQ", currency="USD", exchange="SMART")
_MXN = IBContract(symbol="IBKR", secType="STK", primaryExchange="MEXI", currency="MXN", exchange="MEXI")


def test_the_ack_carries_the_brokers_rows_named_the_way_this_book_names_them():
    client = _IBClient([_STK, _OPT, _MXN])
    # FIXTURE PROPERTY: three rows in, only one of which is a US equity — the filter has work to do.
    assert {c.secType for c in client.contracts} == {"STK", "OPT"} and {c.currency for c in client.contracts} == {"USD", "MXN"}
    ack = _ask(_probe(client))
    assert client.patterns == ["ibk"], "the engine did not ask IB for the pattern it was sent"
    assert ack["status"] == "ok", ack
    assert ack["results"] == [{"instrument_id": "IBKR.XNAS", "symbol": "IBKR", "name": "IBKR", "venue": "XNAS"}], ack


def test_a_node_without_an_ib_client_REFUSES():
    ack = _ask(_probe(None))
    assert ack["status"] == "error" and "no instrument provider" in ack["error"], ack
    assert "results" not in ack, "a refusal must not also carry rows"


def test_other_commands_acks_are_byte_identical():
    """The ack gains `results` for search only; every other command's ack keeps its exact shape."""
    s = _probe(_IBClient([]))
    asyncio.run(s._handle_command("1-1", {"id": "c2", "type": "no_such_command", "payload": "{}"}))
    ack = [p for k, p in s.published if k == "command_ack"][-1]
    assert set(ack) == {"id", "type", "status", "error"}, ack


def test_a_SLOW_venue_search_does_not_hold_the_serialized_command_lane():
    """codex on 920bf48 (BLOCKER): `_handle_command` is awaited to completion before the reader takes
    the next command (orders apply in order). The search handler awaited IB inline, and IB's matching
    request can take the adapter's full 60 s timeout — so one slow search would hold submit / cancel /
    flatten behind it. The search must return the lane immediately and answer from its own task."""
    gate = asyncio.Event()

    class _Slow:
        async def get_matching_contracts(self, pattern):
            await gate.wait()
            return [_STK]

    s = _probe(_Slow())

    async def _drive():
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            s._handle_command("1-0", {"id": "c1", "type": "search_instruments",
                                      "payload": json.dumps({"q": "ibk", "limit": 5})}),
            timeout=0.5,
        )
        held = asyncio.get_running_loop().time() - t0
        acks_before = [p for k, p in s.published if k == "command_ack"]
        gate.set()
        await asyncio.sleep(0.05)
        acks_after = [p for k, p in s.published if k == "command_ack"]
        return held, acks_before, acks_after

    held, before, after = asyncio.run(_drive())
    assert held < 0.2, f"the command lane was held {held:.2f}s waiting on the venue"
    assert before == [], f"an ack was published before the venue answered: {before}"
    assert len(after) == 1 and after[0]["status"] == "ok" and after[0]["results"][0]["instrument_id"] == "IBKR.XNAS", after


def test_a_venue_search_that_never_answers_is_REFUSED_within_the_api_timeout():
    """The api gives up at 4 s; the engine must give up FIRST (3.5 s) and say so, or the ack never
    comes and the api reads 'never answered' for a venue that answered 'no'."""
    class _Never:
        async def get_matching_contracts(self, pattern):
            await asyncio.Event().wait()

    from api import engine_node as mod
    s = _probe(_Never())
    s._search_timeout_s = 0.05  # the production default is asserted below; shortened here for the clock

    async def _drive():
        await s._handle_command("1-0", {"id": "c1", "type": "search_instruments",
                                        "payload": json.dumps({"q": "ibk", "limit": 5})})
        await asyncio.sleep(0.2)
        return [p for k, p in s.published if k == "command_ack"]

    acks = asyncio.run(_drive())
    assert len(acks) == 1 and acks[0]["status"] == "error" and "did not answer" in acks[0]["error"], acks
    assert mod._SEARCH_VENUE_TIMEOUT_S < 4.0, "the engine must give up before the api does"
