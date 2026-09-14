"""Alpaca live market-data client — a pure-Python Nautilus `LiveMarketDataClient` (no SDK, no pyo3).

Increment B (this file): connect + instrument definitions + historical bars (`_request_bars` via the
Alpaca REST bars endpoint, paginated). Increment C adds the live WebSocket (`_subscribe_bars`/trades/
quotes); the subscribe hooks are stubbed here so the load path (history → subscribe) doesn't crash.

The client is the instrument authority for the `ALPACA` client id: the node routes every instrument/bar
request here, and definitions come from `AlpacaInstrumentProvider` (canonical `TICKER.MIC` ids).
"""

from __future__ import annotations

import asyncio
import logging
import os

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.data.messages import (
    RequestBars,
    RequestInstrument,
    SubscribeBars,
    SubscribeQuoteTicks,
    SubscribeTradeTicks,
    UnsubscribeBars,
    UnsubscribeQuoteTicks,
    UnsubscribeTradeTicks,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide, BarAggregation
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

_log = logging.getLogger(__name__)

from api.providers.alpaca.config import AlpacaDataClientConfig
from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.alpaca.providers import AlpacaInstrumentProvider
from api.providers.alpaca.websocket import AlpacaWebSocketClient
from api.providers.base import DataClientSpec

_KEY_ENV = "APCA_API_KEY_ID"
_SECRET_ENV = "APCA_API_SECRET_KEY"

# Which Alpaca live channel serves each bar aggregation. Alpaca streams minute bars ("bars") and
# daily bars ("dailyBars") only — no hourly WS channel, so 1h bars are REST-history-only (no live).
_AGG_TO_CHANNEL: dict[BarAggregation, str] = {
    BarAggregation.MINUTE: "bars",
    BarAggregation.DAY: "dailyBars",
}
# Live data-frame type → the aggregation it belongs to. "b"/"u" = minute bar (fresh/corrected),
# "d" = daily bar. Used to route an incoming frame back to its subscribed BarType.
_FRAME_TYPE_TO_AGG: dict[str, BarAggregation] = {
    "b": BarAggregation.MINUTE,
    "u": BarAggregation.MINUTE,
    "d": BarAggregation.DAY,
}

ALPACA = "ALPACA"  # Nautilus ClientId — requests pin here so Alpaca's definitions win

# Nautilus bar aggregation → Alpaca `timeframe` unit. Alpaca form: "{step}{unit}" e.g. "1Min","5Min",
# "1Hour","1Day","1Week". The step comes from the bar spec, so 5/15/30-MINUTE all map through "Min".
# Used for REST history only — Alpaca serves every one of these natively regardless of the bar type's
# aggregation source (INTERNAL vs EXTERNAL): the source governs LIVE subscription, not the REST timeframe.
_AGGREGATION_UNIT: dict[BarAggregation, str] = {
    BarAggregation.MINUTE: "Min",
    BarAggregation.HOUR: "Hour",
    BarAggregation.DAY: "Day",
    BarAggregation.WEEK: "Week",
}


def _alpaca_timeframe(bar_type: BarType) -> str:
    """'AAPL.XNAS-1-MINUTE-LAST-EXTERNAL' → '1Min'. Raises on aggregations Alpaca can't serve."""
    spec = bar_type.spec
    unit = _AGGREGATION_UNIT.get(spec.aggregation)
    if unit is None:
        raise ValueError(f"Alpaca has no timeframe for aggregation {spec.aggregation!r} ({bar_type})")
    return f"{spec.step}{unit}"


class AlpacaDataClient(LiveMarketDataClient):
    """Live market-data client for Alpaca US equities (REST history now; WS live in increment C)."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: AlpacaInstrumentProvider,
        http_client: AlpacaHttpClient,
        config: AlpacaDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(ALPACA),
            venue=None,  # multi-venue (XNAS/XNYS/…): the node routes by client id, not venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._http = http_client
        self._feed = config.feed
        # The build-time seed set (#1057), read at `_connect`. Stored as its own attribute rather
        # than through a `_config` the base class does not keep: the end-to-end test through the real
        # factory caught that the hosts in the seam tests carried a `_config` the real client never
        # had — a double representing a wire production did not have.
        self._seed_symbols: tuple[str, ...] = tuple(config.seed_symbols or ())
        self._ws = AlpacaWebSocketClient(
            url=f"{config.ws_base_url}/{config.feed}",
            key=config.api_key or "",
            secret=config.api_secret or "",
            handler=self._on_ws_frame,
            log=self._log,
        )
        # (symbol, aggregation) → the subscribed BarType, so an incoming live frame routes to its type.
        self._live_bar_types: dict[tuple[str, BarAggregation], BarType] = {}
        # symbol → InstrumentId for live trade-tick subs (Alpaca trade frames carry only the bare symbol,
        # so we map it back to the canonical TICKER.MIC id). This is the real-time last-price feed.
        self._live_trade_ids: dict[str, InstrumentId] = {}
        self._live_quote_ids: dict[str, InstrumentId] = {}

    # -- lifecycle --------------------------------------------------------------------------------
    async def _connect(self) -> None:
        await self._http.connect()
        # Load the tradable universe once; per-symbol definitions are then served from the provider
        # on `_request_instrument` (which seeds the cache). Called directly rather than via
        # `initialize()` since the provider has no load_all=True config.
        await self._instrument_provider.load_all_async()
        self._log.info(f"Loaded {self._instrument_provider.count} Alpaca instruments")
        # SEED THE CACHE WITH THE DEPLOYMENT'S NAMES (#1057). The provider now holds ~13,400
        # definitions and the lanes resolve against the CACHE (`symbol_resolution._resolve_symbols`
        # -> `cache.instrument_ids()`), which on this venue was filled only by `_request_instrument`
        # — a request a lane cannot issue for a symbol it failed to resolve. Measured on paper
        # 2026-09-13: cache 406 of provider 13,435; 11 ordinary listed pool names (BIIB, BOX, DBX,
        # GPRK, NSIT, OKE, SLB, SUNC, UGP, UPRO, XPRO) "have no instrument on this venue" on every
        # boot and "bars missing" on every slot. IBKR pushes every loaded contract into the cache
        # inside `_connect` (the shipped adapter's data.py:147) over `load_contracts`' pool-following
        # set (#511, #871); this is the same step over the SAME set — read through the IBKR module's
        # own functions, never a second list — and ONLY that set: the whole catalogue into a durable
        # Redis cache is not the fix. Before the websocket starts, so no live frame can precede its
        # instrument.
        self._seed_cache_with_deployment_symbols()
        await self._ws.start()

    def _seed_cache_with_deployment_symbols(self) -> None:
        """Hand every provider definition the deployment names to the cache; name what it lacks.

        The set is `self._seed_symbols`, taken from `config.seed_symbols` at construction — computed by `build_data` at BUILD time from
        `api.providers.ibkr._tradeable_symbols()` ∪ `_reference_symbols()` — the exact list
        `load_contracts` follows on IBKR. NOTHING IS READ HERE: this runs inside the node's loop,
        where `_pool_symbols` refuses (paper 2026-09-13 08:19Z, the first version of this step).
        An empty set is reported as such — "nothing seeded" must never read as "nothing to seed".

        A named symbol the provider does not know is reported BY NAME at WARNING, because absence
        must not read as seeded (five ingestion fragments in the pool today: BLLLN, GTLAB, IQVIA,
        JEPO, OVVI).
        """
        # A PLAIN READ of the attribute `__init__` set from the config: no getattr default, which
        # would turn a wrongly built client into "nothing to seed" and blame the build for it.
        wanted = {str(s).upper() for s in self._seed_symbols}
        if not wanted:
            self._log.error(
                "cache seeding: the build-time seed set is EMPTY — no instrument is seeded; every "
                "pool name no earlier boot requested will read 'no instrument on this venue' this "
                "session. `build_data` could not read any symbol set (#1057)")
            return
        by_symbol: dict[str, list] = {}
        for instrument in self._instrument_provider.list_all():
            by_symbol.setdefault(instrument.id.symbol.value.upper(), []).append(instrument)
        seeded, unknown = 0, []
        for sym in sorted(wanted):
            found = by_symbol.get(sym)
            if not found:
                unknown.append(sym)
                continue
            for instrument in found:
                self._handle_data(instrument)
                seeded += 1
        # WHICH SETS CONTRIBUTED, so a half-seeded boot (one set unreadable) reads as exactly that
        # and never as "seeded". Two definitions for one symbol (two MICs) are BOTH handed over:
        # the venue choice belongs to the resolver (`prefer_primary_exchange`), not to the seeder.
        self._log.info(f"cache seeding: {seeded} instrument(s) for {len(wanted)} deployment symbol(s) "
                       f"handed to the cache (set computed at build, #1057)")
        if unknown:
            self._log.warning(
                f"cache seeding: {len(unknown)} of {len(wanted)} deployment symbol(s) have NO Alpaca "
                f"definition and cannot be seeded — {', '.join(unknown)}. A name here is either not a "
                f"ticker (ingestion, #1057) or not served on this venue; it will read 'no instrument "
                f"on this venue' until it leaves the pool")

    async def _disconnect(self) -> None:
        await self._ws.stop()
        await self._http.close()

    # -- instrument definitions -------------------------------------------------------------------
    async def _request_instrument(self, request: RequestInstrument) -> None:
        instrument = self._instrument_provider.find(request.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument {request.instrument_id}")
            return
        self._handle_instrument(instrument, request.id, request.start, request.end, request.params)

    # -- historical bars --------------------------------------------------------------------------
    async def _request_bars(self, request: RequestBars) -> None:
        bar_type = request.bar_type
        instrument = self._instrument_provider.find(bar_type.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot request bars — unknown instrument {bar_type.instrument_id}")
            return

        symbol = bar_type.instrument_id.symbol.value
        timeframe = _alpaca_timeframe(bar_type)
        start = pd.Timestamp(request.start).isoformat() if request.start is not None else None
        end = pd.Timestamp(request.end).isoformat() if request.end is not None else None
        if start is None:
            self._log.error(f"Alpaca bar request needs a start datetime ({bar_type})")
            return

        bars: list[Bar] = []
        page_token: str | None = None
        while True:
            payload = await self._http.get_bars(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                feed=self._feed,
                page_token=page_token,
            )
            for raw in payload.get("bars") or []:
                parsed = self._parse_bar(bar_type, instrument, raw)
                if parsed is None:
                    self._log.warning(f"dropped a malformed Alpaca bar for {bar_type} "
                                      f"(missing OHLCV field) at {raw.get('t')}")
                    continue
                bars.append(parsed)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        if request.limit and len(bars) > request.limit:
            bars = bars[-request.limit :]  # Alpaca returns oldest-first; keep the most recent `limit`
        self._handle_bars(bar_type, bars, request.id, request.start, request.end, request.params)

    @staticmethod
    def _parse_bar(bar_type: BarType, instrument, raw: dict) -> Bar | None:
        """One Alpaca bar dict {t,o,h,l,c,v} → a Nautilus `Bar`, or None if it is malformed.

        `raw.get("v", 0)` used to substitute a real ZERO for an absent volume. Nautilus `Quantity`
        cannot express "unknown", so the choice is between inventing a number and refusing the
        record — and the invented one flows into the Vol KPI, into sorting, and into anything
        reading volume, where a genuine zero-volume bar and a bar whose volume never arrived are
        indistinguishable. A dropped bar leaves a visible hole; a fabricated zero does not.
        """
        if any(raw.get(k) is None for k in ("t", "o", "h", "l", "c", "v")):
            return None
        prec = instrument.price_precision
        ts = pd.Timestamp(raw["t"]).value  # ISO8601 (UTC) → epoch ns
        return Bar(
            bar_type,
            Price(raw["o"], prec),
            Price(raw["h"], prec),
            Price(raw["l"], prec),
            Price(raw["c"], prec),
            Quantity(raw["v"], instrument.size_precision),
            ts,
            ts,
        )

    # -- live subscriptions (WebSocket) -----------------------------------------------------------
    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        # Only EXTERNAL bar types reach here: the DataEngine routes INTERNAL/aggregated types to its own
        # TimeBarAggregator (subscribe_bars is never called for them). So the sole EXTERNAL aggregations we
        # ever see are the ones Alpaca streams live — 1m (`bars`) and 1d (`dailyBars`). Any other EXTERNAL
        # type is a misconfiguration (Alpaca has no venue channel for it) and correctly warns rather than
        # silently freezing. Live-ticking 5m/15m/30m/1h/1w are INTERNAL (see api/bar_spec.py) → handled upstream.
        bar_type = command.bar_type
        agg = bar_type.spec.aggregation
        channel = _AGG_TO_CHANNEL.get(agg)
        if channel is None:
            self._log.warning(f"Alpaca streams no live bars for {bar_type} (aggregation unsupported)")
            return
        symbol = bar_type.instrument_id.symbol.value
        self._live_bar_types[(symbol, agg)] = bar_type
        await self._ws.subscribe(channel, [symbol])

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        bar_type = command.bar_type
        agg = bar_type.spec.aggregation
        channel = _AGG_TO_CHANNEL.get(agg)
        if channel is None:
            return
        symbol = bar_type.instrument_id.symbol.value
        self._live_bar_types.pop((symbol, agg), None)
        await self._ws.unsubscribe(channel, [symbol])

    # -- live subscriptions: trade ticks (the real-time last-price feed) --------------------------
    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        """Subscribe Alpaca's real-time `trades` stream — every execution, sub-second. This is the live
        last-price plane for the cockpit (bars are too coarse to trade against). Note: the free IEX feed
        only carries IEX-executed trades (~a few % of volume) — thin for illiquid names; SIP is the full
        tape. Feed-agnostic: this same path serves SIP once `feed` is switched."""
        symbol = command.instrument_id.symbol.value
        self._live_trade_ids[symbol] = command.instrument_id
        await self._ws.subscribe("trades", [symbol])

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        symbol = command.instrument_id.symbol.value
        self._live_trade_ids.pop(symbol, None)
        await self._ws.unsubscribe("trades", [symbol])

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        """Subscribe Alpaca's real-time `quotes` (NBBO bid/ask) stream — the spread/mid plane for the
        order ticket (marketable-limit prefill) + the auto-select rules engine. Same IEX/SIP caveat as
        trades: the free IEX feed carries only IEX quotes."""
        symbol = command.instrument_id.symbol.value
        self._live_quote_ids[symbol] = command.instrument_id
        await self._ws.subscribe("quotes", [symbol])

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        symbol = command.instrument_id.symbol.value
        self._live_quote_ids.pop(symbol, None)
        await self._ws.unsubscribe("quotes", [symbol])

    def _parse_quote(self, instrument, frame: dict) -> QuoteTick:
        """One Alpaca quote frame {bp,ap,bs,as,t,...} → a Nautilus `QuoteTick`. Alpaca reports bid/ask
        sizes in ROUND LOTS (100 shares); scale to shares to match Equity.lot_size=1."""
        ts = pd.Timestamp(frame["t"]).value  # RFC3339 (UTC) → epoch ns
        return QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price(frame["bp"], instrument.price_precision),
            ask_price=Price(frame["ap"], instrument.price_precision),
            bid_size=Quantity(frame.get("bs", 0) * 100, instrument.size_precision),
            ask_size=Quantity(frame.get("as", 0) * 100, instrument.size_precision),
            ts_event=ts,
            ts_init=ts,
        )

    def _parse_trade(self, instrument, frame: dict) -> TradeTick:
        """One Alpaca trade frame {p,s,i,t,...} → a Nautilus `TradeTick`. Alpaca doesn't report an
        aggressor side → NO_AGGRESSOR."""
        ts = pd.Timestamp(frame["t"]).value  # RFC3339 (UTC) → epoch ns
        return TradeTick(
            instrument_id=instrument.id,
            price=Price(frame["p"], instrument.price_precision),
            size=Quantity(frame.get("s", 0), instrument.size_precision),
            aggressor_side=AggressorSide.NO_AGGRESSOR,
            trade_id=TradeId(str(frame.get("i", ts))),
            ts_event=ts,
            ts_init=ts,
        )

    def _on_ws_frame(self, frame: dict) -> None:
        """Wrapper: one malformed frame costs its ROW, never the CONNECTION (#698, review 2026-08-29).

        The field guard below stops the zero-size print that was measured killing the socket, but it
        is an instance fix: a bad price, an unparseable timestamp or a size that is not a number all
        still escape into the read loop, which treats any exception as a broken socket. Aim at the
        CLASS. Drops are COUNTED and reported on a bounded cadence — a silently swallowed frame is a
        feed outage that reads as a quiet market, which is the failure this file already had once.
        """
        try:
            self._on_ws_frame_inner(frame)
        except Exception as exc:  # noqa: BLE001 — the whole point: do not lose the socket
            self._malformed_frames = getattr(self, "_malformed_frames", 0) + 1
            if self._malformed_frames in (1, 10, 100) or self._malformed_frames % 1000 == 0:
                self._log.warning(
                    f"dropped {self._malformed_frames} malformed live Alpaca frame(s); latest "
                    f"{frame.get('T')}/{frame.get('S')}: {exc!r}"
                )

    def _on_ws_frame_inner(self, frame: dict) -> None:
        """Route one Alpaca live frame → the matching Nautilus data object and push it to the DataEngine.
        `t` = trade tick (real-time last price); `b`/`u`/`d` = bar increments."""
        ftype = frame.get("T")
        symbol = frame.get("S")
        if ftype == "t":  # trade tick
            instrument_id = self._live_trade_ids.get(symbol)
            if instrument_id is None:
                return
            # SKIP A ZERO-SIZE PRINT, DO NOT LET IT REACH THE PARSE (#698). Alpaca emits size-0
            # trades routinely overnight (corrections, odd-lot artefacts); Nautilus's Quantity
            # refuses them, the ValueError escapes this handler, and the READ LOOP reads it as a
            # broken socket — 12 full reconnects in 30 minutes on 2026-08-28, each with a
            # resubscribe burst. The quote branch below has guarded its own version of this since
            # it was written; the trade branch never got it. One bad ROW must not cost the
            # CONNECTION (#643, #697 — the same class at three venue surfaces).
            size = frame.get("s")
            if not size or float(size) <= 0:
                return
            instrument = self._instrument_provider.find(instrument_id)
            if instrument is not None:
                self._handle_data(self._parse_trade(instrument, frame))
            return
        if ftype == "q":  # NBBO quote (bid/ask)
            instrument_id = self._live_quote_ids.get(symbol)
            if instrument_id is None:
                return
            bp, ap = frame.get("bp"), frame.get("ap")
            # Skip zero/one-sided/crossed quotes — they'd poison spread/mid/prefill downstream.
            if not bp or not ap or bp <= 0 or ap <= 0 or bp > ap:
                return
            instrument = self._instrument_provider.find(instrument_id)
            if instrument is not None:
                self._handle_data(self._parse_quote(instrument, frame))
            return
        agg = _FRAME_TYPE_TO_AGG.get(ftype)
        if agg is None:
            return  # control/other frame — not consumed here
        bar_type = self._live_bar_types.get((symbol, agg))
        if bar_type is None:
            return  # a symbol we're not tracking at this aggregation
        instrument = self._instrument_provider.find(bar_type.instrument_id)
        if instrument is None:
            return
        # A malformed live frame must not reach _handle_data as None.
        parsed = self._parse_bar(bar_type, instrument, frame)
        if parsed is None:
            self._log.warning(f"dropped a malformed live Alpaca bar for {bar_type}")
            return
        self._handle_data(parsed)


class AlpacaLiveDataClientFactory(LiveDataClientFactory):
    """Constructs `AlpacaDataClient` (with its REST client + instrument provider) for the node."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: AlpacaDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> AlpacaDataClient:
        http_client = AlpacaHttpClient(
            key=config.api_key or "",
            secret=config.api_secret or "",
            trading_base=config.trading_base_url,
            data_base=config.data_base_url,
        )
        instrument_provider = AlpacaInstrumentProvider(http_client)
        return AlpacaDataClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            http_client=http_client,
            config=config,
        )


#: Alpaca's free/IEX plan 405s the ENTIRE stream once trades+quotes+native-bar channels cross its slot
#: budget, so an over-subscribe is a total feed outage while an under-subscribe only costs live ticks on
#: some symbols. THIS CONSTANT IS ALPACA'S AND LIVES HERE (#619) — it used to sit in `engine_node`, where
#: every provider read it.
_MAX_REALTIME_SYMBOLS_IEX = 7


def realtime_symbol_budget(feed: str) -> float:
    """How many symbols may hold live subscriptions on Alpaca's `feed`.

    `inf` on the paid SIP feed (Algo Trader Plus — unlimited WS subscriptions); the free-plan slot budget
    on anything else. Unknown feed strings fall back to the CAPPED value on purpose: over-subscribing 405s
    the entire stream, while under-subscribing only costs live ticks on some symbols.

    Alpaca-only by construction now. It is reachable only through `build_data` below, so there is no path
    by which another provider can be measured against an Alpaca price plan.
    """
    return float("inf") if feed == "sip" else float(_MAX_REALTIME_SYMBOLS_IEX)


def deployment_seed_symbols() -> tuple[str, ...]:
    """The symbols the data client seeds the cache with — computed OUTSIDE any event loop (#1057).

    ONE derivation with IBKR: `api.providers.ibkr._tradeable_symbols()` (declared universe ∪ pool ∪
    enabled lanes' universes − exclusions) ∪ `_reference_symbols()` (the compass axes, #1041) — the
    set `load_contracts` follows. Function-local import: the IBKR module carries adapter imports
    this client must not pay for at import time.

    A set that cannot be read is ONE ERROR line and its names are absent from the result — the feed
    must still build; `_tradeable_symbols` itself already falls back to the declared universe when
    only the POOL is unreadable and says so at ERROR. Half a set is returned as half a set; the
    client's seeding step reports an EMPTY result as its own condition.
    """
    from api.providers import ibkr as _shared_symbol_sets

    wanted: set[str] = set()
    for label, read in (("tradeable", "_tradeable_symbols"), ("reference", "_reference_symbols")):
        try:
            wanted |= {str(s).upper() for s in getattr(_shared_symbol_sets, read)()}
        except Exception as exc:  # noqa: BLE001 — the feed builds without this set, loudly
            _log.error(
                "cache seeding: the %s symbol set could not be read at build (%r) — no instrument "
                "from it will be seeded; names in it that no earlier boot requested will read 'no "
                "instrument on this venue' this session (#1057)", label, exc)
    return tuple(sorted(wanted))


def build_data(provider_config: dict) -> DataClientSpec:
    """Build the Alpaca data client spec from the `[data.alpaca]` table.

    Secrets come from the environment (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`, override via the
    table's `key_env` / `secret_env`); the launch script injects them from the keychain. The table
    may also set `feed` ("iex"/"sip") and override the base URLs.
    """
    key_env = provider_config.get("key_env", _KEY_ENV)
    secret_env = provider_config.get("secret_env", _SECRET_ENV)
    api_key = os.environ.get(key_env)
    api_secret = os.environ.get(secret_env)
    if not api_key or not api_secret:
        raise RuntimeError(
            f"{key_env}/{secret_env} unset — launch via backend/scripts/run-api.sh (injects from keychain)."
        )
    defaults = AlpacaDataClientConfig()
    config = AlpacaDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        trading_base_url=provider_config.get("trading_base_url", defaults.trading_base_url),
        data_base_url=provider_config.get("data_base_url", defaults.data_base_url),
        ws_base_url=provider_config.get("ws_base_url", defaults.ws_base_url),
        feed=provider_config.get("feed", defaults.feed),
        # HERE, NOT IN `_connect` (#1057). This builder runs before the node's loop exists — the
        # same place IBKR computes `_data_contracts()` for `load_contracts` — so the pool can be
        # read. `_pool_symbols` refuses inside a running loop by design; the first version of the
        # seeding read the set from the async `_connect` and every boot fell back to the declared
        # universe (paper 2026-09-13 08:19:32Z), seeding 270 names and none of the pool's.
        seed_symbols=deployment_seed_symbols(),
    )
    return DataClientSpec(
        client_id=ALPACA,
        config=config,
        factory=AlpacaLiveDataClientFactory,
        # Declared from THIS provider's own feed, not read out of the engine (#619).
        realtime_symbol_budget=realtime_symbol_budget(config.feed),
        # Alpaca daily OHLC is the REGULAR session by construction (#616): `/v2/stocks/{sym}/bars`
        # has no session parameter, and Alpaca's aggregation rules (Market Data FAQ, "How are bars
        # aggregated?") mark extended-hours trade conditions `T`/`U` as updating a DAILY bar's
        # volume but never its open/close or high/low. IBKR declares the opposite — that pair
        # differing is #616's measurement, and a test pins it.
        daily_bars_cover="rth",
        # Alpaca streams trades over its live WebSocket — the venue `bar_spec.py`'s INTERNAL split
        # was written for, and the one where it works.
        streams_trade_ticks=True,
        # Alpaca streams NBBO quotes on the same WebSocket as trades — the plane the order ticket's
        # spread/mid prefill and the marketable-limit auto-select read (#40). Declaring this False
        # would silently delete a working plane on the tenant that is actually trading, which is a
        # far worse outcome than the log noise #812 is about.
        streams_quote_ticks=True,
    )
