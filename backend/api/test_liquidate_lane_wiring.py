"""The wiring for #922: the HTTP route enqueues `liquidate_lane`, the engine dispatch reaches the handler,
and the ack carries the report — `partial` and `refused` as ERRORS, `ok`/deferred as ok with `results`.
A green handler with no caller is the seam this repo keeps shipping (CLAUDE.md, five times in one day).
The HTTP route is pinned in test_app.py on its module-scoped client — one Nautilus engine per process."""

from __future__ import annotations

import asyncio
import json
from api.engine_node import UiFeedStrategy


class _Dispatch:
    """Duck-typed self for the REAL `_handle_command`, with the liquidate handler scripted."""

    def __init__(self, outcome):
        self.acks: list[dict] = []
        self.xacked: list[str] = []
        self._outcome = outcome
        self._handle_command = UiFeedStrategy._handle_command.__get__(self)

    async def _handle_liquidate_lane_command(self, cid, payload, entry_id):
        self.seen = (cid, payload, entry_id)
        return self._outcome

    def _publish_ack(self, cid, ctype, status, error, results=None):
        self.acks.append({"id": cid, "type": ctype, "status": status, "error": error, "results": results})

    def _xack_command(self, entry_id):
        self.xacked.append(entry_id)


def _fields(payload):
    return {"id": "c" * 32, "type": "liquidate_lane", "payload": json.dumps(payload)}


def test_dispatch_reaches_the_handler_and_an_ok_ack_carries_the_report_in_results():
    detail = {"state": "ok", "held": 2, "submitted": 2, "failed": [], "remainder": []}
    d = _Dispatch(("ok", detail))
    asyncio.run(d._handle_command("1-1", _fields({"strategy_id": "MOMENTUM-002"})))
    assert d.seen[1] == {"strategy_id": "MOMENTUM-002"} and d.seen[0] == "c" * 32
    ack = d.acks[0]
    assert ack["status"] == "ok" and ack["error"] == "" and ack["results"] == detail
    assert d.xacked == ["1-1"]


def test_partial_and_refused_are_ERRORS_on_the_ack_with_the_same_report():
    partial = {"state": "partial", "held": 2, "submitted": 1, "failed": [{"symbol": "XLV", "qty": 40, "error": "x"}], "remainder": []}
    d = _Dispatch(("partial", partial))
    asyncio.run(d._handle_command("1-2", _fields({})))
    assert d.acks[0]["status"] == "error" and d.acks[0]["error"].startswith("partial:") and d.acks[0]["results"] == partial
    r = _Dispatch(("refused", {"why": "expected 1 positions, the lane holds 2"}))
    asyncio.run(r._handle_command("1-3", _fields({})))
    assert r.acks[0]["status"] == "error" and "expected 1 positions" in r.acks[0]["error"]


def test_deferred_is_ok_on_the_ack_but_SAYS_so_in_the_error_text():
    d = _Dispatch(("deferred_to_next_open", {"state": "deferred_to_next_open", "submitted": 0}))
    asyncio.run(d._handle_command("1-4", _fields({})))
    assert d.acks[0]["status"] == "ok" and "deferred_to_next_open" in d.acks[0]["error"]
