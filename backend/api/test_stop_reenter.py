"""Tests for STOP-AND-REENTER (#47) — the two chained manager kinds (`stop_reenter_watch` /
`stop_reenter_rearm`) built on the generic manager framework (#55, `api.managers`).

Offline unit tests use a `_FakeStrategy`/`_FakePosition`/`_FakeOrder` double exposing exactly what
`ManagerHandler.trigger_met`/`apply` touch on a real `UiFeedStrategy` — no Nautilus Cache, no DB. The
hand-off paths that call `mg.attach()` (a real DB write) are `needs_services` integration tests, matching
this codebase's existing convention (`test_command_ledger.py`) — not run here (no isolated test Postgres
available; the only reachable instance is the live paper stack's own DB, not something to write test rows
into).

No `pytest-asyncio` plugin in this repo — async bodies run via `asyncio.run()` inside a plain sync test,
matching `test_command_ledger.py`'s own `run()`/`asyncio.run(run())` pattern."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientId

from api.engine_node import (
    UiFeedStrategy,
    _StopReenterRearm,
    _StopReenterWatch,
    _validate_stop_reenter_params,
)
from api.feed_config import load_feed_config
from api.managers import ManagerRow


class _FakeOrder:
    def __init__(
        self,
        order_type: str,
        tags: list[str] | None,
        ts_last: int,
        status: str = "FILLED",
        avg_px: float = 0.0,
        side: OrderSide = OrderSide.SELL,  # matches every existing test's LONG-position stop-out scenario
        # Real Nautilus orders carry this; the double didn't, which is how the stale-qty re-entry stayed
        # invisible to the suite. Sizing the rearm off the closing fill needs it.
        filled_qty: float = 0.0,
    ):
        self.order_type = _Enum(order_type)
        self.tags = tags
        self.ts_last = ts_last
        self.status = _Enum(status)
        self.avg_px = avg_px
        self.side = side
        self.filled_qty = filled_qty


class _Enum:
    """Stand-in for a Nautilus enum member — real code reads `.name`."""

    def __init__(self, name: str):
        self.name = name


class _FakePosition:
    def __init__(self, quantity: float = 1, account_id: str = "ACC-1"):
        self.quantity = quantity
        self.account_id = account_id


class _FakeCycleDTO:
    def __init__(self, instrument_id: str, strategy_id: str, cycle_id: str):
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.cycle_id = cycle_id


class _FakeTradeCycles:
    """Stand-in for ONE projection inside `strategy._trade_cycles`. Production keys that attribute by
    strategy id (`dict[str, TradeCycleProjection]`), so a test must hand the strategy a DICT of these —
    handing it a bare projection is what let the multi-strategy break ship: the fake answered
    `.project()`, production raised `'dict' object has no attribute 'project'`, and every manager's
    cycle-drift guard failed silently in the live engine while the suite stayed green."""

    def __init__(self, dtos: list[_FakeCycleDTO] | None = None):
        self._dtos = dtos or []

    def project(self, cache, ts_ns):
        return self._dtos


class _FakeClock:
    def timestamp_ns(self) -> int:
        return 0


class _FakeStrategy:
    def __init__(self, *, position=None, closing_order=None, last_price=None, trade_cycles=None):
        self._position = position
        self._closing_order = closing_order
        self._last_price = last_price
        # DICT, keyed by strategy id — the production shape. `None` stays None so the guard's falsy
        # check is still exercised.
        self._trade_cycles = ({"MANUAL-001": trade_cycles} if trade_cycles is not None else trade_cycles)  # None by default — matches the guard's own `is not None` check
        self.cache = object()  # only ever passed through to `_trade_cycles.project(cache, ...)`, ignored there
        self.clock = _FakeClock()
        self.built_orders: list[dict] = []
        self.submitted: list[object] = []
        self.bracket_reentries: list[dict] = []
        self._seen_orders: set[str] = set()


    def current_cycle_for(self, instrument_id: str, strategy_id: str):
        """Mirrors production: look up ONE strategy's projection in the dict, then select by
        instrument AND strategy. A double that answered `.project()` directly is what hid the
        multi-strategy break; one that folded every projection would hide the cross-strategy
        mis-selection codex flagged."""
        proj = (self._trade_cycles or {}).get(str(strategy_id))
        if proj is None:
            return None
        return next(
            (d for d in proj.project(self.cache, 0)
             if d.instrument_id == instrument_id and d.strategy_id == strategy_id),
            None,
        )

    def _submit_stop_bracket_reentry(self, **kwargs):
        self.bracket_reentries.append(kwargs)

    def _position_for(self, instrument_id, strategy_id, side):
        return self._position

    def _closing_order_for(self, instrument_id, strategy_id, since=None, reducing_side=None):
        if self._closing_order is None:
            return None
        if since is not None and self._closing_order.ts_last < since.timestamp() * 1e9:
            return None
        if reducing_side is not None and self._closing_order.side != reducing_side:
            return None
        return self._closing_order

    def _last_price_for(self, instrument_id):
        return self._last_price

    def _build_order(self, payload: dict):
        self.built_orders.append(payload)
        return object()

    def _submit(self, order):
        self.submitted.append(order)


def _row(**overrides) -> ManagerRow:
    base = dict(
        manager_id="m1",
        kind="stop_reenter_watch",
        kind_version=1,
        account_id="ACC-1",
        client_id="CLIENT-1",
        instrument_id="JNJ.XNYS",
        strategy_id="MANUAL-001",
        cycle_id="cyc-1",
        leash="AUTO",
        params={
            "expected_side": "LONG",
            "reclaim_price": 260.0,
            "base_price": 255.0,
            "floor_price": 248.0,
            "qty": 40,
            "rearm_count": 0,
            "rearm_max": 3,
        },
        state="ARMED",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    base.update(overrides)
    return ManagerRow(**base)


def _sync(run) -> None:
    asyncio.run(run())


# --- _validate_stop_reenter_params ---------------------------------------------------------------------


def test_validate_rejects_missing_field():
    params = _row().params.copy()
    del params["floor_price"]
    assert "floor_price" in _validate_stop_reenter_params(params)


def test_validate_rejects_bad_side():
    params = {**_row().params, "expected_side": "SIDEWAYS"}
    assert "invalid expected_side" in _validate_stop_reenter_params(params)


def test_validate_accepts_well_formed_params():
    assert _validate_stop_reenter_params(_row().params) is None


# --- price params must be on the instrument's tick, checked at ATTACH (#375) -----------------------------


def test_both_phases_declare_their_PRICE_PARAMS():
    """A stop-and-reenter armed with an off-tick floor attached cleanly and died hours later.

    Live 2026-08-19: MNDY armed with `floor_price 81.155`. It validated, attached, sat ARMED through the
    stop-out, and only failed when the rearm tried to build the re-entry's protective stop from it —
    `price 81.155 is not a valid tick for MNDY.XNAS`, FAILED and terminal, `rearm_count` still 0. The operator was
    watching the chart for a re-entry that had already been abandoned, with nothing on screen saying so.

    `_canon_price` refuses rather than rounds, which is right for an order price — but it was only ever
    applied to order payloads via `_PRICE_FIELDS`, and manager params went round it entirely.

    Declared per handler, not inferred from the name: `base_price` is a price and `rearm_max` is not, and
    a "_price" heuristic would be a rule nobody can see. Both phases declare the SAME set because the
    identical params travel the whole chain — one trusting the other to have checked is how they drift.
    """
    from api.engine_node import _StopReenterRearm, _StopReenterWatch

    expected = ("reclaim_price", "base_price", "floor_price")
    for cls in (_StopReenterWatch, _StopReenterRearm):
        declared = getattr(cls, "PRICE_PARAMS", None)
        assert declared is not None, (
            f"{cls.__name__} declares no PRICE_PARAMS — its prices reach the rearm unchecked, which is "
            f"how floor_price 81.155 armed cleanly and failed terminally hours later"
        )
        assert tuple(declared) == expected, (
            f"{cls.__name__}.PRICE_PARAMS is {declared!r}, not {expected!r} — the two phases share one "
            f"params dict, so a set that differs between them leaves a field checked in one and not the other"
        )
    # Every declared name is a REAL param. A typo here validates nothing and reads as coverage.
    for name in expected:
        assert name in _row().params, f"PRICE_PARAMS names {name!r}, which is not a stop_reenter param"


def test_an_off_tick_manager_price_is_ROUNDED_to_the_instruments_tick():
    """Operator, 2026-08-19: "if it is subdecimal you can round. no problem."

    MNDY armed with `floor_price 81.155` validated, attached, sat ARMED through the stop-out, and only
    met the tick check when the rearm tried to build the re-entry's protective stop from it —
    `price 81.155 is not a valid tick for MNDY.XNAS`, FAILED and terminal, `rearm_count` still 0. The
    re-entry was abandoned hours before the operator stopped waiting for it.

    Rounding, not refusing: 81.155 on a penny-ticked instrument is the same intention as 81.16 written at
    a precision the venue cannot express. Refusing would trade a re-entry that dies silently for one that
    never arms.
    """
    from api.engine_node import UiFeedStrategy, _StopReenterWatch

    class _Instrument:
        """Rounds to whole cents the way a us_equity instrument does — HALF_EVEN, as Nautilus does, not
        however Python's `round` feels. The stored level and the level the rearm later builds its stop
        from must agree to the cent, and a double that rounded differently would hide the exact mismatch
        this fix exists to remove."""

        def make_price(self, value):
            from decimal import ROUND_HALF_EVEN, Decimal

            return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    host = UiFeedStrategy.__new__(UiFeedStrategy)
    host._instrument_or_raise = lambda _iid: _Instrument()

    params = {
        "expected_side": "LONG", "qty": 112.0, "rearm_count": 0, "rearm_max": 3,
        "reclaim_price": 92.13, "base_price": 82.28, "floor_price": 81.155,   # the live MNDY arm
    }
    out = UiFeedStrategy._canon_manager_params(host, _StopReenterWatch(), "MNDY.XNAS", params)

    assert out["floor_price"] == 81.16, (
        f"floor_price came back {out['floor_price']} — an off-tick level still reaches the rearm, which "
        f"is how 81.155 armed cleanly and failed terminally hours later"
    )
    # Levels already on tick are untouched to the cent. A canonicaliser that nudged everything would move
    # levels the operator chose deliberately.
    assert out["reclaim_price"] == 92.13
    assert out["base_price"] == 82.28
    # And it does not quietly rewrite non-price params on its way through.
    assert out["qty"] == 112.0 and out["rearm_max"] == 3 and out["expected_side"] == "LONG"
    # A COPY: the caller's dict is what the idempotency hash is taken over, so mutating it in place would
    # change the hash under a caller that had already taken it.
    assert params["floor_price"] == 81.155, "the input dict was mutated in place"


def test_a_handler_with_no_PRICE_PARAMS_needs_no_instrument():
    """`deferred_flatten` carries no prices. Resolving an instrument for it would make an unrelated
    manager kind fail on a symbol this node cannot resolve."""
    from api.engine_node import UiFeedStrategy

    class _NoPrices:
        pass

    def _boom(_iid):
        raise AssertionError("resolved an instrument for a handler that declares no price params")

    host = UiFeedStrategy.__new__(UiFeedStrategy)
    host._instrument_or_raise = _boom
    params = {"expected_side": "LONG"}
    assert UiFeedStrategy._canon_manager_params(host, _NoPrices(), "MNDY.XNAS", params) is params


def test_the_attach_path_canonicalises_manager_price_params():
    """The wiring — a canonicaliser nothing calls is decoration, and the seam is where this broke."""
    import ast
    import inspect
    import textwrap

    from api.engine_node import UiFeedStrategy

    src = textwrap.dedent(inspect.getsource(UiFeedStrategy._reserve_attach_command))
    called = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_canon_manager_params"
    ]
    assert called, (
        "the attach path never canonicalises manager prices — an off-tick level still reaches the rearm"
    )
    assert "params = self._canon_manager_params" in src, (
        "the canonicalised params are computed and discarded — the rounded values must be what gets "
        "attached and hashed, not merely what gets logged"
    )
# --- _StopReenterWatch.trigger_met — stop-out vs target-hit vs manual-flatten discrimination -------------


def test_watch_trigger_not_met_while_still_held():
    async def run():
        strategy = _FakeStrategy(position=_FakePosition())
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def test_watch_trigger_not_met_when_flat_but_no_closing_fill_seen_yet():
    async def run():
        strategy = _FakeStrategy(position=None, closing_order=None)
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def test_watch_trigger_met_on_bracket_stop_fill():
    async def run():
        order = _FakeOrder(
            "STOP_MARKET", ["bracket:g1"], ts_last=int(datetime.now(UTC).timestamp() * 1e9)
        )
        strategy = _FakeStrategy(position=None, closing_order=order)
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is True

    _sync(run)


def test_watch_trigger_met_on_bracket_target_fill_too():
    """trigger_met fires on ANY closing fill (flat + a fill seen) — the stop-vs-target-vs-manual
    discrimination happens in apply(), not here (codex review: a manager that only fired on a bracket-stop
    fill stayed ARMED FOREVER on a target-hit or manual exit — see the apply()-level termination tests
    below)."""

    async def run():
        order = _FakeOrder("LIMIT", ["bracket:g1"], ts_last=int(datetime.now(UTC).timestamp() * 1e9))
        strategy = _FakeStrategy(position=None, closing_order=order)
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is True

    _sync(run)


def test_watch_trigger_met_on_manual_flatten_too():
    async def run():
        order = _FakeOrder("MARKET", None, ts_last=int(datetime.now(UTC).timestamp() * 1e9))
        strategy = _FakeStrategy(position=None, closing_order=order)
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is True

    _sync(run)


def test_watch_apply_terminates_cleanly_on_target_hit_no_rearm():
    """A LIMIT leg of the same bracket firing is a WIN, not a stop-out — must terminate (APPLIED, no
    hand-off), not stay ARMED forever and not rearm."""

    async def run():
        order = _FakeOrder("LIMIT", ["bracket:g1"], ts_last=int(datetime.now(UTC).timestamp() * 1e9))
        strategy = _FakeStrategy(position=None, closing_order=order)
        state, detail = await _StopReenterWatch().apply(strategy, _row())
        assert state == "APPLIED"
        assert "not a stop-out" in detail

    _sync(run)


def test_watch_apply_terminates_cleanly_on_manual_flatten_no_rearm():
    """No bracket tag (the FL- flatten path) — an explicit human exit, must terminate, never rearm."""

    async def run():
        order = _FakeOrder("MARKET", None, ts_last=int(datetime.now(UTC).timestamp() * 1e9))
        strategy = _FakeStrategy(position=None, closing_order=order)
        state, detail = await _StopReenterWatch().apply(strategy, _row())
        assert state == "APPLIED"
        assert "not a stop-out" in detail

    _sync(run)


def test_watch_apply_refuses_when_a_different_cycle_is_now_open():
    """Cycle-drift guard — if a DIFFERENT cycle has opened on this instrument+strategy since attach, this
    manager's watch is stale and must not act (codex review: without this, a manager left ARMED across a
    close->reopen->close could misattribute a completely unrelated LATER stop-out to itself)."""

    async def run():
        order = _FakeOrder("STOP_MARKET", ["bracket:g1"], ts_last=int(datetime.now(UTC).timestamp() * 1e9))
        drifted = _FakeTradeCycles([_FakeCycleDTO("JNJ.XNYS", "MANUAL-001", "cyc-DIFFERENT")])
        strategy = _FakeStrategy(position=None, closing_order=order, trade_cycles=drifted)
        state, detail = await _StopReenterWatch().apply(strategy, _row())  # _row()'s cycle_id is "cyc-1"
        assert state == "FAILED"
        assert "stale" in detail

    _sync(run)


def test_watch_trigger_ignores_a_closing_fill_from_before_attach():
    async def run():
        stale_ts = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1e9)
        order = _FakeOrder("STOP_MARKET", ["bracket:g1"], ts_last=stale_ts)
        strategy = _FakeStrategy(position=None, closing_order=order)
        # row created_at is 5 minutes ago (see _row()) — a fill an hour ago predates the attach.
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def test_watch_trigger_ignores_a_same_side_add_fill():
    """A scale-in/add fill (BUY, same side as the LONG position) is not a close — reducing_side filters it
    out, so trigger_met correctly stays False rather than mistaking an add for the eventual exit."""

    async def run():
        add_fill = _FakeOrder(
            "MARKET", None, ts_last=int(datetime.now(UTC).timestamp() * 1e9), side=OrderSide.BUY
        )
        strategy = _FakeStrategy(position=None, closing_order=add_fill)
        assert await _StopReenterWatch().trigger_met(strategy, _row()) is False

    _sync(run)


def test_watch_apply_overwrites_reclaim_price_with_actual_fill_not_ui_placeholder():
    """`_handle_attach_manager_command` has no way to know a position's real stop-trigger price today (no
    way yet to tell a resting protective stop apart from any other order — PositionDetail.tsx's own
    comment), so whatever `reclaim_price` the UI sent at toggle-ON is a placeholder. This is the guard that
    it never actually gets used for the 'never chase higher' check — the ACTUAL fill price does. `mg.attach`
    and the DB session are mocked (not a live Postgres write) — this asserts on the PARAMS COMPUTED before
    the hand-off, not the persisted row."""

    async def run():
        order = _FakeOrder(
            "STOP_MARKET",
            ["bracket:g1"],
            ts_last=int(datetime.now(UTC).timestamp() * 1e9),
            avg_px=258.42,  # the real fill — differs from _row()'s placeholder reclaim_price (260.0)
        )
        strategy = _FakeStrategy(position=None, closing_order=order)
        captured: dict = {}

        async def fake_attach(session, **kwargs):
            captured.update(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
        patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _StopReenterWatch().apply(strategy, _row())

        assert state == "APPLIED"
        assert captured["kind"] == "stop_reenter_rearm"
        assert captured["params"]["reclaim_price"] == 258.42

    _sync(run)


# --- _StopReenterRearm.trigger_met — zone crossing ---------------------------------------------------


def test_rearm_trigger_not_met_with_no_live_price():
    async def run():
        strategy = _FakeStrategy(last_price=None)
        assert await _StopReenterRearm().trigger_met(strategy, _row(kind="stop_reenter_rearm")) is False

    _sync(run)


def test_rearm_trigger_not_met_inside_the_zone():
    async def run():
        strategy = _FakeStrategy(last_price=257.0)  # between base(255) and reclaim(260)
        assert await _StopReenterRearm().trigger_met(strategy, _row(kind="stop_reenter_rearm")) is False

    _sync(run)


def test_rearm_trigger_met_on_reclaim():
    async def run():
        strategy = _FakeStrategy(last_price=260.5)
        assert await _StopReenterRearm().trigger_met(strategy, _row(kind="stop_reenter_rearm")) is True

    _sync(run)


def test_rearm_trigger_met_on_base():
    async def run():
        strategy = _FakeStrategy(last_price=254.0)
        assert await _StopReenterRearm().trigger_met(strategy, _row(kind="stop_reenter_rearm")) is True

    _sync(run)


def test_rearm_trigger_met_on_floor_break():
    async def run():
        strategy = _FakeStrategy(last_price=247.0)
        assert await _StopReenterRearm().trigger_met(strategy, _row(kind="stop_reenter_rearm")) is True

    _sync(run)


# --- _StopReenterRearm.apply — WALK / cap / guards (no DB touched by these branches) --------------------


def test_rearm_apply_refuses_when_a_different_cycle_is_now_open():
    async def run():
        drifted = _FakeTradeCycles([_FakeCycleDTO("JNJ.XNYS", "MANUAL-001", "cyc-DIFFERENT")])
        strategy = _FakeStrategy(last_price=260.5, trade_cycles=drifted)
        row = _row(kind="stop_reenter_rearm")  # cycle_id is "cyc-1"
        state, detail = await _StopReenterRearm().apply(strategy, row)
        assert state == "FAILED"
        assert "stale" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_walks_on_floor_break_no_order_placed():
    async def run():
        strategy = _FakeStrategy(last_price=247.0)
        state, detail = await _StopReenterRearm().apply(strategy, _row(kind="stop_reenter_rearm"))
        assert state == "APPLIED"
        assert "WALK" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_refuses_when_price_drifted_back_inside_zone():
    """trigger_met saw a crossing on an earlier tick; apply() re-reads and finds it's no longer true."""

    async def run():
        strategy = _FakeStrategy(last_price=257.0)
        state, detail = await _StopReenterRearm().apply(strategy, _row(kind="stop_reenter_rearm"))
        assert state == "FAILED"
        assert "re-checked" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_stops_at_cap_no_order_placed():
    async def run():
        strategy = _FakeStrategy(last_price=260.5)
        row = _row(kind="stop_reenter_rearm", params={**_row().params, "rearm_count": 3, "rearm_max": 3})
        state, detail = await _StopReenterRearm().apply(strategy, row)
        assert state == "APPLIED"
        assert "cap reached" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_refuses_when_base_price_configured_above_exit():
    """A misconfigured base_price ABOVE reclaim_price would otherwise let the base branch chase higher —
    the guard must catch this even though it's a config error, not just a live-price edge case."""

    async def run():
        row = _row(kind="stop_reenter_rearm", params={**_row().params, "base_price": 262.0})  # > reclaim (260)
        # Below reclaim(260) so ONLY the base branch fires (reclaim_hit requires price >= 260) — isolates
        # which guard actually catches this, rather than the reclaim branch masking it.
        strategy = _FakeStrategy(last_price=259.0)
        state, detail = await _StopReenterRearm().apply(strategy, row)
        assert state == "FAILED"
        assert "above the exit price" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_refuses_when_position_already_reopened():
    """Competing-automation guard — something else reopened the position between trigger_met and apply."""

    async def run():
        strategy = _FakeStrategy(last_price=260.5, position=_FakePosition())
        state, detail = await _StopReenterRearm().apply(strategy, _row(kind="stop_reenter_rearm"))
        assert state == "FAILED"
        assert "already reopened" in detail
        assert strategy.built_orders == []

    _sync(run)


