"""FastAPI app — the UI↔Nautilus bridge for the #4 vertical slice.

REST is render-truth (read the cache); WS is the live stream the UI subscribes to (no polling).
The UI never touches the engine/broker directly — only these endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

# VALIDATES THE DECLARED REGISTRY AT BOOT (#888). `strategy_registry` self-checks on import; every
# other importer is lazy inside a function, so without this the first refusal would surface on the
# first `/strategies` request rather than when the process starts. `engine_node` does the same.
import api.strategy_registry  # noqa: F401
from api import pool, settings
from api import watchlist as wl
from api.checks import check_postgres, check_redis
from api.claims_endpoint import CLAIMS_SQL, account_from_positions, build_breaches
from api.db.engine import get_session
from api.instrument_search import EngineSearchIndex, InstrumentSearchIndex
from api.ownership import short_violations
from api.eod_observation_store import EodObservationStore
from api.pnl_base import lane_net_terms, unrealized_base_by_period
from api.models import (
    WindowBaseResponse,
    AccountDTO,
    AttachManagerRequest,
    BracketRequest,
    CancelManagerRequest,
    CommandResponse,
    CommandStatusResponse,
    ControlFrame,
    EventFrame,
    ExternalActivityResponse,
    FlattenRequest, LiquidateLaneRequest,
    HealthResponse,
    InstrumentSearchResponse,
    ManagerDTO,
    ManagersResponse,
    ModifyOrderRequest,
    OrderRequest,
    OrdersResponse,
    PoolEntryDTO,
    PoolOverrideRequest,
    PoolResponse,
    PoolSourceDTO,
    PoolSourceRefreshRequest,
    PositionsResponse,
    StatusMessage,
    SubsystemHealth,
    Topic,
    TradesResponse,
    TransferDTO,
    TransferRequestBody,
    TransfersResponse,
    WatchlistAdd,
    WatchlistResponse,
    WSMessage,
)
from api.node_factory import Node, create_node

_log = logging.getLogger("kumo.api")


# How often the per-connection push loop polls the node for new data to stream after replay_end.
# Matches the UI's countdown bar (RefreshBar) cadence — keep the two in sync.
LIVE_PUSH_INTERVAL = 1.0


async def _send(websocket: WebSocket, frame: EventFrame) -> None:
    await websocket.send_text(frame.model_dump_json())


async def _snapshot_for(node: Node, topic: Topic) -> dict | None:
    """The current snapshot payload for a topic, or None if the channel/params can't be served.

    A valid `bars` topic with no data yet returns an EMPTY snapshot (not None) — the tile shows empty,
    not an error, and the push loop fills it in as data arrives. The `bars` topic takes a `granularity`
    param (1m|1h|1d, default 1d).
    """
    if topic.channel == "positions":
        return {"positions": [p.model_dump() for p in node.positions()]}
    if topic.channel == "trades":  # trade-cycle plane (#73) — re-pushed each tick like positions
        # Health travels WITH the rows (#298). The re-push rebuilds the frame from `node.trades()`, so
        # anything not carried here is silently lost between the engine and the tile — which is exactly
        # how a failed projection arrived at the UI looking like a flat book.
        status, error = node.trades_health()
        # REALIZED RIDES HERE TOO, and this line is the whole of #233's second act. The re-push
        # REBUILDS the frame rather than forwarding what the engine published, so any field not named
        # here is silently absent — and the UI is WS-driven, so REST having it proves nothing. The
        # first fix added both fields to `/trades` and a test pinned `/trades`; the tile went on
        # rendering a dash because it never reads that endpoint. Operator: "just the chart. Numbers
        # missing."
        return {"trades": [t.model_dump() for t in node.trades()], "status": status, "error": error,
                "realized_session": node.trades_realized(),
                "realized_periods": node.trades_periods(),
                "realized_periods_swept": node.trades_periods_swept(),
                "realized_legs": node.trades_legs()}
    if topic.channel == "external_activity":  # quarantine plane (#79)
        return {"external": [e.model_dump() for e in node.external()]}
    if topic.channel == "fills":
        return {"fills": [f.model_dump() for f in node.fills()]}
    if topic.channel == "orders":  # Orders blotter (#33) — working + terminal, re-pushed each tick
        return {"orders": [o.model_dump() for o in node.orders()]}
    if topic.channel == "prices":
        # Live-price plane (#28): one topic, latest real-time trade for every streamed symbol. The push
        # loop re-pushes this snapshot each tick (like positions), so the UI's last price stays live.
        return {"prices": [p.model_dump() for p in node.prices()]}
    if topic.channel == "quotes":  # NBBO bid/ask plane (#40) — one topic, latest quote per symbol
        return {"quotes": [q.model_dump() for q in node.quotes()]}
    if topic.channel == "vwaps":  # session VWAP plane (#182 follow-up, KPI Phase 2) — one topic, all symbols
        return {"vwaps": [v.model_dump() for v in node.vwaps()]}
    if topic.channel == "today_ranges":  # today's-range/prior-close plane (#182 follow-up, KPI Phase 3)
        return {"today_ranges": [r.model_dump() for r in node.today_ranges()]}
    if topic.channel == "fundamentals":  # fundamentals plane (#182 follow-up, KPI Phase 2)
        return {"fundamentals": [f.model_dump() for f in node.fundamentals()]}
    if topic.channel == "session":  # strategy plane (#212) — lifecycle, decision, journal, trail
        return node.session()
    if topic.channel == "equity_curve":  # the account's own history per period (#243)
        return node.equity_curve()
    if topic.channel == "rotation":  # the rotation read, graded off our own bars (#384)
        # A PLANE, LIKE EVERY OTHER FACT THE UI READS. 2026-08-21: "It should work similar to
        # trend lines, watch, portfolio etc." He is right and the first cut of #384 only went half way:
        # it moved the SOURCE into the engine but left the TRANSPORT as a 60s REST poll, so the Market
        # tile was still the one tile asking for its data instead of being told.
        return node.rotation()
    if topic.channel == "account":  # account plane (#41) — equity/cash/buying-power, re-pushed each tick
        acc = node.account()
        return {"account": acc.model_dump() if acc else None}
    if topic.channel == "bars":
        symbol = topic.params.get("symbol")
        if not symbol:
            return None
        granularity = topic.params.get("granularity", "1d")
        return {"bars": [b.model_dump() for b in await node.bars_for(symbol, granularity)]}
    return None


async def _push_loop(
    websocket: WebSocket,
    node: Node,
    topics: dict[str, Topic],
    cursors: dict[str, int],
    send_lock: asyncio.Lock,
) -> None:
    """After replay_end, keep the browser live: stream new `bars` as `bar` increments and re-push each
    `positions`/`fills` snapshot every tick. One loop per connection over all its subscribed topics. All
    sends go through the shared lock so they never interleave with the receive loop's frames on one socket."""
    try:
        while True:
            await asyncio.sleep(LIVE_PUSH_INTERVAL)
            for key, topic in list(topics.items()):
                if topic.channel == "bars":
                    symbol = topic.params.get("symbol")
                    granularity = topic.params.get("granularity", "1d")
                    if not symbol:
                        continue
                    # `>= cursor`, not `>`. The consumer OVERWRITES a bar with the same ts_event
                    # (consumer.py) because the venue revises them — Alpaca corrects a bar, and the
                    # forming candle updates in place all session. A strictly-greater cursor pushed
                    # the first version of the current bar and then never its corrections, so an open
                    # tab froze at the candle's first print while a tab opened seconds later showed
                    # the true one. Two clients, same symbol, different chart.
                    #
                    # Re-sending the cursor bar each tick is cheap (one bar) and idempotent on the
                    # client, which keys by timestamp.
                    bars = await node.bars_for(symbol, granularity)
                    cursor = cursors.get(key)
                    new = [b for b in bars if cursor is None or b.ts_event >= cursor]
                    for bar in new:
                        async with send_lock:
                            await _send(
                                websocket,
                                EventFrame(event="data", topic=topic, payload={"frame_type": "bar", "data": bar.model_dump()}),
                            )
                    if new:
                        cursors[key] = new[-1].ts_event
                else:  # positions / fills — no increment key on the client, so re-push the snapshot
                    snap = await _snapshot_for(node, topic)
                    if snap is not None:
                        async with send_lock:
                            await _send(
                                websocket,
                                EventFrame(event="data", topic=topic, payload={"frame_type": "snapshot", "data": snap}),
                            )
    except (WebSocketDisconnect, RuntimeError):
        return  # socket closed — nothing to do


async def _build_search(node=None) -> tuple[InstrumentSearchIndex | EngineSearchIndex | None, asyncio.Task | None]:
    """Build the instrument-search index. Alpaca: the REST catalog index. Every other provider: the
    engine-backed index (#837) — the broker supplies its own data and the engine holds the venue
    session, so the api asks the engine over the command bus. Fail-soft only for the Alpaca path
    (missing keys, unreachable Alpaca) → (None, None) so dev/synthetic runs still boot and the search
    endpoint degrades to 503. Returns the index + its background warm task (Alpaca only)."""
    try:
        from api.feed_config import load_feed_config
        from api.instrument_search import build_search_client

        cfg = load_feed_config()
        if cfg.data_provider != "alpaca":
            if node is None:
                return None, None
            return EngineSearchIndex(node), None
        index = InstrumentSearchIndex(build_search_client(cfg.provider_config))
        await index.start()
    except Exception:
        return None, None
    return index, asyncio.create_task(index.warm())


async def _start_alerts(app: FastAPI, node) -> asyncio.Task | None:
    """Background operator alerts. Fail-soft: the API must start whether or not this can.

    The Alpaca client is optional — without it health and drift alerts still work and only the
    open/close digests are skipped, because those need the venue's own clock to be holiday-aware.
    """
    from api.alerts import AlertsService

    http = None
    try:
        from api.feed_config import load_feed_config
        from api.instrument_search import build_search_client

        cfg = load_feed_config()
        if cfg.data_provider == "alpaca":
            http = build_search_client(cfg.provider_config)
            await http.connect()
    except Exception as exc:                                            # noqa: BLE001
        _log.warning("alerts: no venue clock (%r) — health alerts only, no daily digests", exc)
        http = None
    try:
        service = AlertsService(node, http=http)
        app.state.alerts = service
        task = asyncio.create_task(service.run())
    except Exception as exc:                                            # noqa: BLE001
        _log.error("alerts service could not start (%r) — continuing without alerts", exc)
        return None

    # Execution quality (#210): once a day after the close, compare our fills to the official opening
    # auction and record the drift. Read-only research measurement — two GETs and an INSERT, nothing
    # that can reach an order — and it shares the venue clock the alerts loop already opened. Failing
    # to start it must cost the sample and nothing else.
    try:
        if http is not None:
            from api.db.engine import session_factory
            from api.execquality.store import ExecutionQualityJob

            app.state.execquality = ExecutionQualityJob(session_factory, http)
            app.state.execquality_task = asyncio.create_task(app.state.execquality.run())
    except Exception as exc:                                            # noqa: BLE001
        _log.warning("execution-quality job could not start (%r) — sample will not accumulate", exc)
    # POOL SOURCE REFRESHERS (#486 step 5). An instance owns WHERE its symbols come from and supplies
    # `sources/<name>.sh`; this is the thing that actually runs them. Both reviews of #486 caught its
    # absence independently — the scripts would have existed, been correct, and never been called,
    # with `source_health()` reporting stale and nobody able to say why.
    #
    # Started even when no directory is configured: the runner records an attempt on every tick, so an
    # instance with no refreshers stays distinguishable from a runner that has stopped.
    try:
        from api.source_runner import run_forever

        app.state.source_runner_task = asyncio.create_task(run_forever())
    except Exception as exc:                                            # noqa: BLE001
        _log.warning("pool source runner could not start (%r) — sources will go stale", exc)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    node = create_node()
    # Live node runs cooperatively on this loop (built main-thread for kernel signals, then a run task);
    # synthetic node runs its backtest off-loop. Either way startup waits for the first data to be ready.
    await node.start()
    app.state.node = node
    app.state.search, warm_task = await _build_search(node=node)
    # Startup readiness (#26): probe every dependency once and log a clear up/down summary, so a down system
    # is NAMED at boot — never discovered later as a silent empty UI. Fail-soft: we log + start anyway (so
    # /health can report what's down and the UI can surface it), rather than crash on a degraded dependency.
    for s in await _probe_subsystems(node.health()):
        if s.ok:
            _log.info("readiness ok: %s", s.name)
        else:
            _log.error("readiness FAILED: %s — %s", s.name, s.detail)
    # Operator alerts (#199) run HERE rather than in the engine, because the engine dying is itself
    # an alert and a watcher inside it cannot report its own death. Gated by the `notifications`
    # settings domain (default off), so starting the task is not the same as sending anything.
    alerts_task = await _start_alerts(app, node)
    try:
        yield
    finally:
        if alerts_task is not None:
            alerts_task.cancel()
            with suppress(asyncio.CancelledError):
                await alerts_task
        if warm_task is not None:
            warm_task.cancel()
            with suppress(asyncio.CancelledError):
                await warm_task
        eq_task = getattr(app.state, "execquality_task", None)
        if eq_task is not None:
            eq_task.cancel()
            with suppress(asyncio.CancelledError):
                await eq_task
        if app.state.search is not None:
            await app.state.search.close()
        await node.stop()


