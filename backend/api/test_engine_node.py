"""Offline tests for the engine node's UI bridge (#20) — config gating + the queue→drain→Redis path.
No Redis and no TradingNode: we stub the redis client and drive the queue/drain directly."""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import threading
import time
import types

import pytest
from nautilus_trader.model.identifiers import ClientId

from api.engine_node import UiFeedStrategy, build_node
from api.feed_config import load_feed_config


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def xadd(self, key, fields, **kwargs):
        self.calls.append((key, fields, kwargs))


def _strategy() -> UiFeedStrategy:
    return UiFeedStrategy(load_feed_config(), ClientId("DATABENTO"), "test-key")


def test_build_node_requires_bridge_enabled():
    cfg = dataclasses.replace(load_feed_config(), ui_bridge={})  # disabled
    with pytest.raises(RuntimeError, match="ui_bridge"):
        build_node(cfg)


def test_publish_enqueues_type_and_json():
    s = _strategy()
    s._publish("bar", {"instrument_id": "AAPL.XNAS", "close": 190.0})
    kind, payload = s._queue.get_nowait()
    assert kind == "bar"
    assert json.loads(payload) == {"instrument_id": "AAPL.XNAS", "close": 190.0}


def test_publish_drops_when_queue_full_never_raises():
    s = _strategy()
    s._queue = queue.Queue(maxsize=1)
    s._publish("bar", {"a": 1})
    s._publish("bar", {"b": 2})  # queue full → dropped, must not raise
    assert s._queue.qsize() == 1


def test_drain_writes_queued_frames_to_redis():
    s = _strategy()
    fake = _FakeRedis()
    s._redis = fake
    s._publish("bar", {"x": 1})
    t = threading.Thread(target=s._drain, daemon=True)
    t.start()
    for _ in range(20):  # wait for the writer thread to drain (<=1s)
        if fake.calls:
            break
        time.sleep(0.05)
    s._stopping.set()
    t.join(timeout=2.0)
    assert fake.calls, "writer thread did not XADD the queued frame"
    key, fields, kwargs = fake.calls[0]
    # A "bar" frame goes to the BAR stream now (#309). Sharing one stream with quotes meant a ~127k-frame
    # backfill was evicted inside 7.5 minutes by ~27k quote entries a minute, so no browser ever saw
    # history and the watchlist sat on "loading…" with every long window showing "—".
    assert key == s._bar_stream_key
    assert key != s._stream_key
    assert fields["type"] == "bar" and json.loads(fields["payload"]) == {"x": 1}
    # The bar stream carries its OWN cap, sized for a full backfill rather than a live rate (#309):
    # bars are written once per symbol-granularity and must survive until a browser connects.
    assert kwargs.get("maxlen") == s._bar_maxlen
    assert s._bar_maxlen > s._maxlen and kwargs.get("approximate") is True


def test_drain_survives_redis_error():
    import redis

    class _BoomRedis:
        def xadd(self, *a, **k):
            raise redis.RedisError("down")

    s = _strategy()
    s._redis = _BoomRedis()
    s._publish("bar", {"x": 1})
    t = threading.Thread(target=s._drain, daemon=True)
    t.start()
    time.sleep(0.3)  # drain must swallow the error and keep looping
    assert t.is_alive()
    s._stopping.set()
    t.join(timeout=2.0)


# --- command layer: order commands (#32 increment 2) — gated, mocked (never submits live) --------
from unittest.mock import Mock


