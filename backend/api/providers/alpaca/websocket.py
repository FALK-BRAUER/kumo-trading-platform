"""Alpaca market-data WebSocket transport (aiohttp) — connect, auth, subscribe, reconnect.

Transport only: it owns the socket, the auth handshake, the subscription state (so it can resubscribe
after a reconnect), and hands each parsed data frame to a handler. The Nautilus translation (frame →
`Bar`) lives in `data_client.py`. Alpaca allows **one** concurrent market-data connection per key.

Protocol (v2): connect → server sends `[{"T":"success","msg":"connected"}]` → client sends
`{"action":"auth","key","secret"}` → server `[{"T":"success","msg":"authenticated"}]`. Subscribe with
`{"action":"subscribe","bars":[...],"dailyBars":[...]}`. Data frames: `{"T":"b"|"u"|"d","S":sym,...}`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import aiohttp

# Control-frame types (handshake / subscription ack / error) — everything else is data.
_CONTROL_TYPES = frozenset({"success", "error", "subscription"})
# The subscribable channels the cockpit uses (Alpaca channel name → tracked set).
_CHANNELS = ("bars", "dailyBars", "trades", "quotes")
# Reconnect backoff ceiling (s). A live-data client must self-heal indefinitely — never give up — so a
# transient DNS/network blip can't leave the cockpit permanently frozen (the whole reason prices froze).
_MAX_BACKOFF = 30.0


def parse_ws_message(raw: object) -> list[dict]:
    """Alpaca frames arrive as a JSON array of objects; normalise to a list of dicts."""
    if isinstance(raw, list):
        return [f for f in raw if isinstance(f, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


class AlpacaWebSocketClient:
    """One authenticated Alpaca MD socket. Auto-reconnects and resubscribes while running."""

    def __init__(
        self,
        url: str,
        key: str,
        secret: str,
        handler: Callable[[dict], Awaitable[None] | None],
        log,
        reconnect_secs: float = 3.0,
    ) -> None:
        self._url = url  # includes the feed, e.g. wss://stream.data.alpaca.markets/v2/iex
        self._key = key
        self._secret = secret
        self._handler = handler  # called once per data frame (dict)
        self._log = log
        self._reconnect_secs = reconnect_secs

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._run_task: asyncio.Task | None = None
        self._running = False
        self._subs: dict[str, set[str]] = {c: set() for c in _CHANNELS}

    async def start(self) -> None:
        """Start the client WITHOUT ever aborting on a failed initial connect. One best-effort connect
        attempt, then hand off to the self-healing `_run` loop (which connects-if-needed and reconnects
        with backoff forever). This unblocks node startup: a DNS/network blip at boot no longer kills the
        data client permanently (that bug left the cockpit frozen until a manual engine restart)."""
        self._running = True
        self._session = aiohttp.ClientSession()
        try:
            await self._connect_and_auth()
        except Exception as exc:  # noqa: BLE001 — the run loop will keep retrying; don't abort startup
            self._log.warning(f"Alpaca WS initial connect failed: {exc!r} — retrying in background")
        self._run_task = asyncio.create_task(self._run(), name="alpaca-ws-read")

    @property
    def connected(self) -> bool:
        """True while the socket is open — for surfacing a degraded/stale state to the UI (#26)."""
        return self._ws is not None and not self._ws.closed

    async def stop(self) -> None:
        self._running = False
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def subscribe(self, channel: str, symbols: list[str]) -> None:
        """Add `symbols` to `channel` and push the subscription (if connected)."""
        if channel not in self._subs:
            raise ValueError(f"unknown channel {channel!r}")
        new = [s for s in symbols if s not in self._subs[channel]]
        if not new:
            return
        self._subs[channel].update(new)
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"action": "subscribe", channel: new})

    async def unsubscribe(self, channel: str, symbols: list[str]) -> None:
        drop = [s for s in symbols if s in self._subs[channel]]
        if not drop:
            return
        self._subs[channel].difference_update(drop)
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_json({"action": "unsubscribe", channel: drop})

    # -- internals --------------------------------------------------------------------------------
    async def _connect_and_auth(self, auth_timeout: float = 15.0) -> None:
        assert self._session is not None
        self._ws = await self._session.ws_connect(self._url, heartbeat=30.0)
        # Bound the handshake so a server that connects but never authenticates can't hang the client.
        await asyncio.wait_for(self._await_authenticated(), timeout=auth_timeout)
        await self._resubscribe()
        self._log.info("Alpaca WS connected + authenticated")

    async def _await_authenticated(self) -> None:
        """Wait for `connected`, send auth, wait for `authenticated` — tolerant of frame batching."""
        assert self._ws is not None
        authed = False
        auth_sent = False
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            for frame in parse_ws_message(msg.json()):
                t, m = frame.get("T"), frame.get("msg")
                if t == "error":
                    raise RuntimeError(f"Alpaca WS error during auth: {frame}")
                if t == "success" and m == "connected" and not auth_sent:
                    await self._ws.send_json(
                        {"action": "auth", "key": self._key, "secret": self._secret}
                    )
                    auth_sent = True
                elif t == "success" and m == "authenticated":
                    authed = True
            if authed:
                return
        raise RuntimeError("Alpaca WS closed before authentication completed")

    async def _resubscribe(self) -> None:
        payload = {c: sorted(s) for c, s in self._subs.items() if s}
        if payload and self._ws is not None:
            await self._ws.send_json({"action": "subscribe", **payload})

    async def _run(self) -> None:
        """Self-healing read loop. Connects if not connected, reads until the socket drops, then reconnects
        — forever while running, with exponential backoff + jitter (capped). Never gives up, so the stream
        recovers on its own from any transient DNS/network/venue outage."""
        backoff = self._reconnect_secs
        while self._running:
            if self._ws is None or self._ws.closed:
                try:
                    await self._connect_and_auth()
                    backoff = self._reconnect_secs  # reset on a clean connect
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — keep retrying indefinitely
                    if not self._running:
                        return
                    delay = backoff + random.uniform(0, backoff * 0.3)  # jitter avoids thundering herd
                    self._log.error(f"Alpaca WS connect failed: {exc!r}; retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue

            try:
                assert self._ws is not None
                async for msg in self._ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    for frame in parse_ws_message(msg.json()):
                        await self._dispatch(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — transport must survive any read error
                self._log.error(f"Alpaca WS read error: {exc!r}; reconnecting")
            # Socket closed — loop back to reconnect (unless stopping).
            if self._running:
                self._log.warning("Alpaca WS closed; reconnecting")

    async def _dispatch(self, frame: dict) -> None:
        t = frame.get("T")
        if t in _CONTROL_TYPES:
            if t == "error":
                self._log.error(f"Alpaca WS error frame: {frame}")
            return
        result = self._handler(frame)
        if result is not None:
            await result
