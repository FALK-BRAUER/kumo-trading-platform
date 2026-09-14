"""#857 — IB shortability (generic tick 236 → tick 46 code, tick 89 shares) as a Nautilus custom
Data type, published on the bus, consumed by CRSISHORT's borrow provider.

THE FIXTURE PROPERTY COMES FIRST: the stock Nautilus wrapper DISCARDS tick 46 — `tickGeneric` is a
DEBUG log line and nothing else (installed 1.229, `client/wrapper.py:206`). A test that delivered
tick 46 through any other method would pass against a wrapper that still drops it in production.

Three states, never two: LOCATABLE (code ≥ 2.5 with shares, fresh), None (code below, no shares,
stale), and NEVER ANSWERED — which is None to the lane and its own row on the health frame, because
"0 of 0 subscribed" must not read as "0 refused".

Measured on staging2 2026-09-10 12:50Z: 15 names code 3.0 with shares (SOUN the smallest at 38,594),
QUBT code 2.0 with no shares tick. Those are the fixture values.
"""
from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal

import pytest


from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

# ONLY the tests that read upstream's LOCATABLE skip on a pin without CRSISHORT. The wrapper
# install, attach, health and no-short-lane cases run on EVERY pin — a whole-module skip here is
# what left the build_node → attach → borrow_rates import unchecked on staging2's 0fbcfff pin
# (codex merged review of 87b173f: an engine crash-loop at boot that CI could not see).
needs_crsishort = pytest.mark.skipif(
    not crsishort_installed(),
    reason="reads upstream LOCATABLE — installed kumo-strategies has no CRSISHORT adapter")
from nautilus_trader.model.identifiers import InstrumentId

NS = 1_000_000_000


# -- the fixture property: production discards tick 46 --------------------------------------------
def test_the_STOCK_wrapper_discards_tick_46_so_this_module_has_a_reason_to_exist():
    from nautilus_trader.adapters.interactive_brokers.client.wrapper import InteractiveBrokersEWrapper

    body = inspect.getsource(InteractiveBrokersEWrapper.tickGeneric)
    assert "logAnswer" in body and "_client" not in body, (
        "Nautilus now forwards tickGeneric — re-check whether the override below still adds "
        "anything, and whether it now double-delivers")


# -- doubles built from what the real client exposes ----------------------------------------------
class _Sub:
    def __init__(self, name):
        self.name = name


class _Subscriptions:
    """`client._subscriptions.get(req_id=...)` — the same lookup `process_tick_price` uses."""

    def __init__(self, by_req: dict):
        self._by_req = by_req

    def get(self, *, req_id=None, name=None):
        if req_id is not None:
            return self._by_req.get(req_id)
        return next((s for s in self._by_req.values() if s.name == name), None)


class _Log:
    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Client:
    """What the wrapper reaches for: the subscription registry, the logger, and the stock
    `tickSize` forwarding (`partial(client.process_tick_size, ...)` handed to
    `submit_to_msg_handler_queue`) — modelled so the test can assert it STILL flows after the
    override, which is the "everything else keeps working" half of the contract."""

    def __init__(self, by_req: dict):
        self._subscriptions = _Subscriptions(by_req)
        self._log = _Log()
        self.queued: list = []

    async def process_tick_size(self, *, req_id, tick_type, size):
        return None

    def submit_to_msg_handler_queue(self, task):
        self.queued.append(task)


IONQ = InstrumentId.from_str("IONQ.XNAS")
QUBT = InstrumentId.from_str("QUBT.XNAS")


def drain(client) -> None:
    """Run what the wrapper queued — production awaits these on the client's loop."""
    tasks, client.queued = list(client.queued), []
    for t in tasks:
        asyncio.run(t())


def _wired():
    from api.providers.ibkr_shortable import ShortableRegistry, ShortableWrapper

    published: list = []
    registry = ShortableRegistry(publish=lambda data_type, data: published.append((data_type, data)))
    client = _Client({1001: _Sub((str(IONQ), "market_data")), 1002: _Sub((str(QUBT), "market_data"))})
    wrapper = ShortableWrapper(nautilus_logger=client._log, client=client, registry=registry,
                               now_ns=lambda: 100 * NS)
    return registry, wrapper, published, client


# -- 1. the wrapper forwards what the stock one drops ---------------------------------------------
def test_tick_46_arrives_through_tickGeneric_and_tick_89_through_tickSize():
    """Bitten by: removing the `tickGeneric` override (code never lands) and by resolving the
    instrument from the req id wrongly (lands on the wrong name)."""
    registry, wrapper, published, client = _wired()

    wrapper.tickGeneric(1001, 46, 3.0)
    wrapper.tickSize(1001, 89, Decimal("2064241"))
    wrapper.tickGeneric(1002, 46, 2.0)
    # OFF-LOOP: nothing has touched the registry yet — the wrapper runs in ibapi's worker thread
    # (`asyncio.to_thread(decoder.interpret)`) and must not publish from there (codex, #857).
    assert registry.snapshot() == {}, "the wrapper mutated the registry from the callback thread"
    drain(client)

    snap = registry.snapshot()
    assert snap[IONQ].code == 3.0 and snap[IONQ].shares == 2_064_241
    assert snap[QUBT].code == 2.0 and snap[QUBT].shares is None
    assert snap[IONQ].ts_event == 100 * NS