def test_orders_disarmed_by_default_rejects():
    s = _strategy()
    assert s._orders_armed is False  # default OFF — no order without an explicit human arm
    s._submit = Mock()
    status, error = s._handle_order_command(
        "submit_order", {"instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 10}
    )
    assert status == "error" and "disarmed" in error
    s._submit.assert_not_called()


def test_order_armed_submits_via_factory():
    s = _strategy()
    s._orders_armed = True
    s._build_order = Mock(return_value="ORDER")
    s._submit = Mock()
    status, error = s._handle_order_command(
        "submit_order",
        {"instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 10, "client_order_id": "c1"},
    )
    assert (status, error) == ("ok", "")
    s._build_order.assert_called_once()
    s._submit.assert_called_once_with("ORDER")
    assert "c1" in s._seen_orders


def test_order_idempotent_no_double_submit():
    s = _strategy()
    s._orders_armed = True
    s._seen_orders.add("c1")
    s._submit = Mock()
    status, _ = s._handle_order_command(
        "submit_order",
        {"instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 10, "client_order_id": "c1"},
    )
    assert status == "ok"
    s._submit.assert_not_called()  # duplicate delivery deduped


def test_cancel_order_not_found():
    s = _strategy()
    s._orders_armed = True
    s._lookup_order = Mock(return_value=None)
    status, error = s._handle_order_command("cancel_order", {"client_order_id": "x"})
    assert status == "error" and "not found" in error


def test_cancel_order_found():
    s = _strategy()
    s._orders_armed = True
    order = object()
    s._lookup_order = Mock(return_value=order)
    s._cancel = Mock()
    status, _ = s._handle_order_command("cancel_order", {"client_order_id": "c1"})
    assert status == "ok"
    s._cancel.assert_called_once_with(order)


def test_order_submit_requires_client_order_id():
    s = _strategy()
    s._orders_armed = True
    s._submit = Mock()
    status, error = s._handle_order_command(
        "submit_order", {"instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 10}
    )
    assert status == "error" and "client_order_id" in error
    s._submit.assert_not_called()


def test_order_build_fails_closed_on_bad_side():
    s = _strategy()
    s._orders_armed = True
    s._submit = Mock()
    # armed + malformed side → error, and _submit never called (no unintended order)
    status, error = s._handle_order_command(
        "submit_order",
        {"instrument_id": "AAPL.XNAS", "side": "SIDEWAYS", "quantity": 10, "order_type": "market", "client_order_id": "c9"},
    )
    assert status == "error"
    s._submit.assert_not_called()
    assert "c9" not in s._seen_orders  # failed build must not mark as submitted


def test_on_order_filled_publishes_fill_frame():
    s = _strategy()
    ev = Mock()
    ev.instrument_id = "AAPL.XNAS"
    ev.order_side = Mock()
    ev.order_side.name = "BUY"
    ev.order_type = Mock()
    ev.order_type.name = "MARKET"
    ev.last_qty = 10
    ev.last_px = 190.5
    ev.ts_event = 123
    ev.client_order_id = "c1"
    ev.strategy_id = "MANUAL"
    s.on_order_filled(ev)
    kind, payload = s._queue.get_nowait()
    import json as _j
    d = _j.loads(payload)
    assert kind == "fill"
    assert d["instrument_id"] == "AAPL.XNAS" and d["side"] == "BUY" and d["price"] == 190.5 and d["quantity"] == 10.0


# --- extended-hours + on-open/on-close TIF (#33) — real OrderFactory, no live submit -------------
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import StrategyId, TraderId
from nautilus_trader.test_kit.providers import TestInstrumentProvider


class _StubCache:
    """Minimal cache exposing only `.instrument(iid)` — enough for _build_order's precision resolution."""

    def __init__(self, *instruments):
        self._by_id = {i.id: i for i in instruments}

    def instrument(self, iid):
        return self._by_id.get(iid)


class _FactoryStrategy(UiFeedStrategy):
    """Shadows the (read-only Cython) order_factory + cache with real/stub ones so _build_order runs offline.
    The equity carries size_precision 0 / price_precision 2 — the real precisions the RiskEngine enforces."""

    _test_factory = OrderFactory(TraderId("T-1"), StrategyId("S-1"), TestClock())
    _test_cache = _StubCache(TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS"))

    @property
    def order_factory(self) -> OrderFactory:  # type: ignore[override]
        return self._test_factory

    @property
    def cache(self):  # type: ignore[override]
        return self._test_cache


def _factory_strategy() -> _FactoryStrategy:
    return _FactoryStrategy(load_feed_config(), ClientId("DATABENTO"), "test-key")


def _base_payload(**over) -> dict:
    p = {
        "instrument_id": "AAPL.XNAS",
        "side": "BUY",
        "quantity": 10,
        "order_type": "limit",
        "price": "190.00",
        "client_order_id": "c1",
    }
    p.update(over)
    return p


def test_build_order_extended_hours_tags_limit():
    s = _factory_strategy()
    o = s._build_order(_base_payload(extended_hours=True))
    assert o.tags == ["extended_hours"]  # exec adapter reads this → Alpaca extended_hours=True


def test_build_order_no_extended_hours_has_no_tag():
    s = _factory_strategy()
    o = s._build_order(_base_payload())
    assert not o.tags  # default off — never silently sets extended-hours


def test_build_order_carries_extra_tags_onto_the_real_order():
    """#872 tags an entry floor `mode:entry_floor`, and the UI labels the stop from THAT — not from a
    second derivation over the order type, which a bracket leg or a manual stop would also satisfy.

    Driven through the REAL `_build_order` against a real `OrderFactory`. The protection reconciler's
    own double stubs `_build_order` and records the payload, so a test there proves the payload carries
    the tag and says NOTHING about whether the order does — the wiring half, which is where five
    production breaks lived on 2026-08-14.
    """
    s = _factory_strategy()
    o = s._build_order(_base_payload(order_type="stop_market", price=None, trigger_price="185.50",
                                     side="SELL", reduce_only=True,
                                     extra_tags=["mode:entry_floor"]))
    assert o.tags == ["mode:entry_floor"]
    assert o.is_reduce_only is True
    assert float(o.trigger_price) == 185.50


def test_build_order_refuses_extra_tags_that_are_not_a_list_of_strings():
    """FAIL-CLOSED on the shape. A bare string would be spread into one tag per character, and the
    resulting order would carry 15 meaningless tags instead of the one the UI reads."""
    s = _factory_strategy()
    with pytest.raises(ValueError, match="extra_tags must be a list of strings"):
        s._build_order(_base_payload(extra_tags="mode:entry_floor"))


def test_a_computed_stop_price_between_ticks_is_quantised_DOWN_for_a_SELL():
    """`_canon_price` REJECTS a price that is not a clean tick multiple — right for an operator's typo,
    fatal for `entry - k x ATR`, which lands between ticks routinely. Quantised away from the market:
    nearest-tick rounding could move the trigger half a tick TOWARD the market."""
    s = _factory_strategy()
    instrument = TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS")

    assert float(s._tick_away_from_market(instrument, 96.4895, "SELL")) == 96.48
    assert float(s._tick_away_from_market(instrument, 96.4895, "BUY")) == 96.49
    # A value already ON the tick is unchanged in both directions — no silent drift on every pass.
    assert float(s._tick_away_from_market(instrument, 96.50, "SELL")) == 96.50


def test_build_order_extended_hours_requires_day_limit():
    s = _factory_strategy()
    # Alpaca only allows extended-hours on a DAY limit order → fail-closed before any factory call.
    with pytest.raises(ValueError, match="extended_hours requires a DAY limit"):
        s._build_order(_base_payload(order_type="market", price=None, extended_hours=True))
    with pytest.raises(ValueError, match="extended_hours requires a DAY limit"):
        s._build_order(_base_payload(time_in_force="gtc", extended_hours=True))


def test_build_order_extended_hours_rejects_non_bool():
    s = _factory_strategy()
    # A stray string ("false"/"0") must NOT arm extended-hours — strict bool, fail-closed.
    with pytest.raises(ValueError, match="extended_hours must be a boolean"):
        s._build_order(_base_payload(extended_hours="false"))


def test_build_order_on_open_close_reject_stops():
    s = _factory_strategy()
    # opg/cls are venue-valid only for market/limit — a stop must fail closed, not reach the broker.
    with pytest.raises(ValueError, match="requires a market or limit"):
        s._build_order(_base_payload(order_type="stop", price=None, trigger_price="185.00", time_in_force="opg"))


def test_build_order_maps_on_open_and_on_close_tif():
    s = _factory_strategy()
    o_opg = s._build_order(_base_payload(time_in_force="opg", client_order_id="c2"))
    o_cls = s._build_order(_base_payload(time_in_force="cls", client_order_id="c3"))
    assert o_opg.time_in_force == TimeInForce.AT_THE_OPEN
    assert o_cls.time_in_force == TimeInForce.AT_THE_CLOSE


# --- quantity/price precision: a float qty (16.0) must not be DENIED by the RiskEngine (precision 1 > 0) ---
def test_build_order_quantity_built_at_instrument_precision():
    s = _factory_strategy()
    # Equity size_precision is 0 → the Quantity must be integer-precision, NOT the float's precision 1.
    # Regression for OrderDenied(reason='quantity 16.0 invalid (precision 1 > 0)').
    o = s._build_order(_base_payload(order_type="market", price=None, quantity=16.0))
    assert o.quantity.precision == 0
    assert str(o.quantity) == "16"


def test_build_order_quantity_rounding_rejected_fail_closed():
    s = _factory_strategy()
    # 16.7 shares of a whole-share equity is not a valid multiple — reject, never silently round to 17.
    with pytest.raises(ValueError, match="not a valid multiple"):
        s._build_order(_base_payload(order_type="market", price=None, quantity=16.7))


def test_build_order_limit_price_built_at_instrument_precision():
    s = _factory_strategy()
    # A float price like 190.5 (precision 1) must be widened to the instrument's tick (precision 2).
    o = s._build_order(_base_payload(order_type="limit", price=190.5))
    assert o.price.precision == 2
    assert str(o.price) == "190.50"


def test_build_order_off_tick_price_rejected_fail_closed():
    s = _factory_strategy()
    # 190.123 is finer than the penny tick — reject rather than let the venue bounce it.
    with pytest.raises(ValueError, match="not a valid tick"):
        s._build_order(_base_payload(order_type="limit", price=190.123))


def test_build_order_unknown_instrument_fails_closed():
    s = _factory_strategy()
    with pytest.raises(ValueError, match="not in cache"):
        s._build_order(_base_payload(instrument_id="ZZZZ.XNAS", order_type="market", price=None))


# --- brackets (#34): NATIVE Nautilus bracket via order_factory.bracket + submit_order_list ---------
def _bracket_payload(**over) -> dict:
    p = {
        "instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 100,
        "entry_order_type": "limit", "price": "190.00",
        "stop_trigger": "185.00", "target_price": "200.00", "time_in_force": "day",
        "client_order_id": "entry1", "sl_client_order_id": "sl1", "tp_client_order_id": "tp1",
        "group_id": "entry1",
    }
    p.update(over)
    return p


def test_bracket_disarmed_rejects():
    s = _factory_strategy()
    s._orders_armed = False
    s._submit_list = Mock()
    status, err = s._handle_order_command("submit_bracket", _bracket_payload())
    assert status == "error" and "disarmed" in err
    s._submit_list.assert_not_called()


def test_bracket_requires_stop_trigger():
    s = _factory_strategy()
    s._orders_armed = True
    s._submit_list = Mock()
    p = _bracket_payload()
    del p["stop_trigger"]
    status, err = s._handle_order_command("submit_bracket", p)
    assert status == "error" and "stop_trigger" in err
    s._submit_list.assert_not_called()


def test_bracket_requires_target():
    s = _factory_strategy()
    s._orders_armed = True
    s._submit_list = Mock()
    p = _bracket_payload()
    del p["target_price"]
    status, err = s._handle_order_command("submit_bracket", p)
    assert status == "error" and "target_price" in err
    s._submit_list.assert_not_called()


def test_bracket_submits_native_order_list():
    s = _factory_strategy()
    s._orders_armed = True
    s._submit_list = Mock()
    status, _ = s._handle_order_command("submit_bracket", _bracket_payload())
    assert status == "ok"
    s._submit_list.assert_called_once()
    order_list = s._submit_list.call_args.args[0]
    # native bracket = 3 linked orders (entry + protective STOP_MARKET + take-profit LIMIT)
    assert len(order_list.orders) == 3
    kinds = {(o.side.name, o.order_type.name) for o in order_list.orders}
    assert kinds == {("BUY", "LIMIT"), ("SELL", "STOP_MARKET"), ("SELL", "LIMIT")}
    from nautilus_trader.model.enums import TriggerType
    # NO emulation — the bracket goes to Alpaca as a native order_class=bracket so the legs rest at the broker.
    assert all(o.emulation_trigger == TriggerType.NO_TRIGGER for o in order_list.orders)
    assert all(o.tags == ["bracket:entry1"] for o in order_list.orders)  # blotter grouping
    assert "entry1" in s._seen_orders


def test_bracket_idempotent_no_double_submit():
    s = _factory_strategy()
    s._orders_armed = True
    s._seen_orders.add("entry1")
    s._submit_list = Mock()
    status, _ = s._handle_order_command("submit_bracket", _bracket_payload())
    assert status == "ok"
    s._submit_list.assert_not_called()  # duplicate delivery deduped


def test_durable_configs_off_by_default(monkeypatch):
    # All new persistence gates default OFF (three-overnight-bug rule) — the paper stack is unaffected until
    # KUMO_DURABLE_CACHE is explicitly set.
    from api.engine_node import _durable_configs, durable_cache_enabled

    monkeypatch.delenv("KUMO_DURABLE_CACHE", raising=False)
    assert durable_cache_enabled() is False
    cache, exec_engine = _durable_configs()
    assert cache is None and exec_engine is None


def test_durable_configs_on_builds_redis_persistence(monkeypatch):
    from api.engine_node import _durable_configs

    monkeypatch.setenv("KUMO_DURABLE_CACHE", "true")
    monkeypatch.setenv("KUMO_REDIS_HOST", "redis")
    monkeypatch.setenv("KUMO_REDIS_PORT", "6379")
    cache, exec_engine = _durable_configs()
    assert cache.database.type == "redis"
    assert cache.database.host == "redis"
    assert cache.flush_on_start is False  # restart truth lives in Redis + broker — never wiped on boot
    assert cache.use_trader_prefix is True
    assert exec_engine.load_cache is True
    assert exec_engine.snapshot_positions is True  # persists closed-leg states → the #73 P&L gap-fill source
    assert exec_engine.reconciliation is True


def test_node_config_default_exec_engine_is_not_none():
    # Regression (deploy crash): Nautilus's TradingNodeConfig.exec_engine default is a real LiveExecEngineConfig,
    # so passing exec_engine=None (as the durable-OFF path did) WIPES it and NautilusKernel crashes on
    # _exec_engine. build_node must OMIT cache/exec_engine kwargs when None so the defaults are used.
    from nautilus_trader.config import TradingNodeConfig

    assert TradingNodeConfig(trader_id="X-1").exec_engine is not None


def test_build_node_constructs_without_exec_engine_crash():
    """Regression for the #90 deploy crash: the LIVE build_node path (durable OFF) must construct a TradingNode
    past NautilusKernel without the exec_engine=None wipeout — the crash the suite missed (it uses BacktestEngine/
    synthetic, never build_node). Run in a SUBPROCESS: a TradingNode can't share an interpreter with the suite's
    BacktestEngines (native globals segfault). Offline — dummy keys, build() does not connect."""
    import subprocess
    import sys

    code = (
        "import os; os.environ.pop('KUMO_DURABLE_CACHE', None);"
        "from api.engine_node import build_node;"
        "n = build_node();"
        "assert n.kernel.exec_engine is not None;"
        "n.dispose();"
        "print('BUILD_NODE_OK')"
    )
    env = {**os.environ, "APCA_API_KEY_ID": "dummy", "APCA_API_SECRET_KEY": "dummy", "KUMO_ENGINE": "alpaca"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, f"build_node crashed:\n{result.stderr[-2000:]}"
    assert "BUILD_NODE_OK" in result.stdout


def test_build_node_propagates_settings_feed_override_to_the_strategy(tmp_path):
    """Regression (codex review, SIP upgrade 8dacee8): `build_node` applied an explicit alpaca-settings
    `feed` override to the DATA CLIENT spec but not to the `FeedConfig` handed to `UiFeedStrategy` — so an
    explicit `feed: "iex"` override, layered over feed.toml's `sip` default, would leave the actual
    websocket client on IEX while the strategy's realtime-subscription budget (and Today's Range fetch)
    still computed as if unlimited/SIP. That reopens the whole-stream 405 the budget gate exists to
    prevent. `UiFeedStrategy._cfg.provider_config` must reflect the SAME effective (overridden) feed the
    data client was built from.

    Same subprocess-required constraints as `test_build_node_constructs_without_exec_engine_crash` — a
    TradingNode can't share an interpreter with the suite's BacktestEngines."""
    import json as _json
    import subprocess
    import sys

    (tmp_path / "alpaca.json").write_text(_json.dumps({"feed": "iex"}))
    code = (
        "import os; os.environ.pop('KUMO_DURABLE_CACHE', None);"
        "from api.engine_node import build_node;"
        "n = build_node();"
        "strat = n.trader.strategies()[0];"
        "assert strat._cfg.provider_config.get('feed') == 'iex', strat._cfg.provider_config;"
        # ...AND the realtime budget derived from it (#619, codex implementation review). Asserting
        # only the config field passes while the spec was built from a STALE feed, leaving the budget
        # unlimited on a capped socket — the whole-stream 405 this override exists to prevent.
        "assert strat._realtime_budget() == 7.0, strat._realtime_budget();"
        "n.dispose();"
        "print('OVERRIDE_PROPAGATED_OK')"
    )
    env = {
        **os.environ,
        "APCA_API_KEY_ID": "dummy",
        "APCA_API_SECRET_KEY": "dummy",
        "KUMO_ENGINE": "alpaca",
        "KUMO_SETTINGS_DIR": str(tmp_path),
    }
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, f"build_node crashed:\n{result.stderr[-2000:]}"
    assert "OVERRIDE_PROPAGATED_OK" in result.stdout


def _armed_strat():
    from nautilus_trader.model.identifiers import ClientId

    from api.feed_config import load_feed_config

    # _FactoryStrategy gives a real order_factory + a stub cache (equity precision) so _build_order/_bracket
    # resolve the instrument offline; behaviour is otherwise the production UiFeedStrategy.
    return _FactoryStrategy(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))


def _fake_result(outcome, existing_status=None, hash_mismatch=False):
    from api.command_ledger import ReserveResult

    return ReserveResult(outcome, existing_status=existing_status, hash_mismatch=hash_mismatch)


def test_idempotent_wrapper_skips_duplicate_without_submitting():
    """#78 wiring: the wrapper runs the handler on FIRST sight (RESERVED) and, on a redelivered command whose
    prior attempt is DONE, returns idempotent ok WITHOUT calling the handler again — the double-send guard."""
    import asyncio

    from api.command_ledger import Reserve

    strat = _armed_strat()

    seq = [_fake_result(Reserve.RESERVED), _fake_result(Reserve.DUPLICATE_COMMAND, existing_status="DONE")]

    class _FakeLedger:
        def __init__(self):
            self.n = 0

        async def reserve(self, *a):
            r = seq[self.n]
            self.n += 1
            return r

        async def mark(self, *a):
            pass

    strat._cmd_ledger = _FakeLedger()
    submitted: list = []
    strat._handle_order_command = lambda ctype, payload: (submitted.append(payload["client_order_id"]), ("ok", ""))[1]

    payload = {"client_order_id": "C1", "instrument_id": "AAPL.XNAS", "side": "buy", "quantity": 1}
    asyncio.run(strat._handle_order_command_idempotent("cmd-1", "submit_order", payload, "e1"))
    status, err = asyncio.run(strat._handle_order_command_idempotent("cmd-1", "submit_order", payload, "e1"))

    assert submitted == ["C1"]  # handler ran ONCE; the DONE duplicate did not reach it
    assert status == "ok" and "idempotent" in err


def test_idempotent_wrapper_mirrors_prior_outcome_and_rejects_anomalies():
    """#78 (codex CRITICAL/HIGH): a duplicate must mirror the PRIOR real outcome, never blanket-accept.
    RESERVED-but-never-DONE (crash mid-submit) → error, not a silent lost order; REJECTED → error;
    economic dup + hash-mismatch → error. None of these re-submit."""
    import asyncio

    from api.command_ledger import Reserve

    def run(result):
        strat = _armed_strat()
        strat._cmd_ledger = type("L", (), {"reserve": lambda self, *a: _async(result), "mark": lambda self, *a: _async(None)})()
        submitted = []
        strat._handle_order_command = lambda ctype, payload: (submitted.append(1), ("ok", ""))[1]
        status, err = asyncio.run(
            strat._handle_order_command_idempotent("cmd", "submit_order", {"client_order_id": "C"}, "e")
        )
        return submitted, status, err

    async def _async(v):
        return v

    for result in (
        _fake_result(Reserve.DUPLICATE_COMMAND, existing_status="RESERVED"),  # crash mid-submit
        _fake_result(Reserve.DUPLICATE_COMMAND, existing_status="REJECTED"),
        _fake_result(Reserve.DUPLICATE_COMMAND, existing_status="DONE", hash_mismatch=True),
        _fake_result(Reserve.DUPLICATE_CLIENT_ORDER_ID),
    ):
        submitted, status, _err = run(result)
        assert submitted == [] and status == "error"


def test_idempotent_wrapper_fails_closed_when_ledger_unreachable():
    """#78: if the ledger raises (Postgres down), the wrapper must REJECT — never submit (fail-closed)."""
    import asyncio

    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    strat = UiFeedStrategy(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))

    class _BrokenLedger:
        async def reserve(self, *a):
            raise RuntimeError("postgres down")

    strat._cmd_ledger = _BrokenLedger()
    submitted: list = []
    strat._handle_order_command = lambda ctype, payload: (submitted.append(1), ("ok", ""))[1]

    status, err = asyncio.run(
        strat._handle_order_command_idempotent("cmd-x", "submit_order", {"client_order_id": "C9"}, "e9")
    )
    assert submitted == []  # NEVER submitted
    assert status == "error" and "ledger unavailable" in err


def test_flatten_idempotent_wrapper_skips_duplicate_without_reacting():
    """Deploy-blocking regression (codex): an off-hours `flatten_position` ATTACHES a manager instead of
    submitting — it never touches `_seen_orders`. Without a top-level reserve on `cid` itself, a redelivery of
    that SAME `cid` after the market opens would re-run `decide()` fresh and submit a SECOND real order under
    a different client_order_id than the manager's own, uncaught by any existing dedup. The wrapper must
    reserve `cid` BEFORE `_handle_flatten_command` ever runs, so a redelivery mirrors the first DONE outcome
    and never reaches `decide()`/`_handle_flatten_command` a second time."""
    import asyncio

    from api.command_ledger import Reserve

    strat = _armed_strat()
    seq = [_fake_result(Reserve.RESERVED), _fake_result(Reserve.DUPLICATE_COMMAND, existing_status="DONE")]

    class _FakeLedger:
        def __init__(self):
            self.n = 0

        async def reserve(self, *a):
            r = seq[self.n]
            self.n += 1
            return r

        async def mark(self, *a):
            pass

    strat._cmd_ledger = _FakeLedger()
    calls: list = []

    async def _fake_handle(cid, payload, entry_id):
        calls.append(cid)
        return "ok", ""  # simulates the off-hours QUEUE branch attaching a manager

    strat._handle_flatten_command = _fake_handle

    payload = {"instrument_id": "AAPL.XNAS", "strategy_id": "MANUAL-001", "expected_side": "LONG"}
    status1, _ = asyncio.run(strat._handle_flatten_command_idempotent("cmd-fl-1", payload, "e1"))
    status2, err2 = asyncio.run(strat._handle_flatten_command_idempotent("cmd-fl-1", payload, "e1"))

    assert calls == ["cmd-fl-1"]  # the real decide()/attach path ran ONCE — the redelivery never reached it
    assert status1 == "ok"
    assert status2 == "ok" and "idempotent" in err2


def test_flatten_idempotent_wrapper_fails_closed_when_ledger_unreachable():
    """A hung/unreachable ledger must reject a flatten command, never fall through to decide()/act — the same
    fail-closed contract the order-command wrapper already has."""
    import asyncio

    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    strat = UiFeedStrategy(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))

    class _BrokenLedger:
        async def reserve(self, *a):
            raise RuntimeError("postgres down")

    strat._cmd_ledger = _BrokenLedger()
    calls: list = []
    strat._handle_flatten_command = lambda cid, payload, entry_id: (calls.append(1), ("ok", ""))[1]

    status, err = asyncio.run(
        strat._handle_flatten_command_idempotent("cmd-fl-x", {"instrument_id": "AAPL.XNAS"}, "e9")
    )
    assert calls == []  # NEVER acted on
    assert status == "error" and "ledger unavailable" in err


def test_realtime_subscribe_budget_favors_positions_over_watchlist():
    """Alpaca free-plan symbol cap (405 'symbol limit exceeded' — trades+quotes cap at 30 channel-slots
    combined, i.e. 15 symbols since both are always subscribed together). A HELD POSITION must always get
    live trade/quote data regardless of the budget; a plain watchlist/universe symbol beyond the budget gets
    skipped (bars/history still load — only the live trade/quote planes are gated)."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.engine_node import UiFeedStrategy
    from api.providers.alpaca.data_client import _MAX_REALTIME_SYMBOLS_IEX

    class _PosCache:
        def __init__(self):
            self.open_ids: set[str] = set()

        def positions_open(self):
            return [type("P", (), {"instrument_id": InstrumentId.from_str(iid)})() for iid in self.open_ids]

    class _Strat(UiFeedStrategy):
        @property
        def clock(self):                      # type: ignore[override]
            # PRODUCTION ALWAYS HAS ONE — a Strategy gets its clock when the kernel registers it, and
            # these doubles are built outside a kernel. A double that cannot answer a question
            # production always answers is the bug, not the code that asks it (#618).
            import types as _t
            return _t.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)
        @property
        def cache(self):  # type: ignore[override]
            return self._pos_cache

    strat = _Strat(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))
    strat._pos_cache = _PosCache()
    # THE CAPPED PLAN, DRIVEN THE WAY PRODUCTION DRIVES IT (#619). This used to set `_alpaca_feed`;
    # the budget is now declared by the data provider on `DataClientSpec`, so that field no longer
    # reaches the gate and setting it here would leave this test measuring nothing.
    strat._realtime_symbol_budget = float(_MAX_REALTIME_SYMBOLS_IEX)
    strat._granularities = []  # skip bar subscription — irrelevant to the budget under test

    calls: list = []
    strat.subscribe_trade_ticks = lambda iid, **k: calls.append(str(iid))
    strat.subscribe_quote_ticks = lambda iid, **k: calls.append(str(iid))

    for i in range(_MAX_REALTIME_SYMBOLS_IEX):
        strat._after_definition(InstrumentId.from_str(f"SYM{i}.XNAS"))
    assert len(calls) == _MAX_REALTIME_SYMBOLS_IEX * 2  # every symbol under budget gets both planes

    before = len(calls)
    strat._after_definition(InstrumentId.from_str("OVERFLOW.XNAS"))
    assert len(calls) == before  # budget full — watchlist symbol skipped, no new subscribe calls

    strat._pos_cache.open_ids.add("HELD.XNAS")
    strat._after_definition(InstrumentId.from_str("HELD.XNAS"))
    assert len(calls) == before + 2  # a held position bypasses the exhausted budget


def test_realtime_budget_gates_native_bar_channels_too():
    """1m/1d are Alpaca's NATIVE bar channels — they share the SAME free-plan cap as trades/quotes (code
    review, empirically confirmed: a trades/quotes-only budget still 405'd live once deployed). Historical
    bars always load (a static chart still renders); only the LIVE `subscribe_bars` follow-on is gated for a
    symbol over budget. Internal-aggregation granularities (5m/15m/30m/1h/1w) never reach the Alpaca client
    at all, so they're intentionally left ungated."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.engine_node import UiFeedStrategy
    from api.providers.alpaca.data_client import _MAX_REALTIME_SYMBOLS_IEX

    class _PosCache:
        def positions_open(self):
            return []

    class _Strat(UiFeedStrategy):
        @property
        def clock(self):                      # type: ignore[override]
            # PRODUCTION ALWAYS HAS ONE — a Strategy gets its clock when the kernel registers it, and
            # these doubles are built outside a kernel. A double that cannot answer a question
            # production always answers is the bug, not the code that asks it (#618).
            import types as _t
            return _t.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)
        @property
        def cache(self):  # type: ignore[override]
            return self._pos_cache

    strat = _Strat(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))
    strat._pos_cache = _PosCache()
    strat._realtime_symbol_budget = float(_MAX_REALTIME_SYMBOLS_IEX)  # capped plan under test (#619)
    strat._granularities = ["1m"]
    strat.subscribe_trade_ticks = lambda *a, **k: None
    strat.subscribe_quote_ticks = lambda *a, **k: None

    bar_subscribed: list = []
    strat.subscribe_bars = lambda b: bar_subscribed.append(b)
    requested: list = []

    def _fake_request_bars(bt, *, start, end, client_id, callback):
        requested.append(bt)
        callback(None)  # simulate the historical request completing synchronously

    strat.request_bars = _fake_request_bars

    for i in range(_MAX_REALTIME_SYMBOLS_IEX):
        strat._after_definition(InstrumentId.from_str(f"SYM{i}.XNAS"))
    assert len(requested) == _MAX_REALTIME_SYMBOLS_IEX  # historical always requested, under budget
    assert len(bar_subscribed) == _MAX_REALTIME_SYMBOLS_IEX  # and live-subscribed, still under budget

    strat._after_definition(InstrumentId.from_str("OVERFLOW.XNAS"))
    assert len(requested) == _MAX_REALTIME_SYMBOLS_IEX + 1  # historical STILL requested — chart still renders
    assert len(bar_subscribed) == _MAX_REALTIME_SYMBOLS_IEX  # but NOT live-subscribed — budget enforced


def test_realtime_budget_is_lifted_on_the_paid_sip_feed():
    """Algo Trader Plus (`sip`) documents UNLIMITED WS symbol subscriptions, so the free-plan slot cap must
    not apply — a universe scanner needs far more than 7 live symbols. The gate itself stays wired so a
    downgrade back to `iex` silently re-caps instead of 405-ing the whole stream."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.engine_node import UiFeedStrategy
    from api.providers.alpaca.data_client import _MAX_REALTIME_SYMBOLS_IEX, realtime_symbol_budget

    assert realtime_symbol_budget("sip") == float("inf")
    assert realtime_symbol_budget("iex") == _MAX_REALTIME_SYMBOLS_IEX
    # An unrecognised feed must budget as CAPPED, not unlimited: over-subscribing kills the whole stream.
    assert realtime_symbol_budget("") == _MAX_REALTIME_SYMBOLS_IEX

    class _PosCache:
        def positions_open(self):
            return []

    class _Strat(UiFeedStrategy):
        @property
        def clock(self):                      # type: ignore[override]
            # PRODUCTION ALWAYS HAS ONE — a Strategy gets its clock when the kernel registers it, and
            # these doubles are built outside a kernel. A double that cannot answer a question
            # production always answers is the bug, not the code that asks it (#618).
            import types as _t
            return _t.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)
        @property
        def cache(self):  # type: ignore[override]
            return self._pos_cache

    strat = _Strat(load_feed_config(), ClientId("ALPACA"), "", exec_client_id=ClientId("ALPACA"))
    strat._pos_cache = _PosCache()
    strat._realtime_symbol_budget = realtime_symbol_budget("sip")
    strat._granularities = []

    calls: list = []
    strat.subscribe_trade_ticks = lambda iid, **k: calls.append(str(iid))
    strat.subscribe_quote_ticks = lambda iid, **k: calls.append(str(iid))

    over_the_old_cap = _MAX_REALTIME_SYMBOLS_IEX * 5
    for i in range(over_the_old_cap):
        strat._after_definition(InstrumentId.from_str(f"SYM{i}.XNAS"))
    assert len(calls) == over_the_old_cap * 2  # every symbol keeps both live planes — no cap on SIP


def test_build_order_rejects_action_id_order_type_mismatch():
    """#50 (codex CRITICAL): validation keys off order_type, so a divergent action_id must be REJECTED — it must
    not build a type the validation didn't check."""
    import pytest

    strat = _armed_strat()
    with pytest.raises(ValueError, match="must match order_type"):
        strat._build_order(
            {
                "instrument_id": "AAPL.XNAS", "side": "buy", "quantity": 1, "order_type": "limit",
                "price": "1.00", "action_id": "stop_market", "client_order_id": "C",
            }
        )


def test_build_order_rejects_unknown_order_type():
    import pytest

    strat = _armed_strat()
    with pytest.raises(ValueError):
        strat._build_order(
            {"instrument_id": "AAPL.XNAS", "side": "buy", "quantity": 1, "order_type": "bogus", "client_order_id": "C"}
        )



def test_order_frame_surfaces_deny_reason():
    # The deny/reject reason (Nautilus carries it on the event, not the order) must reach the blotter frame
    # so the order detail can show WHY an order died — regression for the #115 precision denial being invisible.
    s = _armed_strat()
    o = s._build_order(_base_payload(order_type="market", price=None, client_order_id="cR"))
    assert s._order_frame(o)["reason"] is None  # no denial yet
    s._deny_reasons["cR"] = "quantity 16.0 invalid (precision 1 > 0)"
    assert s._order_frame(o)["reason"] == "quantity 16.0 invalid (precision 1 > 0)"


# ----------------------------------------------------------------------------------------------------
# Bar-series heal detection (#peng-stale-bars) — _series_stale_bars: empty / thin / holed → re-fetch.
# ----------------------------------------------------------------------------------------------------
def test_series_stale_bars():
    from types import SimpleNamespace

    from api.engine_node import _MIN_HEALTHY_BARS, _series_stale_bars

    DAY = 24 * 3600 * 1_000_000_000
    def series(ts_list):
        return [SimpleNamespace(ts_event=t) for t in ts_list]

    base = 1_700_000_000 * 1_000_000_000
    # empty → stale
    assert _series_stale_bars([], "1d") is True
    # thin (< min) → stale
    assert _series_stale_bars(series([base + i * DAY for i in range(5)]), "1d") is True
    # healthy consecutive daily series → NOT stale
    healthy = series([base + i * DAY for i in range(_MIN_HEALTHY_BARS + 10)])
    assert _series_stale_bars(healthy, "1d") is False
    # HOLED — enough bars but the two newest are months apart (old cluster + a lone carried live bar) → stale
    holed = series([base + i * DAY for i in range(_MIN_HEALTHY_BARS + 5)] + [base + 300 * DAY])
    assert _series_stale_bars(holed, "1d") is True
    # normal weekend gap (Fri→Mon = 3 days) on a healthy daily series → NOT stale
    weekend = series([base + i * DAY for i in range(_MIN_HEALTHY_BARS + 5)] + [base + (_MIN_HEALTHY_BARS + 7) * DAY])
    assert _series_stale_bars(weekend, "1d") is False


# ----------------------------------------------------------------------------------------------------
# Session VWAP (#182 follow-up, watchlist KPI Phase 2) — _update_vwap_from_bar: ET-session-anchored
# reset, RTH-only, backfill-safe, zero-volume-bar guarded, idempotent per ts_event (codex review round 2:
# a corrected/revised bar or refetch-heal replaying an already-seen minute must REPLACE its contribution,
# not double-count it), tombstones the prior session's value at the session boundary (codex review round
# 2: a stale value must not linger in the consumer/WS layer past its own session).
# ----------------------------------------------------------------------------------------------------
from datetime import datetime as _datetime
from zoneinfo import ZoneInfo as _ZoneInfo

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId as _InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from api.bar_spec import bar_type as _bar_type
from api.engine_node import _et_session_ts

_VWAP_IID = _InstrumentId.from_str("VWT.XNAS")


def _et_ns(y, mo, d, h, mi, s=0) -> int:
    return int(_datetime(y, mo, d, h, mi, s, tzinfo=_ZoneInfo("America/New_York")).timestamp() * 1e9)


def _minute_bar(ts_ns: int, close: float, volume: float, iid=_VWAP_IID) -> Bar:
    bt = _bar_type(iid, "1m")
    px = Price(close, precision=2)
    return Bar(bt, px, px, px, px, Quantity(volume, precision=0), ts_ns, ts_ns)


def _vwap_frames(s) -> list[dict]:
    """Every `vwap`-kind frame enqueued so far, in publish order (drains the queue)."""
    frames = []
    while not s._queue.empty():
        kind, payload = s._queue.get_nowait()
        if kind == "vwap":
            frames.append(json.loads(payload))
    return frames


def _last_vwap_frame(s) -> dict | None:
    frames = _vwap_frames(s)
    return frames[-1] if frames else None


def test_et_session_ts_rejects_weekend_and_extended_hours():
    assert _et_session_ts(_et_ns(2026, 8, 1, 10, 0)) is None  # Saturday
    assert _et_session_ts(_et_ns(2026, 7, 27, 9, 0)) is None  # weekday, before 9:30 ET open
    assert _et_session_ts(_et_ns(2026, 7, 27, 16, 30)) is None  # weekday, after 16:00 ET close
    assert _et_session_ts(_et_ns(2026, 7, 27, 10, 0)) is not None  # weekday, in RTH


def test_vwap_ignores_extended_hours_bars():
    s = _strategy()
    bar = _minute_bar(_et_ns(2026, 7, 27, 8, 0), close=100.0, volume=1000)  # pre-market
    s._update_vwap_from_bar(bar, historical=False)
    assert str(_VWAP_IID) not in s._vwap_bars
    assert _last_vwap_frame(s) is None


def test_vwap_ignores_non_1m_granularity():
    s = _strategy()
    bt = _bar_type(_VWAP_IID, "1d")
    px = Price(100.0, precision=2)
    ts = _et_ns(2026, 7, 27, 10, 0)
    bar = Bar(bt, px, px, px, px, Quantity(1000, precision=0), ts, ts)
    s._update_vwap_from_bar(bar, historical=False)
    assert _last_vwap_frame(s) is None


def test_vwap_zero_volume_first_bar_tombstones_but_does_not_publish_a_real_value():
    s = _strategy()
    bar = _minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=0)
    s._update_vwap_from_bar(bar, historical=False)
    frames = _vwap_frames(s)
    assert len(frames) == 1  # the session-start tombstone, nothing else
    assert frames[0]["vwap"] is None
    assert sum(v for _, v in s._vwap_bars[str(_VWAP_IID)].values()) == 0


