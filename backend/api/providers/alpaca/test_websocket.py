"""Offline tests for the Alpaca WS transport — parsing, subscription state, frame dispatch.

No network: a fake socket records sent JSON so subscribe/resubscribe/dispatch logic is asserted
deterministically. The connect/auth handshake needs a live server and is covered at market open.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from api.providers.alpaca.websocket import AlpacaWebSocketClient, parse_ws_message


def _run(coro):
    """Drive one coroutine to completion (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


class _FakeWS:
    """Minimal stand-in for aiohttp's ws: records send_json payloads, never closed."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _client() -> AlpacaWebSocketClient:
    frames: list[dict] = []
    c = AlpacaWebSocketClient(
        url="wss://example/v2/iex",
        key="k",
        secret="s",
        handler=frames.append,
        log=logging.getLogger("alpaca-paper-ws"),
    )
    c._received = frames  # type: ignore[attr-defined]  # inspected by dispatch tests
    return c


def test_parse_ws_message_normalises_shapes():
    assert parse_ws_message([{"T": "b"}, {"T": "t"}]) == [{"T": "b"}, {"T": "t"}]
    assert parse_ws_message({"T": "b"}) == [{"T": "b"}]
    assert parse_ws_message("garbage") == []
    assert parse_ws_message([{"T": "b"}, 42, None]) == [{"T": "b"}]  # non-dicts dropped


def test_subscribe_sends_only_new_symbols():
    c = _client()
    c._ws = _FakeWS()  # type: ignore[assignment]
    _run(c.subscribe("bars", ["AAPL", "MSFT"]))
    _run(c.subscribe("bars", ["AAPL", "TSLA"]))  # AAPL already subscribed → only TSLA goes out

    assert c._ws.sent == [
        {"action": "subscribe", "bars": ["AAPL", "MSFT"]},
        {"action": "subscribe", "bars": ["TSLA"]},
    ]
    assert c._subs["bars"] == {"AAPL", "MSFT", "TSLA"}


def test_subscribe_rejects_unknown_channel():
    c = _client()
    with pytest.raises(ValueError):
        _run(c.subscribe("candles", ["AAPL"]))


def test_unsubscribe_drops_tracked_symbols():
    c = _client()
    c._ws = _FakeWS()  # type: ignore[assignment]
    _run(c.subscribe("dailyBars", ["AAPL", "MSFT"]))
    c._ws.sent.clear()
    _run(c.unsubscribe("dailyBars", ["AAPL", "NVDA"]))  # NVDA was never subscribed → ignored

    assert c._ws.sent == [{"action": "unsubscribe", "dailyBars": ["AAPL"]}]
    assert c._subs["dailyBars"] == {"MSFT"}


def test_resubscribe_replays_all_tracked_subscriptions():
    c = _client()
    c._ws = _FakeWS()  # type: ignore[assignment]
    c._subs["bars"] = {"MSFT", "AAPL"}
    c._subs["dailyBars"] = {"AAPL"}
    _run(c._resubscribe())

    assert c._ws.sent == [
        {"action": "subscribe", "bars": ["AAPL", "MSFT"], "dailyBars": ["AAPL"]},
    ]


def test_dispatch_forwards_data_frames_and_swallows_control():
    c = _client()
    _run(c._dispatch({"T": "success", "msg": "authenticated"}))  # control → not forwarded
    _run(c._dispatch({"T": "subscription", "bars": ["AAPL"]}))  # control → not forwarded
    _run(c._dispatch({"T": "error", "code": 400, "msg": "bad"}))  # control → not forwarded
    _run(c._dispatch({"T": "b", "S": "AAPL", "c": 1.0}))  # data → forwarded

    assert c._received == [{"T": "b", "S": "AAPL", "c": 1.0}]
