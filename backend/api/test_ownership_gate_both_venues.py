"""The exit-ownership guard runs on EVERY exec client and knows the lane's SIDE (#1030).

MEASURED on the deployed paper image (cockpit 021c9c4, 2026-09-12 04:10Z), driving the real functions
inside `kumo-paper-api-1`:

    CRSISHORT-006  held=   +0 SELL  10: ok=False  the lane holds nothing   <- a PERMITTED short entry
    CRSISHORT-006  held=  -10 BUY   20: ok=True   reduces                  <- flips the lane +10 LONG
    ibkr.py:               classify_exit refs=0   SHORT_PERMITTED refs=0
    alpaca/exec_client.py: classify_exit refs=3   SHORT_PERMITTED refs=0
    gated_exec.py:         exports: install_budget_gate only

One rule, one venue, one side. #748's guard was written inline in the Alpaca client with the long-only
question "would this leave the lane below zero", and never consulted `ownership.SHORT_PERMITTED`. So an
Alpaca instance refuses the short lane's every entry, and the IBKR instance — where the short lane
actually trades, and where the operator trades by hand — has no guard for the WHD/CGAU shape at all.

THE SHAPE IS #782 AGAIN. The budget gate had exactly one call site, in the Alpaca client, until
`gated_exec.install_budget_gate` moved it to a wrap installed on BOTH factories and
`test_every_exec_client_is_gated` pinned the PROPERTY. This file does the same for ownership:

  * the rule is side-aware, and the side comes from ONE place (`api.ownership`), read at call time;
  * the guard is a wrap beside the budget gate, installed by every factory this repo hands Nautilus;
  * ownership runs BEFORE budget, and installing them the other way round RAISES;
  * a refusal is `OrderDenied` (refused before the venue), not `OrderRejected` (the venue's word);
  * protective stops are skipped by IDENTITY, not by `is_reduce_only` — the flag-based skip made the
    guard blind to every kumo-trading-strategies lane exit (all submitted `reduce_only=is_exit`), and on
    1.229.0 neither Nautilus check covers them pre-trade (risk engine needs `position_id`; execution
    engine checks post-fill). Verified against the installed package, not the docs.

Every test here was seen RED against 021c9c4 before the fix existed (22 of 29 in the first cut); the
ones marked FIXTURE assert the double can express the bug first, so a green run cannot be the double's
silence. Coverage and scope reviewed cross-repo by the kumo-trading-strategies session (codex out until
2026-09-15); its findings are the second half of this file.
"""

from __future__ import annotations

import asyncio
import types

import pytest


# ==================================================================================================
# Doubles — built from what production emits, and made to REJECT what production rejects
# ==================================================================================================
class _Log:
    """Records (level, message). The level is part of the contract: a refusal at WARNING is one an
    operator's filter never shows."""

    def __init__(self):
        self.records = []

    def warning(self, msg, *a):
        self.records.append(("warning", str(msg)))

    def info(self, msg, *a):
        self.records.append(("info", str(msg)))

    def error(self, msg, *a):
        self.records.append(("error", str(msg)))

    def debug(self, msg, *a):
        self.records.append(("debug", str(msg)))

    exception = error

    @property
    def lines(self):
        return [m for _, m in self.records]

    def at(self, level):
        return [m for l, m in self.records if l == level]


class _Clock:
    def timestamp_ns(self):
        return 1_000


def _order(*, lane="MOMENTUM-002", side="SELL", qty=10, iid="WHD.XNYS", reduce_only=False,
           coid=None, is_closed=False):
    return types.SimpleNamespace(
        strategy_id=lane, instrument_id=iid,
        client_order_id=coid or f"coid-{lane}-{side}-{qty}",
        quantity=qty, side=types.SimpleNamespace(name=side), price=100.0,
        is_reduce_only=reduce_only, is_closed=is_closed,
    )


class _Cache:
    """`positions_open(strategy_id=, instrument_id=)` is Nautilus's own per-strategy book — the SAME
    call on any venue, which is what lets one guard serve both. `None` means UNREADABLE (distinct from
    an empty list, which means flat); the real cache never returns None, but the guard must not
    collapse the two, so the double can produce it. `held` may be a list, for a lane holding one
    instrument as several positions. The kwargs are RECORDED: a wrap that reads the whole book
    (`positions_open()`) would be green against a one-position double and wrong on a live stack."""

    def __init__(self, held):
        self._held = held
        self.asked = []

    def positions_open(self, strategy_id=None, instrument_id=None):
        self.asked.append((strategy_id, instrument_id))
        if self._held is None:
            return None
        qtys = self._held if isinstance(self._held, list) else [self._held]
        return [types.SimpleNamespace(signed_qty=q) for q in qtys if q]

    def quote_tick(self, iid):
        return None

    def trade_tick(self, iid):
        return None