app = FastAPI(
    title="kumo-trading-platform API",
    version="0.1.0",
    summary="UI↔FastAPI↔Nautilus bridge (live-trading cockpit). REST + WebSocket, render-only UI.",
    lifespan=lifespan,
)

# Dev CORS: UI is render-only (GET) with no credentials, and may be served over localhost, LAN IP,
# or tailscale (mobile). Allow any origin in dev — tighten to an allowlist before any non-paper use.
#: Allowed browser origins. `*` is correct for a paper stack reachable over localhost, LAN and
#: tailscale, and WRONG for anything else — the comment above has said "tighten before any non-paper
#: use" for months while the value stayed in source, where tightening it means a rebuild.
#:
#: An INSTANCE sets `KUMO_CORS_ORIGINS` as a comma-separated list. Unset keeps today's `*`, so this is
#: inert until a deployment chooses otherwise (#486).
def _parse_cors(raw: str) -> list[str]:
    """Comma-separated origins, empties dropped.

    A trailing comma is the most likely hand-edit, and an empty origin matches nothing while looking
    like a value — so it is dropped rather than handed to the middleware.
    """
    return [o.strip() for o in raw.split(",") if o.strip()]


_CORS_ORIGINS = _parse_cors(os.environ.get("KUMO_CORS_ORIGINS", "*"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    # Mostly render-only (GET); POST/DELETE are the runtime watchlist writes (benign app data, not orders).
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


async def _probe_subsystems(observed: dict) -> list[SubsystemHealth]:
    """Live connectivity of every dependency: redis + postgres probed directly (concurrently); engine from
    the health frames it publishes (the api can't reach the engine process, #20)."""
    redis_host = os.environ.get("KUMO_REDIS_HOST", "127.0.0.1")
    redis_port = int(os.environ.get("KUMO_REDIS_PORT", 6379))
    (redis_ok, redis_detail), (pg_ok, pg_detail) = await asyncio.gather(
        check_redis(redis_host, redis_port),
        check_postgres(),
    )
    # POOL OCCUPANCY ON THE POSTGRES LINE (#542). Not a separate subsystem — the pool being busy is
    # not an outage — but an exhaustion is invisible from outside without it, and on 2026-08-25 the
    # only evidence was a repeating `QueuePool limit ... reached` in the engine's log. A pool that
    # CLIMBS is a leak; one that SPIKES is a sizing problem, and the two look identical unless the
    # number is somewhere a reader can watch it.
    from api.db.engine import pool_stats

    pool = pool_stats()
    pg_detail = (pg_detail or "") + (
        f" pool {pool['checked_out']}/{pool['size']}+{pool['max_overflow']}"
        if pool.get("checked_out") is not None else " pool unknown")
    engine_ok = bool(observed.get("bridge_ok")) and bool(observed.get("engine_ok"))
    # A LANE THAT SHOULD EXIST AND DOES NOT MAKES THE NODE NOT-OK (#539).
    #
    # `status` is "ok" iff every subsystem is ok, so an absent lane has to BE a subsystem or it cannot
    # move the headline. On 2026-08-25 QC345-003 failed to build and /health reported
    # `status=ok, 3/3` for an entire session — the lane was in neither number because it was never
    # registered. #498's defect, fixed for the deploy path and never for the thing an operator watches.
    # UNKNOWN IS NOT A VERDICT (#859). `observed.get("lanes_absent") or {}` then `ok = not absent`
    # turned an absent engine frame into `ok: True` — a positive answer manufactured from no
    # information, observed in all 18 degraded samples of the 2026-09-11 00:34 recreate. The consumer
    # now says None for "never told"; that must reach the verdict rather than being re-emptied here.
    absent = observed.get("lanes_absent")
    return [
        SubsystemHealth(
            name="lanes",
            # None -> None: nobody has told us whether a lane failed to build. `status` treats a falsy
            # `ok` as not-ok, so this degrades the headline without ever claiming health.
            ok=None if absent is None else not absent,
            detail="engine has not reported lane builds" if absent is None
            else "" if not absent
            else "; ".join(f"{k} did not build ({v})" for k, v in sorted(absent.items())),
        ),
        SubsystemHealth(name="redis", ok=redis_ok, detail=redis_detail),
        SubsystemHealth(name="postgres", ok=pg_ok, detail=pg_detail),
        SubsystemHealth(
            name="engine",
            ok=engine_ok,
            detail="" if engine_ok else "no fresh engine health frames on the bus",
        ),
    ]


def _ownership_violations(node, inert: list[str]):
    """Lanes holding a quantity they may not hold — or None when the book could not be READ.

    NEVER RAISES, because this endpoint promises not to. `signed_qty_of` raises by design on a shape
    it cannot understand: returning 0.0 would report "no shorts" for a book it failed to read, a
    silent all-clear, which is the exact failure #437 exists to remove. That refusal is correct and
    is kept — it is just not allowed to take the whole endpoint with it.

    Unguarded, one renamed DTO field would have killed drift display, subsystems, provenance and the
    banner together, and the UI would render `apiDown`: the cockpit reported unreachable while
    running fine, because a single check was confused. The ALERT path already guards the same call
    with `_check_failed`; this one did not, which is a regression #437's own PR introduced against
    the contract stated two functions below.

    NULL IS NOT AN EMPTY LIST. `[]` claims there are no violations; `None` says the check could not
    tell. The failure also appends to `inert`, so a check that broke REPORTS ITSELF rather than
    reading as clean — the difference between "asked and clear" and "not asked".
    """
    try:
        return [
            {"strategy_id": v.strategy_id, "instrument_id": v.instrument_id,
             "signed_qty": v.signed_qty}
            for v in short_violations(node.positions())
        ]
    except Exception as exc:                                            # noqa: BLE001 — see docstring
        inert.append(
            f"the ownership check could not read the position book ({type(exc).__name__}: {exc}); "
            f"lane-level short violations are UNKNOWN, not absent"
        )
        return None


#: The claims read's deadline, matching `check_postgres`'s own 2s probe. `/health` is polled every 3s
#: by the UI, so a read with NO deadline lets a pool wait or a slow query pile requests up behind each
#: other and break this endpoint's "answers even when degraded" contract (codex, implementation
#: review). A timeout here degrades THIS field and leaves the rest of the answer intact.
_CLAIMS_READ_TIMEOUT_S = 2.0


async def _claim_rows():
    """The strategies' claim ledger, as SQLAlchemy rows. Its own function so it can be substituted.

    Same query the alert runs (`alerts.py:663`) and the same one `/claims` uses — `CLAIMS_SQL`. Split
    out rather than inlined so a test can drive the health path without a database, which is the
    idiom this module already uses for `short_violations`.

    BOUNDED. `check_postgres` already bounds its probe at 2s; an unbounded second read on the same
    request would make the probe's deadline meaningless.
    """
    from api.db.engine import session_factory

    async with asyncio.timeout(_CLAIMS_READ_TIMEOUT_S):
        async with session_factory() as db:
            return (await db.execute(sa_text(CLAIMS_SQL))).all()


async def _lanes_bleeding() -> dict:
    """Lanes going to cash under a TRADING label (#1098), for `/health`. NEVER RAISES.

    Its own function so a test can substitute the read, the same idiom as `_claim_rows`. The fold and
    the query live in `api.lanes_bleeding`; this is the one call site on the health path. Bounded
    like the claims read: an unbounded second query on the same request would make `check_postgres`'s
    deadline meaningless. A timeout is an UNREADABLE read, reported as such — not a clean list.
    """
    from api.db.engine import session_factory
    from api.lanes_bleeding import scan_bleeding

    try:
        async with asyncio.timeout(_CLAIMS_READ_TIMEOUT_S):
            return await scan_bleeding(session_factory)
    except Exception as exc:                                            # noqa: BLE001
        return {"status": "unreadable", "lanes": None, "error": f"{type(exc).__name__}: {exc}"}


async def _split_divergence(node):
    """A claim the engine cache contradicts, for `/health` (#817). NEVER RAISES.

    WHY THIS IS ON /health AT ALL. `split_divergence` had exactly one caller — the Telegram alert,
    inside `if self._enabled():` — so `notifications.enabled` decided whether the system LOOKED, not
    merely whether it SENT. ibkr-paper stood at eleven divergent pairs and 1,504 shares with every
    surface green. The send stays gated; the looking must not be.

    NO DEBOUNCE, deliberately, and this is the one place it differs from the alert. The alert waits
    two consecutive polls because claims are written asynchronously around fills and a poll landing
    mid-update sees a blip that self-heals. A health surface is read on demand by an operator who
    wants the CURRENT truth — a two-poll confirmation would make the page lie for one interval, and
    the person refreshing after a deploy is exactly who reads it.

    Reads through `build_split`, which wraps the SAME predicate the alert uses. Two derivations of
    one fact drift, and a divergence surface that disagreed with the divergence page would be its
    own instance of the bug.
    """
    from api.split_divergence import build_split

    detail = ""
    try:
        claims = await _claim_rows()
    except Exception as exc:                                            # noqa: BLE001
        # TYPE AND MESSAGE, ABOVE DEBUG (codex, implementation review). A bare `claims_unreadable`
        # reads as a database problem, so a typo or a renamed attribute in `_claim_rows` would be
        # mistaken for a Postgres hiccup and survive for as long as nobody looked at DEBUG logs.
        # `check_postgres` reports its failures the same way: `f"{type(exc).__name__}: {exc}"`.
        detail = f"{type(exc).__name__}: {exc}"
        _log.warning("split divergence — claims read failed: %s", detail)
        claims = None
    try:
        # DTO -> dict at the boundary, exactly as `alerts.py:666` does: `split_divergence` reads
        # `.get()`, and the detector module must not import the API's DTOs.
        cache_rows = [
            {"strategy_id": getattr(p, "strategy_id", ""),
             "instrument_id": getattr(p, "instrument_id", ""),
             "quantity": getattr(p, "quantity", 0.0),
             "side": getattr(p, "side", "")}
            for p in node.positions()
        ]
    except Exception as exc:                                            # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        _log.warning("split divergence — position read failed: %s", detail)
        cache_rows = None

    # THE FOLD IS INSIDE THE GUARD TOO. It was outside, and the docstring above said NEVER RAISES —
    # prose that reads as safety while a row-shape drift, a missing `_mapping` key or a non-float
    # `qty` could 500 the whole endpoint (codex, implementation review). Guarding only the IO is
    # guarding the half that was expected to fail.
    try:
        r = build_split(cache_rows, claims)
    except Exception as exc:                                            # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        _log.warning("split divergence — fold failed: %s", detail)
        return {"status": "compute_failed", "pairs": [],
                "error": f"split divergence could not be computed — {detail}"}

    # A DICT, not the dataclass. Pydantic v2 refuses a plain dataclass as input for a nested
    # BaseModel field (codex, scope review), and it fails at RESPONSE SERIALISATION — after the
    # handler returns, where the traceback names the model rather than this line.
    error = r.error if r.error is None or not detail else f"{r.error} ({detail})"
    return {"status": r.status, "pairs": r.pairs, "error": error}


async def _inert_contradictions(observed: dict) -> list[str]:
    """Ways the stack contradicts its own instructions. NEVER RAISES — a health endpoint that fails
    because one of its checks failed tells an operator nothing about the other checks."""
    from api.cancel_attribution import PROTECTION_OWNER
    from api.db.engine import session_factory
    from api.inert import inert_contradictions

    try:
        armed_env = os.environ.get("KUMO_ORDERS_ARMED", "").strip().lower()
        orders_armed = armed_env in {"1", "true", "yes", "on"} if armed_env else None
        lanes = observed.get("armed_lanes")
        # `armed_lanes` holds the SIBLING strategies. MANUAL-001 is the feed itself, so the engine
        # answering at all is its registration — omitting it would report it missing on every stack.
        #
        # A STALE BRIDGE MEANS UNKNOWN, NOT EMPTY. `consumer.py` drops `armed_lanes` to `{}` on a
        # stale frame, deliberately — "a lane that WAS armed when the engine died is not armed now" —
        # and its own comment adds "an empty list here is not a claim of health". Reading that `{}` as
        # "no lanes are registered" turned it into exactly that claim: minutes after the deploy that
        # shipped this check, `/strategies` listed five registered lanes while `/health` said four
        # were "set to TRADING but never registered". The detector was the one lying, and it FAILED
        # `make up`. `{}` is not `None`, so the unknown-guard below never fired — absence of evidence
        # read as evidence of absence, inside the check built to catch that.
        #
        # It also fired on the NORMAL PATH: every deploy reports it until the first health frame
        # lands, and an alarm that fires on the normal path gets switched off.
        if not observed.get("bridge_ok"):
            registered = None
        else:
            registered = None if lanes is None else [*lanes, PROTECTION_OWNER]
        async with session_factory() as s:
            rows = (await s.execute(sa_text(
                "SELECT strategy_id FROM exec_strategy_state WHERE state = 'TRADING'"))).all()
        return inert_contradictions(orders_armed=orders_armed,
                                    declared_trading=[r[0] for r in rows], registered=registered)
    except Exception as exc:  # noqa: BLE001 — unknown is not wrong; see api/inert.py
        _log.debug("inert contradiction check unavailable: %s", exc)
        return []


def _feed_stale(observed: dict) -> bool | None:
    """Whether the feed is stale ON TRADING TIME — READ, never re-derived.

    The engine computes it because that process owns the venue calendar. The first version of this
    asked the node for a calendar object, which does not exist on the node interface, so it excepted
    to None on every call: a check that could never fire, which is the exact class this surface was
    added to end.

    None is a real third answer and deliberately NOT degraded — a boot before the open has told us
    nothing, and paging on that is how a real alarm gets muted.
    """
    v = observed.get("feed_stale")
    return None if v is None else bool(v)


def _unpriced(observed: dict) -> list[str] | None:
    """Held instruments the engine cannot price, from the engine's own frame.

    One derivation, read twice — the status verdict and the published field must not disagree about
    whether the book is priceable, which is the shape that produced three mutually inconsistent
    "standing" figures on the portfolio tile.

    NONE SURVIVES AS NONE (#859). `list(... or [])` turned "the engine has not told us what it can
    price" into "every held instrument is priceable" — a clean book asserted from silence, and the
    status verdict below reads the same value, so both halves of the one derivation were wrong
    together. The caller must be able to tell an unpriceable book from an unasked one.
    """
    rows = observed.get("unpriced_positions")
    return None if rows is None else list(rows)


@app.get("/health", operation_id="getHealth", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Live connection/health across every dependency (#26) — the UI polls this for the global banner.

    The api answers even when degraded. `status` is "ok" only when every subsystem is up AND the book
    is safe to size from: an unpriced book, a stale feed, or a claims/cache split divergence each
    degrade it alone. It has not been "all subsystems are up" since #757, and this line said it was
    until #817."""
    node: Node = app.state.node
    observed = node.health()
    subsystems = await _probe_subsystems(observed)
    inert = await _inert_contradictions(observed)
    split = await _split_divergence(node)
    bleeding = await _lanes_bleeding()
    return HealthResponse(
        # STATUS IS NOT ONLY CONNECTIVITY (#757). Every subsystem can be up while the engine cannot
        # price half the book — measured: staging held 22, priced 11, and this said "ok". A price is
        # an input to protection sizing, exit sizing and every displayed figure, so a book that
        # cannot be priced is a degraded engine no matter how healthy its sockets are.
        # NOT ONLY CONNECTIVITY, and not a raw age. Every subsystem can be up while the engine
        # cannot price half the book (measured: staging held 22, priced 11, and this said "ok"), and
        # a stale feed is only a fault when the VENUE WAS OPEN — 59h across a weekend is healthy,
        # 59h on a Tuesday is dead. `feed_is_stale` returns None when the calendar cannot say, and
        # unknown must not degrade: that would page every boot before the open until it was muted.
        # A DIVERGENT SPLIT DEGRADES, for the same reason an unpriced book does (#817, codex).
        # `test_unpriced_book_is_reported.py` states the rule these follow: a book that cannot be
        # SIZED from is not `ok`. Split divergence is exit-sizing unsafe by exactly that argument —
        # it is the condition under which a lane sells shares it does not hold — so a green banner
        # over it would be the same lie, one field along. A NON-OK split status does NOT degrade:
        # "could not read the claims ledger" is already carried in the field's own status, and
        # letting an unreadable half move the banner would page on every Postgres hiccup.
        status="ok" if (
            all(s.ok for s in subsystems)
            # `not None` is True, so an UNKNOWN book does not degrade the headline — deliberately,
            # and for the same reason `_feed_stale(...) is not True` below does not: the engine
            # subsystem already reports the bridge down, and degrading twice for one cause would page
            # on every boot until someone muted it. Unknown is carried by the field and the subsystem,
            # not by the banner.
            and not _unpriced(observed)
            and _feed_stale(observed) is not True
            and not (split.get("pairs") or [])
            # A LANE GOING TO CASH UNDER A TRADING LABEL DEGRADES (#1098) — only on a read that
            # SUCCEEDED and found one. `unreadable` is carried in the field and never moves the
            # banner by itself: paging on every Postgres hiccup is how an operator learns to scroll
            # past the banner, and the lead's scope names this as the three-state rule.
            and not (bleeding.get("status") == "ok" and bleeding.get("lanes"))
        ) else "degraded",
        subsystems=subsystems,
        # THE FIELD THE CONSUMER'S OWN COMMENT TELLS READERS TO USE, finally forwarded (#859).
        bridge_ok=bool(observed.get("bridge_ok")),
        bridge_gaps=int(observed.get("bridge_gaps") or 0),
        bridge_gap_max_s=float(observed.get("bridge_gap_max_s") or 0.0),
        feed_last_tick_ts=int(observed.get("last_tick_ts", 0)),
        market_data_type=observed.get("market_data_type"),
        reconcile_drift=observed.get("reconcile_drift"),
        # COMPUTED HERE, from the same call the alert makes (`_announce_short_violations`), so the
        # banner and the page cannot disagree about whether a lane is violating. Not read from
        # `observed`: this is a property of the position book itself, not something the engine
        # health frame reports, and deriving it twice from two sources is how the three mutually
        # inconsistent "standing" figures on the portfolio tile happened.
        ownership_violations=_ownership_violations(node, inert),
        split_divergence=split,
        lanes_bleeding=bleeding,
        protection_divergence=observed.get("protection_divergence"),
        # FORWARDED, not merely modelled: this endpoint builds its response from an explicit field
        # list, so a field the model has and nobody passes is empty forever (#546 / the
        # DTO-drops-published-fields class).
        naked_after_reject=observed.get("naked_after_reject"),
        # FORWARDED EXPLICITLY. This endpoint builds its response from a field list, so an engine
        # field nobody passes is empty forever — the trap documented two lines above, which ate
        # these two on their first commit.
        unpriced_positions=_unpriced(observed),
        failed_requests=observed.get("failed_requests"),
        # Requested-versus-bound (#618). Added to the engine payload AND to the model, and still
        # empty on both live stacks until this line existed — the trap documented six lines above,
        # hit again, one field along, in the commit that quoted it. `{}` here is not "nothing was
        # subscribed": it is this endpoint never having asked, which is why the generic forwarding
        # test now drives every engine-owned key rather than naming them one at a time.
        subscriptions=observed.get("subscriptions"),
        book_truth=observed.get("book_truth"),
        inferred_fills=observed.get("inferred_fills"),
        fills_on_terminal_orders=observed.get("fills_on_terminal_orders"),
        # `int(... or 0)` made "the engine has not told us" indistinguishable from "nothing was
        # dropped" (#859) — 0 of 0 is not 0 of 4, and this counter exists precisely to say that a
        # defence discarded something.
        fills_on_terminal_orders_dropped=(
            None if observed.get("fills_on_terminal_orders_dropped") is None
            else int(observed["fills_on_terminal_orders_dropped"])),
        shortable=observed.get("shortable"),
        venue_unanswered_lookups=observed.get("venue_unanswered_lookups"),
        observations=observed.get("observations"),
        # NOT `or {}`: None means the pass has never run, and an empty dict would read as a pass that
        # ran and did nothing.
        log_compaction=observed.get("log_compaction"),
        realized_legs=observed.get("realized_legs"),
        # COMPUTED AND THEN DISCARDED until now. `_feed_stale` reads the engine's own verdict — the
        # engine owns the venue calendar — and nothing passed it on, so the field every reader
        # consulted answered None regardless of what the engine said.
        feed_stale=_feed_stale(observed),
        # ARMING, AND WHEN EACH LANE NEXT DECIDES. Absent from the response entirely, so a reader
        # could not tell "no lane is armed" from "this endpoint does not carry arming". On
        # 2026-09-01 that nearly caused a restart of an engine whose lanes were armed and which
        # decided two minutes later.
        armed_lanes=observed.get("armed_lanes"),
        next_fire_ns=observed.get("next_fire_ns"),
        flip_pending=observed.get("flip_pending"),
        market_aware=observed.get("market_aware"),
        lanes_absent=observed.get("lanes_absent"),
        automated_lanes_registered=observed.get("automated_lanes_registered"),
        automated_lanes_running=observed.get("automated_lanes_running"),
        # CONTRADICTIONS BETWEEN WHAT IS DECLARED AND WHAT IS RUNNING (api/inert.py).
        #
        # On 2026-08-24 this endpoint answered `ok` for a stack that would have placed nothing all
        # day: KUMO_ORDERS_ARMED=false, MOMENTUM-002 and BCTROT-004 never registered, and all five
        # lifecycle rows still saying TRADING. Every subsystem was genuinely up, so `status` was
        # honest about what it measured — it simply measured nothing that could tell.
        #
        # The lane COUNT could not show it either: it read 2/2, which is a healthy-looking ratio when
        # the missing two are not counted. Names, not counts.
        inert=inert,
        # PROVENANCE, ALWAYS (#486). `_provenance()` never raises: an unavailable measurement is
        # `None`, which is a third state distinct from a mismatch. A gate that cannot tell "different
        # code" from "no reading" fails forever on a correct stack or passes vacuously on any stack.
        **_provenance(),
    )



def _provenance() -> dict[str, str | None]:
    """What is ACTUALLY running, by content, beside what the build CLAIMED.

    Each of the four is independently optional. A missing digest must not take `/health` down — the
    endpoint's job is to report, and an endpoint that dies while reporting is the outage it was meant
    to describe. Absent reads as `None`, never as agreement.
    """
    import os

    out: dict[str, str | None] = {
        "cockpit_sha": os.environ.get("KUMO_GIT_SHA") or None,
        "strategies_sha": os.environ.get("KUMO_STRATEGIES_SHA") or None,
        "cockpit_digest": None,
        "strategies_digest": None,
    }
    try:
        from api.provenance import digest as _cockpit_digest

        out["cockpit_digest"] = _cockpit_digest()
    except Exception:  # noqa: BLE001 — a measurement that cannot be taken is None, not a 500
        pass
    try:
        from kumo_strategies.provenance import digest as _strategies_digest

        out["strategies_digest"] = _strategies_digest()
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/positions", operation_id="getPositions", response_model=PositionsResponse, tags=["trading"])
async def get_positions() -> PositionsResponse:
    """Live read of the Nautilus cache → typed positions."""
    node: Node = app.state.node
    return PositionsResponse(positions=node.positions())


@app.get("/slots", operation_id="getSlots", tags=["trading"])
async def get_slots(hours: int = 72) -> dict:
    """Every strategy slot that decided and did nothing, attempted and lost it, or errored (#349).

    `judge_slot` has been correct since it was written and had NO caller: QC345's 2026-08-21 session
    is `decisions 1, intents 0` in TRADING, which is `DECIDED, NEVER ATTEMPTED` exactly. It went
    unseen for a week while four separate tickets were filed about its symptoms.

    Served as a plain dict, not a `response_model` — this shape has silently eaten a published field
    three times (#233, #322, #336).
    """
    from api.alerts import AlertsService
    from api.db.engine import session_factory
    from api.slot_outcome import scan_slots

    svc = app.state.alerts if hasattr(app.state, "alerts") else None
    try:
        may_submit = await svc._may_submit_map() if svc is not None else {}
    except Exception:  # noqa: BLE001 — an unreadable lifecycle must not blank the whole answer
        may_submit = {}
    try:
        found = await scan_slots(session_factory, hours=hours, may_submit=may_submit)
    except Exception as exc:  # noqa: BLE001
        # STATUS IS THE POINT: an empty list with no status reads identically to a healthy stack,
        # which is how an empty tile stood in for eight held positions on 2026-08-14.
        return {"status": "unavailable", "error": repr(exc), "slots": []}
    _ = AlertsService  # imported for the may_submit contract above; keeps the dependency explicit
    return {"status": "ok", "hours": hours, "slots": [
        {"strategy_id": sid, "session": session, "slot": slot, "verdict": v.verdict.value,
         "detail": v.detail, "may_be_benign": v.may_be_benign}
        for sid, session, slot, v in found]}


@app.get("/claims", operation_id="getClaims", tags=["trading"])
async def get_claims(session: AsyncSession = Depends(get_session)) -> dict:
    """Claims-vs-account: who claims what, and what that costs (#437).

    UNREACHABLE UNTIL NOW. `build_breaches` was written, tested and documented with the incident it
    caught, and no route served it — so the platform's own answer to "is any position frozen" lived in
    a terminal. The alarm that found BETA (79 held, 2 sellable) came from an ad-hoc monitor script.

    NOT a `response_model`. A Pydantic DTO silently drops keys it does not declare, and this payload
    has eaten a published field three times already (#233, #322, #336) — `sellable` and `stranded`
    would be exactly the next two. The dict is returned as it is built.

    The account comes from the engine's own netted legs rather than a second broker call: the two were
    verified identical across all six held symbols on 2026-08-22, and the venue rate-limits.
    """
    node: Node = app.state.node
    try:
        account = account_from_positions(node.positions())
    except Exception:  # noqa: BLE001 — an unreadable account is reported, never rendered as a clean ledger
        account = None
    rows = (await session.execute(sa_text(CLAIMS_SQL))).all() if account is not None else ()
    b = build_breaches(rows, account)
    return {"status": b.status, "breaches": b.breaches, "error": b.error}


@app.get("/trades", operation_id="getTrades", response_model=TradesResponse, tags=["trading"])
async def get_trades() -> TradesResponse:
    """The trade-cycle plane (#73) — native positions/snapshots/orders projected into cycles (HELD/ARMED/
    WATCH/CLOSED) with cycle_id + cycle-total P&L. Empty on the data-only synthetic node."""
    node: Node = app.state.node
    status, error = node.trades_health()
    # `realized_session` must be passed EXPLICITLY: the response model declares the field, but this
    # endpoint builds the object field-by-field, so a declared-but-unpassed field is silently None. That
    # is exactly how this reached Redis and then vanished at the REST boundary (#233).
    return TradesResponse(trades=node.trades(), status=status, error=error,
                          realized_session=node.trades_realized(),
                          realized_periods=node.trades_periods(),
                          realized_periods_swept=node.trades_periods_swept(),
                          realized_legs=node.trades_legs(),
                          lane_flows=node.trades_flows())


@app.get("/pnl/unrealized-base", operation_id="getUnrealizedBase",
         response_model=WindowBaseResponse, tags=["trading"])
async def get_unrealized_base() -> WindowBaseResponse:
    """Per-lane unrealized at each window's START, from the EOD observation table (#699 / #734).

    THE HALF THE TILE CANNOT COMPUTE. It already knows each lane's standing unrealized NOW; the
    delta it wants is `now − base`, and the broker publishes only account-level curves. This serves
    the base.

    ITS OWN ENDPOINT, not a field on `/trades`. That one is pushed on every engine frame and this
    needs a database read — one per frame would put a Postgres round trip on the hot path for an
    answer that changes once a day. All periods come back together so switching the selector cannot
    briefly show one window's base against another's standing.

    ET, NOT THE CONTAINER'S CLOCK. Session dates in the table are ET and the container runs UTC;
    after 20:00 ET they are different days, so a local date would ask for tomorrow's base all evening
    and get nothing, every evening, silently. `venue_hours` documents the same trap and `eod_hook`
    was caught by it.

    NEVER RAISES. A failed read answers with everything UNKNOWN and says which dates failed, rather
    than an error page — the tile then draws em dashes, which is correct, instead of leaving an
    operator unable to tell a broken endpoint from a broken stack. Same contract `/health` states.
    """
    from datetime import timedelta

    from api.realized_broker import PERIOD_DAYS

    # ONE CLOCK (#1106). This used to be an inline `datetime.now(ET).date()` — a second copy of
    # `_today()`, which exists so a test can pin a day far from the wall clock. The copy could not be
    # pinned, so the seam test's fixture (anchored on 2026-09-14) drifted out of the window it was
    # written for and the test went red by the calendar alone from 09-15.
    today = _today()
    store = EodObservationStore()
    try:
        bases = await unrealized_base_by_period(store, today=today)
    except Exception as exc:  # noqa: BLE001 — see the docstring: this endpoint must answer
        _log.warning("window bases could not be assembled (%s); every period is UNKNOWN, not flat", exc)
        return WindowBaseResponse(
            by_period={p: None for p in PERIOD_DAYS},
            market_value={p: None for p in PERIOD_DAYS},
            base_date={p: None for p in PERIOD_DAYS},
            net={p: None for p in PERIOD_DAYS},
            error=f"assembly failed: {type(exc).__name__}: {exc}"[:200],
        )
    # THE NET TERMS (#699 a): `net(W) = mv_now − mv_base − invested(W)`. Three sources only this
    # endpoint holds together — the bases above, the engine's `lane_flows` (the trades frame, via the
    # consumer; None on an engine that predates it) and the manifest's coverage. A failed coverage
    # read costs the net terms, LOUDLY, and never the bases: fabricating empty coverage would render
    # every window as un-partial.
    error = None
    try:
        oldest = min((d for d in bases.base_date.values() if d), default=None)
        since = oldest or (today - timedelta(days=max(v or 0 for v in PERIOD_DAYS.values()))).isoformat()
        coverage = await store.coverage(since)
        # THE POSITIONS PLANE, signed by side, for the qty invariant (#1072 b): a lane's position may
        # change only by its own fills. FLAT rows carry nothing. Unreadable → None → every lane on a
        # scoping frame is refused, named — never read as "every lane holds nothing".
        try:
            from api.ownership import signed_qty_of

            qty_now: dict[str, dict[str, float]] | None = {}
            for p in app.state.node.positions():
                q = signed_qty_of(p)                     # ONE derivation for the DTO and Nautilus shapes
                if not q:
                    continue
                qty_now.setdefault(str(p.strategy_id), {})[str(p.instrument_id)] = float(q)
        except Exception as exc:  # noqa: BLE001 — a failed positions read is a refusal, not a flat book
            _log.warning("positions unreadable for the qty invariant (%r)", exc)
            qty_now = None
        net = lane_net_terms(bases, app.state.node.trades_flows(), coverage=coverage, today=today, qty_now=qty_now)
    except Exception as exc:  # noqa: BLE001 — the bases still answer; the net terms say why they do not
        _log.warning("net terms could not be assembled (%s); every period's net is UNKNOWN", exc)
        net = {p: None for p in bases.by_period}
        error = f"coverage failed: {type(exc).__name__}: {exc}"[:200]
    return WindowBaseResponse(by_period=bases.by_period, market_value=bases.market_value,
                              base_date=bases.base_date, net=net,
                              unreadable=list(bases.unreadable), error=error)


@app.get("/external-activity", operation_id="getExternalActivity", response_model=ExternalActivityResponse, tags=["trading"])
async def get_external_activity() -> ExternalActivityResponse:
    """The quarantine plane (#79) — broker activity the cockpit didn't originate, surfaced so it never
    silently joins a strategy's P&L. Reads native cache rows only: a transfer genuinely reduces the source
    position, so there is nothing to subtract here."""
    node: Node = app.state.node
    return ExternalActivityResponse(external=node.external())


@app.get("/account", operation_id="getAccount", response_model=AccountDTO | None, tags=["trading"])
async def get_account() -> AccountDTO | None:
    """Account snapshot (#41) — equity/cash/buying-power for %-of-equity sizing. None until the first frame."""
    return app.state.node.account()


# --- Settings framework (#49) — per-domain JSON-Schema config + values on a volume ----------------
@app.get("/settings", operation_id="listSettings", tags=["settings"])
async def list_settings() -> dict[str, list[str]]:
    """The registered settings domains (one JSON Schema per domain in the repo)."""
    return {"domains": settings.domains()}


@app.get("/settings/{domain}", operation_id="getSettings", tags=["settings"])
async def get_settings(domain: str) -> dict:
    """A domain's JSON Schema (the contract the UI renders + validates against) + its resolved values
    (schema defaults merged with the values file, coerced)."""
    try:
        return {"schema": settings.load_schema(domain), "values": settings.resolve(domain)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown settings domain {domain!r}") from None


@app.put("/settings/{domain}", operation_id="putSettings", tags=["settings"])
async def put_settings(domain: str, values: dict) -> dict:
    """Validate values strictly against the domain schema and persist them (atomic). 422 with per-field
    errors if invalid — nothing invalid is written. Returns the resolved (defaulted) values."""
    try:
        # strict: an unknown key means the caller asked for something this domain does not have, and
        # answering 200 would report a change that did not happen.
        return {"values": settings.save(domain, values, strict=True)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown settings domain {domain!r}") from None
    except settings.SettingsError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from None


def _today() -> date:
    """THE ONE CLOCK the `/strategies` row is built on, in the calendar the LEDGER writes in.

    `sessions_since_decision` and `next_rebalance` are two readings of one calendar and must not be
    able to disagree about what day it is. Session dates are ET and the container runs UTC (the same
    trap `unrealized_bases` and `eod_hook` document): `date.today()` here read tomorrow every evening
    after 20:00 ET, so the liveness count over-reported by one all evening and the rebalance date could
    flip a day early. Module-level so a test can pin a day far from the wall clock.
    """
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()


class UnreadableSession(ValueError):
    """A `last_decision` the ledger holds that is not a date. Its own class so the endpoint can name
    that state on the row (`next_rebalance_state: "unreadable_ledger"`) without also swallowing the
    unknown-cadence refusal below, which shares the base type and must never be reachable."""


def _parse_session(last_session: str) -> date:
    try:
        return date.fromisoformat(last_session)
    except (TypeError, ValueError) as exc:
        raise UnreadableSession(f"last_decision {last_session!r} is not a date") from exc


def _sessions_since(last_session: str, today: date) -> int:
    """US TRADING sessions since `last_session`, not calendar days.

    Calendar days would report 4 across a weekend and read as an outage when nothing was missed — the
    false alarm that gets a liveness indicator ignored, which is the failure mode this exists to prevent.
    Weekends only; market holidays are not modelled here, so this can over-report by one on a holiday
    week. Over-reporting is the safe direction for an alarm.

    An unparseable session RAISES (`UnreadableSession`). It used to return 0 — "decided today", the
    healthiest value there is, for a row that cannot be read — and the endpoint names that state instead.
    """
    start = _parse_session(last_session)
    sessions, day = 0, start + timedelta(days=1)
    while day <= today:
        if day.weekday() < 5:
            sessions += 1
        day += timedelta(days=1)
    return sessions


def _first_weekday(year: int, month: int) -> date:
    day = date(year, month, 1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _next_rebalance(cadence: str, last_session: str | None, today: date) -> str | None:
    """The next session on which a lane of this `cadence` will DECIDE, or `None` for a cadence that has
    no rebalance calendar (#888).

    `monthly` is the first weekday of the month (QC345: `rebalance_dates()` upstream keeps the first
    SESSION per month over its own panel). Weekdays only, holidays not modelled — the same honesty as
    `_sessions_since`; a month whose first weekday is a holiday is where this and the adapter's date
    disagree, and that disagreement is the detector, which is why `/strategies` carries the source.

    The answer is the first weekday-of-month on or after `start`, where `start` is today (today counts
    — before the slot fires, today IS the rebalance) or the day after the last decision, whichever is
    later. A mid-month decision (a forced rebalance) spends its month; a ledger AHEAD of our clock by
    any distance spends the month it names — the first version stepped from today's month under a
    fixed bound and raised past it, taking every row on the screen with it (review). Bounded by
    construction now: `start`'s month or the next.

    `None` for `daily`/`manual` is readable only because `cadence` sits beside it on the row; an
    UNKNOWN cadence raises rather than joining them, or "unknown" would render as "daily" — the #888
    misreading with a new face.
    """
    if cadence in ("daily", "manual"):
        return None
    if cadence != "monthly":
        raise ValueError(f"no rebalance calendar for cadence {cadence!r}")
    start = today
    if last_session is not None:
        start = max(today, _parse_session(last_session) + timedelta(days=1))
    candidate = _first_weekday(start.year, start.month)
    if candidate < start:
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        candidate = _first_weekday(year, month)
    return candidate.isoformat()


def _arm_row(value) -> dict:
    """One lane's arm state, from whatever the engine published (#997).

    MIXED VERSIONS ARE THE NORMAL CASE FOR SECONDS AT EVERY DEPLOY: the api and the engine are
    separate containers that recreate at slightly different times, so a NEW api reads an OLD engine's
    frame where `armed_lanes` values are bare BOOLS. The bool still answers "armed?", and everything
    the old frame cannot carry reads UNKNOWN rather than being invented — the same rule as a lane that
    cannot be asked. Found because this change broke `test_cadence_on_strategies`, whose doubles
    publish the old shape exactly as a lagging engine would.
    """
    # BOTH DIRECTIONS, not just the one we happened to hit (ffv73l93, #998 re-review). The old→new
    # window above is the one that broke a test and got fixed. The new→old window is the same deploy,
    # seconds apart the other way: an engine whose row RENAMES or DROPS a key was previously returned
    # VERBATIM, and `/strategies` indexing `arm["armed"]` then raised KeyError inside the route.
    #
    # Normalising the SHAPE rather than guarding the one index, because the guard only protects the
    # caller written today. Unknown keys are KEPT — discarding them would make this the thing that
    # breaks the next field the engine adds.
    row = {"armed": None, "state": "unknown", "session": None, "slot": None, "next_fire_ns": None,
           "reason": None, "reason_available": False, "attempts": None, "reason_at_ns": None}
    if isinstance(value, dict):
        return {**row, **value}
    return {**row, "armed": value if isinstance(value, bool) else None}


@app.get("/strategies", operation_id="getStrategies", tags=["settings"])
async def get_strategies() -> dict:
    """Every declared strategy with its capital sleeve (#320).

    One place that answers "what strategies exist, and how much is each allowed" — the question an
    operator has to answer before touching a target. The registry supplies identity; the sleeve supplies
    `target` (intent) and `actual` (the sleeve's net asset value). The gap between them IS the
    instruction: `actual > target` means the strategy is reducing, `actual < target` means it is growing
    into its allocation as capital arrives.

    A declared strategy with no sleeve row reports zeros rather than being omitted. Absent and
    zero-allocated are different states, and hiding the former is how a strategy silently never gets
    funded.

    READ-ONLY. Targets are edited through the `strategies` SETTINGS domain, which already renders a
    form from its schema with no per-setting UI code. A second write path here would be a second place
    to validate the same number, and the two would disagree.
    """
    from api.budget import Sleeve
    from api.budget_store import last_decisions, load_book
    from api.db.engine import session_factory
    from api.strategy_registry import REGISTRY, UNALLOCATED_TITLE

    try:
        async with session_factory() as session:
            book = await load_book(session)
    except Exception as exc:  # noqa: BLE001 — a settings screen must not 500 because Postgres blinked
        raise HTTPException(status_code=503, detail=f"sleeves unavailable: {exc!r}") from None

    # LIVENESS, on the screen an operator already opens (#349). MOMENTUM-002 stopped deciding on
    # 2026-08-17 and was found on 08-18 by a human reading container logs, two sessions and $1,178 later
    # — while this endpoint reported `target 20,000 · actual 67,111`, a picture of a healthy funded
    # strategy. Capital without liveness renders a dead strategy identically to a live one.
    #
    # A FIELD, NOT AN ALERT. An alert is a second mechanism that can itself go inert, which is the
    # category that produced four separate defects in one night. This is the fact; alerting consumes it.
    try:
        async with session_factory() as session:
            decided = await last_decisions(session)
    except Exception as exc:  # noqa: BLE001 — liveness must not 503 a screen that answers other questions
        _log.warning("liveness unavailable for /strategies (%r) — reporting capital only", exc)
        decided = {}

    # THE ENGINE'S FRAME, for `armed`. The strategy objects live in the engine process (#20), so this
    # endpoint cannot ask them directly — it reads what the engine published. Failure here must not 503
    # a screen that answers other questions, so an unreadable frame yields {} and every lane reports
    # `armed: None`, which is "could not ask" rather than "not armed".
    try:
        _h = app.state.node.health() or {}
        # THREE STATES AT THE SOURCE (#1099). `armed_lanes` is None on a stale bridge (consumer.py
        # `health()`), {} when the frame was read and no lane is on it — and `or {}` collapsed them,
        # so every per-lane `arm.state: "unknown"` below meant EITHER "not on this node" OR "could not
        # ask". A reader of the rows (the Portfolio chips) cannot tell those apart, and would drop
        # every lane on the tick a deploy lands. Said once, top-level, never inferred from the rows.
        # `armed_lanes` is None on a stale bridge AND on a live frame that lacks the key (an engine
        # predating it — consumer.py); a falsy `health()` was already coerced to {} above and reads
        # unreadable through the same test. `{}` is a READ frame with no lane on it.
        engine_frame = "ok" if _h.get("armed_lanes") is not None else "unreadable"
        armed_lanes = _h.get("armed_lanes") or {}
        next_fires = _h.get("next_fire_ns") or {}
        # THREE STATES (#907): None = the frame could not be read (never 0 — the collapse of
        # "unreadable" into "nothing pending" is what the ticket exists to prevent); [] = none.
        flip_rows = _h.get("flip_pending")
        # #873 phase 1: the market-aware container — None when the frame could not be read or the
        # build lacks the plane; a dict otherwise, whose `lanes[sid]` is None until the first poll.
        market_aware = _h.get("market_aware")
    except Exception as exc:  # noqa: BLE001
        _log.warning("armed state unavailable for /strategies (%r)", exc)
        engine_frame = "unreadable"
        armed_lanes = {}
        next_fires = {}
        market_aware = None
        flip_rows = None

    today = _today()
    rows = []
    for entry in REGISTRY:
        sleeve = book.sleeves.get(entry.strategy_id) or Sleeve(entry.strategy_id, 0.0, 0.0)
        last = decided.get(entry.strategy_id)
        # THE CADENCE BESIDE THE ALARM (#888). `sessions_since_decision` is calibrated for daily lanes;
        # on 2026-09-11 it read 7 on the MONTHLY QC345-003 and was taken for a dead lane. Three states
        # for the date: scheduled, not a rotation calendar, or a ledger row this cannot read — named,
        # never guessed and never a 500 on a screen that answers other questions.
        # Both readings of the ledger date in ONE try: a row must not say "0 sessions since decision"
        # (the healthiest value) beside `unreadable_ledger` (review) — if the date cannot be read,
        # neither can be, and the state field says so.
        last_session = last["session"] if last else None
        try:
            sessions_since = _sessions_since(last_session, today) if last else None
            next_rebalance = _next_rebalance(entry.cadence, last_session, today)
            next_rebalance_state = "scheduled" if next_rebalance is not None else "not_a_rotation"
        except UnreadableSession as exc:
            _log.warning("%s: %s — liveness and next_rebalance unreadable", entry.strategy_id, exc)
            sessions_since, next_rebalance, next_rebalance_state = None, None, "unreadable_ledger"
        # ONE OBJECT, not two calls (ffv73l93, #998 review). `_arm_row` is pure, so two calls agree
        # today — but then "one derivation" is true by luck rather than by construction, and the next
        # edit that gives the helper any state makes the served bool and the served row disagree
        # silently. Bind it once and serve both from the same dict.
        arm = _arm_row(armed_lanes.get(entry.strategy_id))
        rows.append({
            "strategy_id": entry.strategy_id,
            "name": entry.name,
            "title": entry.title,
            "settings_domain": entry.settings_domain,
            "target": sleeve.target,
            "actual": sleeve.actual,
            "must_reduce": sleeve.must_reduce,
            "headroom": sleeve.headroom,
            "is_reducing": sleeve.is_reducing,
            # NEVER-DECIDED AND STOPPED-DECIDING ARE DIFFERENT STATES and must not render alike.
            # BCTROT-004 has never decided and is unarmed by design; MOMENTUM-002 decided and stopped.
            # Collapsing them is how a screen trains its reader to ignore it. `None` means never.
            # ARMED, from the engine's health frame (#20 split — the strategy objects live there).
            # `None` means the engine could not be asked or could not ask the lane; a reader must be
            # able to tell that from False. RUNNING does not imply able to decide: a lane whose
            # calendar never answered is alive, counted, and will never fire its slot.
            # ONE DERIVATION (#997): the bool is READ OUT of the arm row, never computed again. The
            # row itself rides beside it so a reader can see WHICH SESSION the lane armed for and,
            # when the installed strategies package carries it, WHY it is not armed.
            # `armed` and `state` CAN DISAGREE AND THAT IS NOT A BUG: a lane armed for a STALE
            # session with an upstream `refused_permanent` reads `armed: True, state:
            # refused_permanent`. They are two different facts — did the lane arm, and what does it
            # say about arming now. Collapsing them to agree would discard the one an operator acts on.
            "armed": arm["armed"],
            "arm": arm,
            # WHEN THIS LANE IS NEXT DUE, from its own Nautilus alert rather than a slot table — see
            # `next_fire_by_lane`. `None` means no alert or unaskable, which a reader must be able to
            # tell from "not due soon".
            "next_fire_ns": next_fires.get(entry.strategy_id),
            "last_decision": last["session"] if last else None,
            "last_decision_slot": last["slot"] if last else None,
            # A DATE IS NOT AN ALARM. "last decided 2026-08-14" only alarms a reader who already knows
            # the schedule and suspects a problem — which nobody did for three days. A count of missed
            # TRADING sessions is wrong at a glance, which is what makes a screen an alarm.
            "sessions_since_decision": sessions_since,
            # Declared in cockpit's registry today; `cadence_source` names that so the adapter's own
            # value (issue 157), when it arrives, can be compared rather than overwrite.
            "cadence": entry.cadence,
            "cadence_source": "registry",
            "next_rebalance": next_rebalance,
            "next_rebalance_state": next_rebalance_state,
            # HOW MANY CONSECUTIVE PASSES this lane has wanted to flip a stop and not (#907) — the
            # max over its legs, 0 when none, None when the engine frame could not be read.
            "flip_pending_passes": None if flip_rows is None else max(
                (int(r.get("passes") or 0) for r in flip_rows if str(r.get("strategy_id") or "") == entry.strategy_id),
                default=0,
            ),
            # #873 phase 1, THREE STATES OUTSIDE AND INSIDE: None = the engine frame could not be read
            # or the build lacks the plane; {contract, dwell, lane: None} = the plane is up and this
            # lane has not been polled (or is not registered on the node); {contract, dwell, lane:
            # {...}} = readings, each hook carrying its own five-state answer.
            "market_aware": None if market_aware is None else {
                "contract": market_aware.get("contract"),
                "dwell": market_aware.get("dwell"),
                "polled_at_ns": market_aware.get("polled_at_ns"),
                "lane": (market_aware.get("lanes") or {}).get(entry.strategy_id),
            },
        })

    # UNALLOCATED is not a strategy, but its balance is real money and hiding it would make the sum of
    # the rows disagree with the account. Reported separately so nothing iterating strategies treats it
    # as one.
    parked = book.sleeves.get("UNALLOCATED")
    return {
        "strategies": rows,
        # Whether the per-lane `arm` rows above were READ or DEFAULTED (#1099): "ok" | "unreadable".
        # An older api omits the key; a reader treats absence as unreadable.
        "engine_frame": engine_frame,
        "unallocated": {"actual": parked.actual if parked else 0.0, "title": UNALLOCATED_TITLE},
    }


@app.get(
    "/instruments/search",
    operation_id="searchInstruments",
    response_model=InstrumentSearchResponse,
    tags=["instruments"],
)
async def search_instruments(
    q: str = "", limit: int = Query(20, ge=1, le=50)
) -> InstrumentSearchResponse:
    """Ranked instrument search over the tradable catalog (symbol- and name-weighted). Blank `q` → empty.

    503 when search is unavailable (provider isn't Alpaca / keys unset) or the catalog hasn't loaded yet.
    """
    index: InstrumentSearchIndex | EngineSearchIndex | None = app.state.search
    if index is None:
        raise HTTPException(status_code=503, detail="instrument search unavailable (no provider index)")
    try:
        results = await index.search(q, limit)
    except Exception:
        raise HTTPException(status_code=503, detail="instrument catalog not loaded yet") from None
    return InstrumentSearchResponse(results=results)


async def _enqueue(ctype: str, payload: dict) -> str:
    """Enqueue a command on the bus, returning its `command_id` (#39). A bus failure → 503, so a command that
    was never enqueued NEVER returns ok. The engine's accept/reject arrives later via `GET /commands/{id}`."""
    try:
        return await app.state.node.send_command(ctype, payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"command bus unavailable: {exc}") from exc


@app.post("/orders", operation_id="submitOrder", response_model=CommandResponse, tags=["orders"])
async def submit_order(body: OrderRequest) -> CommandResponse:
    """Submit a discretionary MANUAL-strategy order → a `submit_order` command over the command layer (#32/#5).
    The api mints the client_order_id (idempotency key). `ok` here means ENQUEUED, not placed — the order is
    only PLACED if the engine is armed (KUMO_ORDERS_ARMED); poll `GET /commands/{command_id}` for accept/reject
    (#39). Human-gated — this relays the human's explicit order, it originates nothing."""
    client_order_id = uuid.uuid4().hex
    payload = {**body.model_dump(exclude_none=True), "client_order_id": client_order_id}
    command_id = await _enqueue("submit_order", payload)
    return CommandResponse(command_id=command_id, client_order_id=client_order_id)


@app.post("/orders/bracket", operation_id="submitBracket", response_model=CommandResponse, tags=["orders"])
async def submit_bracket(body: BracketRequest) -> CommandResponse:
    """Submit a bracketed entry (#34) → a `submit_bracket` command. The api mints the entry + protective
    stop (+ optional target) client_order_ids; the engine arms the protective legs on the entry fill (OTO)
    and OCO-links them. `ok` = enqueued; poll `GET /commands/{command_id}` for accept/reject. Human-gated."""
    entry_coid = uuid.uuid4().hex
    payload: dict = {
        **body.model_dump(exclude_none=True),
        "client_order_id": entry_coid,
        "sl_client_order_id": uuid.uuid4().hex,
        "tp_client_order_id": uuid.uuid4().hex,
        "group_id": entry_coid,
    }
    command_id = await _enqueue("submit_bracket", payload)
    return CommandResponse(command_id=command_id, client_order_id=entry_coid)


@app.get("/orders", operation_id="getOrders", response_model=OrdersResponse, tags=["orders"])
async def get_orders() -> OrdersResponse:
    """The Orders blotter (#33) — working orders first, then recent terminal. Read-only projection of the
    engine's order cache streamed over ui:stream."""
    return OrdersResponse(orders=app.state.node.orders())


@app.post("/orders/{client_order_id}/cancel", operation_id="cancelOrder", response_model=CommandResponse, tags=["orders"])
async def cancel_order(client_order_id: str) -> CommandResponse:
    """Cancel a working order → a `cancel_order` command. Risk-reducing; still gated by KUMO_ORDERS_ARMED
    (a disarmed engine acks it as such and does nothing). Poll `GET /commands/{command_id}` for accept/reject."""
    command_id = await _enqueue("cancel_order", {"client_order_id": client_order_id})
    return CommandResponse(command_id=command_id, client_order_id=client_order_id)


@app.post("/orders/{client_order_id}/modify", operation_id="modifyOrder", response_model=CommandResponse, tags=["orders"])
async def modify_order(client_order_id: str, body: ModifyOrderRequest) -> CommandResponse:
    """Modify a working order's qty/price/trigger → a `modify_order` command (only supplied fields change)."""
    payload = {**body.model_dump(exclude_none=True), "client_order_id": client_order_id}
    command_id = await _enqueue("modify_order", payload)
    return CommandResponse(command_id=command_id, client_order_id=client_order_id)


@app.get("/commands/{command_id}", operation_id="getCommandStatus", response_model=CommandStatusResponse, tags=["orders"])
async def get_command_status(command_id: str) -> CommandStatusResponse:
    """Engine ack for a command (#39). No ack yet → `pending` (the caller applies its OWN timeout → `unknown`;
    a lost ack must never read as a false `rejected`). Ack seen → engine status `ok`→`accepted`, else
    `rejected` (with the engine's `error`). After `accepted`, the Orders blotter is the authoritative lifecycle."""
    ack = app.state.node.command_status(command_id)
    if ack is None:
        return CommandStatusResponse(command_id=command_id, status="pending")
    status = "accepted" if ack.get("status") == "ok" else "rejected"
    msg = ack.get("error") or None
    return CommandStatusResponse(
        command_id=command_id,
        status=status,
        command_type=ack.get("type"),
        error=msg if status == "rejected" else None,
        detail=msg if status == "accepted" else None,
    )


@app.post("/instruments/{instrument_id}/stream", operation_id="requestStream", tags=["instruments"])
async def request_stream(instrument_id: str) -> dict[str, bool]:
    """Ask the engine to stream this symbol on demand (viewing a search result). Idempotent, TTL'd."""
    await app.state.node.request_stream(instrument_id)
    return {"ok": True}


@app.post("/eod/backfill", operation_id="eodBackfill", response_model=CommandResponse, tags=["meta"])
async def eod_backfill(body: dict) -> CommandResponse:
    """Write reconstructed end-of-day position history for the given sessions (#734).

    OPERATOR-DRIVEN AND GATED OFF (`KUMO_EOD_BACKFILL`). This endpoint is a convenience; the ENGINE
    re-checks the gate, because a hand-crafted bus message must not reach a write path the operator
    never enabled — a check that lives only here would make the endpoint's absence cosmetic.

    Applied in the engine rather than here: the acceptance gate compares against the LIVE Nautilus
    cache, and this process holds only stale snapshots. Same reason as `transfer_position`.

    `sessions` (list of YYYY-MM-DD) and `ledger_start` are both REQUIRED. `ledger_start` states what
    the activity fetch COVERS and is never inferred from the earliest fill — that is a property of the
    fetch, so a ledger truncated by a lookback window would report its own truncation point as the
    account's inception.

    `ok` means ENQUEUED. Poll `GET /commands/{command_id}` for the outcome: the gate runs there, and a
    disagreeing gate writes NOTHING and reports why.
    """
    return CommandResponse(
        command_id=await _enqueue("eod_backfill", {
            "sessions": body.get("sessions"),
            "ledger_start": body.get("ledger_start"),
        }),
    )


@app.post("/positions/flatten", operation_id="flattenPosition", response_model=CommandResponse, tags=["orders"])
async def flatten_position(body: FlattenRequest) -> CommandResponse:
    """Close a position, long or short (#170 first slice).

    `ok` means ENQUEUED. The engine sizes the close against its LIVE position and rejects if the position
    moved since the operator read it — poll `GET /commands/{command_id}` for the outcome. Human-gated: this
    places a real order and only runs when the engine is armed."""
    return CommandResponse(
        command_id=await _enqueue(
            "flatten_position",
            {
                "instrument_id": body.instrument_id,
                "strategy_id": body.strategy_id,
                "expected_side": body.expected_side,
                "expected_qty": body.expected_qty,
                "cycle_id": body.cycle_id,
            },
        )
    )


@app.post("/strategies/{strategy_id}/liquidate", operation_id="liquidateLane", response_model=CommandResponse, tags=["orders"])
async def liquidate_lane(strategy_id: str, body: LiquidateLaneRequest) -> CommandResponse:
    """Flatten a LANE's whole book, now (#922) — the act behind the #873 LIQUIDATE condition.

    `ok` means ENQUEUED. The engine writes LIQUIDATING first (so the lane cannot re-enter), cancels the lane's
    working orders, closes every open position through the lane's own strategy (DAY market orders), drops
    the claims, re-reads the book and reports what remains — poll `GET /commands/{command_id}`: the ack's
    `results` carries submitted / held / failed / remainder, and `partial` or `refused` arrive as errors with
    the same report. Human-gated: real orders, only when the engine is armed."""
    return CommandResponse(
        command_id=await _enqueue(
            "liquidate_lane",
            {
                "strategy_id": strategy_id,
                "expected_positions": body.expected_positions,
                "expected_total_qty": body.expected_total_qty,
                "invoked_by": body.invoked_by,
                "reason": body.reason,
            },
        )
    )


@app.get("/managers", operation_id="getManagers", response_model=ManagersResponse, tags=["orders"])
async def get_managers() -> ManagersResponse:
    """Every manager instance (#55) and its current state, whatever the kind.

    Read directly from Postgres — the ack for the original attach command expires long before an overnight
    manager fires, so this is the durable source a screen re-opened hours later reads instead."""
    from api import managers as mg
    from api.db.engine import session_factory

    async with session_factory() as session:
        rows = await mg.all_managers(session)
        dtos = []
        for r in rows:
            error = None
            if r.state == "FAILED":
                # One extra query, only for the rare FAILED case — a screen showing "queue failed" with no
                # reason is a real regression from what the flatten-specific predecessor of this framework
                # showed.
                detail = await mg.last_event_detail(session, r.manager_id, "FAILED")
                error = (detail or {}).get("detail") if detail else None
            dtos.append(
                ManagerDTO(
                    manager_id=r.manager_id,
                    kind=r.kind,
                    instrument_id=r.instrument_id,
                    strategy_id=r.strategy_id,
                    cycle_id=r.cycle_id,
                    leash=r.leash,
                    state=r.state,
                    params=r.params,
                    error=error,
                    # The columns were always on the row; only the DTO was missing them (#400).
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
            )
    return ManagersResponse(managers=dtos)


@app.post("/managers/attach", operation_id="attachManager", response_model=CommandResponse, tags=["orders"])
async def attach_manager(body: AttachManagerRequest) -> CommandResponse:
    """Arm a manager instance (#55/#47) — e.g. STOP-AND-REENTER's `stop_reenter_watch` on a HELD position.

    `ok` means ENQUEUED, not armed yet — poll `GET /commands/{command_id}` for the engine's accept/reject
    (a mismatched position, disarmed orders, or an unknown `kind` reject here). Human-gated: arming IS the
    confirmation (2026-08-02) — once armed, the action fires with no further per-trigger approval."""
    return CommandResponse(
        command_id=await _enqueue(
            "attach_manager",
            {
                "kind": body.kind,
                "instrument_id": body.instrument_id,
                "strategy_id": body.strategy_id,
                "cycle_id": body.cycle_id,
                "leash": body.leash,
                "params": body.params,
            },
        )
    )


@app.post(
    "/managers/{manager_id}/cancel", operation_id="cancelManager", response_model=CommandResponse, tags=["orders"]
)
async def cancel_manager(manager_id: str, body: CancelManagerRequest) -> CommandResponse:
    """Deactivate an armed manager (#55/#47) — the toggle's OFF path. A no-op (`ok`) if the manager already
    reached a terminal state (APPLIED/FAILED/CANCELLED); never overwrites a real outcome."""
    del body  # empty on purpose — manager_id is the only input, carried in the path
    return CommandResponse(command_id=await _enqueue("cancel_manager", {"manager_id": manager_id}))


@app.get("/market/rotation", operation_id="getRotation", tags=["market"])
async def get_rotation() -> dict:
    """The rotation read — published by the engine off our own bars (#351, #384).

    Each axis is a RATIO of two ETFs, graded by the same weekly-governs / daily-times Ichimoku stack
    that grades single names. THAT STACK IS KUMO CODE — `strategies/rotation_grade.py`. It used to be
    imported at runtime from a host mount behind `KUMO_LEDGER_TOOL_TOOLS`, which meant an env var could
    switch the market view off and `/nonexistent` read as deliberate configuration.

    IT USED TO COME OFF DISK, AND NO LONGER DOES. A sidecar shelled out to that tool on a host mount,
    wrote `rotation.json`, and this route read it back. 2026-08-21: "it should not feed from a
    file". What was wrong with it:

      * A SECOND DATA SOURCE. The tool pulls daily OHLC from Yahoo, so the cockpit graded rotations off
        one feed while trading off another. Same session, two answers available.
      * NO RELATIONSHIP TO ENGINE LIVENESS. On 2026-08-21 the sidecar's bind mount went stale, every
        refresh failed with `FileNotFoundError`, and this route served a four-hour-old payload with no
        indication anything was wrong — because a file that is present and parseable looks healthy.
      * A FILE IS NOT A PLANE. Everything else here — trades, account, equity_curve, session — is
        published by the engine and carried on the bus.

    ABSENCE IS STILL THE INTERESTING CASE, and it is reported exactly as before. Nothing published yet
    is an ERROR with an empty axes list and a null `generated`, never an empty rotation: "there is no
    rotation right now" and "nobody has looked" are different claims, and rendering the first for the
    second turns a dead feed into a calm market. Same rule as an unpriced symbol that must not render as
    flat (#356).

    The payload is passed through UNCHANGED. The contract belongs to the tool; reshaping it here would
    put a second schema between generator and tile.
    """
    empty = {"generated": None, "source": None, "axes": [], "errors": [], "error": None}
    node: Node = app.state.node
    payload = node.rotation()
    if not payload:
        return {
            **empty,
            "error": "no rotation published yet — the engine computes it on a 5-minute timer",
        }
    if not isinstance(payload.get("axes"), list):
        return {**empty, "error": "rotation payload is not the expected shape"}
    # SPREAD, NOT REBUILT. This used to hand-list five keys while the docstring above promised an
    # unchanged passthrough, so every field the engine added later died here silently. Two did:
    # `depth` (per-ticker bar counts, added to diagnose two stacks grading the same market
    # differently) and `unavailable` (the reason a provider cannot grade ANY axis) — the second added
    # specifically so an absence would stop being silent, and then silenced by this route.
    #
    # Fourth time a published field has been eaten between engine and UI here (#233, #322, #336).
    # The contract keys are still guaranteed, by filling them AFTER the spread rather than instead
    # of it: a tile reading `axes` must never get None because the engine omitted it.
    return {
        **payload,
        "generated": payload.get("generated"),
        "source": payload.get("source"),
        "axes": payload["axes"],
        "errors": payload.get("errors") or [],
        "error": None,
    }


@app.post("/sleeves/distribute", operation_id="distributeUnallocated", tags=["transfers"])
async def distribute_unallocated_capital(payload: dict | None = None) -> dict:
    """Put unallocated capital to work, proportionally to how short each sleeve is (#373).

    THE MECHANISM ALREADY EXISTED AND NOTHING COULD CALL IT. `plan_distribution` (proportional to
    shortfall, conservation- and intent-capped) and `distribute_unallocated` (idempotent, single
    transaction) were both written and both tested — and `grep` found them referenced only from their
    own test files. On 2026-08-19 BCTROT-004 and QC345-003 sat at `target 20000, actual 0`, `deployable`
    pinned at 0 and unable to open a position, while $45,000.00 sat unallocated. The only route to
    funding them was waiting for MOMENTUM to sell something.

    So this route adds no arithmetic. It is the caller the primitive never had.

    `available` is optional and defaults to whatever UNALLOCATED holds. It is exposed because that
    default is known to be wrong: `mark_to_market` has no production caller either, so `actual` is
    decrement-only and drifts from the account the moment a position moves. An operator with the broker
    cash figure in front of them has a better number than the book does, and the primitive already
    accepts it.

    `run_id` makes a retry safe. Allocation ids are derived from it, and `SleeveTransfer.fill_id` is
    uniquely indexed, so the same run lands once however many times it is submitted — the same
    guarantee fills get. A caller that omits it gets a fresh one, which means a double-click funds twice;
    that is the caller's choice to make, and the UI will pass one.
    """
    from api.budget_store import distribute_unallocated
    from api.db.engine import session_factory

    body = payload or {}
    available = body.get("available")
    run_id = body.get("run_id") or f"dist-{uuid.uuid4().hex[:16]}"
    async with session_factory() as session:
        allocations = await distribute_unallocated(
            session,
            available=float(available) if available is not None else None,
            actor=str(body.get("actor") or "operator"),
            run_id=run_id,
        )
        # COMMIT. `distribute_unallocated` only flushes, and `AsyncSession.__aexit__` calls `close()`,
        # which ROLLS BACK — so without this the route answered 200 with a full allocation list and the
        # database was untouched. No sleeve_transfer row, no `actual` moved, BCTROT still at
        # deployable 0, and the next call reporting the same $30,000 again forever.
        #
        # Every other `session_factory() as session` in this module is a READ; this is the only write
        # using the pattern, which is how it was the only one missing the commit.
        await session.commit()
    out = [{"to_strategy": a.to_strategy, "amount": float(a.amount)} for a in allocations]
    return {
        "run_id": run_id,
        "allocations": out,
        # The total is returned rather than left to the caller to sum. An operator moving capital needs
        # the number back, and a client that re-derives it is a second derivation that can disagree.
        "distributed": round(sum(a["amount"] for a in out), 2),
    }


@app.get("/transfers", operation_id="getTransfers", response_model=TransfersResponse, tags=["transfers"])
async def get_transfers() -> TransfersResponse:
    """Internal position transfers and their outbox state (#80 spin-off)."""
    from api import transfers as tr
    from api.db.engine import session_factory

    async with session_factory() as session:
        progress = await tr.all_transfers(session)
    return TransfersResponse(
        transfers=[
            TransferDTO(
                transfer_id=p.transfer_id,
                instrument_id=p.request.instrument_id,
                source_strategy_id=p.request.source_strategy_id,
                target_strategy_id=p.request.target_strategy_id,
                side=p.request.side,
                quantity=float(p.request.quantity),
                pricing_mode=p.request.pricing_mode,
                transfer_px=float(p.request.transfer_px),
                source_avg_px=float(p.request.source_avg_px),
                state=p.state,
                reason_code=p.request.reason_code,
            )
            for p in progress
        ]
    )


@app.post("/transfers", operation_id="transferPosition", response_model=CommandResponse, tags=["transfers"])
async def transfer_position(body: TransferRequestBody) -> CommandResponse:
    """Move a position (or part of one) from one strategy to another.

    Places NO order: the broker net is identical before and after, and only the owning strategy changes. `ok`
    means ENQUEUED — the engine validates against its live cache (the api holds only stale snapshots) and the
    accept/reject arrives via `GET /commands/{command_id}`."""
    src = app.state.node.strategy_position(body.instrument_id, body.source_strategy_id, body.side)
    if src is None:
        raise HTTPException(status_code=404, detail="no source position for that instrument/strategy/side")
    payload = {
        "instrument_id": body.instrument_id,
        "source_strategy_id": body.source_strategy_id,
        "target_strategy_id": body.target_strategy_id,
        "side": body.side,
        "quantity": body.quantity,
        "pricing_mode": body.pricing_mode,
        "reason_code": body.reason_code,
        "source_ts_last": src["ts_last"],  # stale-read guard, re-checked by the engine
    }
    return CommandResponse(command_id=await _enqueue("transfer_position", payload))


@app.get("/watchlist", operation_id="getWatchlist", response_model=WatchlistResponse, tags=["watchlist"])
async def get_watchlist(session: AsyncSession = Depends(get_session)) -> WatchlistResponse:
    """The runtime watchlist (Postgres) — instrument ids, oldest-added first."""
    return WatchlistResponse(symbols=await wl.list_symbols(session))


@app.post("/watchlist", operation_id="addWatchlist", response_model=WatchlistResponse, tags=["watchlist"])
async def add_watchlist(
    body: WatchlistAdd, session: AsyncSession = Depends(get_session)
) -> WatchlistResponse:
    """Add a symbol to the watchlist (idempotent) → the updated list."""
    await wl.add_symbol(session, body.instrument_id)
    return WatchlistResponse(symbols=await wl.list_symbols(session))


@app.delete(
    "/watchlist/{instrument_id}",
    operation_id="removeWatchlist",
    response_model=WatchlistResponse,
    tags=["watchlist"],
)
async def remove_watchlist(
    instrument_id: str, session: AsyncSession = Depends(get_session)
) -> WatchlistResponse:
    """Remove a symbol from the watchlist → the updated list."""
    await wl.remove_symbol(session, instrument_id)
    return WatchlistResponse(symbols=await wl.list_symbols(session))


# --- Symbol pool (#79 follow-on) ----------------------------------------------
# The pool lives in the executor's Postgres tables and is what MOMENTUM ranks each session. It is
# surfaced HERE, in the cockpit, rather than in a second web app beside the strategy: the cockpit is
# the UI, and a parallel operator page is how the whitelist/blacklist ended up somewhere the operator could
# not reach it from his phone.
@app.get("/pool", operation_id="getPool", response_model=PoolResponse, tags=["pool"])
async def get_pool() -> PoolResponse:
    """The symbol pool with operator overrides, plus per-source freshness."""
    node: Node = app.state.node
    held = {p.instrument_id.split(".")[0] for p in node.positions() if p.quantity}
    rows = await pool.list_pool(held)
    return PoolResponse(
        # `count` is the RANKABLE pool, so it excludes blacklisted rows even though they are listed.
        count=sum(1 for r in rows if r["override"] != "exclude"),
        symbols=[PoolEntryDTO(**r) for r in rows],
        sources=[PoolSourceDTO(**s) for s in await pool.source_health()],
    )


def _replace_detail(body, detail: dict):
    """Return `body` carrying a new `detail` — pydantic models are immutable by config here, and a
    refusal has to reach the pool surface rather than only a log (#663/#557)."""
    try:
        return body.model_copy(update={"detail": detail})
    except AttributeError:          # a plain object in tests
        body.detail = detail
        return body


@app.post("/pool/source/{name}", operation_id="refreshPoolSource", response_model=PoolResponse,
          tags=["pool"])
async def refresh_pool_source(name: str, body: PoolSourceRefreshRequest) -> PoolResponse:
    """Replace a source's whole set — the write end of the instance-supplied refreshers (#486).

    An instance owns WHERE its symbols come from: a folder, a Google Sheet, a CSV in GitHub, a script.
    The platform owns only the contract, which is this endpoint. It writes nothing itself; the single
    writer stays in `PgSymbolPool`.
    """
    from kumo_strategies.runtime.executor.pgpool import ShrinkRejected, UnknownSource

    from api.pool_validation import validate_pool_symbols

    # REFUSE WHAT THE VENUE DOES NOT LIST, AT THE DOOR (#663/#557). Nine symbols have sat
    # unresolvable in the paper pool for days — BLLLN, IQVIA, JEPO, OVVI and friends — dropped at
    # ERROR on every boot by the lanes, which is correct but leaves nine permanent entries that
    # hide the tenth. The catalog this consults is the one the API already keeps for symbol search.
    # A catalog that has not loaded ACCEPTS everything and says so: refusing a pool because a REST
    # call failed would take every lane out of the market over an unrelated outage.
    verdict = validate_pool_symbols(body.symbols, app.state.search)
    symbols = list(body.symbols)
    if verdict.refused:
        # THE GOOD SYMBOLS GO THROUGH. Refusing the whole push over one bad name would block a
        # 106-symbol refresh on a single typo, and `verdict.accepted` already carries the answer —
        # a computed-and-discarded value is its own defect class here. The refusal is REPORTED
        # instead: named, with its correction where one is knowable, in the source's own detail so
        # it lands on the pool surface rather than in a log nobody opens.
        named = ", ".join(
            f"{sym} (did you mean {verdict.suggestions[sym]}?)" if sym in verdict.suggestions
            else sym for sym in verdict.refused
        )
        _log.error("pool refresh %s: %d symbol(s) not listed by the venue, dropped: %s",
                   name, len(verdict.refused), named)
        symbols = list(verdict.accepted)
        detail = dict(body.detail or {})
        detail["refused_symbols"] = list(verdict.refused)
        detail["refused_reason"] = f"not listed by the venue: {named}"
        body = _replace_detail(body, detail)

    try:
        await pool.refresh_source(name, symbols, detail=body.detail,
                                  create=body.create, allow_shrink=body.allow_shrink)
    except UnknownSource as exc:
        # A typo must not become a source that is fresh, real and feeds nothing while the one it was
        # meant to refresh ages quietly into a hard block on deciding.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ShrinkRejected as exc:
        # 409, not 400: the payload is well-formed and the CONFLICT is with what the source held
        # before. Retrying it unchanged is wrong; supplying a reason is the resolution.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await get_pool()


@app.post("/pool/override", operation_id="setPoolOverride", response_model=PoolResponse, tags=["pool"])
async def set_pool_override(body: PoolOverrideRequest) -> PoolResponse:
    """Pin or exclude a symbol → the updated pool.

    Takes effect at the NEXT session, not immediately: an exclude marks the symbol for liquidation
    when the strategy next runs, it does not sell anything here.
    """
    from api.pool_validation import validate_pool_symbols

    # A PIN WARNS, IT DOES NOT REFUSE (#663). GTLAB is a pin row from 2026-08-11 and has been
    # unresolvable ever since, so this door needs the check too — but a pin is the operator's
    # explicit override, and the case it exists for includes "our catalog is the thing that is
    # wrong". So the verdict is recorded and returned, never enforced.
    verdict = validate_pool_symbols([body.symbol], app.state.search)
    if verdict.refused:
        hint = verdict.suggestions.get(body.symbol.upper())
        _log.warning(
            "pool override %s %s: the venue does not list this symbol%s — pinned anyway (operator "
            "override), but it will never resolve and will sit in the unresolved list",
            body.kind, body.symbol, f" (did you mean {hint}?)" if hint else "",
        )
    await pool.set_override(body.symbol, body.kind, body.reason)
    return await get_pool()


@app.delete(
    "/pool/override/{symbol}/{kind}",
    operation_id="clearPoolOverride",
    response_model=PoolResponse,
    tags=["pool"],
)
async def clear_pool_override(symbol: str, kind: str) -> PoolResponse:
    """Remove a pin or exclude → the updated pool."""
    if kind not in ("pin", "exclude"):
        raise HTTPException(status_code=422, detail=f"kind must be 'pin' or 'exclude', got {kind!r}")
    await pool.clear_override(symbol, kind)
    return await get_pool()


@app.get(
    "/ws/contract",
    operation_id="getWsContract",
    response_model=WSMessage,
    tags=["meta"],
    summary="WS frame contract (codegen anchor — do not call)",
)
async def ws_contract() -> WSMessage:
    """Documents the `/ws/stream` frame shape so the TS client generates WS types too.

    WebSocket payloads are invisible to OpenAPI, so this REST stub pins the discriminated
    `bar | fill | status` union into the schema. Not used at runtime by the UI.
    """
    return StatusMessage(data="replay_start")


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket) -> None:
    """Multiplexed topic stream (#7 P3). Holds the socket open and serves `control` frames.

    On subscribe to a valid topic: `control_ack(ok)` → `replay_start` → per-topic `snapshot` →
    `replay_end`, after which a single per-connection push loop streams live updates over all subscribed
    topics (new `bars` as `bar` increments; `positions`/`fills` re-pushed as snapshots). Unknown/invalid
    topic → `control_ack(error)` + an `error` frame.
    """
    await websocket.accept()
    node: Node = app.state.node
    topics: dict[str, Topic] = {}  # subscribed topics by key — the push loop iterates these
    cursors: dict[str, int] = {}  # per-bars-topic last ts_event streamed (dedup increments)
    push_task: asyncio.Task | None = None
    send_lock = asyncio.Lock()  # serialize sends: receive-loop frames vs the push loop on one socket

    async def send(frame: EventFrame) -> None:
        async with send_lock:
            await _send(websocket, frame)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = ControlFrame.model_validate_json(raw)
            except ValidationError:
                await send(EventFrame(event="error", payload={"frame_type": "status", "data": {"code": "bad_frame"}}))
                continue

            if frame.op == "ping":
                await send(EventFrame(event="heartbeat", payload={"info": {"engine": "ok"}}))
                continue

            topic = frame.topic
            if topic is None:
                await send(EventFrame(event="error", payload={"frame_type": "status", "data": {"code": "missing_topic"}}))
                continue

            if frame.op == "unsubscribe":
                key = topic.key()
                topics.pop(key, None)
                cursors.pop(key, None)
                await send(EventFrame(event="control_ack", topic=topic, payload={"op": "unsubscribe", "status": "ok"}))
                continue

            # subscribe
            snapshot = await _snapshot_for(node, topic)
            if snapshot is None:
                err = {"code": "unknown_topic", "message": f"cannot serve topic {topic.key()}"}
                await send(
                    EventFrame(event="control_ack", topic=topic, payload={"op": "subscribe", "status": "error", "error": err})
                )
                await send(
                    EventFrame(
                        event="error",
                        topic=topic,
                        payload={"frame_type": "status", "data": {"code": "subscription_error", "details": err}},
                    )
                )
                continue

            key = topic.key()
            topics[key] = topic
            # Seed the bars cursor at the snapshot's newest bar so the push loop only sends genuinely new ones.
            cursors[key] = max((b["ts_event"] for b in snapshot.get("bars", [])), default=-1)
            await send(EventFrame(event="control_ack", topic=topic, payload={"op": "subscribe", "status": "ok"}))
            await send(
                EventFrame(event="status", topic=topic, payload={"frame_type": "status", "data": {"code": "replay_start"}})
            )
            await send(EventFrame(event="data", topic=topic, payload={"frame_type": "snapshot", "data": snapshot}))
            await send(
                EventFrame(event="status", topic=topic, payload={"frame_type": "status", "data": {"code": "replay_end"}})
            )
            if push_task is None:  # one live-push loop per connection, started on first subscribe
                push_task = asyncio.create_task(_push_loop(websocket, node, topics, cursors, send_lock))
    except WebSocketDisconnect:
        return
    finally:
        if push_task is not None:
            push_task.cancel()