def test_vwap_first_real_volume_bar_publishes():
    s = _strategy()
    bar = _minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000)
    s._update_vwap_from_bar(bar, historical=False)
    frames = _vwap_frames(s)
    assert [f["vwap"] for f in frames] == [None, pytest.approx(100.0)]  # tombstone, then the real value


def test_vwap_accumulates_within_session():
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 31), close=110.0, volume=1000), historical=False)
    frame = _last_vwap_frame(s)
    assert frame["vwap"] == pytest.approx(105.0)  # equal-volume average of 100 and 110


def test_vwap_same_ts_event_replaces_not_adds():
    # A corrected/"revision" live bar for a minute already seen (Alpaca's `u` update), or a refetch-heal
    # replaying an already-seen minute, must REPLACE that minute's contribution, not double-count it.
    s = _strategy()
    ts = _et_ns(2026, 7, 27, 9, 30)
    s._update_vwap_from_bar(_minute_bar(ts, close=100.0, volume=1000), historical=False)
    s._update_vwap_from_bar(_minute_bar(ts, close=100.0, volume=1000), historical=False)  # exact repeat
    assert _last_vwap_frame(s)["vwap"] == pytest.approx(100.0)  # NOT 100 again blended in — still just 100
    s._update_vwap_from_bar(_minute_bar(ts, close=200.0, volume=500), historical=False)  # revised bar, same ts
    frame = _last_vwap_frame(s)
    assert frame["vwap"] == pytest.approx(200.0)  # the ONE contribution for this minute is now the revised bar
    assert sum(v for _, v in s._vwap_bars[str(_VWAP_IID)].values()) == 500  # not 1000+1000+500