def test_rearm_apply_submits_bracket_reentry_with_floor_as_protective_stop_and_does_not_rechain_at_cap():
    """Last allowed rearm (rearm_count+1 == rearm_max): a BRACKET re-entry submitted (entry + protective
    stop at the floor — codex review: a naked plain-limit re-entry would violate the platform's "every
    position has a protective stop" invariant), no further stop_reenter_watch re-attach — this branch
    touches no DB, unlike the mid-chain case."""

    async def run():
        strategy = _FakeStrategy(last_price=260.5)
        row = _row(kind="stop_reenter_rearm", params={**_row().params, "rearm_count": 2, "rearm_max": 3})
        state, detail = await _StopReenterRearm().apply(strategy, row)
        assert state == "APPLIED"
        assert strategy.built_orders == []  # NOT the old naked-limit path
        assert strategy.submitted == []
        assert len(strategy.bracket_reentries) == 1
        reentry = strategy.bracket_reentries[0]
        assert reentry["side"] == "BUY"
        assert reentry["entry_price"] == 260.0  # reclaim level, not the live tick price
        assert reentry["stop_price"] == 248.0  # floor, from _row()'s params
        assert reentry["quantity"] == 40

    _sync(run)


def test_rearm_apply_refuses_when_floor_is_not_below_the_reentry_level():
    """Protective-stop geometry guard — a misconfigured floor at/above the re-entry level would build an
    invalid (or instantly-triggering) broker-side stop; must refuse rather than submit."""

    async def run():
        # floor(261) misconfigured >= reclaim(260); price(262) is ABOVE floor (floor not "broken" — that
        # check fires first and would otherwise mask this one) and above reclaim (reclaim_hit fires).
        strategy = _FakeStrategy(last_price=262.0)
        row = _row(kind="stop_reenter_rearm", params={**_row().params, "floor_price": 261.0})
        state, detail = await _StopReenterRearm().apply(strategy, row)
        assert state == "FAILED"
        assert "invalid stop" in detail
        assert strategy.bracket_reentries == []

    _sync(run)


