"""Pure unit tests for TradeCycleProjection (#73) that need no engine. The full native fold (cycle identity,
state machine, per-cycle P&L partitioning) is proven against a real BacktestEngine in test_projection_replay.py
(marked `engine`, run separately)."""

from __future__ import annotations

from nautilus_trader.model.identifiers import ClientId

from api.strategy_ids import MANUAL
from api.trade_cycle import TradeCycleProjection


class _EmptyCache:
    """Minimal cache stub: no account yet, nothing in play."""

    def accounts(self):
        return []

    def positions_open(self):
        return []

    def orders_open(self):
        return []


def test_no_mint_before_account_resolves():
    # If project() runs before the native account is populated, it must NOT mint a cycle keyed on an empty
    # account_id — that entry would be re-minted under a new key (and a new cycle_id) once the account resolves,
    # stranding the live cycle. So: emit nothing until the account is known. (codex-flagged bug.)
    proj = TradeCycleProjection(ClientId("SIM"), MANUAL)
    assert proj.project(_EmptyCache()) == []
    # And the fold stays clean — nothing was tracked.
    assert not proj._active


# --- #289: a protective stop protects the SHARES, whoever placed it --------------------------------


class _Order:
    """Enough of a Nautilus Order for `_working_orders` and `_order_dto`."""

    def __init__(self, *, instrument_id, strategy_id, coid, order_type="TRAILING_STOP_MARKET",
                 side="SELL", reduce_only=False, quantity=100.0, tags=None):
        from types import SimpleNamespace
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.client_order_id = SimpleNamespace(value=coid)
        self.order_type = SimpleNamespace(name=order_type)
        self.side = SimpleNamespace(name=side)
        self.status = SimpleNamespace(name="ACCEPTED")
        self.is_reduce_only = reduce_only
        self.quantity = quantity
        self.leaves_qty = quantity
        self.time_in_force = SimpleNamespace(name="GTC")
        self.price = None
        self.trigger_price = None
        self.is_open = True
        # `None` is what Nautilus actually gives an untagged order — not `[]`. A double that answered
        # `[]` could not represent the DTO's own coercion, which is the hop being tested below.
        self.tags = tags
        self.has_price = False
        self.has_trigger_price = False
        self.ts_last = 1
        self.ts_init = 1
        self.filled_qty = 0.0


def test_a_protective_stop_placed_by_ANOTHER_strategy_still_counts_as_protection():
    """#289, observed on the paper book 2026-08-14: the BOOK tile reported 5 of 8 unprotected when 6 of 8
    had a working trailing stop at the venue.

    `_working_orders` filters by `strategy_id`, so a MOMENTUM-002 position could not see a stop carrying
    MANUAL-001. Every #239 backstop order carries MANUAL-001, because the reconciler places them through
    the display strategy — so all three MOMENTUM positions read as naked, plus the two genuinely naked
    ones, giving exactly the 5 that was reported.

    The broker does not know about our sleeves. A stop resting on the instrument protects those shares
    whoever claims them — the same rule `protection.py` states for the backstop's own audit. Attribution
    is a separate question from "is this position covered", and answering the second with the first is
    how a covered position gets displayed as naked on a SAFETY tile.
    """
    proj = TradeCycleProjection(ClientId("SIM"), "MOMENTUM-002")
    foreign = _Order(instrument_id="AEM.XNYS", strategy_id="MANUAL-001", coid="PROT-SELL-AEM-XNYS-abc")

    class _Cache:
        def orders_open(self):
            return [foreign]

    covering = proj._protective_orders_on_instrument(_Cache(), "AEM.XNYS")
    assert [o.client_order_id.value for o in covering] == ["PROT-SELL-AEM-XNYS-abc"]


def test_an_ENTRY_order_from_another_strategy_is_NOT_adopted():
    """The counter-case, and why `_working_orders` keeps its strategy filter.

    Note the doubles carry `reduce_only=False`, matching what the #239 backstop actually submits. The
    first version of this fix filtered on `is_reduce_only` and adopted nothing, while the test passed
    because its double set a flag production does not.

    Cycle lifecycle uses `working` to decide whether an entry order bridges a flat. Widening THAT to every
    strategy would let one sleeve's buy keep another sleeve's cycle alive — a cross-strategy identity bug
    of exactly the kind #273 and the NETTING position-id design exist to prevent. Only reduce-only
    protective orders are shared, and only for the coverage question.
    """
    proj = TradeCycleProjection(ClientId("SIM"), "MOMENTUM-002")
    entry = _Order(instrument_id="AEM.XNYS", strategy_id="MANUAL-001", coid="ENTRY-1",
                   order_type="MARKET", side="BUY", reduce_only=False)

    class _Cache:
        def orders_open(self):
            return [entry]

    assert proj._protective_orders_on_instrument(_Cache(), "AEM.XNYS") == []
    assert proj._working_orders(_Cache(), "AEM.XNYS") == []