def test_vwap_resets_on_next_session():
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 31), close=200.0, volume=1000), historical=False)
    # next day, first bar of the new session — must NOT carry yesterday's accumulated price/volume
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 28, 9, 30), close=50.0, volume=500), historical=False)
    frame = _last_vwap_frame(s)
    assert frame["vwap"] == pytest.approx(50.0)  # today's single bar only, not blended with yesterday's 150 avg
    assert sum(v for _, v in s._vwap_bars[str(_VWAP_IID)].values()) == 500  # session tracker reset, not carried over


def test_vwap_stale_prior_session_value_tombstoned_at_session_start():
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    _vwap_frames(s)  # drain yesterday's published frames — only today's matters below
    # next session's first bar arrives with zero volume (e.g. a halted open) — must tombstone yesterday's
    # value (so a consumer/UI never keeps showing 100.0 as if it were today's), but must NOT publish a
    # real value yet since today has no volume.
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 28, 9, 30), close=90.0, volume=0), historical=False)
    frames = _vwap_frames(s)
    assert len(frames) == 1
    assert frames[0]["vwap"] is None
    assert frames[0]["session_date"] == "2026-07-28"


def test_vwap_restart_backfill_rebuilds_from_historical_bars():
    # A restart mid-session must rebuild VWAP purely from replayed historical 1m bars — on_historical_data
    # and on_bar route through the SAME helper, so this is just calling it with historical=True.
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=True)
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 31), close=110.0, volume=1000), historical=True)
    # then the live bar arrives (the moment the restart caught up)
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 32), close=120.0, volume=1000), historical=False)
    frame = _last_vwap_frame(s)
    assert frame["vwap"] == pytest.approx(110.0)  # (100+110+120)/3 equal-volume average, session rebuilt correctly