class _VendorClient:
    """A client this repo did not write. `_submit_order` IS the venue."""

    def __init__(self, held=0.0):
        self._cache = _Cache(held)
        self._log = _Log()
        self._clock = _Clock()
        self.reached_venue = []
        self.rejected = []
        self.denied = []

    async def _submit_order(self, command):
        order = getattr(command, "order", None)
        self.reached_venue.append(order.client_order_id if order is not None else "<no order>")

    def generate_order_rejected(self, **kw):
        self.rejected.append(kw)

    def generate_order_denied(self, **kw):
        self.denied.append(kw)


def _submit(client, order):
    asyncio.run(client._submit_order(types.SimpleNamespace(order=order)))
    return client


def _gate(client):
    from api.providers.gated_exec import install_ownership_gate
    return install_ownership_gate(client)


def _ce(**kw):
    from api.exit_ownership import classify_exit
    base = dict(side="SELL", quantity=10.0, lane="CRSISHORT-006", instrument_id="X.XNAS",
                lane_signed_qty=0.0)
    return classify_exit(**{**base, **kw})


# ==================================================================================================
# FIXTURE PROPERTIES FIRST
# ==================================================================================================
def test_FIXTURE_the_bare_double_reaches_the_venue_with_a_flat_lane_SELL():
    """Without the gate a SELL from a flat lane goes straight to the venue — the WHD shape. If the
    double could not do this, every refusal assertion below would pass with the gate removed."""
    c = _submit(_VendorClient(held=0.0), _order(lane="MOMENTUM-002", side="SELL"))
    assert c.reached_venue == ["coid-MOMENTUM-002-SELL-10"]
    assert c.denied == [] and c.rejected == []


def test_FIXTURE_the_short_lane_is_declared_in_ONE_place():
    """The side is not an assumption of this file. `api.ownership.SHORT_PERMITTED` names the short lane
    and only it; the rule below must read THAT, not a list of its own."""
    from api.ownership import SHORT_PERMITTED
    assert "CRSISHORT-006" in SHORT_PERMITTED
    assert "MOMENTUM-002" not in SHORT_PERMITTED


def test_FIXTURE_the_double_cache_records_what_it_was_asked():
    c = _Cache(0.0)
    c.positions_open(strategy_id="L", instrument_id="I")
    assert c.asked == [("L", "I")]


# ==================================================================================================
# THE RULE: side-aware, from the declared side
# ==================================================================================================
def test_the_lane_side_is_DERIVED_from_ownership_not_declared_twice():
    from api.ownership import lane_side
    assert lane_side("CRSISHORT-006") == "SHORT"
    for lane in ("MOMENTUM-002", "BCTROT-004", "QC345-003", "TECHIVOL-005", "SMHGLD-007", "MANUAL-001"):
        assert lane_side(lane) == "LONG", lane


def test_an_UNKNOWN_lane_is_LONG__the_direction_that_refuses_shorts():
    """A new short lane nobody added to SHORT_PERMITTED cannot open a position until someone does,
    and finds out at its first order rather than its first fill. Fail-safe, and said so."""
    from api.ownership import lane_side
    assert lane_side("PENNYGAP-008") == "LONG"
    assert lane_side("") == "LONG"


def test_the_side_is_read_at_CALL_time_from_the_ONE_set(monkeypatch):
    """Asserting VALUES would let `classify_exit` hardcode `{"CRSISHORT-006"}` and stay green.
    Move the permission and both `lane_side` and the rule must follow — proving each reads the set
    at call time (import-per-call, like the budget gate), not a copy bound at import."""
    import api.ownership as own
    monkeypatch.setattr(own, "SHORT_PERMITTED", frozenset({"MOMENTUM-002"}))
    assert own.lane_side("MOMENTUM-002") == "SHORT"
    assert own.lane_side("CRSISHORT-006") == "LONG"
    assert _ce(lane="MOMENTUM-002", side="SELL", lane_signed_qty=0.0).ok
    assert not _ce(lane="CRSISHORT-006", side="SELL", lane_signed_qty=0.0).ok


def test_a_Nautilus_StrategyId_OBJECT_reads_the_same_side_as_its_string():
    """The set holds strings; the order carries a `StrategyId`. Compared as an object it is never in
    the set, reads LONG, and the short lane is refused again — the defect one type away."""
    from nautilus_trader.model.identifiers import StrategyId

    from api.ownership import lane_side
    assert lane_side(StrategyId("CRSISHORT-006")) == "SHORT"
    assert lane_side(StrategyId("MOMENTUM-002")) == "LONG"