def test_the_DTO_actually_carries_a_foreign_strategys_protective_order():
    """#289 — the SEAM, not the helper.

    The first version of this fix called `_protective_orders_on_instrument(cache, ...)` from `_build_dto`,
    which never receives `cache`. Every unit test passed, because they all called the helper directly. In
    production it raised `NameError: name 'cache' is not defined` on every tick, the whole trade-cycle
    projection was skipped, and the UI showed an EMPTY BOOK while eight positions were held at the broker.

    Tested the helper, not the seam — the same failure as the Cython Quantity mismatch and the attach-path
    scaling. This exercises `project()` end to end so the wiring cannot be wrong again.
    """
    from types import SimpleNamespace

    proj = TradeCycleProjection(ClientId("SIM"), "MOMENTUM-002")
    foreign = _Order(instrument_id="AEM.XNYS", strategy_id="MANUAL-001", coid="PROT-SELL-AEM-XNYS-abc")

    class _Pos:
        instrument_id = "AEM.XNYS"
        strategy_id = "MOMENTUM-002"
        id = SimpleNamespace(value="AEM.XNYS-MOMENTUM-002")
        is_open = True
        side = SimpleNamespace(name="LONG")
        quantity = 54.0
        avg_px_open = 175.0
        realized_pnl = None
        ts_opened = 1
        ts_closed = None
        ts_last = 1
        events = []
        closing_order_id = None
        opening_order_id = None

    class _Cache:
        def accounts(self):
            return [SimpleNamespace(id=SimpleNamespace(value="ALPACA-1"))]

        def positions_open(self):
            return [_Pos()]

        def orders_open(self):
            return [foreign]

        def position(self, pos_id):
            return _Pos()

        def position_snapshots(self, pos_id):
            return []

    dtos = proj.project(_Cache(), now_ns=1)
    assert dtos, "projection produced nothing — the wiring is broken, which is invisible to helper tests"
    coids = [w.client_order_id for d in dtos for w in d.working_orders]
    assert "PROT-SELL-AEM-XNYS-abc" in coids


def test_a_protective_stop_alone_does_NOT_mint_a_cycle_for_the_strategy_that_placed_it():
    """A second regression from #239, seen on the live book 2026-08-14.

    The backstop submits through the display strategy, so every `PROT-` stop carries MANUAL-001 — even
    when it protects a MOMENTUM-002 position. `_instruments_in_play` treats ANY open order as putting the
    instrument in play, so MANUAL-001 minted a phantom ARMED cycle with qty 0 on seven MOMENTUM holdings:

        BDX  BETA  CGAU  FSM  VCTR  WHD  WPM

    Each rendered as its own portfolio row reading `flat · 0 held · 1 armed`, doubling those names in the
    list and claiming an armed manager that does not exist — only three managers were live.

    A protective stop is not a claim on the position. It says "these shares have cover", not "this
    strategy has a cycle here". An ENTRY order is what opens a cycle.
    """
    from types import SimpleNamespace

    proj = TradeCycleProjection(ClientId("SIM"), "MANUAL-001")
    backstop = _Order(instrument_id="WPM.XNYS", strategy_id="MANUAL-001",
                      coid="PROT-SELL-WPM-XNYS-abc123")

    class _Cache:
        def accounts(self):
            return [SimpleNamespace(id=SimpleNamespace(value="ALPACA-1"))]

        def positions_open(self):
            return []          # MANUAL-001 holds nothing here — MOMENTUM-002 does

        def orders_open(self):
            return [backstop]

    assert "WPM.XNYS" not in proj._instruments_in_play(_Cache())
    assert proj.project(_Cache(), now_ns=1) == [], "a phantom cycle was minted from a protective stop"


def test_an_ENTRY_order_STILL_puts_an_instrument_in_play():
    """The counter-case. A resting entry order with no position yet is exactly the ARMED state the
    projection exists to show — narrowing this must not lose it."""

    proj = TradeCycleProjection(ClientId("SIM"), "MANUAL-001")
    entry = _Order(instrument_id="WPM.XNYS", strategy_id="MANUAL-001", coid="entry-1",
                   order_type="LIMIT", side="BUY", reduce_only=False)

    class _Cache:
        def positions_open(self):
            return []

        def orders_open(self):
            return [entry]

    assert "WPM.XNYS" in proj._instruments_in_play(_Cache())


def test_the_order_DTO_carries_the_orders_own_tags():
    """#872: the UI labels an entry-floor stop from `mode:entry_floor` on the order. A field the model
    declares and the projection never fills is a field the UI reads as absent forever — the same shape
    as #233, #322 and #336, three times over.

    THROUGH `_order_dto`, the real projection hop, not by constructing a `WorkingOrderDTO` by hand: the
    model would happily accept the tag from a test that proves nothing about whether anything sets it.
    """
    from api.trade_cycle import TradeCycleProjection

    floor = _Order(instrument_id="AMAT.XNAS", strategy_id=MANUAL, coid="PROT-SELL-AMAT-XNAS-abc",
                   order_type="STOP_MARKET", tags=["mode:entry_floor"])

    assert TradeCycleProjection._order_dto(floor).tags == ["mode:entry_floor"]
    # An untagged order carries an EMPTY list, never null — "no tags" is a fact the UI can read.
    untagged = _Order(instrument_id="AMAT.XNAS", strategy_id=MANUAL, coid="c-2")
    assert TradeCycleProjection._order_dto(untagged).tags == []
