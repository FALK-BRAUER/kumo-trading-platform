"""IB shortability as a Nautilus custom Data type — the borrow gate CRSISHORT demands (#857).

WHAT IB PROVIDES AND WHAT NAUTILUS DOES WITH IT. Generic tick 236 on `reqMktData` delivers tick 46
(SHORTABLE: > 2.5 easy to borrow, 1.5–2.5 locate required, < 1.5 not shortable) via `tickGeneric`
and tick 89 (SHORTABLE_SHARES) via `tickSize`. There is NO fee-rate tick in the TWS API. The
installed NautilusTrader 1.229 has no concept of shortability at all: its `InteractiveBrokersEWrapper`
overrides `tickGeneric` to a DEBUG log line and discards it (`client/wrapper.py:206`), while the
REQUEST side is public — `subscribe_market_data(instrument_id, contract, generic_tick_list="236")`.

So this module is out-of-tree, never a fork: a wrapper SUBCLASS that forwards the two ticks into a
registry and otherwise defers to the stock wrapper, installed onto the cached IB client's `EClient`
after `node.build()`; a `Shortable(Data)` published through `Actor.publish_data`, so the UI and any
other actor read it off the bus like a quote; and a `borrow_rates` provider that answers the adapter
in the protocol kumo-strategies defined (7bf2a2c): `LOCATABLE` (identity) for "available, fee
unknown", `None` for "no locate" — and NEVER a number, because 0.0 would typecheck, pass the fee
ceiling and assert a free borrow nobody measured.

THREE STATES ON THE HEALTH FRAME, never two: subscribed-and-answered, answered-but-stale, and
never answered. "0 of 0" must not read as "0 refused".

Measured on staging2 2026-09-10 12:50Z (own client id, read-only): 118 of the backtest's 130 names
code 3.0 with shares, 4 locate-required, 8 with no security definition today.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from functools import partial

from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import InstrumentId

_log = logging.getLogger("kumo.providers.ibkr_shortable")

TICK_SHORTABLE = 46          # ibapi TickTypeEnum.SHORTABLE
TICK_SHORTABLE_SHARES = 89   # ibapi TickTypeEnum.SHORTABLE_SHARES
GENERIC_TICK_LIST = "236"
#: IB's own thresholds for tick 46.
EASY_TO_BORROW = 2.5
#: A read older than this is not today's locate. IB refreshes the shortable list every ~15 min.
DEFAULT_MAX_AGE_NS = 15 * 60 * 1_000_000_000


class Shortable(Data):
    """One instrument's shortability as IB last reported it. `shares` is None until tick 89 arrives
    — absent is not zero."""

    def __init__(self, instrument_id: InstrumentId, code: float | None, shares: int | None,
                 ts_event: int, ts_init: int) -> None:
        self.instrument_id = instrument_id
        self.code = code
        self.shares = shares
        self._ts_event = int(ts_event)
        self._ts_init = int(ts_init)

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    @property
    def easy_to_borrow(self) -> bool:
        return (self.code is not None and self.code >= EASY_TO_BORROW
                and self.shares is not None and self.shares > 0)

    def __repr__(self) -> str:
        return (f"Shortable({self.instrument_id}, code={self.code}, shares={self.shares}, "
                f"ts_event={self._ts_event})")

    def __eq__(self, other) -> bool:
        return (isinstance(other, Shortable) and other.instrument_id == self.instrument_id
                and other.code == self.code and other.shares == self.shares
                and other._ts_event == self._ts_event)

    def __hash__(self) -> int:
        return hash((str(self.instrument_id), self.code, self.shares, self._ts_event))


SHORTABLE_TYPE = DataType(Shortable)


@dataclass(frozen=True)
class _Record:
    code: float | None
    shares: int | None
    ts_event: int


class ShortableRegistry:
    """What every subscribed instrument last said, and who was asked. `publish` is
    `Actor.publish_data`-shaped: `(DataType, Data) -> None`."""

    def __init__(self, *, publish: Callable[[DataType, Data], None]) -> None:
        self._publish = publish
        #: Whether the installed kumo-strategies carries the LOCATABLE contract the provider speaks.
        #: THREE STATES: "unknown" until `borrow_rates_provider` has looked, then "present" or
        #: "absent". staging2's pin 0fbcfff predates the module; a build-time import here crashed
        #: `build_node` for a lane that could not even exist on that pin (codex merged review of
        #: 87b173f). Absent is reported on /health.shortable and refused by name on call — never a
        #: silent {sym: None}, which is an ANSWER ("no locate") and not what a missing contract has.
        self.contract: str = "unknown"
        self._by_iid: dict[InstrumentId, _Record] = {}
        self._subscribed: set[InstrumentId] = set()

    # -- writers -------------------------------------------------------------------------------
    def subscribed(self, instrument_id: InstrumentId) -> None:
        self._subscribed.add(instrument_id)

    def on_code(self, instrument_id: InstrumentId, code: float, ts_ns: int) -> None:
        prior = self._by_iid.get(instrument_id)
        self._set(instrument_id, _Record(float(code), prior.shares if prior else None, ts_ns))

    def on_shares(self, instrument_id: InstrumentId, shares, ts_ns: int) -> None:
        n = int(shares)
        if n < 0:
            # A negative size is not a share count; ignoring it keeps `shares` None (absent), which
            # the provider reads as "no locate" — never as a borrow (codex, #857 review).
            _log.warning("shortable: negative shares %s for %s ignored", n, instrument_id)
            return
        prior = self._by_iid.get(instrument_id)
        self._set(instrument_id, _Record(prior.code if prior else None, n, ts_ns))

    # LOOP-SIDE ENTRY POINTS. The IB wrapper's callbacks run in a worker thread
    # (`asyncio.to_thread(decoder.interpret, ...)` in the Nautilus client); publishing on the bus from
    # there is a cross-thread call. The wrapper hands these to `client.submit_to_msg_handler_queue`,
    # which awaits them on the client's loop — the node's — exactly as the stock `tickSize` path does.
    async def aon_code(self, instrument_id: InstrumentId, code: float, ts_ns: int) -> None:
        self.on_code(instrument_id, code, ts_ns)

    async def aon_shares(self, instrument_id: InstrumentId, shares, ts_ns: int) -> None:
        self.on_shares(instrument_id, shares, ts_ns)

    def _set(self, instrument_id: InstrumentId, rec: _Record) -> None:
        self._by_iid[instrument_id] = rec
        self._publish(SHORTABLE_TYPE, Shortable(instrument_id, rec.code, rec.shares,
                                                ts_event=rec.ts_event, ts_init=rec.ts_event))

    # -- readers -------------------------------------------------------------------------------
    def snapshot(self) -> dict[InstrumentId, _Record]:
        return dict(self._by_iid)

    def get(self, symbol: str) -> _Record | None:
        for iid, rec in self._by_iid.items():
            if str(iid.symbol) == symbol:
                return rec
        return None

    def health(self, *, now_ns: int, max_age_ns: int = DEFAULT_MAX_AGE_NS) -> dict:
        """Three states. `never` counts instruments asked and not answered; `state` summarises
        so a health reader has one word per condition, none of which is 'ok' for an empty set."""
        subscribed = len(self._subscribed)
        # ANSWERED means tick 46 arrived. A shares-only record (tick 89 first) is not an answer the
        # provider can act on — it still reads `code is None` → no locate (codex, #857 review).
        answered = sum(1 for iid in self._subscribed
                       if iid in self._by_iid and self._by_iid[iid].code is not None)
        stale = sum(1 for iid in self._subscribed
                    if iid in self._by_iid and self._by_iid[iid].code is not None
                    and now_ns - self._by_iid[iid].ts_event > max_age_ns)
        never = subscribed - answered
        if subscribed == 0:
            state = "never_subscribed"
        elif stale:
            state = "stale"
        elif never:
            state = "partial"
        else:
            state = "complete"
        return {"contract": self.contract, "subscribed": subscribed, "answered": answered, "stale": stale, "never": never,
                "state": state}


def _resolve_instrument(client, req_id: int) -> InstrumentId | None:
    """The same lookup the stock wrapper's `process_tick_price` uses: the subscription registry
    keyed by req id, whose name is `(str(instrument_id), "market_data")`."""
    subscriptions = getattr(client, "_subscriptions", None)
    sub = subscriptions.get(req_id=req_id) if subscriptions is not None else None
    if sub is None:
        return None
    name = getattr(sub, "name", None)
    raw = name[0] if isinstance(name, tuple) and name else name
    try:
        return InstrumentId.from_str(str(raw))
    except Exception:  # noqa: BLE001 — a name that is not an instrument id is not ours
        return None


def _wrapper_base():
    from nautilus_trader.adapters.interactive_brokers.client.wrapper import InteractiveBrokersEWrapper

    return InteractiveBrokersEWrapper


class ShortableWrapper(_wrapper_base()):
    """The stock wrapper plus the two ticks it discards. Everything else is inherited unchanged,
    and both overrides call `super()` first so the stock DEBUG line and `tickSize` forwarding
    (bid/ask sizes) keep flowing."""

    def __init__(self, *, nautilus_logger, client, registry: ShortableRegistry,
                 now_ns: Callable[[], int]) -> None:
        super().__init__(nautilus_logger=nautilus_logger, client=client)
        self._registry = registry
        self._now_ns = now_ns

    def tickGeneric(self, reqId, tickType, value) -> None:  # noqa: N802 — ibapi's name
        super().tickGeneric(reqId, tickType, value)
        if int(tickType) != TICK_SHORTABLE:
            return
        iid = _resolve_instrument(self._client, reqId)
        if iid is None:
            return
        # OFF-LOOP HERE (worker thread); the registry mutation and the bus publish happen on the
        # loop, via the same queue the stock wrapper uses for every tick it forwards.
        self._client.submit_to_msg_handler_queue(
            partial(self._registry.aon_code, iid, float(value), self._now_ns()))

    def tickSize(self, reqId, tickType, size) -> None:  # noqa: N802 — ibapi's name
        super().tickSize(reqId, tickType, size)
        if int(tickType) != TICK_SHORTABLE_SHARES:
            return
        iid = _resolve_instrument(self._client, reqId)
        if iid is None:
            return
        self._client.submit_to_msg_handler_queue(
            partial(self._registry.aon_shares, iid, int(Decimal(size)), self._now_ns()))


def install_shortable_wrapper(client, registry: ShortableRegistry, *, now_ns: Callable[[], int],
                              _client_type=None) -> ShortableWrapper:
    """Swap the cached IB client's `EClient.wrapper` for the subclass. REFUSES anything that is not
    an `InteractiveBrokersClient` — a silent no-op here is an inert gate that reads as armed (#26).
    `_client_type` admits a test double; production passes nothing."""
    if _client_type is None:
        from nautilus_trader.adapters.interactive_brokers.client.client import InteractiveBrokersClient

        _client_type = InteractiveBrokersClient
    if not isinstance(client, _client_type):
        raise TypeError(
            f"install_shortable_wrapper needs an InteractiveBrokersClient, got {type(client).__name__}"
            f" — shortable data exists only on IBKR; on any other venue this must not be wired")
    wrapper = ShortableWrapper(nautilus_logger=client._log, client=client, registry=registry,
                               now_ns=now_ns)
    client._eclient.wrapper = wrapper
    # ibapi's `EClient.connect` snapshots `self.wrapper` into `Decoder(self.wrapper, ...)`; a client
    # that has already connected (a reconnect re-creates the decoder from `wrapper`, so that case is
    # covered) dispatches through the decoder's copy — swap that reference too (codex, #857 review).
    decoder = getattr(client._eclient, "decoder", None)
    if decoder is not None and hasattr(decoder, "wrapper"):
        decoder.wrapper = wrapper
    return wrapper


def install_on_cached_clients(registry: ShortableRegistry, *, now_ns: Callable[[], int]) -> int:
    """Install on every IB client the adapter's factory has cached (`factories.IB_CLIENTS`, keyed
    host/port/client_id). Called after `node.build()`, before `node.run()`; the reader thread that
    dispatches to the wrapper does not exist until connect. Returns how many were wrapped — ZERO is
    reported by the caller as its own condition, never as success."""
    from nautilus_trader.adapters.interactive_brokers.factories import IB_CLIENTS

    n = 0
    for client in list(IB_CLIENTS.values()):
        install_shortable_wrapper(client, registry, now_ns=now_ns)
        n += 1
    return n


async def subscribe_shortable(client, registry: ShortableRegistry, instrument) -> None:
    """Ask IB for tick 236 on one cached instrument through the adapter's own subscription
    registry, so cancel and de-duplication work as for any other market-data subscription."""
    from nautilus_trader.adapters.interactive_brokers.common import IBContract

    contract = IBContract(**instrument.info["contract"])
    registry.subscribed(instrument.id)
    await client.subscribe_market_data(instrument.id, contract, generic_tick_list=GENERIC_TICK_LIST)


def borrow_rates_provider(registry: ShortableRegistry, *, now_ns: Callable[[], int],
                          max_age_ns: int = DEFAULT_MAX_AGE_NS) -> Callable[[list[str]], dict]:
    """`symbols -> {symbol: LOCATABLE | None}` in kumo-strategies' protocol. LOCATABLE is identity
    and is not a number; None is "no locate" and covers locate-required, no shares, stale, and
    never answered. Never 0.0."""
    # IMPORT AT CALL TIME, AND SAY SO IF IT FAILS. This ran at module scope of the provider's own
    # build — inside `build_node`, for every IBKR instance, whether or not a short lane exists — and on a
    # pin without the module it was an engine crash-loop at boot. Everything else in this repo imports
    # kumo_strategies inside functions; this was one of two exceptions.
    try:
        from kumo_strategies.strategies.crsi_short import LOCATABLE
    except ImportError as exc:
        registry.contract = "absent"
        missing = f"{type(exc).__name__}: {exc}"

        def refusing(symbols) -> dict:
            raise RuntimeError(
                "shortable provider called but the installed kumo-strategies has no "
                f"kumo_strategies.strategies.crsi_short ({missing}) — no LOCATABLE contract to "
                "answer in. The pin predates CRSISHORT; /health.shortable.contract says 'absent'.")

        return refusing
    registry.contract = "present"

    if isinstance(LOCATABLE, (int, float)):  # pragma: no cover — upstream contract, asserted there too
        raise RuntimeError("LOCATABLE is a number in the installed kumo-strategies — refusing to "
                           "publish availability as a fee")

    def provider(symbols) -> dict:
        now = now_ns()
        out: dict = {}
        for sym in symbols:
            rec = registry.get(str(sym))
            if rec is None or rec.code is None:
                out[sym] = None
                continue
            fresh = (now - rec.ts_event) <= max_age_ns
            out[sym] = LOCATABLE if (fresh and rec.code >= EASY_TO_BORROW
                                     and rec.shares is not None and rec.shares > 0) else None
        return out

    return provider


class IBShortablePlane:
    """What the engine holds: vendor-neutral verbs over the IB registry and client."""

    def __init__(self, registry: ShortableRegistry, client, *, now_ns: Callable[[], int]) -> None:
        self.registry = registry
        self.client = client
        self._now_ns = now_ns

    def health(self, *, now_ns: int | None = None) -> dict:
        return self.registry.health(now_ns=self._now_ns() if now_ns is None else now_ns)

    def borrow_rates(self, *, now_ns: Callable[[], int] | None = None):
        return borrow_rates_provider(self.registry, now_ns=now_ns or self._now_ns)

    async def subscribe(self, instrument) -> None:
        await subscribe_shortable(self.client, self.registry, instrument)


def build_plane(publish: Callable[[DataType, Data], None], now_ns: Callable[[], int], *,
                host: str, port: int, client_id: int) -> IBShortablePlane | None:
    """Install the wrapper on every cached IB client and bind the DATA client by its factory key
    `(host, port, client_id)`. Returns None — and SAYS SO at ERROR — when no client is cached under
    that key or nothing was wrapped; the caller then attaches nothing and the CRSISHORT builder
    refuses for want of a provider, which is the loud outcome."""
    from nautilus_trader.adapters.interactive_brokers.factories import IB_CLIENTS

    registry = ShortableRegistry(publish=publish)
    wrapped = install_on_cached_clients(registry, now_ns=now_ns)
    key = (host, port, client_id)
    client = IB_CLIENTS.get(key)
    if client is None or wrapped == 0:
        _log.error("shortable plane NOT built: %d clients wrapped, data client key %r not in %r",
                   wrapped, key, list(IB_CLIENTS))
        return None
    _log.info("shortable plane built: %d IB client(s) wrapped, subscribing via %r", wrapped, key)
    return IBShortablePlane(registry, client, now_ns=now_ns)