def test_the_rule_reads_the_side_from_OWNERSHIP_not_from_a_kwarg_the_caller_could_forget():
    """Two derivations of one fact will disagree. If the classifier took `lane_side=` and defaulted it
    to LONG, one call site forgetting the kwarg would refuse the short lane again — the exact defect,
    one level in. So the classifier asks `ownership.lane_side(lane)` itself."""
    import inspect

    from api.exit_ownership import classify_exit
    params = inspect.signature(classify_exit).parameters
    assert "lane_side" not in params and "position_side" not in params


def test_a_SHORT_lane_may_SELL_from_flat__that_is_its_entry():
    """The measured refusal. `CRSISHORT-006 held=+0 SELL 10: ok=False the lane holds nothing` on the
    deployed image — a permitted short's every entry refused at the exec client."""
    v = _ce(side="SELL", lane_signed_qty=0.0)
    assert v.ok, v


def test_a_SHORT_lane_may_SELL_to_ADD_to_its_short():
    assert _ce(side="SELL", quantity=5, lane_signed_qty=-10.0).ok


def test_a_SHORT_lane_may_BUY_to_cover_EXACTLY_and_PARTIALLY():
    assert _ce(side="BUY", quantity=10, lane_signed_qty=-10.0).ok
    assert _ce(side="BUY", quantity=3, lane_signed_qty=-10.0).ok


@pytest.mark.parametrize("eps", [1e-12, -1e-12])
def test_venue_ROUNDING_does_not_make_a_SHORT_full_cover_look_oversize(eps):
    """`test_exit_ownership.py` pins this for the long side only; the short mirror had no test."""
    assert _ce(side="BUY", quantity=10 + eps, lane_signed_qty=-10.0).ok


def test_a_SHORT_lane_cover_that_OVERSHOOTS_is_refused_as_OVERSIZE_and_names_the_excess():
    """The mirror defect: `held=-10 BUY 20: ok=True reduces` on the deployed image. It flips the lane
    +10 LONG, which for the short lane is the WHD shape with the sign reversed."""
    from api.exit_ownership import OVERSIZE
    v = _ce(side="BUY", quantity=20, lane_signed_qty=-10.0)
    assert not v.ok
    assert v.reason == OVERSIZE
    assert "10" in v.detail, "the excess is not named"
    assert "LONG" in v.detail and "long-only" not in v.detail, (
        "the verdict still describes a long-only book to a short lane")


def test_a_SHORT_lane_BUY_from_FLAT_is_refused_as_NO_POSITION():
    """A BUY on a short lane holding nothing does not cover anything — it opens a LONG on the short
    lane, exactly as a SELL on a flat long lane opens a short."""
    from api.exit_ownership import NO_POSITION
    v = _ce(side="BUY", quantity=10, lane_signed_qty=0.0)
    assert not v.ok
    assert v.reason == NO_POSITION
    assert "LONG" in v.detail and "long-only" not in v.detail


def test_a_SHORT_lane_holding_the_WRONG_SIDE_may_not_add_to_it():
    from api.exit_ownership import WRONG_SIDE
    v = _ce(side="BUY", quantity=10, lane_signed_qty=+10.0)
    assert not v.ok
    assert v.reason == WRONG_SIDE


def test_a_SHORT_lane_holding_the_WRONG_SIDE_may_SELL_it_off__whole_or_through():
    """+10 on the short lane is a corrupted split. Selling it flat REPAIRS the split; selling through
    it (SELL 15 → −5) lands on the declared side. Both allowed."""
    assert _ce(side="SELL", quantity=10, lane_signed_qty=+10.0).ok
    assert _ce(side="SELL", quantity=15, lane_signed_qty=+10.0).ok


def test_a_PARTIAL_repair_of_the_WRONG_SIDE_is_refused__by_design_on_both_sides():
    """Long rule today: held −28, BUY 1 → −27, still short → WRONG_SIDE. The short mirror: held +10,
    SELL 3 → +7, still long → WRONG_SIDE. A partial repair leaves the lane holding the side it must
    not; only flat-or-through is accepted. Deliberate and symmetric."""
    from api.exit_ownership import WRONG_SIDE
    assert _ce(side="SELL", quantity=3, lane_signed_qty=+10.0).reason == WRONG_SIDE
    assert _ce(side="BUY", quantity=1, lane_signed_qty=-28.0, lane="MOMENTUM-002").reason == WRONG_SIDE


def test_a_NEGATIVE_quantity_input_is_read_as_its_magnitude_on_both_sides():
    """`abs()` at the top of the rule; unpinned before on either side."""
    assert _ce(side="BUY", quantity=-10, lane_signed_qty=-10.0).ok
    assert not _ce(side="BUY", quantity=-20, lane_signed_qty=-10.0).ok
    assert _ce(side="SELL", quantity=-28, lane_signed_qty=28.0, lane="MOMENTUM-002").ok


