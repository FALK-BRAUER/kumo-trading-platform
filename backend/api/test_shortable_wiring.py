"""#857 — the shortable plane is WIRED, not just written: the feed subscribes tick 236 for every
instrument a SHORT lane resolved, the health frame carries three states, and the value survives
every hop to `/health` (the pydantic-drop class: #233/#322/#336 — a DTO silently eats a published
field, three times).

Seam, not unit: the catch-up is driven through `_on_refetch`, the real 30 s timer callback, on a
`UiFeedStrategy.__new__` double carrying only what that path reads.
"""
from __future__ import annotations

import asyncio
import time

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

from api.engine_node import UiFeedStrategy
from api.observation import Observations

IONQ = InstrumentId.from_str("IONQ.XNAS")
RGTI = InstrumentId.from_str("RGTI.XNAS")


class _Clock:
    def timestamp_ns(self):
        return 100 * 1_000_000_000


class _Instrument:
    def __init__(self, iid):
        self.id = iid
        self.info = {"contract": {"symbol": str(iid.symbol), "secType": "STK", "exchange": "SMART",
                                  "currency": "USD"}}


class _Cache:
    def __init__(self, *iids):
        self._by = {i: _Instrument(i) for i in iids}

    def instrument(self, iid):
        return self._by.get(iid)


class _ShortLane:
    POSITION_SIDE = "short"          # kumo-strategies sides.SHORT is the string "short"
    id = "CRSISHORT-006"

    def __init__(self, *iids):
        self._iids = list(iids)


class _LongLane:
    POSITION_SIDE = "long"
    id = "BCTROT-004"
    _iids = [IONQ]


class _Client:
    """`subscribe_market_data` is the adapter's public request hook; `_loop` is what the feed
    schedules on when its own loop is not captured."""

    def __init__(self):
        self.calls: list = []
        self._loop = None

    async def subscribe_market_data(self, instrument_id, contract, generic_tick_list=""):
        self.calls.append((instrument_id, contract.symbol, generic_tick_list))


class _FeedDouble:
    """A plain object carrying the REAL feed methods this path runs (bound from the class — an
    `Actor.__new__` double cannot take a clock; `clock` is a read-only Cython attribute)."""

    _on_refetch = UiFeedStrategy._on_refetch
    attach_shortable = UiFeedStrategy.attach_shortable
    _short_lanes = UiFeedStrategy._short_lanes
    _shortable_catchup = UiFeedStrategy._shortable_catchup
    _schedule_shortable = UiFeedStrategy._schedule_shortable


def _feed(*lanes, cache=None, attach=True):
    from api.providers.ibkr_shortable import IBShortablePlane, ShortableRegistry

    f = _FeedDouble()
    f.clock = _Clock()
    f.cache = cache or _Cache(IONQ, RGTI)
    f._observations = Observations()
    f._observations.declare("shortable")
    f._sibling_strategies = {str(lane.id): lane for lane in lanes}
    f._bar_types = []
    f._inflight = {}
    f._shortable_plane = None
    f._shortable_subscribed = set()
    f._shortable_pending = {}
    f._shortable_subscribe_failures = 0
    f.shortable_provider = None
    f._loop = None
    # The scheduler is the ONE seam replaced: production hands the coroutine to the node loop from
    # the clock thread and gets a Future back; here it runs to completion and returns a done
    # future-shaped object, so the client double records the request and the next pass can read
    # the outcome (a failed subscription is retried and counted — codex, #857).
    class _Done:
        def __init__(self, exc=None):
            self._exc = exc

        def done(self):
            return True

        def exception(self):
            return self._exc

    def _run(coro):
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 — the future carries it, as run_coroutine_threadsafe's does
            return _Done(exc)
        return _Done()

    f._schedule_shortable = _run
    client = _Client()
    if attach:
        f.attach_shortable(IBShortablePlane(ShortableRegistry(publish=lambda *a: None), client,
                                            now_ns=f.clock.timestamp_ns))
    return f, client