def test_vwap_clock_driven_rollover_tombstones_a_symbol_with_no_bar_all_day():
    # A symbol that gets NO 1m bar at all in the new session (halted all day, or just illiquid) never
    # calls _update_vwap_from_bar for that session, so its bar-driven tombstone can't fire. The snapshot
    # timer's clock-driven check must catch it independently (codex review, Phase 2 round 2).
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    _vwap_frames(s)  # drain yesterday's frames
    # next session, no bar ever arrives — the snapshot timer still ticks (every ~2s in production)
    s._check_vwap_session_rollover(_et_ns(2026, 7, 28, 10, 0))
    frames = _vwap_frames(s)
    assert len(frames) == 1
    assert frames[0]["vwap"] is None
    assert frames[0]["session_date"] == "2026-07-28"
    assert s._vwap_session_date[str(_VWAP_IID)] == "2026-07-28"


def test_vwap_clock_driven_rollover_is_a_noop_outside_a_session():
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    _vwap_frames(s)  # drain
    s._check_vwap_session_rollover(_et_ns(2026, 7, 27, 20, 0))  # same evening, after close — no session to roll into
    assert _vwap_frames(s) == []
    assert s._vwap_session_date[str(_VWAP_IID)] == "2026-07-27"  # unchanged


def test_vwap_clock_driven_rollover_does_not_refire_once_a_bar_already_rolled_it():
    s = _strategy()
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 27, 9, 30), close=100.0, volume=1000), historical=False)
    s._update_vwap_from_bar(_minute_bar(_et_ns(2026, 7, 28, 9, 30), close=50.0, volume=500), historical=False)
    _vwap_frames(s)  # drain — the bar-driven path already tombstoned + republished for the new session
    s._check_vwap_session_rollover(_et_ns(2026, 7, 28, 10, 0))  # same session, later — must be a no-op
    assert _vwap_frames(s) == []


# ----------------------------------------------------------------------------------------------------
# Today's Range + Prior Close (#182 follow-up, watchlist KPI Phase 3) — _refresh_today_ranges: one
# batch Alpaca snapshot call for every loaded symbol, missing/failed data isolated per symbol (and per
# whole-batch failure), stale-dailyBar detection + tombstone (codex review round 2), multi-venue-correct
# ticker↔instrument_id mapping via InstrumentId.from_str (codex review round 2 — NOT a config-wide
# default venue, which would mislabel any symbol not on that one venue).
# ----------------------------------------------------------------------------------------------------
class _FakeAlpacaHttp:
    """Stand-in for AlpacaHttpClient.get_stock_snapshots — controlled per test via `snapshots`/`error`."""

    def __init__(self, snapshots: dict | None = None, error: Exception | None = None) -> None:
        self.snapshots = snapshots or {}
        self.error = error
        self.calls: list[tuple[list[str], str]] = []

    async def get_stock_snapshots(self, symbols: list[str], feed: str = "iex") -> dict:
        self.calls.append((list(symbols), feed))
        if self.error is not None:
            raise self.error
        return self.snapshots


def _today_range_frames(s) -> list[dict]:
    frames = []
    while not s._queue.empty():
        kind, payload = s._queue.get_nowait()
        if kind == "today_range":
            frames.append(json.loads(payload))
    return frames


_TODAY_NOW = _et_ns(2026, 7, 27, 10, 0)  # a Monday, mid-RTH — matches _TODAY_ISO's date
_TODAY_ISO = "2026-07-27T20:00:00Z"  # 16:00 ET — same ET calendar date as _TODAY_NOW
_YESTERDAY_ISO = "2026-07-24T20:00:00Z"  # the preceding Friday


def _snap(daily_t: str = _TODAY_ISO, prev_t: str = _YESTERDAY_ISO) -> dict:
    return {
        "dailyBar": {"t": daily_t, "o": 170.0, "h": 172.34, "l": 169.80, "c": 171.5, "v": 1000},
        "prevDailyBar": {"t": prev_t, "o": 168.5, "h": 170.6, "l": 167.9, "c": 169.75, "v": 900},
    }


def test_today_range_no_http_client_is_a_noop():
    s = _strategy()
    s._http = None
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    assert _today_range_frames(s) == []


def test_today_range_no_loaded_symbols_is_a_noop():
    s = _strategy()
    s._http = _FakeAlpacaHttp()
    s._loaded = set()
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    assert _today_range_frames(s) == []
    assert s._http.calls == []  # never even asked — nothing loaded to ask about


def test_today_range_publishes_high_low_prev_close():
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap()})
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    frames = _today_range_frames(s)
    assert len(frames) == 1
    assert frames[0] == {
        "instrument_id": "AAPL.XNAS", "high": 172.34, "low": 169.80, "prev_close": 169.75, "ts_event": _TODAY_NOW,
    }


def test_today_range_requests_bare_tickers_not_full_instrument_ids():
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={})
    s._loaded = {"AAPL.XNAS", "MSFT.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    symbols, feed = s._http.calls[0]
    assert set(symbols) == {"AAPL", "MSFT"}  # no ".XNAS" suffix — Alpaca wants bare tickers
    assert feed == s._alpaca_feed


def test_today_range_multi_venue_symbols_publish_under_their_OWN_venue():
    # Alpaca is multi-venue (NASDAQ→XNAS, NYSE→XNYS) — a config-wide default venue would mislabel IBM
    # here as XNAS instead of its actual XNYS. Also covers a dotted ticker (BRK.B) round-tripping intact.
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap(), "IBM": _snap(), "BRK.B": _snap()})
    s._loaded = {"AAPL.XNAS", "IBM.XNYS", "BRK.B.XNYS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    published_ids = {f["instrument_id"] for f in _today_range_frames(s)}
    assert published_ids == {"AAPL.XNAS", "IBM.XNYS", "BRK.B.XNYS"}
    requested_tickers = set(s._http.calls[0][0])
    assert requested_tickers == {"AAPL", "IBM", "BRK.B"}  # NOT "BRK" — dotted ticker preserved whole


def test_today_range_missing_symbol_in_response_tombstones_and_isolates_the_rest():
    s = _strategy()
    # AAPL has full data, MSFT is simply absent (Alpaca has nothing for it yet) — must not blow up the
    # batch, and MSFT gets tombstoned so a prior value for it (if any) can't linger.
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap()})
    s._loaded = {"AAPL.XNAS", "MSFT.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    frames = {f["instrument_id"]: f for f in _today_range_frames(s)}
    assert frames["AAPL.XNAS"]["high"] == 172.34
    assert frames["MSFT.XNAS"]["high"] is None


def test_today_range_partial_snapshot_missing_prev_daily_bar_tombstones():
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": {"dailyBar": _snap()["dailyBar"]}})  # no prevDailyBar
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    frames = _today_range_frames(s)
    assert len(frames) == 1 and frames[0]["high"] is None


def test_today_range_stale_daily_bar_tombstones_not_published_as_fresh():
    # dailyBar.t is still YESTERDAY's date (a thin symbol before its first tick, a holiday, an outage) —
    # must not be treated as "today"'s range even though the payload shape is otherwise complete.
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap(daily_t=_YESTERDAY_ISO)})
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    frames = _today_range_frames(s)
    assert len(frames) == 1 and frames[0]["high"] is None


def test_today_range_stale_daily_bar_tombstones_a_previously_published_value():
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap()})
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))  # publishes a real value
    _today_range_frames(s)  # drain
    # next batch: Alpaca's dailyBar has gone stale (still showing yesterday, e.g. an outage) — the
    # PREVIOUSLY published value must not keep rendering as current.
    s._http.snapshots = {"AAPL": _snap(daily_t=_YESTERDAY_ISO)}
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW + 5_000_000_000))
    frames = _today_range_frames(s)
    assert len(frames) == 1
    assert frames[0] == {"instrument_id": "AAPL.XNAS", "high": None, "low": None, "prev_close": None, "ts_event": _TODAY_NOW + 5_000_000_000}


def test_today_range_tombstones_even_without_a_local_prior_value_restart_safety():
    # The regression case codex round 3 flagged: an engine RESTART wipes any in-memory record of "did I
    # publish a value for this symbol before" — but the consumer/Redis can still be replaying an OLD
    # `today_range` frame from before the restart. A fresh strategy instance (nothing published locally,
    # ever) with stale/missing data must STILL tombstone, every single call, unconditionally — not just
    # the first time. This is what makes the tombstone reach a symbol that went stale during downtime.
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={})
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    frames1 = _today_range_frames(s)
    assert len(frames1) == 1 and frames1[0]["high"] is None
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW + 5_000_000_000))
    frames2 = _today_range_frames(s)
    assert len(frames2) == 1 and frames2[0]["high"] is None  # tombstones again — not a one-shot