# --- _handle_attach_manager_command / _handle_cancel_manager_command (command layer) ---------------------


def _bare_strategy() -> UiFeedStrategy:
    # Same construction as test_engine_node.py's own `_strategy()` helper — a bare, unregistered
    # UiFeedStrategy. `.cache` is None on one of these (no Nautilus registration), so tests here must
    # monkeypatch `_position_for` rather than populate a real cache.
    return UiFeedStrategy(load_feed_config(), ClientId("DATABENTO"), "test-key")


def test_attach_manager_command_binds_qty_to_live_position_not_ui_value():
    """Codex review: a stale UI render (or a crafted payload) sending qty=999 against an actual 10-share
    position must not size a future re-entry order off that untrusted value — `_handle_attach_manager_command`
    overrides `params["qty"]` from the LIVE position it just found, before ever reaching `_handle_attach_manager`
    (mocked out here — this test is about what params get passed to it, not the DB attach itself)."""

    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: _FakePosition(quantity=10, account_id="ACC-9")
        captured: dict = {}

        async def fake_handle_attach_manager(cid, entry_id, **kwargs):
            captured.update(kwargs)
            return "ok", "stubbed"

        s._handle_attach_manager = fake_handle_attach_manager

        status, _ = await s._handle_attach_manager_command(
            "cid-1",
            {
                "kind": "stop_reenter_watch",
                "instrument_id": "JNJ.XNYS",
                "strategy_id": "MANUAL-001",
                "params": {"expected_side": "LONG", "qty": 999},  # untrusted — must be overridden
            },
            "entry-1",
        )

        assert status == "ok"
        assert captured["params"]["qty"] == 10.0
        assert captured["account_id"] == "ACC-9"

    _sync(run)