def test_the_STOCK_tickSize_forwarding_still_runs_beside_ours():
    registry, wrapper, _p, client = _wired()
    wrapper.tickSize(1001, 89, Decimal("5"))
    names = [getattr(t, "func", t).__name__ for t in client.queued]
    assert "process_tick_size" in names, "the override swallowed the stock tickSize forwarding"
    assert "aon_shares" in names


def test_other_generic_ticks_and_unknown_req_ids_are_ignored_not_recorded():
    registry, wrapper, _, client = _wired()
    wrapper.tickGeneric(1001, 49, 1.0)      # halted — not ours
    wrapper.tickGeneric(9999, 46, 3.0)      # a req id nobody subscribed
    wrapper.tickSize(1001, 8, Decimal("5"))  # volume, not shortable shares
    drain(client)
    assert registry.snapshot() == {}


# -- 2. it is a Nautilus Data type and it is PUBLISHED --------------------------------------------
def test_a_Shortable_is_a_Data_the_bus_can_carry_and_it_is_published_on_every_update():
    """`Actor.publish_data(DataType(cls), obj)` type-checks `obj` against `cls` and reads
    `ts_event`/`ts_init` — the same checks a real bus applies. Bitten by: dropping the publish
    call (the lane then reads a registry the UI cannot see)."""
    from nautilus_trader.core.data import Data
    from nautilus_trader.core.correctness import PyCondition
    from nautilus_trader.model.data import DataType

    from api.providers.ibkr_shortable import SHORTABLE_TYPE, Shortable

    registry, wrapper, published, client = _wired()
    wrapper.tickGeneric(1001, 46, 3.0)
    wrapper.tickSize(1001, 89, Decimal("2064241"))
    drain(client)

    assert len(published) == 2, "every tick that changes the record is a publication"
    data_type, obj = published[-1]
    assert data_type == SHORTABLE_TYPE and isinstance(data_type, DataType)
    assert isinstance(obj, Shortable) and isinstance(obj, Data)
    PyCondition.type(obj, data_type.type, "data")
    assert obj.ts_event == obj.ts_init == 100 * NS
    assert obj.instrument_id == IONQ and obj.code == 3.0 and obj.shares == 2_064_241


# -- 3. the borrow provider: LOCATABLE / None / never ---------------------------------------------
@needs_crsishort
def test_the_provider_answers_LOCATABLE_None_and_never_and_NEVER_a_number():
    """LOCATABLE is identity (kumo-strategies 7bf2a2c): `borrow[sym] is LOCATABLE`. A 0.0 would
    typecheck, pass the fee ceiling and assert a free borrow nobody measured — the exact lie the
    sentinel exists to make inexpressible. Bitten by: returning 0.0 for an easy borrow; by treating
    code 2.0 (locate required) as available; by answering a name that never arrived."""
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    from api.providers.ibkr_shortable import borrow_rates_provider

    registry, wrapper, _, client = _wired()
    wrapper.tickGeneric(1001, 46, 3.0)
    wrapper.tickSize(1001, 89, Decimal("2064241"))
    # Locate-required WITH shares reported: the fixture must carry shares, or the shares floor
    # answers None and the threshold is never exercised (the 2.5→1.5 bite survived without this).
    wrapper.tickGeneric(1002, 46, 2.0)
    wrapper.tickSize(1002, 89, Decimal("1000"))
    drain(client)
    provider = borrow_rates_provider(registry, now_ns=lambda: 100 * NS, max_age_ns=900 * NS)

    got = provider(["IONQ", "QUBT", "NEVER"])
    assert got["IONQ"] is LOCATABLE
    assert registry.get("QUBT").shares == 1000, "fixture: QUBT must carry shares for this to bite"
    assert got["QUBT"] is None, "code 2.0 = locate required — not available at size"
    assert got["NEVER"] is None, "never answered is None to the lane"
    assert not any(isinstance(v, (int, float)) for v in got.values()), "a number is a fabricated fee"


@needs_crsishort
def test_a_STALE_answer_is_None_and_shares_of_zero_is_None():
    from api.providers.ibkr_shortable import borrow_rates_provider

    from kumo_strategies.strategies.crsi_short import LOCATABLE

    registry, wrapper, _, client = _wired()
    wrapper.tickGeneric(1001, 46, 3.0)
    wrapper.tickSize(1001, 89, Decimal("2064241"))
    drain(client)
    # Fixture property: FRESH, this name IS locatable — so staleness alone flips the answer.
    fresh = borrow_rates_provider(registry, now_ns=lambda: 100 * NS, max_age_ns=900 * NS)
    assert fresh(["IONQ"])["IONQ"] is LOCATABLE
    stale = borrow_rates_provider(registry, now_ns=lambda: 100 * NS + 901 * NS, max_age_ns=900 * NS)
    assert stale(["IONQ"])["IONQ"] is None, "a 15-minute-old read is not today's locate"
    wrapper.tickSize(1001, 89, Decimal("0"))
    drain(client)
    assert fresh(["IONQ"])["IONQ"] is None, "code 3.0 with ZERO shares is not a borrow"
    wrapper.tickSize(1001, 89, Decimal("-1"))
    drain(client)
    assert registry.get("IONQ").shares == 0, "a negative size is ignored, not stored"
    assert fresh(["IONQ"])["IONQ"] is None, "a negative size is not a borrow (codex, #857)"


