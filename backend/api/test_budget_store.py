"""Tests for the durable sleeve store (#320).

The idempotency guard IS a database unique constraint, so these are `needs_services` integration tests
against real Postgres. An in-memory double could not represent the thing under test — it would be a
double that cannot fail the way production fails, which is the shape this repo has been bitten by
repeatedly.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

_IDS = ["T-DONOR", "T-RECIP", "T-FULL", "UNALLOCATED"]
_FILLS = ["t-fill-1", "t-fill-2", "t-fill-dup"]


def _sf():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["KUMO_DATABASE_URL"])
    return async_sessionmaker(engine, expire_on_commit=False)


async def _clean(sf) -> None:
    async with sf() as s:
        await s.execute(text("DELETE FROM sleeve_transfer WHERE fill_id = ANY(:f)"), {"f": _FILLS})
        await s.execute(text("DELETE FROM strategy_sleeve WHERE strategy_id = ANY(:i)"), {"i": _IDS})
        await s.commit()



def _targets(monkeypatch, mapping: dict[str, float], registered: set[str] | None = None) -> None:
    """Targets come from SETTINGS, not from `strategy_sleeve.target`.

    `load_book` reads `_targets()` and the table column is VESTIGIAL by design (budget_store.py:48).
    These tests seeded the column, so every target came back 0 and every `headroom`/`must_reduce`
    assertion was measuring the wrong thing. They have been failing in any environment that actually
    runs them — `needs_services` is deselected by default, so nothing reported it.
    """
    import sys

    import api

    fake = type("m", (), {"resolve": staticmethod(lambda domain: dict(mapping))})

    # THE REGISTRY TOO. `_targets` now intersects the settings domain with the registered strategy ids,
    # because settings FLAGS (`QC345_ENABLED`) were arriving as zero-target strategies and could be
    # funded once switched on. That filter is production behaviour, so a double that supplies targets
    # without supplying the registry is not representing production — it hands over targets the real
    # code would drop, and every test using a synthetic strategy id silently distributes nothing.
    import api.strategy_registry as _reg

    entry = type("e", (), {})
    # `registered` defaults to every key, which is right for tests that only pass strategies. A test
    # asserting that settings FLAGS cannot be funded must pass it explicitly — otherwise the double
    # registers `QC345_ENABLED` as a strategy and defeats the very filter under test.
    fake_registry = tuple(
        type("E", (entry,), {"strategy_id": sid})()
        for sid in (mapping if registered is None else registered)
    )
    monkeypatch.setattr(_reg, "REGISTRY", fake_registry, raising=True)
    # BOTH bindings. `_targets` does `from api import settings`, which resolves the ATTRIBUTE on the
    # `api` package — so once any other test has imported the real module, patching `sys.modules` alone
    # is ignored and the stub silently does nothing. These tests passed in isolation and failed the
    # moment they ran after `test_budget_store.py`, which is the signature of a double that only works
    # when nothing else has loaded.
    monkeypatch.setitem(sys.modules, "api.settings", fake)
    monkeypatch.setattr(api, "settings", fake, raising=False)


@pytest.mark.needs_services
def test_a_replayed_fill_moves_capital_exactly_once(monkeypatch):
    """The guard that only a database can enforce.

    Reconciliation re-reports fills and a reconnect replays them. An in-memory applied-set forgets
    across a restart — which is exactly when reconciliation replays hardest — so uniqueness lives in
    the table and the duplicate insert simply fails.
    """
    from api.budget_store import load_book, on_sell_fill
    from api.db.models import StrategySleeve

    sf = _sf()

    async def run():
        await _clean(sf)
        _targets(monkeypatch, {"T-DONOR": 30_000, "T-RECIP": 20_000})
        async with sf() as s:
            s.add(StrategySleeve(strategy_id="T-DONOR", target=30_000, actual=50_000))
            s.add(StrategySleeve(strategy_id="T-RECIP", target=20_000, actual=0))
            await s.commit()

        async with sf() as s:
            first = await on_sell_fill(s, seller_id="T-DONOR", proceeds=8_000,
                                       fill_id="t-fill-1", recipient_id="T-RECIP")
            await s.commit()
        assert first is not None and first.amount == 8_000

        async with sf() as s:
            replay = await on_sell_fill(s, seller_id="T-DONOR", proceeds=8_000,
                                        fill_id="t-fill-1", recipient_id="T-RECIP")
            await s.commit()
        assert replay is None, "a replayed fill transferred a second time"

        async with sf() as s:
            book = await load_book(s)
        assert book.sleeves["T-DONOR"].actual == 42_000
        assert book.sleeves["T-RECIP"].actual == 8_000

        # A DISTINCT fill must still move — idempotency must not swallow real fills.
        async with sf() as s:
            second = await on_sell_fill(s, seller_id="T-DONOR", proceeds=4_000,
                                        fill_id="t-fill-2", recipient_id="T-RECIP")
            await s.commit()
        assert second is not None and second.amount == 4_000

        async with sf() as s:
            book = await load_book(s)
        assert book.sleeves["T-RECIP"].actual == 12_000
        await _clean(sf)

    asyncio.run(run())


@pytest.mark.needs_services
def test_capital_parked_in_UNALLOCATED_is_credited_not_dropped():
    """Conservation across the path it was failing on.

    A donor over target selling with a FULL recipient must still balance: the capital has left the
    strategies but not the account. Debiting the donor without crediting anywhere would shrink the book
    silently.
    """
    from api.budget_store import UNALLOCATED, load_book, on_sell_fill
    from api.db.models import StrategySleeve

    sf = _sf()

    async def run():
        await _clean(sf)
        async with sf() as s:
            s.add(StrategySleeve(strategy_id="T-DONOR", target=0, actual=50_000))
            s.add(StrategySleeve(strategy_id="T-FULL", target=20_000, actual=20_000))
            await s.commit()

        async with sf() as s:
            before = sum(x.actual for x in (await load_book(s)).sleeves.values())
            t = await on_sell_fill(s, seller_id="T-DONOR", proceeds=10_000,
                                   fill_id="t-fill-dup", recipient_id="T-FULL")
            await s.commit()
        assert t is not None and t.to_strategy == UNALLOCATED

        async with sf() as s:
            book = await load_book(s)
        after = sum(x.actual for x in book.sleeves.values())
        assert after == before, "capital vanished when parked in UNALLOCATED"
        assert book.sleeves[UNALLOCATED].actual == 10_000
        assert book.sleeves["T-FULL"].actual == 20_000, "a full sleeve received capital"
        await _clean(sf)

    asyncio.run(run())


# `test_setting_a_target_moves_no_capital` WAS HERE AND IS DELETED WITH THE FUNCTION IT TESTED (#463).
#
# It carried an xfail(strict=True) reading: "whoever fixes it — by deleting the dead function, or by
# making it write settings — must delete this marker and read why." Deleted, and read: `set_target`
# wrote `strategy_sleeve.target`, which has no readers, so the guarantee it asserted was one the
# system never provided. The function is gone and `test_one_writer_per_target.py` pins the invariant.
#
# WORTH RECORDING: THIS TRIPWIRE COULD NEVER HAVE FIRED. It is marked `needs_services`, and those
# tests cannot run in this environment — they error on collection and are deselected — so the strict
# marker was inert. Four hours earlier the identical device on QC345's sizing worked perfectly and
# forced the fix to be read, because that one was an ordinary test. A strict marker inside a test that
# never runs is a comment with extra steps.


def test_an_empty_fill_id_is_refused():
    """Offline. The idempotency key is not optional — accepting an empty one would silently produce a
    transfer that can be applied any number of times."""
    import asyncio as _asyncio

    from api.budget_store import on_sell_fill

    with pytest.raises(ValueError, match="fill_id"):
        _asyncio.run(on_sell_fill(None, seller_id="X", proceeds=1.0, fill_id="", recipient_id=None))


# -- the SEAM: does the engine actually call any of this ------------------------------------------
#
# The store passing proves nothing about wiring. That shape has shipped defects here repeatedly with a
# fully green suite, so these drive `_maybe_transfer_budget` — the real handler — rather than the
# store it delegates to.


def _filled(*, side: str = "SELL", qty: int = 100, px: float = 80.0,
            strategy_id: str = "MOMENTUM-002", trade_id: str = "T-1"):
    """A REAL `OrderFilled`, built through Nautilus's own stubs.

    The first version of this was a hand-rolled class carrying `order_side`, `last_qty` and
    `trade_id` — every field production reads. It still proved nothing, because production gates the
    whole path on `isinstance(event, OrderFilled)` and the double is not one, so it returned early and
    captured nothing. The test failed for a reason production would never hit.

    That is the drifted-double shape this repo keeps paying for, and the fix is the same every time:
    make the double the real type rather than loosen the check that rejected it.
    """
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import StrategyId, TradeId
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    instrument = TestInstrumentProvider.equity(symbol="AAPL")
    order = TestExecStubs.market_order(
        instrument=instrument,
        order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
        quantity=Quantity.from_int(qty),
    )
    return TestEventStubs.order_filled(
        order=order,
        instrument=instrument,
        strategy_id=StrategyId(strategy_id),
        trade_id=TradeId(trade_id) if trade_id else None,
        last_qty=Quantity.from_int(qty),
        last_px=Price.from_str(str(px)),
    )


def _drive(monkeypatch, event, *, recipient: str = ""):
    """Call the REAL `_maybe_transfer_budget` against a double, capturing what it would persist."""
    from api import engine_node

    # The recipient is a SETTING now (#323), so patch the resolver rather than the environment.
    from api import settings as _settings

    monkeypatch.setattr(_settings, "resolve",
                        lambda domain: {"TRANSFER_TO": recipient} if domain == "strategies" else {})

    captured: list = []

    class _Fake:
        _loop = object()          # non-None: production returns early without an event loop

        def _apply_budget_transfer(self, seller, proceeds, fill_id, recipient):
            captured.append((seller, proceeds, fill_id, recipient))

    # `run_coroutine_threadsafe` would need a real loop; the call itself is what is under test.
    monkeypatch.setattr(engine_node.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: captured or None)
    engine_node.UiFeedStrategy._maybe_transfer_budget(_Fake(), event)
    return captured


def test_budget_accounting_has_no_gate_and_records_unconditionally(monkeypatch):
    """Operator, 2026-08-15: "why does that even exist?"

    An earlier version hid this behind `KUMO_BUDGET_TRANSFER_ENABLED`, defaulting off, by reflex from
    the "new automation is opt-in" rule. That rule is about automation that ACTS on the book — this
    places no order. The gate guarded nothing and added a failure mode: with it off, `actual` never
    updates, so every sleeve number is silently STALE, and stale is worse than absent once anything
    reads it.

    Pinned so the flag is not reintroduced out of habit. The real bounds are `deployable()` and the
    lifecycle.
    """
    import os as _os

    monkeypatch.delenv("KUMO_BUDGET_TRANSFER_ENABLED", raising=False)
    assert _drive(monkeypatch, _filled(), recipient="QC345-003"), "accounting was gated off"
    assert "KUMO_BUDGET_TRANSFER_ENABLED" not in _os.environ


def test_a_sell_fill_reaches_the_budget_path_with_PROCEEDS(monkeypatch):
    """Proceeds, not basis: `actual` is a net asset value, so the sleeve keeps its own P&L."""
    got = _drive(monkeypatch, _filled(qty=100, px=80), recipient="QC345-003")
    assert got, "a sell fill never reached the budget path"
    seller, proceeds, fill_id, recipient = got[0]
    assert (seller, proceeds, fill_id, recipient) == ("MOMENTUM-002", 8_000.0, "T-1", "QC345-003")


def test_a_BUY_fill_transfers_nothing(monkeypatch):
    """A buy commits capital inside the sleeve; it frees none. Treating it as a transfer would drain a
    strategy every time it entered a position."""
    assert _drive(monkeypatch, _filled(side="BUY"), recipient="QC345-003") == []


def test_nautilus_always_supplies_a_trade_id_so_the_guard_is_defence_in_depth(monkeypatch):
    """MEASURED, not assumed: a real `OrderFilled` always carries a trade id.

    This test originally asserted that a fill with an EMPTY trade id is skipped — an input Nautilus
    will not produce. Building one through the real stubs with `trade_id=None` makes Nautilus GENERATE
    one (`E-20210410-...`), so the assertion was about a state that cannot occur.

    The guard in production stays, because a reconciliation-generated event is not bound by the same
    constructor. But what is pinned here is the fact that makes the idempotency key trustworthy in the
    ordinary case: the venue's own fill identity is always present, and it is per FILL rather than per
    order, so partial fills of one order do not collapse into a single transfer.
    """
    from nautilus_trader.model.events import OrderFilled

    event = _filled()
    assert isinstance(event, OrderFilled)
    assert str(event.trade_id), "a real fill arrived without a trade id"

    got = _drive(monkeypatch, event)
    assert got and got[0][2] == str(event.trade_id), "the venue trade id is not the idempotency key"


def test_no_recipient_configured_still_transfers_to_UNALLOCATED(monkeypatch):
    """Unset means capital returns to UNALLOCATED — visible in the account total, granted to nobody."""
    got = _drive(monkeypatch, _filled(), recipient="")
    assert got and got[0][3] is None


def test_the_ORDER_EVENT_HANDLER_reaches_the_budget_path(monkeypatch):
    """The call site, not the callee.

    Every test above calls `_maybe_transfer_budget` directly, so deleting the ONE LINE that invokes it
    from `_handle_order_event` left them all green — the wiring was untested by the section labelled
    "the seam". That is the shape this repo has shipped five production breaks with: the unit correct,
    the call site absent.

    So this drives `_handle_order_event` — what Nautilus actually calls — and asserts the budget path
    was reached.
    """
    from api import engine_node

    monkeypatch.setenv("KUMO_BUDGET_TRANSFER_ENABLED", "true")
    monkeypatch.setenv("KUMO_BUDGET_TRANSFER_RECIPIENT", "QC345-003")
    reached: list = []

    class _Cache:
        def order(self, coid):
            return None                       # no blotter frame; not what is under test

    class _Fake:
        _loop = object()
        _deny_reasons: dict = {}
        cache = _Cache()

        def _publish(self, *a, **k):
            pass

        def _publish_trades(self):
            pass

        def _maybe_transfer_budget(self, event):
            reached.append(event)

    engine_node.UiFeedStrategy._handle_order_event(_Fake(), _filled())
    assert reached, "_handle_order_event never reached the budget path"


def test_the_registry_declares_no_environment_gates():
    """Adding a strategy is code; ENABLING one must not be.

    An earlier registry carried an `enabled_env` per strategy, copying `KUMO_MOMENTUM_ENABLED`. That
    makes turning a strategy on a DEPLOY — edit a compose file, restart a container — with no route
    from the UI. Enablement is database state: the lifecycle machine plus the strategy's sleeve row.

    Asserted on the DATACLASS FIELDS and on whether the module reads the environment at all — not on
    the source text. The first version scanned for the string and failed on this module's own docstring,
    which explains why the field was removed. A test that cannot tell an explanation from a declaration
    is a test that will be deleted the next time someone documents something.
    """
    import dataclasses
    from pathlib import Path

    import api.strategy_registry as reg

    fields = {f.name for f in dataclasses.fields(reg.StrategyEntry)}
    assert "enabled_env" not in fields, f"the registry declares an environment gate again: {fields}"

    # The sharper property: this module must not consult the environment for ANYTHING. Enablement is
    # database state, and an env read here is how the pattern comes back under a different name.
    source = Path(reg.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#") and not line.strip().startswith("*")
    )
    assert "os.environ" not in code and "getenv" not in code, "the registry reads the environment"


def test_the_registry_refuses_a_duplicate_order_id_tag():
    """Nautilus rejects this at `Trader.add_strategy` — the node does not start. Catching it here names
    the conflict instead of turning a deploy into a crash-loop."""
    import pytest as _pytest

    from api.strategy_registry import RegistryError, StrategyEntry, validate

    clash = (
        StrategyEntry("MOMENTUM", "002", "momentum", "MOMENTUM", "x", "daily", "trail"),
        StrategyEntry("QC345", "002", "qc345", "QC345", "y", "monthly", "trail"),
    )
    with _pytest.raises(RegistryError, match="002"):
        validate(clash)


def test_the_registry_protects_tags_that_live_positions_are_keyed_to():
    """MANUAL-001 has live positions. Reassigning 001 re-homes them, because the NETTING position id is
    {instrument}-{strategy_id}."""
    import pytest as _pytest

    from api.strategy_registry import RegistryError, StrategyEntry, validate

    with _pytest.raises(RegistryError, match="MANUAL"):
        validate((StrategyEntry("QC345", "001", "qc345", "QC345", "z", "monthly", "trail"),))


def test_the_shipped_registry_is_valid_and_allocates_the_next_tag():
    from api.strategy_registry import REGISTRY, next_free_tag, validate

    validate()
    assert {e.strategy_id for e in REGISTRY} == {
        "MANUAL-001", "MOMENTUM-002", "QC345-003", "BCTROT-004", "TECHIVOL-005", "CRSISHORT-006",
        "SMHGLD-007",
    }
    # CRSISHORT holds 006 (#858), so the next free is 007. `next_free_tag` must skip a reserved tag
    # as well as a taken one — a tag nobody holds yet is not the same as a tag nobody has claimed.
    # 008: TECHIVOL-005, CRSISHORT-006 and SMHGLD-007 (#965) were allocated by this registry.
    assert next_free_tag() == "008", "the next strategy would be handed a taken tag"


def test_the_settings_schema_and_the_registry_declare_the_SAME_strategies():
    """Two declarations of one fact, so they will disagree — this is the detector.

    The `strategies` settings domain lists a target per strategy and the registry lists the strategies.
    Add a strategy to one and not the other and the failure is silent in the worst direction: the new
    strategy renders no budget field, so it can never be funded, and it looks like a strategy that is
    simply set to zero.
    """
    import json
    from pathlib import Path

    from api.strategy_registry import REGISTRY

    schema = json.loads(
        (Path(__file__).parent.parent / "config" / "settings" / "strategies.schema.json").read_text()
    )
    # Policy fields, not strategies. Still declared EXPLICITLY rather than by a naming convention —
    # a convention ("anything without a dash") would silently swallow a real strategy someone typo'd,
    # which is the failure this test exists to catch.
    #
    # But the list grew from one to four in two days, and a literal that only ever grows stops being
    # a decision and becomes a place to add things. So it is asserted to be EXHAUSTIVE below: every
    # schema key must be either a registered strategy or a named policy field, and a typo'd strategy
    # id is neither. That keeps the original guarantee while making an unreviewed addition fail.
    policy = {"TRANSFER_TO", "QC345_UNIVERSE", "QC345_ENABLED", "QC345_UNIVERSE_REFRESH",
              # TECHIVOL-005 (kumo-strategies#63). Same shape as QC345's pair: a gate and a
              # universe the strategy cannot derive live. Added to the literal rather than
              # matched by a `QC27_*` convention, because a convention would swallow a
              # typo'd strategy id, which is what this test exists to catch.
              "QC27_UNIVERSE", "QC27_ENABLED",
              # CRSISHORT-006 knobs (#858): gate, universe, breadth, and the explicit borrow-gate-off.
              "CRSI_ENABLED", "CRSI_UNIVERSE", "CRSI_MIN_WARM_SYMBOLS", "CRSI_BORROW_GATE_OFF",
              "CRSI_ACCEPT_EXTENDED_DAILY_BARS",
              "SMHGLD_ENABLED", "SMHGLD_SYMBOLS",
              # MOMENTUM/BCTROT operator overrides (#1054): the three knobs `momentum._live_overrides`
              # reads per prefix. Undeclared, the schema stripped them and the knob was dead.
              "MOMENTUM_N_HOLD", "MOMENTUM_BUFFER", "MOMENTUM_GIVE_BACK_FRAC", "MOMENTUM_TAKE_PROFIT_ATR",
              "BCTROT_N_HOLD", "BCTROT_BUFFER", "BCTROT_GIVE_BACK_FRAC", "BCTROT_TAKE_PROFIT_ATR",
              # #873 market-aware poller knobs: a dwell and a cadence for the platform's poll of every
              # registered lane — policy of the poller, not of any one strategy. Named literally, per
              # the rule above, so a typo'd strategy id cannot hide behind a MARKET_AWARE_ convention.
              "MARKET_AWARE_DWELL_POLLS", "MARKET_AWARE_POLL_SECS"}
    # Decision-slot fields (#360) are DERIVED from the registry, not added to the literal above. The
    # comment on `policy` is right that a list which only ever grows stops being a decision — so this
    # is a rule instead: `{strategy_id}_SLOTS` is a schedule field for a strategy that already has to
    # be registered. A typo'd `MOMENTUM-2_SLOTS` still fails, because it derives from no registered id.
    slot_fields = {f"{e.strategy_id}_SLOTS" for e in REGISTRY}
    declared = set(schema["properties"]) - policy - slot_fields
    registered = {e.strategy_id for e in REGISTRY}

    assert declared == registered, (
        f"settings schema and registry disagree — only in schema: {declared - registered}, "
        f"only in registry: {registered - declared}"
    )
    stray = policy - set(schema["properties"])
    assert not stray, f"policy fields listed here that the schema no longer has: {stray}"


def test_every_budget_field_is_a_non_negative_number():
    """A negative allocation is not a wind-down, it is nonsense, and the schema is the only thing
    standing between a typed minus sign and a sleeve that can never satisfy its own invariants."""
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).parent.parent / "config" / "settings" / "strategies.schema.json").read_text()
    )
    for name, spec in schema["properties"].items():
        if name == "TRANSFER_TO":
            assert spec["type"] == "string", "the transfer target must be a strategy id, not a number"
            continue
        if name in ("QC345_UNIVERSE", "QC27_UNIVERSE", "CRSI_UNIVERSE", "SMHGLD_SYMBOLS"):
            assert spec["type"] == "array", "the universe is a list of symbols, not a budget"
            # QC27's is DEFAULTED rather than empty: it cannot derive sector membership live, so an
            # empty pool makes the strategy refuse to register. QC345 refreshes its own, so [] is
            # right there. The difference is deliberate and asserted so neither drifts to the other.
            if name == "QC27_UNIVERSE":
                assert spec["default"], "QC27 cannot derive its universe live; [] makes it unbootable"
            continue
        if name.endswith(("_N_HOLD", "_BUFFER")):
            # Counts, not budgets (#1054): integers with a floor of 1 and NO default — the researched
            # value lives in strategies/momentum.py and a schema default would replace it silently.
            assert spec["type"] == "integer" and spec.get("minimum") == 1 and "default" not in spec, name
            continue
        if name.endswith("_TAKE_PROFIT_ATR"):
            # A target in ATR multiples (#1055): the lab's grid [3.0, 10.0] (never below 3.25; < 3 ATR
            # costs ~9pp), NO default — absent is OFF, today's behaviour.
            assert spec["type"] == "number" and spec.get("minimum") == 3.0 and spec.get("maximum") == 10.0, name
            assert "default" not in spec, name
            continue
        if name.endswith("_GIVE_BACK_FRAC"):
            # A fraction of the peak gain (#1054): (0, 1], NO default. 0 is refused rather than
            # accepted — kumo-strategies' exits.py:291 gates on `if cfg.give_back_frac:`, so a written
            # 0.0 would silently switch the exit OFF while the operator reads a value they set.
            assert spec["type"] == "number" and spec.get("exclusiveMinimum") == 0 and spec.get("maximum") == 1, name
            assert "default" not in spec, name
            continue
        if name == "CRSI_MIN_WARM_SYMBOLS":
            # A breadth, not a budget: an INTEGER with a floor of 1 (the adapter refuses < 1), and a
            # default the lane's own docstring calls a deployment decision — stated, never 0.
            assert spec["type"] == "integer" and spec.get("minimum") == 1 and spec["default"] >= 1
            continue
        if name in ("QC345_ENABLED", "QC345_UNIVERSE_REFRESH", "QC27_ENABLED", "CRSI_ENABLED",
                    "CRSI_BORROW_GATE_OFF", "CRSI_ACCEPT_EXTENDED_DAILY_BARS", "SMHGLD_ENABLED"):
            # Gates, and the DEFAULT is the assertion that matters: CLAUDE.md requires every new
            # automation flag to default False, and a gate that ships on is the one failure mode
            # nobody reports because everything appears to work.
            assert spec["type"] == "boolean", f"{name} is not a boolean gate"
            assert spec["default"] is False, f"{name} defaults ON"
            continue
        if name.startswith("MARKET_AWARE_"):
            # #873 poller knobs, not budgets: INTEGERS with a floor of 1 (a dwell of 0 would trigger on
            # nothing; a cadence of 0 would spin) and a stated default. Nothing here can place an order.
            assert spec["type"] == "integer" and spec.get("minimum", 0) >= 1 and spec["default"] >= 1, name
            continue
        if name.endswith("_SLOTS"):
            # A schedule, not a budget. The PATTERN is the assertion that matters: an unvalidated slot
            # is stored happily and then silently ignored at load, which presents as a setting that
            # does not work rather than as an error.
            assert spec["type"] == "array", f"{name} is not a list of slots"
            assert spec["items"].get("pattern"), f"{name} accepts any string as a decision time"
            assert spec["default"] == [], f"{name} defaults to a schedule nobody chose"
            continue
        assert spec["type"] == "number", f"{name} is not a number"
        assert spec.get("minimum") == 0, f"{name} allows a negative budget"


def test_targets_come_from_settings_not_from_the_sleeve_table():
    """One writer each: settings owns intent, the table owns reality.

    The `strategy_sleeve.target` column is vestigial. Reading it again would give two sources for one
    number, and the one the UI edits would not be the one the engine obeys.
    """
    import inspect

    from api import budget_store

    source = inspect.getsource(budget_store.load_book)
    assert "_targets()" in source, "load_book no longer reads targets from settings"
    assert "r.target" not in source, "load_book reads the vestigial table column again"


def test_a_RESERVED_tag_cannot_be_taken_even_though_nobody_holds_it():
    """The mistake this table exists to prevent, and which I made anyway.

    QC345 was assigned tag 003 by taking the next free NUMBER. kumo-strategies#32 is titled
    "BCTROT-003", so the tag was already claimed by a strategy that simply had not been built yet — and
    "not built yet" looks identical to "free" if you only count what is registered here.

    It cost nothing only because QC345 had never registered or filled. After one position the id is
    permanent: the NETTING position id is {instrument}-{strategy_id}, so changing it re-homes
    everything it owns.
    """
    import pytest as _pytest

    from api.strategy_registry import RegistryError, StrategyEntry, validate

    with _pytest.raises(RegistryError, match="QC345"):
        validate((StrategyEntry("SOMETHING", "003", "x", "SOMETHING", "y", "daily", "trail"),))


def test_every_registered_strategy_has_a_settings_domain_that_EXISTS():
    """The gap the operator asked about: the registry claimed `momentum` and no such domain existed, so every
    MOMENTUM parameter was invisible to the operator while the registry asserted otherwise.

    A declared domain that does not resolve is worse than none — it reads as configured. So does a
    domain that resolves but which the strategy never consults: MOMENTUM's `live_config()` hardcodes
    its parameters, so a `momentum` domain would have shown an operator the DATACLASS defaults
    (`give_back_frac: null`, `n_hold: 5`) while the live strategy ran 0.5 and 8.

    The invariant is therefore two-sided: declare a domain and it must resolve; have no domain and
    declare none. An empty string is the honest way to say "not configurable yet".
    """
    from api.settings import store
    from api.strategy_registry import REGISTRY

    available = set(store.domains())
    broken = [e.strategy_id for e in REGISTRY
              if e.settings_domain and e.settings_domain not in available
              and e.settings_domain != "manual"]
    assert broken == [], f"strategies declaring a settings domain that does not resolve: {broken}"


def test_MOMENTUM_declares_no_settings_domain_while_its_config_is_hardcoded():
    """Pinned so the domain is not re-added out of tidiness.

    `strategies/momentum.py::live_config()` returns a hand-built `MomentumRotationConfig` and consults
    nothing. Until it READS settings, any domain here misrepresents a strategy holding real positions.
    """
    import inspect

    from api.strategy_registry import by_id
    from strategies import momentum

    assert by_id("MOMENTUM-002").settings_domain == ""
    source = inspect.getsource(momentum.live_config)
    assert "resolve(" not in source, (
        "live_config now reads settings — give MOMENTUM a domain and delete this test"
    )


def test_assign_tag_is_idempotent_for_an_already_registered_strategy():
    """The id is PERMANENT once anything trades under it — the NETTING position id is
    {instrument}-{strategy_id}, so a changed tag re-homes every position it owns. Asking twice must
    never produce two answers."""
    from api.strategy_registry import assign_tag

    assert assign_tag("MOMENTUM") == "002"
    assert assign_tag("MOMENTUM") == assign_tag("MOMENTUM")
    assert assign_tag("QC345") == "003"


def test_assign_tag_honours_a_name_reserved_before_it_was_registered():
    """BCTROT is named in kumo-strategies#32 and not yet built here. Asking for its tag must return the
    reserved one rather than allocating a fresh one — otherwise the published name and the assigned tag
    disagree the day it is built."""
    from api.strategy_registry import assign_tag

    # BCTROT is registered now, so this is the idempotent path rather than an allocation. Its adapter
    # DEFAULTS to 003 — the tag QC345 holds — so cockpit passing 004 explicitly is what keeps the node
    # bootable. Two adapters defaulting to the same number is exactly the collision neither repo can
    # see alone.
    assert assign_tag("BCTROT") == "004"


def test_assign_tag_gives_a_genuinely_new_strategy_the_next_free_one():
    from api.strategy_registry import assign_tag

    # 008, not 007: TECHIVOL-005, CRSISHORT-006 and SMHGLD-007 (#965) were allocated by this registry.
    assert assign_tag("SOMETHING-NEW") == "008"


def test_BCTROT_and_QC345_do_not_share_a_tag_despite_both_adapters_defaulting_to_003():
    """The collision the ownership rule exists for, now a real one rather than a hypothetical.

    QC345's adapter hardcoded 003. BCTROT's adapter DEFAULTS to 003. Neither repository could see the
    other's choice, and `register_external_order_claims`/`add_strategy` does not degrade on a duplicate
    tag — the node does not boot.

    Cockpit allocates: QC345 keeps 003, BCTROT gets 004, and both are passed explicitly at construction.
    """
    from api.strategy_registry import REGISTRY

    tags = [e.tag for e in REGISTRY]
    assert len(tags) == len(set(tags)), f"duplicate order_id_tag in the registry: {tags}"

    from api.strategy_registry import by_external_id

    assert by_external_id("QC345").tag == "003"
    assert by_external_id("BCTROT").tag == "004"