def test_attach_manager_command_rejects_when_orders_disarmed():
    async def run():
        s = _bare_strategy()
        s._orders_armed = False
        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "stop_reenter_watch", "instrument_id": "JNJ.XNYS", "params": {"expected_side": "LONG", "qty": 1}},
            "entry-1",
        )
        assert status == "error"
        assert "disarmed" in error

    _sync(run)


def test_attach_manager_command_rejects_when_no_matching_position():
    async def run():
        s = _bare_strategy()
        s._orders_armed = True
        s._position_for = lambda instrument_id, strategy_id, side: None
        status, error = await s._handle_attach_manager_command(
            "cid-1",
            {"kind": "stop_reenter_watch", "instrument_id": "JNJ.XNYS", "params": {"expected_side": "LONG", "qty": 1}},
            "entry-1",
        )
        assert status == "error"
        assert "no matching" in error

    _sync(run)


def test_cancel_manager_command_cancels_the_WHOLE_chain_not_just_the_named_row():
    """OFF must stop a chaining kind. `_PeakWatch` hands off to a fresh successor after every trim, so
    one logical "PEAK is on" spans many manager_ids and the UI can only ever name one of them —
    cancelling that one left the successor armed and the toggle sprang back to ON."""

    async def run():
        s = _bare_strategy()
        seen = {}

        async def fake_get(session, manager_id):
            return SimpleNamespace(kind="peak_watch", instrument_id="FIG.XNYS", strategy_id="MANUAL-001", cycle_id="cyc-1")

        async def fake_cancel_chain(session, kind, instrument_id, strategy_id, cycle_id):
            seen.update(kind=kind, instrument_id=instrument_id, strategy_id=strategy_id, cycle_id=cycle_id)
            return 3

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.get", fake_get),
            patch("api.managers.cancel_chain", fake_cancel_chain),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            status, detail = await s._handle_cancel_manager_command("cid-1", {"manager_id": "m1"}, "entry-1")

        assert status == "ok"
        assert "3" in detail  # reported how many it actually stopped
        assert seen == {"kind": "peak_watch", "instrument_id": "FIG.XNYS", "strategy_id": "MANUAL-001", "cycle_id": "cyc-1"}

    _sync(run)