def test_today_range_whole_batch_failure_is_caught_and_tombstones_loaded_symbols():
    # A failed HTTP request must not raise, AND must not silently preserve stale consumer state — that
    # reopens the same stale-value hole as a per-symbol miss, just for the whole batch (codex round 4).
    s = _strategy()
    s._http = _FakeAlpacaHttp(error=RuntimeError("network down"))
    s._loaded = {"AAPL.XNAS", "MSFT.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))  # must not raise
    frames = {f["instrument_id"]: f for f in _today_range_frames(s)}
    assert set(frames) == {"AAPL.XNAS", "MSFT.XNAS"}
    assert all(f["high"] is None for f in frames.values())
    assert s._today_range_inflight is False  # guard released even on failure — next tick can retry


def test_today_range_inflight_guard_skips_overlapping_calls():
    s = _strategy()
    s._http = _FakeAlpacaHttp(snapshots={"AAPL": _snap()})
    s._loaded = {"AAPL.XNAS"}
    s._today_range_inflight = True  # simulate a batch already running
    import asyncio as _asyncio
    _asyncio.run(s._refresh_today_ranges(_TODAY_NOW))
    assert s._http.calls == []  # skipped entirely — no double-fetch
    assert _today_range_frames(s) == []


# ----------------------------------------------------------------------------------------------------
# Fundamentals (#182 follow-up, watchlist KPI Phase 2) — _refresh_fundamentals: per-symbol FMP fetch
# (no batch endpoint on the free tier, confirmed empirically), per-call failure isolation (one of
# quote/profile/eps failing doesn't block the others), NO tombstone on failure (unlike vwap/today_range —
# fundamentals have no daily-boundary reset), inflight guard against overlapping refreshes.
# ----------------------------------------------------------------------------------------------------
class _FakeFmp:
    """Stand-in for FmpHttpClient — controlled per test via quote/profile/eps values or *_error."""

    def __init__(self, quote=None, profile=None, eps=None, quote_error=None, profile_error=None, eps_error=None):
        self.quote = quote
        self.profile = profile
        self.eps = eps
        self.quote_error = quote_error
        self.profile_error = profile_error
        self.eps_error = eps_error
        self.calls: list[str] = []

    async def get_quote(self, symbol: str):
        self.calls.append(f"quote:{symbol}")
        if self.quote_error is not None:
            raise self.quote_error
        return self.quote

    async def get_profile(self, symbol: str):
        self.calls.append(f"profile:{symbol}")
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile

    async def get_annual_eps(self, symbol: str):
        self.calls.append(f"eps:{symbol}")
        if self.eps_error is not None:
            raise self.eps_error
        return self.eps


def _fundamentals_frames(s) -> list[dict]:
    frames = []
    while not s._queue.empty():
        kind, payload = s._queue.get_nowait()
        if kind == "fundamentals":
            frames.append(json.loads(payload))
    return frames


_FUND_NOW = _et_ns(2026, 7, 27, 10, 0)


def test_fundamentals_no_fmp_client_is_a_noop():
    s = _strategy()
    s._fmp = None
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s) == []


def test_fundamentals_no_loaded_symbols_is_a_noop():
    s = _strategy()
    s._fmp = _FakeFmp()
    s._loaded = set()
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s) == []
    assert s._fmp.calls == []


def test_fundamentals_publishes_all_fields():
    s = _strategy()
    s._fmp = _FakeFmp(
        quote={"price": 200.0, "marketCap": 1_000_000, "timestamp": 1785528001},
        profile={"beta": 1.1, "lastDividend": 1.05, "marketCap": 1_000_000},
        eps=10.0,
    )
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    frames = _fundamentals_frames(s)
    assert len(frames) == 1
    f = frames[0]
    assert f["instrument_id"] == "AAPL.XNAS"
    assert f["market_cap"] == 1_000_000
    assert f["beta"] == 1.1
    assert f["eps"] == 10.0
    assert f["pe"] == pytest.approx(20.0)  # 200.0 / 10.0
    assert f["dividend_amount"] == 1.05
    assert f["ts_event"] == _FUND_NOW


def test_fundamentals_requests_bare_ticker_not_full_instrument_id():
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 1.0}, profile={}, eps=1.0)
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert s._fmp.calls == ["quote:AAPL", "profile:AAPL", "eps:AAPL"]


def test_fundamentals_dotted_ticker_preserved_whole():
    # codex review: naive `iid_str.split(".", 1)[0]` would request "BRK" for "BRK.B.XNYS", losing the
    # share class entirely — InstrumentId.from_str().symbol.value handles it correctly (splits on the
    # LAST dot). Same bug class already fixed for Today's Range.
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 1.0}, profile={}, eps=1.0)
    s._loaded = {"BRK.B.XNYS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert s._fmp.calls == ["quote:BRK.B", "profile:BRK.B", "eps:BRK.B"]
    assert _fundamentals_frames(s)[0]["instrument_id"] == "BRK.B.XNYS"


def test_fundamentals_pe_null_when_eps_missing_or_zero():
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 200.0}, profile={}, eps=None)
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s)[0]["pe"] is None

    s2 = _strategy()
    s2._fmp = _FakeFmp(quote={"price": 200.0}, profile={}, eps=0.0)
    s2._loaded = {"AAPL.XNAS"}
    _asyncio.run(s2._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s2)[0]["pe"] is None  # never divide by zero


def test_fundamentals_one_call_failing_does_not_block_the_others():
    # profile fails (network blip) — quote + eps still succeed and still publish, with beta/dividend null.
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 200.0, "marketCap": 500.0}, eps=10.0, profile_error=RuntimeError("down"))
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    frames = _fundamentals_frames(s)
    assert len(frames) == 1
    assert frames[0]["market_cap"] == 500.0
    assert frames[0]["eps"] == 10.0
    assert frames[0]["beta"] is None
    assert frames[0]["dividend_amount"] is None


def test_fundamentals_all_calls_failing_publishes_nothing_not_a_tombstone():
    # Unlike vwap/today_range, a total failure publishes NOTHING at all — no null-tombstone frame —
    # because fundamentals have no daily-boundary reset; keeping the prior value on failure is correct.
    s = _strategy()
    s._fmp = _FakeFmp(
        quote_error=RuntimeError("down"), profile_error=RuntimeError("down"), eps_error=RuntimeError("down")
    )
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))  # must not raise
    assert _fundamentals_frames(s) == []


def test_fundamentals_inflight_guard_skips_overlapping_calls():
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 1.0}, profile={}, eps=1.0)
    s._loaded = {"AAPL.XNAS"}
    s._fundamentals_inflight = True
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert s._fmp.calls == []
    assert _fundamentals_frames(s) == []


def test_fundamentals_as_of_derived_from_quote_timestamp():
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 1.0, "timestamp": 1785528001}, profile={}, eps=1.0)  # 2026-07-27-ish UTC
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s)[0]["as_of"] != "unknown"


def test_fundamentals_as_of_unknown_when_quote_missing():
    s = _strategy()
    s._fmp = _FakeFmp(quote=None, profile={}, eps=1.0)
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))
    assert _fundamentals_frames(s)[0]["as_of"] == "unknown"


def test_fundamentals_msgbus_publish_failure_does_not_undo_the_ui_publish():
    # publish_data raises on a bare/unregistered strategy (no trader_id — the real production case is a
    # registered node where this succeeds; see test_data_types.py for FundamentalsData/DataType coverage
    # in isolation). The Redis/UI frame must still land regardless — one path failing must not undo the
    # other or crash the refresh loop.
    s = _strategy()
    s._fmp = _FakeFmp(quote={"price": 1.0}, profile={}, eps=1.0)
    s._loaded = {"AAPL.XNAS"}
    import asyncio as _asyncio
    _asyncio.run(s._refresh_fundamentals(_FUND_NOW))  # must not raise
    frames = _fundamentals_frames(s)
    assert len(frames) == 1
    assert frames[0]["instrument_id"] == "AAPL.XNAS"


# -- bar-heal in-flight guard --------------------------------------------------------------------

def test_a_failed_refetch_does_not_disable_healing_forever():
    """`_inflight` was a bare set cleared only by the request's SUCCESS callback. A failed request —
    Alpaca 429/5xx, a timeout, any raise inside _request_bars — never calls back, so the key stayed
    in the set for the life of the process and `_on_refetch` skipped that bar type permanently. One
    transient error and a thin series could never heal again, regardless of configured lookback.

    The other two in-flight guards in this file clear in `finally`; this one had no failure path.
    """
    from api.engine_node import _HEAL_COOLDOWN_NS, _INFLIGHT_TIMEOUT_NS

    assert _INFLIGHT_TIMEOUT_NS > 0, "a guard with no expiry is permanent, not protective"
    # The timeout must outlast a real multi-page fetch but expire within a couple of heal cycles,
    # or a stuck series needs a restart to recover.
    assert _INFLIGHT_TIMEOUT_NS < 2 * _HEAL_COOLDOWN_NS


def test_the_inflight_guard_is_keyed_by_start_time_not_membership():
    """Membership alone cannot express 'started long enough ago that the callback is not coming'."""
    import inspect

    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy.__init__)
    assert "_inflight: dict[str, int]" in src, \
        "a bare set cannot expire — the failed-request case has no way to clear itself"


def test_the_stop_flag_does_not_shadow_the_framework_method():
    """`Strategy._stop` is Nautilus's own FSM action. Assigning a threading.Event over it made the
    stop transition call the Event — TypeError('Event' object is not callable) — which HALTS the
    transition, so on_stop() never ran: timers uncancelled, writer and command-reader threads
    unjoined, redis and the HTTP session unclosed, on every single restart.

    It failed loudly in the logs and silently in effect, which is why it survived."""
    import inspect

    from nautilus_trader.trading.strategy import Strategy

    import api.engine_node as en

    src = inspect.getsource(en.UiFeedStrategy)
    assert "self._stop = " not in src, "shadows Strategy._stop — the stop transition will raise"
    assert callable(getattr(Strategy, "_stop", None)), "premise: the framework defines _stop"


def _mark_row(has_account: bool, broker_basis: dict | None = None):
    """Run _enrich_position_financials over one long position with a known mark.

    `broker_basis` is the BROKER's cost basis cache (#370). Defaults to None — "the broker has not been
    asked" — which is the state these two tests were written under and must keep exercising.
    """
    from types import SimpleNamespace

    from api.engine_node import UiFeedStrategy

    class _Iid:                    # SimpleNamespace defines __eq__, so it is unhashable
        venue = "XNYS"
        def __str__(self): return "AFL.XNYS"

    iid = _Iid()
    # `strategy_id` because production positions always carry one and the marking is keyed by
    # (instrument, strategy) since #807 item 3 — a double without it cannot reach the mark.
    pos = SimpleNamespace(instrument_id=iid, strategy_id="MANUAL-001", avg_px_open=100.0, quantity=10, signed_qty=10.0)
    calls: list[str] = []

    class _Portfolio:
        def account(self, venue):
            return object() if has_account else None

        def net_exposure(self, instrument_id, price):
            calls.append("net_exposure")

        def unrealized_pnl(self, instrument_id, price):
            calls.append("unrealized_pnl")

    # `portfolio` and `cache` are read-only Cython attributes on Actor, so the methods are called
    # unbound against a duck-typed self rather than stubbed on an instance.
    strat = SimpleNamespace(_venue_account_cache={}, portfolio=_Portfolio(),
                            cache=SimpleNamespace(positions_open=lambda: [pos]),
                            _last_close={iid: 110.0})
    strat._venue_has_account = lambda v: UiFeedStrategy._venue_has_account(strat, v)
    # THE REAL METHOD, BOUND TO THE DOUBLE — not a stub returning a convenient answer. `_marking_basis`
    # is the seam that decides which cost basis the mark uses (#370), and a double that answered it
    # itself could not tell a working basis decision from a bypassed one.
    strat._broker_avg_entry = broker_basis
    strat.BASIS_DIVERGENCE_TOL = UiFeedStrategy.BASIS_DIVERGENCE_TOL
    strat.log = SimpleNamespace(warning=lambda *a, **k: None)
    strat._marking_basis = lambda iid, avg: UiFeedStrategy._marking_basis(strat, iid, avg)

    row = SimpleNamespace(source="POSITION", instrument_id=str(iid), strategy_id="MANUAL-001", avg_px=None, last_px=None,
                          market_value=None, unrealized_pl=None, unrealized_plpc=None)
    UiFeedStrategy._enrich_position_financials(strat, [row])
    return row, calls