def test_the_refetch_tick_subscribes_tick_236_for_every_SHORT_lane_instrument_ONCE():
    """Bitten by: dropping the catch-up call from `_on_refetch` (no calls); by subscribing LONG
    lanes too (BCTROT's IONQ would appear); by forgetting the seen-set (two ticks, four calls)."""
    f, client = _feed(_ShortLane(IONQ, RGTI), _LongLane())

    f._on_refetch(None)
    f._on_refetch(None)

    assert sorted(c[0] for c in client.calls) == [IONQ, RGTI]
    assert all(c[2] == "236" for c in client.calls), "generic tick 236 is the whole point"
    assert f._shortable_plane.health(now_ns=0)["subscribed"] == 2
    assert f._observations.summary()["ok"] == 1 and f._observations.summary()["failing"] == 0


def test_an_instrument_not_yet_defined_is_asked_again_next_pass():
    f, client = _feed(_ShortLane(IONQ, RGTI), cache=_Cache(IONQ))
    f._on_refetch(None)
    assert [c[0] for c in client.calls] == [IONQ]
    f.cache = _Cache(IONQ, RGTI)
    f._on_refetch(None)
    assert sorted(c[0] for c in client.calls) == [IONQ, RGTI]


def test_a_SHORT_lane_with_NO_plane_attached_is_a_RECORDED_failure_not_a_quiet_zero():
    """Armed and inert is the #26 shape. The observation registry must show `failing`, and the
    bar heal below it must still run (the timer callback must not die)."""
    f, _ = _feed(_ShortLane(IONQ), attach=False)
    f._on_refetch(None)
    obs = f._observations.summary()
    assert obs["failing"] == 1 and obs["ok"] == 0
    assert "inert" in obs["rows"][0]["last_error"]


def test_a_node_with_only_LONG_lanes_subscribes_nothing_and_is_ok():
    f, client = _feed(_LongLane())
    f._on_refetch(None)
    assert client.calls == []
    assert f._observations.summary()["ok"] == 1


@needs_crsishort
def test_the_provider_the_builder_reads_is_attached_and_answers_None_before_any_tick():
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    f, _ = _feed(_ShortLane(IONQ))
    assert callable(f.shortable_provider)
    assert f.shortable_provider(["IONQ"]) == {"IONQ": None}
    f._shortable_plane.registry.on_code(IONQ, 3.0, f.clock.timestamp_ns())
    f._shortable_plane.registry.on_shares(IONQ, 606_315, f.clock.timestamp_ns())
    assert f.shortable_provider(["IONQ"])["IONQ"] is LOCATABLE


# -- the hops -----------------------------------------------------------------------------------
def test_HOP_1_the_health_frame_carries_shortable_and_None_where_there_is_no_plane():
    from api.providers.ibkr_shortable import ShortableRegistry

    from api.providers.ibkr_shortable import IBShortablePlane

    f = _FeedDouble()
    f.clock = _Clock()
    f._shortable_plane = None
    assert (f._shortable_plane.health() if f._shortable_plane else None) is None
    f._shortable_plane = IBShortablePlane(ShortableRegistry(publish=lambda *a: None), _Client(),
                                          now_ns=f.clock.timestamp_ns)
    f._shortable_plane.registry.subscribed(IONQ)
    h = f._shortable_plane.health()
    assert h == {"contract": "unknown", "subscribed": 1, "answered": 0, "stale": 0, "never": 1, "state": "partial"}


def test_HOP_1b_the_health_publisher_names_the_key():
    """The frame is a hand-typed dict; the key must be there or every downstream hop is moot."""
    import inspect

    src = inspect.getsource(UiFeedStrategy)
    assert '"shortable": ({**self._shortable_plane.health(' in src


def test_HOP_2_the_consumer_passes_shortable_through_and_drops_it_on_a_stale_bridge():
    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    # CONSTRUCTED, not `__new__`-ed: a double that bypasses `__init__` breaks on every attribute the
    # real class gains (#859 added two). Six siblings were fixed the same way.
    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "shortable": {"subscribed": 2, "answered": 1, "stale": 0,
                                                  "never": 1, "state": "partial"}}
    c._health_at = time.monotonic()
    assert c.health()["shortable"]["state"] == "partial"
    c._health_at = time.monotonic() - 10_000
    assert c.health()["shortable"] is None