def test_cancel_manager_command_still_reports_ok_when_nothing_was_cancelable():
    """Every row already APPLYING or terminal. The cancel still counts: `cancel_chain` records the
    INTENT, and an apply in flight consults it before handing off — so OFF takes effect at the handoff
    even though no state flipped here."""

    async def run():
        s = _bare_strategy()

        async def fake_get(session, manager_id):
            return SimpleNamespace(kind="peak_watch", instrument_id="FIG.XNYS", strategy_id="MANUAL-001", cycle_id="cyc-1")

        async def fake_cancel_chain(session, kind, instrument_id, strategy_id, cycle_id):
            return 0

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.get", fake_get),
            patch("api.managers.cancel_chain", fake_cancel_chain),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            status, detail = await s._handle_cancel_manager_command("cid-1", {"manager_id": "m1"}, "entry-1")

        assert status == "ok"
        assert "next handoff" in detail

    _sync(run)


def test_submit_stop_bracket_reentry_refuses_non_positive_short_target():
    """Codex review, round 4: a SHORT with a very wide stop (floor far above entry) can drive the 3R
    take-profit past zero. The guard fires BEFORE any instrument/cache lookup (this uses a bare,
    unregistered strategy — `.cache` is None — so if the guard were placed after the instrument lookup,
    this test would fail on THAT instead of proving the guard works)."""

    async def run():
        s = _bare_strategy()
        try:
            s._submit_stop_bracket_reentry(
                instrument_id="JNJ.XNYS",
                side="SELL",
                quantity=10,
                entry_price=100.0,
                stop_price=140.0,  # far above entry -> risk=40 -> target = 100 - 3*40 = -20
                entry_coid="SRR-m1",
                manager_id="m1",
            )
            assert False, "expected a ValueError"
        except ValueError as exc:
            assert "non-positive" in str(exc)

    _sync(run)