def test_position_is_marked_when_no_account_exists_for_its_venue():
    """The Alpaca account is registered under venue ALPACA while instruments are XNYS/XNAS, so
    Portfolio.net_exposure/unrealized_pnl find no account, log at ERROR and return None.

    P&L had a fallback; market value did not, so every position in the UI showed an empty market
    value for the whole run. Nothing tested this enrichment, which is why it shipped that way."""
    row, calls = _mark_row(has_account=False)

    assert calls == [], "Portfolio must not be asked when it holds no account for the venue"
    assert row.market_value == 1100.0                 # 110 mark x 10 shares
    assert row.unrealized_pl == 100.0                 # (110 - 100) x 10
    assert row.unrealized_plpc == pytest.approx(0.1)  # 100 / (100 x 10)


def test_portfolio_still_owns_the_math_when_the_venue_has_an_account():
    """The fallback is for the venue mismatch only — it must not quietly take over the normal path."""
    _, calls = _mark_row(has_account=True)
    assert calls == ["net_exposure", "unrealized_pnl"]


def _feed_with(projections: dict):
    """A duck-typed UiFeedStrategy carrying a set of per-strategy projections."""
    from types import SimpleNamespace
    return SimpleNamespace(_trade_cycles=projections, _exec_client_id="ALPACA", id="MANUAL-001")


def test_every_node_strategy_is_owned_not_quarantined():
    """MOMENTUM-002's eight positions rendered as "UNCLAIMED · foreign strategy" for the whole run.

    `owned` was `{str(self.id)}` — the FEED strategy, MANUAL-001 — so every other cockpit strategy in
    the same node failed the ownership test and fell into the #79 quarantine plane. The Orders tab
    showed them as MOMENTUM at the same time, because only this plane consulted `owned`.
    """
    from api.engine_node import UiFeedStrategy

    feed = _feed_with({"MANUAL-001": object(), "MOMENTUM-002": object()})
    owned = set(feed._trade_cycles) or {str(feed.id)}

    assert owned == {"MANUAL-001", "MOMENTUM-002"}
    assert "MOMENTUM-002" in owned, "a strategy this node runs is not external activity"
    # The registration hook is what puts a peer in that set at all.
    assert hasattr(UiFeedStrategy, "register_strategy")


def test_registering_a_strategy_gives_it_its_own_projection():
    """Ownership and the trades plane must move together: marking a strategy owned removes it from the
    quarantine plane, so if it did not ALSO project cycles the positions would render nowhere at all —
    strictly worse than showing them as unclaimed."""
    from api.engine_node import UiFeedStrategy

    feed = _feed_with({"MANUAL-001": object()})
    UiFeedStrategy.register_strategy(feed, "MOMENTUM-002")

    assert set(feed._trade_cycles) == {"MANUAL-001", "MOMENTUM-002"}
    assert feed._trade_cycles["MOMENTUM-002"] is not None, "owned but unprojected renders nowhere"


def test_registering_is_idempotent_and_needs_an_exec_client():
    """Re-registering must not replace a live projection (it holds the active-cycle map), and a
    data-only node with no exec client has nothing to project."""
    from api.engine_node import UiFeedStrategy

    existing = object()
    feed = _feed_with({"MANUAL-001": object(), "MOMENTUM-002": existing})
    UiFeedStrategy.register_strategy(feed, "MOMENTUM-002")
    assert feed._trade_cycles["MOMENTUM-002"] is existing, "must not reset the active-cycle map"

    dataonly = _feed_with({})
    UiFeedStrategy.register_strategy(dataonly, "MOMENTUM-002")
    assert dataonly._trade_cycles == {}, "no exec client → nothing to project"


def _publisher(maxsize: int):
    """A duck-typed UiFeedStrategy with a real bounded queue, for the publish path only."""
    import queue as q
    import time as t
    from types import SimpleNamespace
    return SimpleNamespace(_queue=q.Queue(maxsize=maxsize), _dropped={}, _dropped_at=t.monotonic(),
                           _backfill_stalled_at=0.0)


def test_backfill_frames_wait_for_queue_space_instead_of_being_dropped():
    """A 7-day 1m backfill is ~1700 frames per symbol — ~127k across the pool against a 10k queue.
    Dropping them left the UI with 6 minute bars for a series the engine had received 1761 of, which
    the trend strip then rendered as a flat 0.00%.

    A dropped live tick is superseded a second later; a dropped history bar never comes again."""
    import threading

    from api.engine_node import UiFeedStrategy

    pub = _publisher(maxsize=1)
    UiFeedStrategy._publish(pub, "bar", {"n": 1}, backfill=True)  # fills the queue

    done = threading.Event()

    def drain_soon():
        pub._queue.get()  # make room while the producer is waiting
        done.set()

    threading.Timer(0.05, drain_soon).start()
    UiFeedStrategy._publish(pub, "bar", {"n": 2}, backfill=True)

    assert done.wait(2), "the backfill put should have waited for the drain"
    assert pub._dropped == {}, "a backfill frame that fits after waiting must not count as dropped"


def test_live_frames_never_wait():
    """The trading loop must not stall on Redis backpressure."""
    from api.engine_node import UiFeedStrategy

    pub = _publisher(maxsize=1)
    UiFeedStrategy._publish(pub, "bar", {"n": 1})
    UiFeedStrategy._publish(pub, "bar", {"n": 2})  # full → dropped immediately, no wait

    assert pub._dropped.get("bar") == 1


def test_a_stuck_writer_suspends_backfill_blocking():
    """Waiting per frame is only safe while the writer drains. With Redis dead, 127k frames each
    waiting would turn startup into hours, so blocking is suspended after the first timeout."""
    from api.engine_node import UiFeedStrategy

    pub = _publisher(maxsize=1)
    UiFeedStrategy._publish(pub, "bar", {"n": 1}, backfill=True)   # fills it
    UiFeedStrategy._publish(pub, "bar", {"n": 2}, backfill=True)   # waits, times out, trips the breaker

    assert pub._backfill_stalled_at > 0, "a timed-out backfill put must suspend blocking"
    assert pub._dropped.get("bar") == 1


# -- order events reach the display strategy for EVERY strategy (#207) ----------------------------
def test_the_display_strategy_asks_the_bus_for_every_strategys_order_events():
    """Nautilus delivers `on_order_event` only for the subscribing strategy's OWN orders
    (`trading/strategy.pyx:322` subscribes `events.order.{self.id}`), and this strategy is registered
    as MANUAL. So the trade receipt — whose entire job is reporting AUTOMATED orders — was wired to the
    one source structurally incapable of carrying them: eight orders filled on 2026-08-10 and not one
    receipt was sent. The wildcard subscription is the fix; assert it is asked for."""
    import inspect

    from api.engine_node import _ORDER_EVENTS_TOPIC

    assert _ORDER_EVENTS_TOPIC == "events.order.*"
    src = inspect.getsource(UiFeedStrategy.on_start)
    assert "_ORDER_EVENTS_TOPIC" in src, "on_start no longer subscribes to all order events"


def test_a_manual_order_is_not_blotted_twice():
    """The wildcard subscription ADDS to Nautilus's per-strategy one rather than replacing it, so a
    MANUAL order arrives on both paths. Without the guard every operator click publishes two frames."""
    s = _strategy()
    handled: list[str] = []
    s._handle_order_event = lambda event: handled.append(str(event.strategy_id))

    class Event:
        strategy_id = s.id                        # this strategy's own — already delivered natively

    s._on_any_order_event(Event())
    assert handled == [], "manual order double-handled"

    class Automated:
        strategy_id = "MOMENTUM-002"

    s._on_any_order_event(Automated())
    assert handled == ["MOMENTUM-002"], "automated order never reached the handler"


# -- held positions must stream (#206) ------------------------------------------------------------
def test_open_positions_are_streamed_not_just_the_watchlist():
    """The startup universe is a SNAPSHOT: nothing subscribed a symbol at the moment it was bought. On
    2026-08-10 six freshly-entered names streamed zero frames until a restart, and the UI showed
    `LAST —` across the automated book. Worse than cosmetic — `_trail_exits` falls back to yesterday's
    CLOSE with no live quote, so an overnight gap through an exit level reads as 'not triggered'."""
    import inspect

    src = inspect.getsource(UiFeedStrategy._reconcile_symbols)
    assert "positions_open()" in src, "the periodic sweep still ignores held positions"


# ==================================================================================================
# MARKET VALUE MUST CARRY THE SIGN (2026-08-21, live paper book).
#
# `/trades` reported WHD twice, both `HELD quantity 28 market_value +1924.72`:
#
#     WHD.XNYS  BCTROT-004    LONG   28  market_value +1924.72  unrealized -51.24
#     WHD.XNYS  MOMENTUM-002  SHORT  28  market_value +1924.72  unrealized +51.24
#
# The broker held ZERO WHD. Signed, those two net to 0 and agree with the broker exactly - which is why
# `_report_reconcile_drift` correctly reported no drift. Unsigned, the book renders 56 shares and ~$3,850
# of exposure that does not exist, and the session monitor counted the SHORT as an unprotected long.
#
# `unrealized_pl` two lines above uses `pos.signed_qty` and got the sign right (-51.24 / +51.24). The
# comment directly above the market-value line even reads "signed_qty is + long / - short". The next
# statement then calls `abs(pos.quantity)`.
# ==================================================================================================
class _SignedPos:
    """Shaped like the Nautilus Position where `_enrich_position_financials` touches it.

    `quantity` is UNSIGNED and `signed_qty` is signed - that is Nautilus's real contract, and a double
    returning a negative `quantity` would hide the very bug this pins.
    """

    def __init__(self, instrument_id: str, signed_qty: float, avg_px: float):
        from nautilus_trader.model.identifiers import InstrumentId
        self.instrument_id = InstrumentId.from_str(instrument_id)
        self.strategy_id = "MANUAL-001"  # production positions always carry one; marking keys on it (#807)
        self.signed_qty = signed_qty
        self.quantity = abs(signed_qty)
        self.avg_px_open = avg_px
        self.is_open = signed_qty != 0


class _Row:
    """The POSITION row shape `_enrich_position_financials` mutates."""

    def __init__(self, instrument_id: str, avg_px: float, last_px: float):
        self.source = "POSITION"
        self.instrument_id = instrument_id
        self.strategy_id = "MANUAL-001"  # the same lane as `_SignedPos`: a row is marked from ITS OWN position
        self.avg_px = avg_px
        self.last_px = last_px
        self.market_value = None
        self.unrealized_pl = None
        self.unrealized_plpc = None
        self.basis_contested = None


def _enriched(signed_qty: float, *, avg_px: float = 70.57, last_px: float = 68.74):
    """Drive the REAL method on a real UiFeedStrategy instance - not a reimplementation of its
    arithmetic, which is how a green unit test can sit beside a broken production path."""
    from api.engine_node import UiFeedStrategy

    iid = "WHD.XNYS"
    pos = _SignedPos(iid, signed_qty, avg_px)
    row = _Row(iid, avg_px, last_px)

    # `cache` is a read-only Cython attribute on nautilus Actor, so a real instance cannot be stubbed.
    # Call the REAL function with a duck-typed `self` instead — the production code path runs verbatim;
    # only the collaborators it reaches for are substituted.
    node = types.SimpleNamespace(
        cache=types.SimpleNamespace(positions_open=lambda: [pos]),
        # The method marks off ITS OWN streamed close, not the row's — supplying it here is what makes
        # the double able to reach the market-value line at all.
        _last_close={pos.instrument_id: last_px},
        _venue_has_account=lambda venue: False,   # the real paper layout: no account registered for XNYS
        _marking_basis=lambda instrument_id, avg: (avg, False),
    )
    UiFeedStrategy._enrich_position_financials(node, [row])
    return row


