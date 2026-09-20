"""#1099 — `/strategies` must SAY whether the engine frame was read, at the top level.

Every per-lane `arm` row reads `state: "unknown"` in two different worlds: the frame was read and
this lane is not on the node (CRSISHORT-006 on paper, 2026-09-18), and the frame could NOT be read
(a stale bridge — `consumer.health()` gives `armed_lanes: None`; or `health()` raised). The route
collapsed them with `_h.get("armed_lanes") or {}`, so a reader of the rows cannot tell "not here"
from "could not ask" — and the Portfolio chips built on that predicate would drop every lane on the
tick a deploy lands (cross-review, h2ho0jjf). Three states at the SOURCE: `engine_frame: "ok" |
"unreadable"`, beside the rows, never inferred from them.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

import pytest


def _drive(monkeypatch, node) -> dict:
    """The REAL route through the doubles it reads — same seams as `_drive_strategies`, but the
    whole body is returned because the fact under test is top-level, not per row."""
    from api import app as app_mod
    from api import budget_store
    from api.budget import Sleeve
    from api.db import engine as db_engine

    class _Book:
        sleeves = {"QC345-003": Sleeve("QC345-003", 20_000.0, 20_000.0)}

    @asynccontextmanager
    async def _session():
        yield object()

    async def _load_book(_session):
        return _Book()

    async def _last_decisions(_session):
        return {}

    monkeypatch.setattr(db_engine, "session_factory", _session)
    monkeypatch.setattr(budget_store, "load_book", _load_book)
    monkeypatch.setattr(budget_store, "last_decisions", _last_decisions)
    monkeypatch.setattr(app_mod, "_today", lambda: date(2026, 9, 18))
    monkeypatch.setattr(app_mod.app.state, "node", node, raising=False)
    return asyncio.run(app_mod.get_strategies())


class _Node:
    def __init__(self, frame):
        self._frame = frame

    def health(self):
        if isinstance(self._frame, Exception):
            raise self._frame
        return self._frame


def test_FIXTURE_the_stale_bridge_shape_is_what_the_consumer_actually_emits():
    """`armed_lanes: None` is the consumer's own stale-bridge value (consumer.py `health()`), not an
    invention of this file — the double must reject nothing production emits."""
    import time

    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    stale = RedisConsumer(load_feed_config())
    assert stale.health()["armed_lanes"] is None
    live = RedisConsumer(load_feed_config())
    live._health = {"engine_ok": True, "armed_lanes": {}}
    live._health_at = time.monotonic()
    assert live.health()["armed_lanes"] == {}


def test_a_READ_frame_with_NO_lanes_is_ok_and_every_lane_reads_unknown(monkeypatch):
    body = _drive(monkeypatch, _Node({"armed_lanes": {}, "next_fire_ns": {}}))
    assert body["engine_frame"] == "ok"
    assert all(r["arm"]["state"] == "unknown" for r in body["strategies"])


def test_a_STALE_bridge_is_unreadable_even_though_the_rows_look_identical(monkeypatch):
    body = _drive(monkeypatch, _Node({"armed_lanes": None, "next_fire_ns": None}))
    assert body["engine_frame"] == "unreadable"
    assert all(r["arm"]["state"] == "unknown" for r in body["strategies"]), (
        "the rows are the SAME as the ok case above — that is the point: only the top-level field "
        "tells the two apart")


def test_a_frame_read_that_RAISES_is_unreadable_and_the_route_still_answers(monkeypatch):
    body = _drive(monkeypatch, _Node(RuntimeError("bridge down")))
    assert body["engine_frame"] == "unreadable"
    assert {r["strategy_id"] for r in body["strategies"]} >= {"QC345-003", "MANUAL-001"}


def test_a_read_frame_with_lanes_is_ok_and_names_them(monkeypatch):
    body = _drive(monkeypatch, _Node({"armed_lanes": {"QC345-003": {"armed": True, "state": "armed"}},
                                      "next_fire_ns": {}}))
    assert body["engine_frame"] == "ok"
    rows = {r["strategy_id"]: r for r in body["strategies"]}
    assert rows["QC345-003"]["arm"]["state"] == "armed"
    assert rows["MANUAL-001"]["arm"]["state"] == "unknown"


def test_FIXTURE_the_engine_frame_ALWAYS_carries_the_key_empty_when_no_lane():
    """`armed_by_lane({})` is `{}`, never None — so a live frame WITHOUT the key can only be an
    engine that predates it (the new-api/old-engine deploy window), and the consumer may read that
    absence as unreadable without mislabelling a healthy empty node."""
    from api.engine_node import armed_by_lane

    assert armed_by_lane({}) == {}
    assert armed_by_lane({}) is not None


def test_a_LIVE_bridge_whose_frame_LACKS_the_key_is_unreadable_not_an_empty_node(monkeypatch):
    """The gap the cross-review named (h2ho0jjf, #1105): `consumer.health()` used to default a
    missing `armed_lanes` to `{}` while `bridge_ok` was true, so an OLD engine's frame — every deploy
    has seconds of new api reading old engine (`api-and-engine-recreate-seconds-apart`) — read as
    "frame ok, no lane is here", and every registry chip would drop on that tick. The key's ABSENCE
    must survive the hop as None."""
    import time

    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    old_engine = RedisConsumer(load_feed_config())
    old_engine._health = {"engine_ok": True}                       # no `armed_lanes` key at all
    old_engine._health_at = time.monotonic()
    observed = old_engine.health()
    assert observed["bridge_ok"] is True, "fixture: the bridge IS live — that is the point"
    assert observed["armed_lanes"] is None, "a frame without the key must not be defaulted to {}"

    body = _drive(monkeypatch, _Node(observed))
    assert body["engine_frame"] == "unreadable"