def test_watch_sizes_the_rearm_from_what_ACTUALLY_closed_not_the_arm_time_snapshot():
    """`qty` is captured at toggle-ON and goes stale — PEAK trims the position, a partial closes, a
    manual add lands. FIG carried `qty: 424` against a position that had become 233 and then 128.

    A stale SELL is refused by the broker ("insufficient qty available"), which is how every other
    stale-quantity bug tonight failed safely. A stale BUY has no brake — it just fills, and the operator
    owns a position they never sized. So the re-entry is sized from the fill that actually closed it.
    """

    async def run():
        order = _FakeOrder(
            "STOP_MARKET", ["bracket:g1"],
            ts_last=int(datetime.now(UTC).timestamp() * 1e9),
            avg_px=23.5, filled_qty=128,
        )
        strategy = _FakeStrategy(position=None, closing_order=order)
        captured = {}

        async def fake_attach(session, **kwargs):
            captured.update(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        row = _row(kind="stop_reenter_watch", params={**_row().params, "qty": 424.0})
        with (
            patch("api.managers.attach", fake_attach),
        patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=False)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, _ = await _StopReenterWatch().apply(strategy, row)

        assert state == "APPLIED"
        assert captured["params"]["qty"] == 128.0, "must size off the closing fill, not the stale 424"

    _sync(run)


def test_watch_does_not_arm_a_rearm_when_the_operator_turned_stop_reenter_off():
    """The Watch hands off to a Rearm — a DIFFERENT kind. `family_of` makes the pair one chain, so OFF
    on either stops both; without that, cancelling the watch left the rearm armed to buy."""

    async def run():
        from unittest.mock import AsyncMock, patch

        order = _FakeOrder(
            "STOP_MARKET", ["bracket:g1"],
            ts_last=int(datetime.now(UTC).timestamp() * 1e9), avg_px=23.5, filled_qty=128,
        )
        strategy = _FakeStrategy(position=None, closing_order=order)
        attached: list = []

        async def fake_attach(session, **kwargs):
            attached.append(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=True)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _StopReenterWatch().apply(strategy, _row(kind="stop_reenter_watch"))

        assert state == "APPLIED"
        assert attached == [], "a cancelled chain must not arm a re-entry watch"
        assert "turned off" in detail

    _sync(run)


def test_family_of_treats_the_watch_rearm_pair_as_one_chain():
    """The UI toggles both kinds as ONE control (PositionDetail.tsx). Cancelling only the kind it
    happened to name would leave the other half armed."""
    from api import managers as mg

    assert mg.family_of("stop_reenter_watch") == mg.family_of("stop_reenter_rearm")
    assert "stop_reenter_rearm" in mg.family_of("stop_reenter_watch")
    # A kind that chains alone is its own family — no accidental cross-cancelling.
    assert mg.family_of("peak_watch") == frozenset({"peak_watch"})


def test_rearm_does_not_arm_a_fresh_watch_when_the_chain_was_cancelled():
    """The Rearm's own handoff — the other half of the family, and the one that BUYS. Coverage codex
    flagged as missing: a cancelled chain must not leave a fresh watch armed to re-enter again."""

    async def run():
        from unittest.mock import AsyncMock, patch

        strategy = _FakeStrategy(last_price=23.5)
        attached: list = []

        async def fake_attach(session, **kwargs):
            attached.append(kwargs)

        class _FakeSessionCtx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        with (
            patch("api.managers.attach", fake_attach),
            patch("api.managers.chain_cancelled", AsyncMock(return_value=True)),
            patch("api.db.engine.session_factory", lambda: _FakeSessionCtx()),
        ):
            state, detail = await _StopReenterRearm().apply(strategy, _row(kind="stop_reenter_rearm"))

        # Whatever the zone decided, a cancelled chain must never hand off another watch.
        assert attached == [], "a cancelled chain must not arm a fresh watch"
        if state == "APPLIED" and "turned off" in (detail or ""):
            assert "chain stops here" in detail

    _sync(run)