def test_a_SHORT_positions_market_value_is_NEGATIVE():
    """The exact live numbers: 28 WHD short, marked 68.74, avg 70.57. Shipped as +1924.72."""
    short = _enriched(-28.0)
    assert short.unrealized_pl == pytest.approx(51.24, abs=0.01), "sanity: the short profits as price falls"
    assert short.market_value == pytest.approx(-1924.72, abs=0.01), (
        f"a SHORT reported market_value {short.market_value} - positive exposure for a position that is "
        "short. This is what rendered a flat WHD book as 56 shares long."
    )


def test_a_matched_long_and_short_on_one_instrument_net_to_FLAT():
    """The invariant the book depends on, and the one the broker confirmed: BCTROT-004 long 28 plus
    MOMENTUM-002 short 28 is zero WHD, which is exactly what Alpaca held."""
    long_row = _enriched(+28.0)
    short_row = _enriched(-28.0)
    # The fixture's own property first: these must actually be opposite sides, or the sum below is
    # vacuous and would pass against the unsigned version too.
    assert long_row.unrealized_pl == pytest.approx(-short_row.unrealized_pl, abs=0.01)
    assert long_row.market_value + short_row.market_value == pytest.approx(0.0, abs=0.01), (
        f"long {long_row.market_value} + short {short_row.market_value} != 0 - unsigned they sum to "
        "twice the notional, inflating the book by exposure that does not exist"
    )


def test_market_value_and_unrealized_pl_derive_the_side_from_the_SAME_field():
    """Two derivations of one fact, three lines apart in the same method.

    unrealized_pl reads `pos.signed_qty`; market_value read `abs(pos.quantity)`. They disagreed about
    which way a short points and only one was right.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._enrich_position_financials)
    # Strip comments so this cannot be satisfied by the prose ABOUT the bug.
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    mv = [ln.strip() for ln in code.splitlines() if "market_value =" in ln and "last_px" in ln]
    assert mv, "market-value fallback line not found - did the method get restructured?"
    assert not any("abs(" in ln for ln in mv), (
        "market_value is computed from an UNSIGNED quantity; a short then reports positive exposure. "
        f"Offending line(s): {mv}"
    )


# ==================================================================================================
# REDUCE-ONLY IS DECORATION UNLESS `_submit` NAMES THE POSITION (2026-08-21, WHD).
#
# MOMENTUM-002 submitted a reduce-only SELL for 28 WHD owned by BCTROT-004's NETTING position:
#
#     SubmitOrder(order=MarketOrder(SELL 28 WHD.XNYS MARKET DAY, ... reduce_only=True ...),
#                 position_id=None)
#
# A SHORT was opened under MOMENTUM-002 instead of BCTROT-004's LONG being closed. Nautilus named it:
#
#     Cannot open NETTING position PositionId('WHD.XNYS-MOMENTUM-002') from reduce-only fill ...;
#     matching_reduce_positions=[WHD.XNYS-BCTROT-004:strategy_id=BCTROT-004,signed_qty=28]
#
# VERIFIED IN THE PINNED PACKAGE, not from memory — `nautilus_trader/risk/engine.pyx`:
#
#     if command.position_id is not None:
#         if order.is_reduce_only:
#             position = self._cache.position(command.position_id)
#             if position is None or not order.would_reduce_only(...):
#                 self._deny_command(...)
#
# The ENTIRE reduce-only check sits behind `position_id is not None`. Without it the flag is never
# examined, for any order the cockpit sends.
#
# THIS IS THE FOURTH OCCURRENCE. engine_node.py's own protective-stop comment already lists three:
# "That is FIG in #252, the oversell in #245, and the phantom HSBC short." WHD is the next one, and the
# comment describing the mechanism sits eleven lines above a `_submit(order)` call that omits the id.
# ==================================================================================================
def test_nautilus_gates_the_WHOLE_reduce_only_check_behind_position_id():
    """The installed package is the source of truth for this claim, not the docs and not memory.

    If a future Nautilus moves the check out from behind that condition, `reduce_only` starts being
    enforced without an id and the guard below becomes unnecessary — this test is what tells us.
    """
    import pathlib

    import nautilus_trader

    pyx = pathlib.Path(nautilus_trader.__file__).parent / "risk" / "engine.pyx"
    if not pyx.is_file():                      # a wheel without sources — skip rather than assert falsely
        pytest.skip("nautilus risk/engine.pyx not shipped in this install")
    src = pyx.read_text()
    i = src.index("if command.position_id is not None:")
    guarded = src[i : i + 600]
    assert "if order.is_reduce_only:" in guarded, (
        "the reduce-only check is no longer nested under `position_id is not None` — re-derive whether "
        "passing the id is still required for enforcement"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REPRODUCED, NOT FIXED (2026-08-21). The protective stop at engine_node.py:1474 submits "
        "reduce_only=True with no position_id, so risk/engine.pyx never examines the flag. Fixing it is "
        "not a one-liner: a protective stop rests against the ACCOUNT'S NET position, while NETTING "
        "position ids are per-strategy (`{instrument}-{strategy_id}`). When two strategies hold the same "
        "instrument — the WHD case itself — there is no single correct id to name, and passing a wrong "
        "or stale one makes the risk engine DENY the order, i.e. no protection at all. That is a design "
        "decision on the one mechanism in this system that has never failed, so it is not being taken "
        "unilaterally. strict=True: if this starts passing, delete the marker and this comment."
    ),
)
def test_every_reduce_only_submit_NAMES_THE_POSITION():
    """AIMED AT THE CLASS, NOT AT WHD.

    The question is not "is the rotation exit fixed" but "which other orders carry a reduce-only flag
    that nothing enforces". Of the `_submit` call sites in this module, exactly one passed a
    position_id when this was written — including the protective stop, whose OWN comment eleven lines
    above says the flag alone is decoration.
    """
    import inspect
    import re

    from api import engine_node

    src = inspect.getsource(engine_node)
    lines = [ln.split("#")[0] for ln in src.splitlines()]
    offenders = []
    for i, ln in enumerate(lines):
        if not re.search(r"_submit\(\s*[a-z_]+\s*\)", ln):
            continue
        # Look back for the order construction feeding this submit; a reduce-only order must be
        # accompanied by a position_id at the submit site.
        window = "\n".join(lines[max(0, i - 40) : i])
        if '"reduce_only": True' in window or "reduce_only=True" in window:
            offenders.append(i + 1)
    assert not offenders, (
        "reduce-only order(s) submitted with no position_id at line(s) "
        f"{offenders} — `risk/engine.pyx` never examines the flag, so it cannot prevent the order from "
        "opening the opposite side. This is the WHD/FIG(#252)/oversell(#245)/HSBC shape."
    )


# ==================================================================================================
# THE /trades PATH — the one the live symptom actually came through (2026-08-21, codex review).
#
# The first fix landed on `_enrich_position_financials`, which serves `/positions`. But `/trades` is
# marked by `_mark_cycle_financials`, and THAT is where WHD reported +1924.72 on a SHORT. Same defect,
# a second time, in the path that actually rendered the wrong book:
#
#     signed = d.quantity if d.side == "LONG" else -d.quantity
#     d.last_px = last
#     d.market_value = last * d.quantity          # <-- unsigned
#     d.unrealized_pl = (last - basis) * signed   # <-- signed, one line later
#
# `signed` is computed on the line IMMEDIATELY ABOVE and then ignored by the very next statement. The
# method's own docstring says "signed by side".
#
# Caught by a conformance-framed codex review, not by me and not by the first round of tests — which is
# the point of running one: I had checked the arithmetic and never checked WHICH FUNCTION FEEDS /trades.
# ==================================================================================================
class _CycleDTO:
    """Shaped like TradeDTO where `_mark_cycle_financials` touches it. `quantity` is UNSIGNED and the
    direction lives in `side` — that is the real DTO contract (models.py: 'LONG | SHORT | FLAT')."""

    def __init__(self, instrument_id: str, side: str, quantity: float, avg_px_open: float):
        self.instrument_id = instrument_id
        self.side = side
        self.quantity = quantity
        self.avg_px_open = avg_px_open
        self.is_capital_deployed = True
        self.last_px = None
        self.market_value = None
        self.unrealized_pl = None
        self.unrealized_plpc = None
        self.venue_avg_px = None
        self.basis_contested = None


def _marked(side: str, *, qty: float = 28.0, avg: float = 70.57, last: float = 68.74):
    from api.engine_node import UiFeedStrategy

    dto = _CycleDTO("WHD.XNYS", side, qty, avg)
    node = types.SimpleNamespace(
        _last_close={"WHD.XNYS": last},
        _marking_basis=lambda instrument_id, avg_px: (avg_px, False),
        _broker_avg_entry={},
    )
    UiFeedStrategy._mark_cycle_financials(node, [dto])
    return dto


def test_the_TRADES_path_signs_a_SHORTs_market_value():
    """The live row, reproduced: WHD SHORT 28 marked 68.74 reported market_value +1924.72."""
    short = _marked("SHORT")
    assert short.unrealized_pl == pytest.approx(51.24, abs=0.01), "sanity: a short profits as price falls"
    assert short.market_value == pytest.approx(-1924.72, abs=0.01), (
        f"/trades reported market_value {short.market_value} for a SHORT — this is the number that "
        "rendered a flat WHD book as 56 shares and ~$3,849 of exposure that did not exist"
    )


def test_the_TRADES_path_nets_a_matched_pair_to_flat():
    long_row, short_row = _marked("LONG"), _marked("SHORT")
    # Fixture property first: genuinely opposite sides, else the sum below is vacuous.
    assert long_row.unrealized_pl == pytest.approx(-short_row.unrealized_pl, abs=0.01)
    assert long_row.market_value + short_row.market_value == pytest.approx(0.0, abs=0.01), (
        f"long {long_row.market_value} + short {short_row.market_value} != 0"
    )


def test_BOTH_marking_paths_sign_market_value():
    """AIMED AT THE CLASS. Two functions mark money and both got this wrong; a third would too.

    Scans every `market_value =` assignment in the module for an unsigned multiplicand. The first fix
    passed its own tests while this second path stayed broken, because those tests named one function.
    """
    import inspect
    import re

    from api import engine_node

    src = inspect.getsource(engine_node)
    offenders = []
    for i, raw in enumerate(src.splitlines()):
        ln = raw.split("#")[0]
        m = re.search(r"\.market_value\s*=\s*(.+)$", ln)
        if not m:
            continue
        rhs = m.group(1)
        if "abs(" in rhs or re.search(r"\*\s*(float\()?\s*[a-z_]*\.quantity", rhs):
            offenders.append((i + 1, rhs.strip()))
    assert not offenders, (
        f"market_value assigned from an UNSIGNED quantity at {offenders} — a SHORT then reports "
        "positive exposure and the book inflates by twice its notional"
    )