def test_health_has_three_states_and_never_reads_as_clean():
    """`0 of 0` is not `0 of 4`. A registry nobody subscribed must say so."""
    from api.providers.ibkr_shortable import ShortableRegistry

    empty = ShortableRegistry(publish=lambda *a: None)
    assert empty.health(now_ns=0) == {"contract": "unknown", "subscribed": 0, "answered": 0, "stale": 0, "never": 0,
                                       "state": "never_subscribed"}
    registry, wrapper, _, client = _wired()
    registry.subscribed(IONQ)
    registry.subscribed(QUBT)
    # Tick 89 FIRST for QUBT: a shares-only record is not an answer (code is None → no locate).
    wrapper.tickSize(1002, 89, Decimal("1000"))
    wrapper.tickGeneric(1001, 46, 3.0)
    drain(client)
    h = registry.health(now_ns=100 * NS, max_age_ns=900 * NS)
    assert h == {"contract": "unknown", "subscribed": 2, "answered": 1, "stale": 0, "never": 1, "state": "partial"}
    h2 = registry.health(now_ns=100 * NS + 2000 * NS, max_age_ns=900 * NS)
    assert h2["stale"] == 1 and h2["state"] == "stale"


# -- 4. installation is refused on anything that is not the IB client -----------------------------
def test_install_swaps_the_wrapper_on_a_real_shaped_client_and_refuses_anything_else():
    from nautilus_trader.adapters.interactive_brokers.client.wrapper import InteractiveBrokersEWrapper

    from api.providers.ibkr_shortable import ShortableRegistry, ShortableWrapper, install_shortable_wrapper

    registry = ShortableRegistry(publish=lambda *a: None)

    class _Decoder:
        """ibapi's `EClient.connect` does `Decoder(self.wrapper, ...)` — a copy of the reference."""

        def __init__(self, wrapper):
            self.wrapper = wrapper

    class _EClient:
        def __init__(self):
            self.wrapper = InteractiveBrokersEWrapper.__new__(InteractiveBrokersEWrapper)
            self.decoder = _Decoder(self.wrapper)

    class _IBClient:
        __module__ = "nautilus_trader.adapters.interactive_brokers.client.client"

        def __init__(self):
            self._eclient = _EClient()
            self._log = _Log()
            self._subscriptions = _Subscriptions({})

    client = _IBClient()
    # `isinstance` against the real client class is the contract; the double is admitted through
    # `_client_type` so this test does not have to construct a live IB client.
    with pytest.raises(TypeError, match="InteractiveBrokersClient"):
        install_shortable_wrapper(object(), registry, now_ns=lambda: 0)
    installed = install_shortable_wrapper(client, registry, now_ns=lambda: 0, _client_type=_IBClient)
    assert isinstance(installed, ShortableWrapper) and client._eclient.wrapper is installed
    assert client._eclient.decoder.wrapper is installed, (
        "a connected client dispatches through the DECODER's copy of the wrapper (codex, #857)")
    assert isinstance(installed, InteractiveBrokersEWrapper), "everything else must keep flowing"


def test_a_Shortable_reaches_a_REAL_Nautilus_bus_subscriber_through_publish_data():
    """The seam, not a list: a registered Actor publishes through `publish_data`, a bus
    subscription on the custom-data topic receives the object. Bitten by: a `publish` that is not
    `Actor.publish_data`-shaped (the DataType check would raise), by dropping the publish."""
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.test_kit.stubs.component import TestComponentStubs

    from api.providers.ibkr_shortable import SHORTABLE_TYPE, Shortable, ShortableRegistry

    msgbus = TestComponentStubs.msgbus()
    actor = Actor()
    actor.register_base(portfolio=TestComponentStubs.portfolio(), msgbus=msgbus,
                        cache=TestComponentStubs.cache(), clock=TestComponentStubs.clock())
    got: list = []
    msgbus.subscribe(topic="data.*", handler=lambda m: got.append(m))
    registry = ShortableRegistry(publish=actor.publish_data)
    registry.on_code(IONQ, 3.0, 100 * NS)
    registry.on_shares(IONQ, 606_315, 100 * NS)
    assert len(got) == 2 and all(isinstance(m, Shortable) for m in got)
    assert got[-1].instrument_id == IONQ and got[-1].shares == 606_315
    assert "Shortable" in SHORTABLE_TYPE.topic
