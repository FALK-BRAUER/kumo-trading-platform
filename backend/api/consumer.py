"""RedisConsumer (issue #20) — the UI-process view of the engine.

Reads the engine's `ui:stream` Redis Stream (bars + position snapshots) and keeps an in-memory view.
Holds NO Nautilus node — so this process can crash/restart freely without touching the engine. Implements
the same `Node` protocol the FastAPI WS/REST layer already depends on (positions/bars/bars_for), so the
serving layer is unchanged whether the engine is embedded (old) or streamed (this).

Startup reads the stream from id 0 (picks up the backfill seed already published), then tails live with a
blocking XREAD. Bars are de-duped by ts_event so historical+live overlap collapses cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import redis.asyncio as aioredis

from api.bar_spec import DEFAULT_GRANULARITY
from api.feed_config import STATE_KEY_PREFIX, STATE_KINDS, FeedConfig, load_feed_config
from api.models import (
    AccountDTO,
    BarDTO,
    ExternalActivityDTO,
    FillDTO,
    FundamentalsDTO,
    OrderDTO,
    PositionDTO,
    PriceDTO,
    QuoteDTO,
    TodayRangeDTO,
    TradeDTO,
    VwapDTO,
)

_log = logging.getLogger("kumo.consumer")


def _stream_name(stream) -> str:
    """redis-py hands the stream name back as bytes or str depending on decode settings."""
    return stream.decode() if isinstance(stream, (bytes, bytearray)) else str(stream)


class RedisConsumer:
    """Tails the engine's UI stream into an in-memory snapshot the FastAPI layer serves."""

    def __init__(self, cfg: FeedConfig | None = None) -> None:
        self._cfg = cfg or load_feed_config()
        bridge = self._cfg.ui_bridge
        self._stream_key: str = bridge.get("stream_key", "ui:stream")
        # Bars live on their own stream (#309) — sharing one with quotes meant a ~127k-frame backfill was
        # evicted inside 7.5 minutes and no browser ever saw history. Read BOTH: splitting the producer
        # without teaching the consumer to read the second stream would LOSE bars entirely, which is
        # strictly worse than evicting them.
        self._bar_stream_key: str = f"{self._stream_key}:bars"
        self._last_bar_id: str = "0-0"
        self._host: str = os.environ.get("KUMO_REDIS_HOST", bridge.get("redis_host", "127.0.0.1"))
        self._port = int(os.environ.get("KUMO_REDIS_PORT", bridge.get("redis_port", 6379)))
        self._redis: aioredis.Redis | None = None
        self._task: asyncio.Task | None = None
        self._state_task: asyncio.Task | None = None  # polls the latest-state Redis keys (#56)
        self._last_id = "0"  # start from the beginning to pick up the backfill seed
        self._bars: dict[tuple[str, str], dict[int, BarDTO]] = {}  # (symbol, granularity) → ts → bar
        self._positions: list[PositionDTO] = []
        self._trades: list[TradeDTO] = []  # trade-cycle projection plane (#73)
        self._trades_flows: dict | None = None  # per-lane net invested per session (#699 a)
        # What the ENGINE said about that projection (#298). Dropping this is what let a broken
        # projection render as an empty book: the rows and the health travelled together and only the
        # rows survived the hop.
        self._trades_status: str | None = None
        self._trades_realized: dict | None = None
        self._trades_periods: dict | None = None
        self._trades_periods_swept: dict | None = None
        self._trades_legs: dict | None = None
        self._trades_error: str | None = None
        self._external: list[ExternalActivityDTO] = []  # quarantine plane (#79)
        self._prices: dict[str, PriceDTO] = {}  # instrument_id → latest real-time trade (live-price plane)
        self._quotes: dict[str, QuoteDTO] = {}  # instrument_id → latest NBBO bid/ask (#40 spread/mid plane)
        self._vwaps: dict[str, VwapDTO] = {}  # instrument_id → latest session VWAP (#182 follow-up, KPI Phase 2)
        self._today_ranges: dict[str, TodayRangeDTO] = {}  # instrument_id → latest today-range/prior-close (KPI Phase 3)
        self._fundamentals: dict[str, FundamentalsDTO] = {}  # instrument_id → latest fundamentals (KPI Phase 2)
        self._account: AccountDTO | None = None  # latest account snapshot (#41 equity/cash/buying-power)
        self._fills: list[FillDTO] = []  # order fills streamed from the engine (#32), newest-bounded
        self._orders: dict[str, OrderDTO] = {}  # client_order_id → latest order state (#33 blotter)
        # Latest strategy-state frame (#212): lifecycle, last decision + reasons, journal, trail.
        # Kept whole rather than parsed into DTOs — the shape is the engine's, and two very different
        # consumers (a homescreen and the narrative evidence bundle) read different parts of it.
        self._session: dict = {}
        self._equity_curve: dict = {}
        #: The rotation read (#384) — published by the engine, no longer read off disk.
        self._rotation: dict = {}
        self._health: dict = {}  # latest engine health frame (#26): engine_ok, last_tick_ts, ts
        self._health_at: float = 0.0  # monotonic time the last health frame was received (bridge liveness)
        #: HOW OFTEN AND HOW BADLY THE BRIDGE HAS GONE QUIET (#859). A COUNT AND A MAX, never a single
        #: last-gap value: measured on paper 2026-09-11 00:34, the bridge does not drop once during
        #: warmup, it FLAPS — 9 s, 3 s and 7 s inside one 30 s span, each exceeding
        #: `_BRIDGE_STALE_SECS`. One number would have reported 9 s and hidden that it happened three
        #: times, which is the difference between "a slow boot" and "a transport that repeatedly
        #: starves". The cause is not known and this does not claim to explain it (see #879); it makes
        #: the next occurrence a measurement instead of an anecdote.
        self._bridge_gap_max_s: float = 0.0
        self._bridge_gaps: int = 0  # gaps that exceeded _BRIDGE_STALE_SECS, i.e. reader-visible outages
        self._acks: dict[str, dict] = {}  # command_id → engine command_ack (#39 ack correlation), bounded

    _MAX_BARS = 6000  # per (symbol, granularity) — bounds API-process memory (Redis maxlen bounds Redis)
    _BRIDGE_STALE_SECS = 6.0  # no health frame within this → engine/bridge considered down (cadence is ~2s)
    _MAX_FILLS = 500  # bound the in-memory fill blotter
    _MAX_ACKS = 2000  # bound the command-ack store (#39); rebuilt from the ui:stream backlog on api restart
    _MAX_TERMINAL_ORDERS = 200  # keep all working orders; bound only the closed/terminal ones
    _TERMINAL_STATUS = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "DENIED"})

    async def start(self) -> None:
        self._redis = aioredis.Redis(host=self._host, port=self._port, decode_responses=True)
        await self._redis.ping()
        await self._drain_backlog()  # warm the snapshot from existing stream data before serving
        await self._poll_state_once()  # warm the latest-state planes (keys) before serving (#56)
        self._task = asyncio.create_task(self._read_loop(), name="consumer-read-loop")
        self._task.add_done_callback(self._on_task_done)
        self._state_task = asyncio.create_task(self._poll_state_loop(), name="consumer-state-poll")
        self._state_task.add_done_callback(self._on_task_done)

    async def _drain_backlog(self) -> None:
        """Read the stream up to its current end (the backfill seed) so the first WS subscriber sees a
        warm snapshot. Bounded by a high-water id captured up front — a busy producer can't keep this
        looping forever; live entries after it are handled by _read_loop."""
        assert self._redis is not None
        try:
            newest = await self._redis.xrevrange(self._stream_key, count=1)
        except Exception as exc:  # noqa: BLE001
            _log.warning("backlog high-water read failed: %s", exc)
            return
        if not newest:
            return
        high_id = newest[0][0]
        while True:
            try:
                resp = await self._redis.xread(
                    {self._stream_key: self._last_id, self._bar_stream_key: self._last_bar_id},
                    count=1000, block=100,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("backlog drain failed: %s", exc)
                return
            if not resp:
                return
            for stream, entries in resp:
                is_bars = _stream_name(stream) == self._bar_stream_key
                for entry_id, fields in entries:
                    if is_bars:
                        self._last_bar_id = entry_id
                    else:
                        self._last_id = entry_id
                    self._apply(fields)
                    # The high-water id bounds the QUOTE stream only; the bar stream is finite by nature
                    # (one backfill), so draining it fully is what warms the charts.
                    if not is_bars and entry_id == high_id:
                        return

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("consumer task %s exited unexpectedly", task.get_name(), exc_info=exc)

    async def stop(self) -> None:
        for task_attr in ("_task", "_state_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def _read_loop(self) -> None:
        assert self._redis is not None
        while True:
            try:
                resp = await self._redis.xread(
                    {self._stream_key: self._last_id, self._bar_stream_key: self._last_bar_id},
                    count=500, block=5000,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # redis hiccup — log, back off, keep serving the last snapshot
                _log.warning("xread failed: %s", exc)
                await asyncio.sleep(1.0)
                continue
            for stream, entries in resp or []:
                is_bars = _stream_name(stream) == self._bar_stream_key
                for entry_id, fields in entries:
                    # Each stream keeps its OWN cursor. Writing a bar-stream id into `_last_id` would
                    # make the quote cursor jump to an unrelated position and skip live frames.
                    if is_bars:
                        self._last_bar_id = entry_id
                    else:
                        self._last_id = entry_id
                    self._apply(fields)

    # Latest-state planes (#56) live on Redis KEYS, not the bar stream — only the newest value matters and
    # their 2s cadence would evict the low-volume historical bars. We poll the keys and feed each through the
    # SAME _apply handlers (kind → store), so the plane logic is unchanged; only the transport differs.
    # STATE_KINDS/STATE_KEY_PREFIX are shared with the producer (feed_config) — one source of truth.
    _STATE_POLL_SECS = 1.0

    async def _poll_state_once(self) -> None:
        assert self._redis is not None
        for kind in STATE_KINDS:
            raw = await self._redis.get(f"{STATE_KEY_PREFIX}{kind}")
            if raw is not None:
                self._apply({"type": kind, "payload": raw})

    async def _poll_state_loop(self) -> None:
        while True:
            await asyncio.sleep(self._STATE_POLL_SECS)
            try:
                await self._poll_state_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # redis hiccup — keep serving the last snapshot, retry next tick
                _log.warning("state poll failed: %s", exc)

    def _apply(self, fields: dict) -> None:
        """Apply one stream entry. Any malformed frame is logged and skipped — never allowed to kill the
        read loop and leave the UI serving stale state forever."""
        try:
            payload = json.loads(fields.get("payload", "{}"))
            if not isinstance(payload, dict):  # e.g. a bare list — would blow up on .get below
                return
            kind = fields.get("type")
            if kind == "bar":
                self._apply_bar(payload)
            elif kind == "session":  # strategy plane (#212)
                self._session = payload
            elif kind == "equity_curve":  # the account's own history, per period (#243)
                self._equity_curve = payload
            elif kind == "rotation":  # the rotation read, graded off our own bars (#384)
                self._rotation = payload
            elif kind == "positions":
                self._positions = [PositionDTO(**p) for p in payload.get("positions", [])]
            elif kind == "trades":  # trade-cycle plane (#73) — latest full set of cycles
                self._trades = [TradeDTO(**t) for t in payload.get("trades", [])]
                self._trades_status = payload.get("status")
                self._trades_error = payload.get("error")
                # Realized P&L for the session (#233). Carried through rather than recomputed: the
                # engine reads it from native closed positions, which this process cannot see.
                self._trades_realized = payload.get("realized_session")
                # Per-period realized (#322). Only overwritten when the frame CARRIES it: the engine
                # publishes trades every 2s and sweeps the broker every 5min, so an absent key means
                # "this frame has nothing to say", not "the periods are now unknown".
                if payload.get("realized_periods") is not None:
                    self._trades_periods = payload.get("realized_periods")
                # The broker sweep beside the legs, and what the legs were computed over (#846). Same
                # rule: only a frame that CARRIES the key overwrites.
                if "realized_periods_swept" in payload:
                    self._trades_periods_swept = payload.get("realized_periods_swept")
                if payload.get("realized_legs") is not None:
                    self._trades_legs = payload.get("realized_legs")
                # Per-lane net invested per session (#699 a). Same rule: only a frame that CARRIES the
                # key overwrites — an absent key says nothing, it does not blank the flows.
                if payload.get("lane_flows") is not None:
                    self._trades_flows = payload.get("lane_flows")
            elif kind == "external_activity":  # quarantine plane (#79) — activity the cockpit didn't originate
                self._external = [ExternalActivityDTO(**e) for e in payload.get("external", [])]
            elif kind == "price":
                self._prices[payload["instrument_id"]] = PriceDTO(**payload)
            elif kind == "quote":
                self._quotes[payload["instrument_id"]] = QuoteDTO(**payload)
            elif kind == "vwap":
                # A `vwap: null` frame is the engine's session-boundary tombstone (codex review, Phase 2:
                # without this, a prior session's value would sit here indefinitely — the engine's own
                # publish gate only fires again once real volume lands THIS session, which can be well
                # after the boundary for an illiquid symbol).
                if payload.get("vwap") is None:
                    self._vwaps.pop(payload["instrument_id"], None)
                else:
                    self._vwaps[payload["instrument_id"]] = VwapDTO(**payload)
            elif kind == "today_range":
                # A `high: null` frame is the engine's staleness tombstone (codex review, Phase 3: a
                # symbol whose dailyBar has aged out without a fresh replacement must not keep rendering
                # yesterday's range as today's).
                if payload.get("high") is None:
                    self._today_ranges.pop(payload["instrument_id"], None)
                else:
                    self._today_ranges[payload["instrument_id"]] = TodayRangeDTO(**payload)
            elif kind == "fundamentals":
                # No tombstone here, unlike vwap/today_range — a fetch failure keeps the prior value
                # (correct: fundamentals have no daily-boundary reset, yesterday's EPS is still today's).
                self._fundamentals[payload["instrument_id"]] = FundamentalsDTO(**payload)
            elif kind == "account":
                self._account = AccountDTO(**payload)
            elif kind == "fill":
                self._fills.append(FillDTO(**payload))
                if len(self._fills) > self._MAX_FILLS:
                    self._fills = self._fills[-self._MAX_FILLS :]
            elif kind == "orders":  # full snapshot of open orders — reconciling backstop (#33)
                self._reconcile_orders(payload.get("orders", []), payload.get("ts"))
            elif kind == "order":  # single lifecycle event — instant status (#33)
                self._upsert_order(payload)
            elif kind == "health":
                # Bridge-liveness is derived from _health_at (last time a FRESH frame arrived). Health now
                # comes from a polled key (#56): a stale key returns the same value every poll, so refresh the
                # liveness clock ONLY when the engine's own ts advances — else a dead engine looks alive.
                if payload.get("ts") != self._health.get("ts"):
                    now = time.monotonic()
                    # MEASURED AT ARRIVAL, on the same clock `_bridge_ok` reads, so the number means
                    # exactly what the staleness verdict means. It records how long readers were shown
                    # an unknown — not how late the ENGINE was; those are different questions and #879
                    # owns the second one. The first gap after boot is skipped: there is no previous
                    # arrival to measure from, and counting startup as an outage would put a permanent
                    # 1 in a counter whose whole value is that a non-zero reading is unusual.
                    if self._health_at:
                        gap = now - self._health_at
                        self._bridge_gap_max_s = max(self._bridge_gap_max_s, gap)
                        if gap > self._BRIDGE_STALE_SECS:
                            self._bridge_gaps += 1
                    self._health_at = now
                self._health = payload
            elif kind == "command_ack":  # engine ack for a UI command (#39): id → status/error
                self._store_ack(payload)
        except Exception as exc:  # noqa: BLE001 — quarantine any bad entry, keep the read loop alive
            _log.warning("skipping bad stream entry (%s): %s", fields.get("type"), exc)

    def _store_ack(self, ack: dict) -> None:
        """Store an engine command_ack keyed by command_id (#39). Bounded: drop the oldest by receive order
        (dict preserves insertion order) once past the cap — the store is a short-lived correlation cache, not
        a durable ledger (that's #78)."""
        cid = ack.get("id")
        if not cid:
            return
        self._acks[cid] = ack
        if len(self._acks) > self._MAX_ACKS:
            for old in list(self._acks)[: len(self._acks) - self._MAX_ACKS]:
                del self._acks[old]

    def command_status(self, command_id: str) -> dict | None:
        """The engine's ack for a command, or None if not seen yet (#39). None → caller reports `pending`
        (and its own timeout → `unknown`); a stored ack maps engine status ok→accepted, error→rejected."""
        return self._acks.get(command_id)

    def _upsert_order(self, d: dict) -> None:
        """Insert/replace an order by client_order_id, then prune terminal orders past the cap. Working
        orders are never pruned by the cap — but the snapshot reconcile (below) drops stale working ones."""
        dto = OrderDTO(**d)
        self._orders[dto.client_order_id] = dto
        terminal = [o for o in self._orders.values() if o.status in self._TERMINAL_STATUS]
        if len(terminal) > self._MAX_TERMINAL_ORDERS:
            for o in sorted(terminal, key=lambda x: x.ts_last)[: len(terminal) - self._MAX_TERMINAL_ORDERS]:
                del self._orders[o.client_order_id]

    def _reconcile_orders(self, snapshot: list[dict], snapshot_ts: int | None = None) -> None:
        """Apply an `orders` snapshot (the engine's full open-order set). Upsert every order in it, then
        DROP any locally-working order absent from the snapshot — it closed while we missed its terminal
        event. Without this, a missed CANCELED/FILLED leaves a phantom 'working' order that never clears
        and leaks memory (the terminal cap can't reach it). Terminal orders we already recorded are kept.

        `snapshot_ts` (#56) is when the engine took this snapshot. Since the snapshot is now polled off a key
        while `order` lifecycle events still arrive immediately over the stream, a snapshot can be STALER than
        what we already hold. So the reconcile is causally gated: a snapshot order is upserted only if it is at
        least as fresh as the one we hold (never revert a newer streamed event), and a working order absent
        from the snapshot is dropped only if the snapshot is at least as new as that order's last event (never
        drop an order the snapshot simply predates). Legacy callers pass no ts → unconditional drop (old
        behavior)."""
        by_id = {o.get("client_order_id"): o for o in snapshot}
        for coid, o in by_id.items():
            existing = self._orders.get(coid)
            if existing is None or o.get("ts_last", 0) >= existing.ts_last:
                self._upsert_order(o)
                continue
            # THE ENGINE IS THE AUTHORITY ON WHAT IS OPEN (#816). The row-ts gate above exists for a
            # LAGGED snapshot (taken before a streamed event). A snapshot taken AFTER the terminal event
            # we hold, that still lists the order as open, is not lag — the engine's truth was rewritten
            # under us (a cache repair, #807/#809). Measured 2026-09-09: three revived stops sat REJECTED
            # on /orders for four hours because their newest event (the original OrderAccepted) was
            # older than the rejection this process remembered. Terminal → open is only ever a rewrite.
            if (
                existing.status in self._TERMINAL_STATUS
                and o.get("status") not in self._TERMINAL_STATUS
                and snapshot_ts is not None
                and snapshot_ts > existing.ts_last
            ):
                self._upsert_order(o)
        for coid, order in list(self._orders.items()):
            if order.status in self._TERMINAL_STATUS or coid in by_id:
                continue
            if snapshot_ts is None or order.ts_last <= snapshot_ts:
                del self._orders[coid]

    def _apply_bar(self, p: dict) -> None:
        key = (p["instrument_id"], p.get("granularity", DEFAULT_GRANULARITY))
        series = self._bars.setdefault(key, {})
        series[int(p["ts_event"])] = BarDTO(
            instrument_id=p["instrument_id"],
            open=p["open"],
            high=p["high"],
            low=p["low"],
            close=p["close"],
            volume=p["volume"],
            ts_event=int(p["ts_event"]),
        )
        if len(series) > self._MAX_BARS:  # drop the oldest to bound memory (unbounded stream over time)
            for ts in sorted(series)[: len(series) - self._MAX_BARS]:
                del series[ts]

    # --- Node protocol (read surface for FastAPI) -------------------------------------------------
    def positions(self) -> list[PositionDTO]:
        return sorted(self._positions, key=lambda p: p.instrument_id)

    def trades_health(self) -> tuple[str | None, str | None]:
        """(status, error) as the ENGINE reported them for the last trades frame (#298).

        A STALE FRAME REPORTS STALENESS, NOT ITS OLD STATUS. The worse half of the incident was not the
        missing book — it was `seeding` still being asserted, which told the UI the emptiness was
        legitimate and kept "Reconciling with the broker…" on screen indefinitely. A false explanation
        is more convincing than absent data.
        """
        if not self._bridge_ok():
            return "stale", "no trades frame from the engine recently — the book below is not current"
        return self._trades_status, self._trades_error

    def trades_realized(self) -> dict | None:
        """Session realized P&L as the engine computed it (#233), or None from a node with no engine.

        None and zero are DIFFERENT answers and the UI relies on it: None means "no engine said", and
        the tile falls back to the live-cycle sum; zero means "the engine looked and nothing closed".
        """
        return self._trades_realized

    def trades_periods(self) -> dict | None:
        """Realized P&L per period from Nautilus's own closed legs (#846), `source: "legs"`. None = no
        trades frame yet."""
        return self._trades_periods

    def trades_flows(self) -> dict | None:
        """Per-lane net invested per ET session from the engine's cache orders (#699 a). None = no
        trades frame carried it yet (an older engine, or none)."""
        return getattr(self, "_trades_flows", None)

    def trades_periods_swept(self) -> dict | None:
        """The broker fill sweep's periods (Alpaca only). None = not swept, or a venue with no ledger."""
        return self._trades_periods_swept

    def trades_legs(self) -> dict | None:
        """What the legs windows were computed over: `{state, restored, held, live, seed_stopped_at,
        complete, error}` — `state` in {never_ran, stopped_early, complete}."""
        return self._trades_legs

    def trades(self) -> list[TradeDTO]:
        """The trade-cycle plane (#73) — one TradeDTO per live-or-just-closed cycle, HELD first then by symbol."""
        # STALE IS NOT EMPTY, AND IT IS NOT LAST-KNOWN EITHER. Serving the last frame forever is what
        # showed one position out of thirty-two for fifteen minutes; `trades_health` below reports the
        # staleness so the tile can say so rather than drawing a book nobody vouches for.
        if not self._bridge_ok():
            return []
        order = {"HELD": 0, "ARMED": 1, "WATCH": 2, "CLOSED": 3}
        return sorted(self._trades, key=lambda t: (order.get(t.state, 9), t.instrument_id))

    def external(self) -> list[ExternalActivityDTO]:
        """The external-activity quarantine plane (#79) — activity the cockpit didn't originate."""
        return sorted(self._external, key=lambda e: (e.instrument_id, e.source))

    def fills(self) -> list[FillDTO]:
        return sorted(self._fills, key=lambda f: f.ts_event)

    def orders(self) -> list[OrderDTO]:
        """The Orders blotter: working orders first, then terminal — each group newest-first (by ts_last)."""
        return sorted(
            self._orders.values(),
            key=lambda o: (o.status in self._TERMINAL_STATUS, -o.ts_last),
        )

    def prices(self) -> list[PriceDTO]:
        """Latest real-time trade per instrument — the live-price plane (one topic, all symbols)."""
        return sorted(self._prices.values(), key=lambda p: p.instrument_id)

    def quotes(self) -> list[QuoteDTO]:
        """Latest NBBO bid/ask per instrument — the spread/mid plane (#40)."""
        return sorted(self._quotes.values(), key=lambda q: q.instrument_id)

    def vwaps(self) -> list[VwapDTO]:
        """Latest session VWAP per instrument (#182 follow-up, KPI Phase 2) — absent for a symbol until
        real volume has accumulated this session."""
        return sorted(self._vwaps.values(), key=lambda v: v.instrument_id)

    def today_ranges(self) -> list[TodayRangeDTO]:
        """Latest today's-range/prior-close per instrument (#182 follow-up, KPI Phase 3) — refreshed on
        the watchlist timer's ~5s cadence; absent for a symbol until the first successful batch fetch."""
        return sorted(self._today_ranges.values(), key=lambda r: r.instrument_id)

    def fundamentals(self) -> list[FundamentalsDTO]:
        """Latest fundamentals per instrument (#182 follow-up, KPI Phase 2) — refreshed on its own
        low-frequency timer; absent for a symbol until the first successful fetch, then persists (no
        tombstone) through transient failures."""
        return sorted(self._fundamentals.values(), key=lambda f: f.instrument_id)

    # --- transfer support (#80 spin-off) ---------------------------------------------------------------
    # Snapshot reads for request shaping only. The engine re-validates every transfer against its own live
    # cache before applying one — the api process holds stale snapshots and cannot enforce anything.

    def strategy_position(self, instrument_id: str, strategy_id: str, side: str) -> dict | None:
        """The position a transfer would move, matched on the FULL key.

        Instrument alone is ambiguous: the same symbol can sit under an owned strategy AND under EXTERNAL,
        and a side flip is a different position entirely."""
        for row in self.external():
            if (
                row.source == "POSITION"
                and row.instrument_id == instrument_id
                and row.strategy_id == strategy_id
                and row.side == side
            ):
                return {"quantity": row.quantity, "avg_px": row.avg_px or 0.0, "ts_last": row.ts_last}
        for p in self.positions():
            if p.instrument_id == instrument_id and p.strategy_id == strategy_id and p.side == side:
                # THE REAL VALUE, not a placeholder (#785). This branch hardcoded 0, so the
                # engine's staleness check compared 0 against the position's actual ts_last and
                # refused EVERY transfer out of a lane as stale — a lock that could never match.
                # The EXTERNAL branch above always returned a real one, which is why the outbox
                # holds only EXTERNAL -> MANUAL transfers.
                return {"quantity": p.quantity, "avg_px": p.avg_px_open, "ts_last": p.ts_last}
        return None

    def account(self) -> AccountDTO | None:
        """Latest account snapshot — equity/cash/buying-power (#41), or None until the first frame."""
        return self._account

    async def send_command(self, ctype: str, payload: dict) -> str:
        """Publish a typed command to the engine over the `ui:commands` stream (command layer #32). Returns
        the command id (for ack correlation). The reverse of the ui:stream data plane."""
        cid = uuid.uuid4().hex
        # Fail LOUD if the command bus is unavailable — the endpoint must 503, never return ok for a command
        # that was never enqueued (#39: REST 'ok' must mean 'enqueued', not silently dropped). An XADD error
        # propagates for the same reason.
        if self._redis is None:
            raise RuntimeError("command bus unavailable — no Redis connection")
        await self._redis.xadd(
            "ui:commands",
            {"id": cid, "type": ctype, "payload": json.dumps(payload), "ts": str(time.time())},
            maxlen=10000,
            approximate=True,  # bound the command stream (the group cursor tracks real progress)
        )
        return cid

    async def request_stream(self, instrument_id: str) -> None:
        """Ask the engine to stream `instrument_id` on demand (a viewed search result) — now a real
        `stream_request` command over the command layer (#32), replacing the old TTL Redis-key poll patch."""
        await self.send_command("stream_request", {"instrument_id": instrument_id})

    def session(self) -> dict:
        """The strategy's own state (#212), as last published — lifecycle, the latest decision with
        its per-symbol reasons, recent journal rows and the exit trail.

        Empty dict before the first frame arrives; a caller must treat that as "not known yet" rather
        than "nothing happened", which are very different things on a homescreen."""
        return dict(self._session)

    def rotation(self) -> dict:
        """The last rotation payload the engine published.

        Empty dict means NOTHING HAS BEEN PUBLISHED YET — not an empty market. The route turns that into
        an explicit error, the same way it did when the payload came from a missing file: "there is no
        rotation right now" and "nobody has looked" are different claims.
        """
        return dict(self._rotation)

    def equity_curve(self) -> dict:
        """The account's own equity history per period (#243), as last published by the broker.

        Empty dict before the first frame — a caller must render "not known yet", never a flat line at
        zero, which would read as an account that has done nothing."""
        return dict(self._equity_curve)

    def _bridge_ok(self) -> bool:
        """Has a health frame arrived recently? ONE predicate, because two would disagree.

        Every plane below drops to a safe value when this is false — "a lane that WAS armed when the
        engine died is not armed now, it is not running at all". The TRADES plane did not, and on
        2026-08-26 it served a `seeding` frame from boot for fifteen minutes while the engine was
        publishing a full book into Redis at TTL 5. The UI showed one position out of thirty-two, and
        `/health` said `ok` the whole time.
        """
        age = time.monotonic() - self._health_at if self._health_at else None
        return age is not None and age < self._BRIDGE_STALE_SECS

    def _ever_bridged(self) -> bool:
        """Has a health frame EVER arrived? Not the same question as `_bridge_ok` (#859).

        "Stale" and "never told" are both not-bridged, and exactly one field cares which:
        `lanes_absent` retains its last value across staleness and has no last value at boot.

        The predicate is `_health_at`, not `_health`. Both start empty, but only `_health_at` is
        guaranteed to advance on arrival — a frame whose payload were somehow empty would still
        stamp the clock, whereas testing the payload would call that boot forever. It is the same
        sentinel `_bridge_ok` already reads, so the two cannot drift apart into disagreeing about
        whether we have heard from the engine.
        """
        return bool(self._health_at)

    def health(self) -> dict:
        """Observed engine/feed health (#26). `bridge_ok` = a health frame arrived recently (the engine is
        publishing over Redis); `engine_ok` + `last_tick_ts` come from that frame. The api never inspects
        the engine directly (separate process, #20) — this is what it can OBSERVE off the stream."""
        bridge_ok = self._bridge_ok()
        return {
            "bridge_ok": bridge_ok,
            # HOW OFTEN THE BRIDGE HAS GONE QUIET AND FOR HOW LONG (#859). NOT gated on `bridge_ok`:
            # these are facts about this process's whole life, not about the current frame, and
            # blanking them while the bridge is down would hide the number precisely when a reader
            # has come looking for it.
            "bridge_gaps": self._bridge_gaps,
            "bridge_gap_max_s": round(self._bridge_gap_max_s, 3),
            "engine_ok": bool(self._health.get("engine_ok")) if bridge_ok else False,
            "last_tick_ts": int(self._health.get("last_tick_ts", 0)),
            # The feed's print type (#834). Gated on the bridge like everything else here: a stale
            # frame must not keep asserting "DELAYED" — or "REALTIME" — after the engine has gone.
            "market_data_type": self._health.get("market_data_type") if bridge_ok else None,
            # Broker-vs-cache position drift (#26) — pass through only while the bridge is live (a stale frame
            # must not keep raising a drift banner after the engine has gone).
            "reconcile_drift": self._health.get("reconcile_drift", []) if bridge_ok else None,
            # Protective orders resting at the broker the engine cannot see (#454). Gated on
            # `bridge_ok` for the SAME reason as drift above — one staleness policy, not two — and a
            # test pins that the two stay identical. A reader distinguishes "no divergence" from "no
            # engine" via `bridge_ok`, which is why an empty list here is not a claim of health.
            # ARMED PER LANE. Dropped entirely on a stale frame rather than carried forward: a lane that
            # WAS armed when the engine died is not armed now, it is not running at all, and reporting
            # the last known value would show a dead lane as ready to trade.
            "armed_lanes": self._health.get("armed_lanes", {}) if bridge_ok else None,
            # THE THIRD FIELD LIST ON ONE PAYLOAD, and the one nothing was guarding. `unpriced_positions`
            # and `failed_requests` shipped with tests covering the engine, the model AND `/health`'s
            # construction, and were still empty on both live stacks — because this hop copies keys one
            # by one too, and neither was named here. Found by reading the running stacks, not by a test.
            #
            # Gated on `bridge_ok` like every sibling above: a dead engine's last known subscription
            # picture is not the current one, and an unpriceable book from a frame nobody is refreshing
            # would read as a live measurement.
            "unpriced_positions": self._health.get("unpriced_positions", []) if bridge_ok else None,
            "failed_requests": self._health.get("failed_requests", []) if bridge_ok else None,
            "flip_pending": self._health.get("flip_pending", []) if bridge_ok else None,
            # #873 phase 1: the per-lane market-aware container. None when the bridge is down (unreadable),
            # a dict otherwise — its own `contract`/`lanes` carry the inner three states.
            "market_aware": self._health.get("market_aware") if bridge_ok else None,
            "subscriptions": self._health.get("subscriptions", {}) if bridge_ok else None,
            "book_truth": self._health.get("book_truth", {}) if bridge_ok else None,
            "inferred_fills": self._health.get("inferred_fills", []) if bridge_ok else None,
            "fills_on_terminal_orders": self._health.get("fills_on_terminal_orders", []) if bridge_ok else None,
            "fills_on_terminal_orders_dropped": self._health.get("fills_on_terminal_orders_dropped", 0) if bridge_ok else None,
            "venue_unanswered_lookups": self._health.get("venue_unanswered_lookups") if bridge_ok else None,
            # IB shortability (#857): three states on the frame; None = no such plane on this node.
            "shortable": self._health.get("shortable") if bridge_ok else None,
            "observations": self._health.get("observations", {}) if bridge_ok else None,
            "log_compaction": self._health.get("log_compaction") if bridge_ok else None,
            "realized_legs": self._health.get("realized_legs") if bridge_ok else None,
            # THE ENGINE'S ANSWER, not a second derivation. `app._feed_stale` documents itself as
            # "READ, never re-derived" because the ENGINE owns the venue calendar — and then this hop
            # dropped it, so that read returned None on every call and the stale-feed check could
            # never fire. The seventh field to die in this gap on 2026-08-31.
            #
            # None is a real third answer here (boot before the open, a day the venue never
            # described) and must survive as None rather than becoming False.
            "feed_stale": self._health.get("feed_stale") if bridge_ok else None,
            # Orders reconciliation had to SKIP (#643) — shares they reserve and protection they
            # provide are invisible to the engine. Published since #643 and dropped HERE, so the
            # alert written for them iterated an empty list on every poll for its entire life.
            # Not a model field: this one is consumed by the alerts service, not by /health.
            "unreconciled_orders": self._health.get("unreconciled_orders", []) if bridge_ok else None,
            # WHEN EACH LANE NEXT DECIDES, for the deploy gate. Dropped on a stale frame for the same
            # reason as `armed_lanes` above: a fire time from a dead engine is not a schedule, and the
            # gate must not wave a deploy through on a stale "nothing due for hours".
            #
            # THIS DICT IS AN ALLOW-LIST, AND THAT IS WHY THIS LINE EXISTS. The engine published
            # `next_fire_ns`, it reached the bridge, and `/strategies` still read None — because a key
            # nobody names here is silently gone. That is the FOURTH field lost to this exact seam
            # after `last_equity`, `realized_session` and `realized_periods`, and it cost a deploy
            # cycle to find again.
            "next_fire_ns": self._health.get("next_fire_ns", {}) if bridge_ok else None,
            # LANES ABSENT (#539). Carried on a stale frame UNLIKE the two above, deliberately: a
            # lane that failed to build is still absent when the engine dies, and forgetting it on
            # staleness would turn a real outage into a clean-looking one at exactly the wrong moment.
            # THREE STATES, AND THIS IS THE FIELD WHERE THE DIFFERENCE IS SHARPEST (#859).
            #
            # RETAINED WHEN STALE, deliberately and unlike every sibling above: a lane that failed to
            # build is still absent when the engine dies, and forgetting it on staleness would turn a
            # real outage into a clean-looking one at exactly the wrong moment. That argument is
            # correct — and it is an argument about STALE, where we were told once and the fact has
            # not expired.
            #
            # It says nothing about NEVER TOLD. At boot there is no last known value to retain, so
            # `{}` there is not "no lane failed to build", it is "nobody has looked yet" — and
            # `_inert_contradictions` tests for None to decide exactly that, which is why it has
            # never fired: `{}` walked straight past its unknown-guard on every boot.
            "lanes_absent": (self._health.get("lanes_absent") or {}) if self._ever_bridged() else None,
            "protection_divergence": (
                self._health.get("protection_divergence", []) if bridge_ok else None
            ),
            # HOW MANY LANES CAN TRADE (#454). `None` on a stale frame, NOT 0 — "no lanes running" from
            # a dead engine is a different claim from the same words about a live one, and only the
            # second is something we know. Reporting 0 would raise a lane-down alarm on every restart,
            # and an alarm that fires on the normal path gets switched off.
            "automated_lanes_registered": (
                self._health.get("automated_lanes_registered") if bridge_ok else None
            ),
            "automated_lanes_running": (
                self._health.get("automated_lanes_running") if bridge_ok else None
            ),
            # POSITIONS LEFT BARE BY AN EXIT THAT NEVER LANDED (#546). Forwarded HERE as well as in
            # the response model: this projection is an explicit allowlist and /health builds from
            # a second one, so a field added to the DTO alone still dies at this gate. Empty on a
            # stale frame like the divergence above — "nothing bare" from a dead engine is a claim
            # we cannot make, but an empty list is what a consumer can safely render.
            "naked_after_reject": (
                self._health.get("naked_after_reject", []) if bridge_ok else None
            ),
        }

    def bars(self) -> list[BarDTO]:
        """Default-granularity series across the streamed universe (the WS snapshot's price history)."""
        out = [
            bar
            for (_sym, gran), by_ts in self._bars.items()
            if gran == DEFAULT_GRANULARITY
            for bar in by_ts.values()
        ]
        out.sort(key=lambda b: (b.ts_event, b.instrument_id))
        return out

    async def bars_for(self, symbol: str, granularity: str) -> list[BarDTO]:
        """One symbol's bars at a granularity — whatever the engine has streamed for it."""
        by_ts = self._bars.get((symbol, granularity), {})
        return sorted(by_ts.values(), key=lambda b: b.ts_event)