def test_HOP_3_the_DTO_keeps_the_field():
    from api.models import HealthResponse

    assert "shortable" in HealthResponse.model_fields, "pydantic would eat it (#233/#322/#336)"
    assert HealthResponse.model_fields["shortable"].default is None


def test_the_plane_is_attached_ONLY_when_the_connector_declares_it(monkeypatch):
    """The engine reads `spec.shortable_plane`, nothing else — no provider name, no IB import
    (test_import_boundary). Bitten by: attaching regardless of the spec; by attaching a None."""
    import api.engine_node as en

    f, _ = _feed(attach=False)
    en._attach_shortable_plane(f, type("Spec", (), {"shortable_plane": None})())
    assert f.shortable_provider is None and f._shortable_plane is None

    en._attach_shortable_plane(f, type("Spec", (), {"shortable_plane": staticmethod(lambda p, n: None)})())
    assert f.shortable_provider is None, "a factory that returned None must attach nothing"

    from api.providers.ibkr_shortable import IBShortablePlane, ShortableRegistry

    seen = {}

    def factory(publish, now_ns):
        seen["publish"], seen["now_ns"] = publish, now_ns
        return IBShortablePlane(ShortableRegistry(publish=publish), _Client(), now_ns=now_ns)

    en._attach_shortable_plane(f, type("Spec", (), {"shortable_plane": staticmethod(factory)})())
    assert callable(f.shortable_provider) and f._shortable_plane is not None
    assert callable(seen["publish"]) and callable(seen["now_ns"])


def test_the_IBKR_connector_declares_the_plane_and_the_Alpaca_one_does_not():
    """The declaration lives in the connector (#857). Read off the real spec builders."""
    import inspect

    from api.providers import ibkr
    from api.providers.base import DataClientSpec

    assert "shortable_plane" in DataClientSpec.__dataclass_fields__
    assert DataClientSpec.__dataclass_fields__["shortable_plane"].default is None
    assert "shortable_plane=" in inspect.getsource(ibkr.build_data)
    try:
        from api.providers import alpaca as _alp
    except Exception:  # noqa: BLE001 — no Alpaca connector module on this tree is also "does not"
        return
    assert "shortable_plane" not in inspect.getsource(_alp), "Alpaca has no shortable tick plane"


def test_a_subscription_that_RAISES_is_counted_and_RETRIED_next_pass():
    """Bitten by: dropping the future (the failure vanishes, the name stays 'subscribed' forever)."""
    f, client = _feed(_ShortLane(IONQ))
    calls = {"n": 0}

    async def flaky(instrument_id, contract, generic_tick_list=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("IB 10197: no market data during competing session")
        client.calls.append((instrument_id, contract.symbol, generic_tick_list))

    client.subscribe_market_data = flaky
    f._on_refetch(None)                      # schedules; the future carries the exception
    f._on_refetch(None)                      # settles it: counted, discarded, re-asked
    assert f._shortable_subscribe_failures == 1
    assert calls["n"] == 2 and [c[0] for c in client.calls] == [IONQ]
    f._on_refetch(None)                      # settles the retry: subscribed, nothing pending
    assert IONQ in f._shortable_subscribed and f._shortable_pending == {}
    assert f._shortable_subscribe_failures == 1 and calls["n"] == 2


def test_HOP_1c_health_carries_failures_and_pending_beside_the_registry_states():
    f, _ = _feed(_ShortLane(IONQ))
    f._on_refetch(None)
    f._shortable_subscribe_failures = 2
    h = {**f._shortable_plane.health(), "subscribe_failures": f._shortable_subscribe_failures,
         "pending": len(f._shortable_pending)}
    assert h["subscribe_failures"] == 2 and "pending" in h and h["subscribed"] == 1