def test_the_LONG_rule_is_UNCHANGED_by_the_side_parameter():
    """Every verdict `test_exit_ownership.py` pins for a long lane must survive, or the fix for the
    short lane has reopened #748 for the four long ones."""
    from api.exit_ownership import NO_POSITION, OVERSIZE, WRONG_SIDE
    L = dict(lane="MOMENTUM-002")
    assert _ce(side="BUY", quantity=9, lane_signed_qty=0.0, **L).ok
    assert _ce(side="BUY", quantity=60, lane_signed_qty=59.0, **L).ok
    assert _ce(side="SELL", quantity=28, lane_signed_qty=28.0, **L).ok
    assert _ce(side="SELL", quantity=28, lane_signed_qty=0.0, **L).reason == NO_POSITION
    assert _ce(side="SELL", quantity=90, lane_signed_qty=88.0, **L).reason == OVERSIZE
    assert _ce(side="SELL", quantity=1, lane_signed_qty=-28.0, **L).reason == WRONG_SIDE


def test_UNREADABLE_still_fails_OPEN_on_a_short_lane():
    v = _ce(side="SELL", lane_signed_qty=None)
    assert v.ok and v.reason == "unreadable"


# ==================================================================================================
# THE WRAP: one guard, installed like the budget gate
# ==================================================================================================
def test_a_flat_LONG_lane_SELL_is_DENIED_and_never_reaches_the_venue():
    c = _submit(_gate(_VendorClient(held=0.0)), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert c.reached_venue == [], "the WHD shape reached the venue"
    assert len(c.denied) == 1 and c.rejected == [], "one refusal, as DENIED — the venue was not asked"
    d = c.denied[0]
    assert d["client_order_id"] == "coid-MOMENTUM-002-SELL-28"
    assert d["reason"].startswith("the lane holds nothing"), d["reason"]
    assert d["ts_event"] == 1_000
    assert any("OWNERSHIP REFUSED" in m for m in c._log.at("error")), c._log.records


def test_a_flat_SHORT_lane_SELL_REACHES_the_venue():
    """The whole point for paper."""
    c = _submit(_gate(_VendorClient(held=0.0)), _order(lane="CRSISHORT-006", side="SELL", qty=10))
    assert c.reached_venue == ["coid-CRSISHORT-006-SELL-10"]
    assert c.denied == [] and c.rejected == []


def test_a_Nautilus_StrategyId_on_the_ORDER_reaches_the_venue_for_the_short_lane():
    """The real order carries a `StrategyId` object, not a str."""
    from nautilus_trader.model.identifiers import StrategyId
    c = _submit(_gate(_VendorClient(held=0.0)),
                _order(lane=StrategyId("CRSISHORT-006"), side="SELL", qty=10, coid="c-1"))
    assert c.reached_venue == ["c-1"], c._log.records


def test_a_SHORT_lane_cover_that_OVERSHOOTS_is_DENIED_at_the_wrap():
    c = _submit(_gate(_VendorClient(held=-10.0)), _order(lane="CRSISHORT-006", side="BUY", qty=20))
    assert c.reached_venue == [] and len(c.denied) == 1


def test_the_holding_is_SUMMED_over_the_lane_s_positions_in_the_instrument():
    """A lane may hold one instrument as several positions. The inline guard summed; a wrap reading
    `positions[0]` would be green against a one-position double."""
    ok = _submit(_gate(_VendorClient(held=[-6.0, -4.0])), _order(lane="CRSISHORT-006", side="BUY", qty=10))
    assert ok.reached_venue and not ok.denied
    bad = _submit(_gate(_VendorClient(held=[-6.0, -4.0])), _order(lane="CRSISHORT-006", side="BUY", qty=11))
    assert bad.denied and not bad.reached_venue


def test_the_cache_is_asked_for_THIS_lane_and_THIS_instrument():
    """A wrap calling `positions_open()` reads the whole book and answers for every lane at once —
    green here with a one-lane double, wrong on a stack with five."""
    c = _submit(_gate(_VendorClient(held=28.0)), _order(lane="MOMENTUM-002", side="SELL", qty=28, iid="WHD.XNYS"))
    assert c._cache.asked == [("MOMENTUM-002", "WHD.XNYS")]


def test_a_reduce_only_LANE_EXIT_IS_asked__the_flag_is_not_a_pass():
    """THE REVIEW FINDING THAT CHANGED THE SCOPE. kumo-trading-strategies submits every lane exit and cover
    `reduce_only=is_exit` (`runtime/nautilus/broker.py:200`). #748's guard skipped every reduce-only
    order on the reasoning that Nautilus refuses one that would open a netting position — measured on
    1.229.0: the risk engine's check needs a `position_id` the strategy never passes
    (`risk/engine.pyx:424`), the execution engine's is POST-FILL (`execution/engine.pyx:1701`), and
    IB drops the flag at the adapter. A flag-based skip made the guard blind to exactly the orders it
    was written for."""
    c = _submit(_gate(_VendorClient(held=0.0)),
                _order(lane="MOMENTUM-002", side="SELL", qty=28, reduce_only=True))
    assert c.reached_venue == [] and len(c.denied) == 1


def test_a_PROTECTIVE_STOP_is_not_asked__by_IDENTITY():
    """What the old skip was actually protecting: cockpit's own stops rest before the entry's position
    appears in this cache read. That is a matter of WHICH order (`PROTECTION_COID_PREFIX`), not of a
    flag any order can carry."""
    from api.protection import PROTECTION_COID_PREFIX
    coid = f"{PROTECTION_COID_PREFIX}SELL-AEM-XNYS-763905c0"
    c = _submit(_gate(_VendorClient(held=0.0)),
                _order(lane="BCTROT-004", side="SELL", qty=12, coid=coid, reduce_only=True))
    assert c.reached_venue == [coid] and c.denied == []


def test_a_CLOSED_order_is_delegated_not_denied():
    """The Alpaca client returns early on `is_closed` (`exec_client.py`, top of `_submit_order`),
    which used to run BEFORE the guard. The wrap now runs outside that check on both venues and must
    not deny an order Nautilus considers closed — an invalid state transition."""
    c = _submit(_gate(_VendorClient(held=0.0)),
                _order(lane="MOMENTUM-002", side="SELL", qty=28, is_closed=True))
    assert c.reached_venue == ["coid-MOMENTUM-002-SELL-28"] and c.denied == []


def test_an_UNREADABLE_book_lets_the_order_through_at_WARNING_and_SAYS_SO():
    c = _submit(_gate(_VendorClient(held=None)), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert c.reached_venue == ["coid-MOMENTUM-002-SELL-28"]
    assert any("unreadable" in m or "could not be read" in m for m in c._log.at("warning")), c._log.records


def test_an_EXCEPTION_in_the_read_lets_the_order_through_at_WARNING():
    """Same fail-open contract as the budget gate: a guard must never be the thing that halts a book."""
    c = _VendorClient(held=0.0)

    class _Hostile:
        def positions_open(self, **kw):
            raise RuntimeError("boom")

    c._cache = _Hostile()
    _submit(_gate(c), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert c.reached_venue == ["coid-MOMENTUM-002-SELL-28"]
    assert any("boom" in m for m in c._log.at("warning"))


def test_a_RAISING_denial_PROPAGATES__the_order_is_neither_refused_nor_sent():
    """The fail-open covers the READ. If `generate_order_denied` itself raises (a state transition),
    the order cannot be honestly refused and must not be sent to the venue in its place — the same
    contract the budget gate has (it does not catch there either). Pinned so the decision is visible."""
    c = _VendorClient(held=0.0)

    def boom(**kw):
        raise RuntimeError("invalid transition")

    c.generate_order_denied = boom
    with pytest.raises(RuntimeError):
        _submit(_gate(c), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert c.reached_venue == []


def test_a_command_with_no_order_is_delegated_unchecked_and_SAID():
    c = _gate(_VendorClient())
    asyncio.run(c._submit_order(types.SimpleNamespace(order=None)))
    assert c.reached_venue == ["<no order>"], "the gate is not an interlock — it must delegate"
    assert any("no order" in m for m in c._log.at("warning")), c._log.records


def test_installing_twice_does_not_double_the_guard():
    c = _gate(_gate(_VendorClient(held=0.0)))
    _submit(c, _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert len(c.denied) == 1, "the refusal was generated twice"
    assert len([m for m in c._log.lines if "OWNERSHIP REFUSED" in m]) == 1


def test_installing_the_guard_SAYS_SO():
    """A control nobody can see running is a control everyone believes is running (#782)."""
    c = _gate(_VendorClient())
    assert any("ownership guard INSTALLED" in m for m in c._log.at("info")), c._log.records


def test_the_guard_INSTALLS_on_the_REAL_client_classes():
    """The factory tests below hand the wrap a double, so nothing there proves it can be installed on
    a Cython-derived `LiveExecutionClient` whose `_cache`/`_log` are read-only attributes. Bind to a
    bare instance of each real class (no constructor) and ask whether `_submit_order` is the wrap."""
    from nautilus_trader.adapters.interactive_brokers.execution import InteractiveBrokersExecutionClient

    from api.providers.alpaca.exec_client import AlpacaExecutionClient
    from api.providers.gated_exec import install_ownership_gate

    for cls in (AlpacaExecutionClient, InteractiveBrokersExecutionClient):
        raw = cls.__new__(cls)
        wrapped = install_ownership_gate(raw, log=_Log())
        assert wrapped is raw
        assert wrapped._submit_order.__name__ == "_owned_submit_order", cls.__name__
        assert getattr(wrapped, "_ownership_gate_installed", False)


def test_OWNERSHIP_runs_BEFORE_BUDGET_when_both_are_installed__refused_AND_allowed_paths():
    """A budget verdict on an order that is secretly an entry answers the wrong question. Pinned by
    the ORDER OF INSTALLATION: ownership wraps outside budget, so a refused exit never reaches the
    budget rule — and an ALLOWED exit does reach it and then the venue (a wrap returning None after
    an ok verdict is green alone and wrong composed)."""
    import api.budget_guard as bg
    from api.providers.gated_exec import install_budget_gate, install_ownership_gate

    real, asked = bg.budget_allows, []

    async def record(order, **kw):
        asked.append(order.client_order_id)
        return (True, "", {})

    bg.budget_allows = record
    try:
        bad = install_ownership_gate(install_budget_gate(_VendorClient(held=0.0)))
        _submit(bad, _order(lane="MOMENTUM-002", side="SELL", qty=28))
        assert bad.reached_venue == [] and bad.denied and asked == [], (
            "the budget rule was asked about an order ownership had already refused")
        ok = install_ownership_gate(install_budget_gate(_VendorClient(held=28.0)))
        _submit(ok, _order(lane="MOMENTUM-002", side="SELL", qty=28))
        assert asked == ["coid-MOMENTUM-002-SELL-28"] and ok.reached_venue == [asked[0]]
        assert ok.denied == []
    finally:
        bg.budget_allows = real


def test_a_BUDGET_denial_still_lands_when_ownership_PASSED():
    """Two gates, two reasons, one `denied` list — the ownership pass must not swallow a budget refusal."""
    import api.budget_guard as bg
    from api.providers.gated_exec import install_budget_gate, install_ownership_gate

    real = bg.budget_allows

    async def refuse(order, **kw):
        return (False, "MOMENTUM-002 has 0 of budget left", {})

    bg.budget_allows = refuse
    try:
        c = install_ownership_gate(install_budget_gate(_VendorClient(held=0.0)))
        _submit(c, _order(lane="MOMENTUM-002", side="BUY", qty=9))
        assert c.reached_venue == [] and len(c.denied) == 1
        assert "budget" in c.denied[0]["reason"]
    finally:
        bg.budget_allows = real


def test_installing_BUDGET_after_OWNERSHIP_RAISES__the_wrong_order_cannot_compose_silently():
    """Reverse composition puts the budget rule outside the ownership rule. Nothing at runtime would
    notice; the install refuses so a future factory cannot get it wrong quietly."""
    from api.providers.gated_exec import install_budget_gate, install_ownership_gate
    with pytest.raises(RuntimeError, match="ownership"):
        install_budget_gate(install_ownership_gate(_VendorClient()))


def test_the_refusal_branch_is_CONDITIONAL_on_the_verdict():
    """The mutation `test_exit_ownership.py` records as a survivor — `if not owns:` → `if False:` —
    left the log line, the rejection and the ordering all present and the guard's answer discarded.
    Driven, not read: a permitted order must pass AND a forbidden one must not, on the same wrap."""
    ok = _submit(_gate(_VendorClient(held=28.0)), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    bad = _submit(_gate(_VendorClient(held=0.0)), _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert ok.reached_venue and not ok.denied
    assert bad.denied and not bad.reached_venue


# ==================================================================================================
# THE CLASS: every exec client this repo hands Nautilus carries BOTH gates, ownership outermost
# ==================================================================================================
def _every_exec_factory():
    """Resolve the factory of every `ExecClientSpec(...)` in `api.providers`, RECURSIVELY.

    The previous walker (`test_every_exec_client_is_gated`) used `pkgutil.iter_modules`, which yields
    the `alpaca` PACKAGE but never enters it — so the Alpaca spec at `providers/alpaca/exec_client.py`
    was outside its reach and "EVERY" was true of one venue. Walk the packages. (This imports every
    non-test module under `api.providers` at collection; `ibkr.py` runs `install_info_sanitiser()` at
    import — a known side effect, not a gap.)
    """
    import ast
    import importlib
    import inspect
    import pkgutil

    import api.providers as providers

    found = []
    for mod in pkgutil.walk_packages(providers.__path__, prefix="api.providers."):
        if ".test_" in mod.name or mod.name.endswith("_test"):
            continue
        m = importlib.import_module(mod.name)
        try:
            tree = ast.parse(inspect.getsource(m))
        except (OSError, TypeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ExecClientSpec"):
                continue
            kw = next((k for k in node.keywords if k.arg == "factory"), None)
            assert kw is not None, f"{mod.name} builds an ExecClientSpec with no factory"
            name = getattr(kw.value, "id", None) or getattr(kw.value, "attr", None)
            factory = getattr(m, name, None)
            assert factory is not None, f"{mod.name}: cannot resolve factory {name}"
            found.append((f"{mod.name}.{name}", factory))
    return found


def test_FIXTURE_the_walk_finds_BOTH_venues():
    names = sorted(n for n, _ in _every_exec_factory())
    assert any("ibkr" in n for n in names), names
    assert any("alpaca" in n for n in names), names


def test_EVERY_exec_client_factory_declares_BOTH_gates():
    for name, factory in _every_exec_factory():
        assert getattr(factory, "gates_budget", False), (
            f"{name} does not declare the budget gate — this is exactly how ibkr-paper-retired ran with no "
            f"per-strategy cap (#782)")
        assert getattr(factory, "gates_ownership", False), (
            f"{name} does not declare the ownership guard — this is how ibkr-paper ran with no guard "
            f"against the WHD shape (#1030). Install it the way the budget gate is installed.")


def test_the_IBKR_factory_RETURNS_a_client_with_BOTH_wraps_ownership_OUTERMOST():
    """Driven, not marked — a marker on a `create` that forgot to install is the 'declared but not
    connected' shape. Patches the vendor factory to hand back a double and asks the RETURNED object."""
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveExecClientFactory as _VendorFactory,
    )

    from api.providers.ibkr import BudgetGatedIBExecClientFactory

    built = _VendorClient(held=0.0)

    # The refless filter (#785) composes on the same instance and reads these two seams; the double
    # carries them so ALL of the factory's wraps install, not so this test exercises them.
    async def _no_orders(account_id):
        return []

    built._client = types.SimpleNamespace(get_open_orders=_no_orders)
    built.account_id = types.SimpleNamespace(get_id=lambda: "DUTEST000")
    original = _VendorFactory.__dict__["create"]
    _VendorFactory.create = staticmethod(lambda **kw: built)
    try:
        client = BudgetGatedIBExecClientFactory.create(
            loop=None, name=None, config=None, msgbus=None, cache=None, clock=None,
        )
    finally:
        _VendorFactory.create = original
    assert client is built
    assert getattr(client, "_budget_gate_installed", False), "the budget gate was lost"
    assert getattr(client, "_ownership_gate_installed", False), "the ownership guard is not installed"
    # ORDER. A WHD-shaped SELL must be refused by OWNERSHIP, with the budget rule never consulted.
    import api.budget_guard as bg
    real, asked = bg.budget_allows, []

    async def record(order, **kw):
        asked.append(1)
        return (True, "", {})

    bg.budget_allows = record
    try:
        _submit(client, _order(lane="MOMENTUM-002", side="SELL", qty=28))
    finally:
        bg.budget_allows = real
    assert client.reached_venue == [] and client.denied and asked == []


def test_the_ALPACA_factory_RETURNS_a_client_with_the_ownership_wrap():
    """Same drive for the client this repo wrote. The inline guard is GONE from `_submit_order` (one
    guard, one site); what protects Alpaca is the same wrap that protects IBKR."""
    from api.providers.alpaca import exec_client as mod

    built = _VendorClient(held=0.0)
    patched = {
        "AlpacaHttpClient": lambda **kw: None,
        "AlpacaInstrumentProvider": lambda *a, **kw: None,
        "AlpacaExecutionClient": lambda **kw: built,
    }
    saved = {k: getattr(mod, k) for k in patched}
    for k, v in patched.items():
        setattr(mod, k, v)
    try:
        client = mod.AlpacaLiveExecClientFactory.create(
            loop=None, name="ALPACA", config=types.SimpleNamespace(
                api_key="k", api_secret="s", trading_base_url="t", data_base_url="d"),
            msgbus=None, cache=None, clock=None,
        )
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)
    assert client is built
    assert getattr(client, "_ownership_gate_installed", False)
    _submit(client, _order(lane="MOMENTUM-002", side="SELL", qty=28))
    assert client.reached_venue == [] and client.denied


def test_the_inline_Alpaca_guard_is_GONE__one_guard_one_site():
    """Two copies of one rule drift; #782 is the proof. Bound to the AST, not a substring: a docstring
    mentioning the name must not turn this red, and a renamed helper must not turn it green. And the
    METHOD is deleted, not orphaned — an unused copy is a copy waiting to be called."""
    import ast
    import inspect
    import textwrap

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    tree = ast.parse(textwrap.dedent(inspect.getsource(AlpacaExecutionClient._submit_order)))
    called = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert not called & {"classify_exit", "_exit_reduces_its_own_lane"}, called
    assert all("_exit_reduces_its_own_lane" not in vars(k) for k in AlpacaExecutionClient.__mro__)


# ==================================================================================================
# THE SEAM: the two declarations of a lane's side — cockpit's and kumo-trading-strategies' — must agree
# ==================================================================================================
def test_the_two_side_declarations_agree():
    """`api.ownership.SHORT_PERMITTED` is keyed by LANE ID; kumo-trading-strategies' `POSITION_SIDE` by
    adapter CLASS. The next revision of the short lane (CRSISHORT-007, say) is SHORT to its adapter
    and LONG to cockpit until someone edits the frozenset — and its every entry is refused at 13:20Z
    on a Monday. Make that a gate failure instead: for every lane cockpit builds, the adapter's side
    and cockpit's permission must say the same thing. Booleans, never strings — the two vocabularies
    differ (`sides.SHORT == "short"`, `ownership.SHORT == "SHORT"`).

    Reachable from cockpit: each `backend/strategies/<lane>.py` builder lazily imports its adapter
    from `kumo_strategies.runtime.nautilus.*`; the lane id is the module's `STRATEGY_ID` or the
    literal `strategy_id=` kwarg. A seam test: it needs the installed kumo-trading-strategies (ks#146 shape).
    """
    import ast
    import importlib
    import pathlib

    from api.ownership import SHORT_PERMITTED

    # When kumo-trading-strategies is not installed, `conftest.py` deselects this whole file by source scan
    # (it names `kumo_strategies`); the merge gate's step 3b runs it against the PINNED tree, which
    # is the run that counts — the editable install cockpit's venv imports is whatever branch the
    # sibling checkout is on.
    sides = importlib.import_module("kumo_strategies.runtime.nautilus.sides")
    root = pathlib.Path(__file__).resolve().parents[1] / "strategies"
    checked = {}
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        adapters = {
            a.name: n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            and (n.module or "").startswith("kumo_strategies.runtime.nautilus.")
            for a in n.names if a.name.endswith("Strategy")
        }
        if not adapters:
            continue
        # PAIR the adapter with its lane, do not cross-product them. `momentum.py` builds TWO lanes
        # from two classes through one helper (`_build_rotation(Cls, ..., strategy_id="...")`); the
        # class is the call's first positional argument. A module-level STRATEGY_ID pairs with every
        # adapter the module imports (each such module builds one lane).
        pairs = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            lane = next((k.value.value for k in n.keywords
                         if k.arg == "strategy_id" and isinstance(k.value, ast.Constant)), None)
            if lane is None:
                continue
            names = [getattr(a, "id", None) for a in n.args] + [getattr(n.func, "id", None)]
            for name in names:
                if name in adapters:
                    pairs.add((lane, name))
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                    and any(getattr(t, "id", "") == "STRATEGY_ID" for t in n.targets):
                pairs |= {(n.value.value, name) for name in adapters}
        for lane, name in sorted(pairs):
            cls = getattr(importlib.import_module(adapters[name]), name, None)
            assert cls is not None, f"{path.name} imports {name} from {adapters[name]} and it is not there"
            # NO DEFAULT. An adapter that drops its declaration must not read LONG and pass; the
            # kumo-trading-strategies contract raises on that absence, and so does this.
            declared_short = getattr(cls, "POSITION_SIDE") == sides.SHORT
            checked[(lane, name)] = declared_short
            assert declared_short == (lane in SHORT_PERMITTED), (
                f"{path.name}: {name}.POSITION_SIDE says short={declared_short} for {lane}, "
                f"cockpit's SHORT_PERMITTED says {lane in SHORT_PERMITTED} — the two declarations "
                f"disagree and one venue will refuse or mis-guard this lane")
    assert checked, "no lane builder imports an adapter — this seam test matched nothing"
    assert any(checked.values()), f"no builder declares SHORT — CRSISHORT is not wired: {checked}"
    # Every lane cockpit builds is checked once against its own class — the momentum pair must be
    # (MOMENTUM-002, MomentumRotationStrategy) and (BCTROT-004, BCTRotationStrategy), not 2x2.
    assert ("MOMENTUM-002", "MomentumRotationStrategy") in checked and ("BCTROT-004", "BCTRotationStrategy") in checked, checked
    assert ("MOMENTUM-002", "BCTRotationStrategy") not in checked, "adapters and lanes were cross-producted"
    # The inverse: a permission with no builder is a dead permission.
    built = {lane for lane, _ in checked}
    assert SHORT_PERMITTED <= built, f"SHORT_PERMITTED names a lane no builder constructs: {SHORT_PERMITTED - built}"
