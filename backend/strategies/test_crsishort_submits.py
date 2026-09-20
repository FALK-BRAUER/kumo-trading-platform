"""CRSISHORT-006 SENDS ORDERS (platform issue 858). The seam, driven: gateway.run() -> the venue.

The lane has computed a full intent since its first session — `enter`, `limits`, `cover`,
`cover_kind`, `cover_px`, `refused` — and journalled it while submitting nothing, because
`CrsiShortSessionGateway` had no submission path. Everything else it needs already existed: the
broker carries LIMIT and cancel, protection covers shorts (`reducing_side` BUY, trigger
`entry + k*atr`), and claims are signed.

WHAT ACTUALLY BLOCKED IT was that `delta_execution.execute_plan` hardcoded the LONG reading of a
venue side: only a BUY passed the budget gate, and a SELL was credited as freeing capital. On this
lane the SELL is the ENTRY — so wiring it up unchanged would have sent every short UNGATED against
its sleeve. `test_short_entries_are_gated.py` pins that predicate; this file pins the lane.

THE DOUBLES ARE THE LONG LANE'S `_Broker` AND `_Journal`, deliberately: `_Broker` refuses what the
installed broker refuses, and a short-specific double could not have caught a defect whose cause is
that both lanes share one executor.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from strategies.test_smhgld_delta_execution import _Broker, _Claims, _Journal

SID = "CRSISHORT-006"
SESSION, SLOT = "2026-09-14", "open-10m"      # the AUCTION slot: pre-open, where the entry rests
POST_SLOT = "open+5m"                          # after the cross, where a replacement is owed
PX = {"AAOI": 27.25, "TSLQ": 11.50, "RGTI": 9.00}


@dataclass
class _Intent:
    """The installed `SessionIntent`'s shape. Pinned to the real dataclass below."""
    cover: dict = field(default_factory=dict)
    cover_kind: dict = field(default_factory=dict)
    cover_px: dict = field(default_factory=dict)
    enter: tuple = ()
    limits: dict = field(default_factory=dict)
    refused: dict = field(default_factory=dict)


@dataclass
class _State:
    value: str = "TRADING"
    decides: bool = True
    may_submit_entries: bool = True
    may_submit_exits: bool = True


class _Life:
    def __init__(self, state):
        self.state = state


def _gateway(*, intent, broker, journal, state=None, sleeve=(100_000.0, 100_000.0), deployed=0.0,
             cfg=None, execution_deps=True):
    from api.budget import Sleeve
    from strategies.crsi_short import CrsiShortSessionGateway
    from kumo_strategies.strategies.crsi_short.config import CrsiShortConfig

    st = state or _State()

    async def _read_state():
        return _Life(st)

    async def _read_budget():
        return Sleeve(strategy_id=SID, target=sleeve[0], actual=sleeve[1]), deployed

    kw = {}
    if execution_deps:
        kw = {"read_budget": _read_budget, "write_claim": _Claims(), "cfg": cfg or CrsiShortConfig(),
              "slots": (SLOT, POST_SLOT)}
    return CrsiShortSessionGateway(journal=journal, broker=broker, read_state=_read_state,
                                   intent=lambda _s, _p: intent, strategy_id=SID, **kw)


def _run(gw, *, order_path=True, slot=SLOT):
    """`order_path` forces kumo-trading-strategies' ORDER_PATH_COMPLETE for the duration.

    It is False on main, so without this every execution test below would pass vacuously by taking
    the refusal branch — which is exactly the shape that let a dead `shadow_only` survive.
    """
    import kumo_strategies.runtime.nautilus.crsi_short as upstream
    was = upstream.ORDER_PATH_COMPLETE
    upstream.ORDER_PATH_COMPLETE = order_path
    try:
        return asyncio.run(gw.run(panel=None, session=SESSION, slot=slot))
    finally:
        upstream.ORDER_PATH_COMPLETE = was


def test_the_lane_SUBMITS_NOTHING_while_the_upstream_order_path_is_incomplete():
    """THE SAFETY PROPERTY, and it is the one that must never regress.

    The entry rests in the OPENING CROSS: 108 of 231 accepted trades fill at the opening print and
    carry 77.6% of the return (+9.99%/trade against +2.53%, `crsi_short.py:89`). A day-limit-only
    lane is a DIFFERENT STRATEGY at a quarter of the edge, so "trades badly" is not a stopgap for
    "does not trade". This also replaces a knob that was DEAD: `shadow_only` is assigned upstream at
    `crsi_short.py:376` and read nowhere.
    """
    intent = _Intent(enter=("AAOI",), limits={"AAOI": 27.25})
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    res = _run(_gateway(intent=intent, broker=broker, journal=journal), order_path=False)
    assert broker.submitted == [], "the lane submitted with the order path incomplete"
    assert res.submitted == 0
    assert "ORDER_PATH_COMPLETE" in (res.blocked or ""), res.blocked


def test_the_DECOMPOSITION_S_PIECES_EXIST_not_merely_the_flag():
    """This REPLACES a reminder that fired. It asserted `ORDER_PATH_COMPLETE is False` and told
    whoever flipped it to wire the submission and a PRE-OPEN slot first; both are now done, so it
    becomes what must be TRUE rather than what must not change yet.

    NOT A FLAG CHECK. A boolean can read True while a piece goes missing — renaming any of these
    turns it red, which is what a constant cannot say about itself. `order_status` is here because
    the post-cross phase refuses without it; `has_pre_open` because `open-10m` clamps FORWARD
    without it and places the auction leg after the cross, failing nowhere.
    """
    from kumo_strategies.runtime.nautilus import crsi_short as up
    from kumo_strategies.runtime.nautilus.broker import NautilusBroker
    from kumo_strategies.strategies.momentum_rotation import slots as slotmod

    assert up.ORDER_PATH_COMPLETE is True
    for name in ("opening_leg", "replacement_leg", "replacement_is_owed"):
        assert callable(getattr(up, name, None)), f"the decomposition lost {name}"
    assert callable(getattr(NautilusBroker, "order_status", None)), (
        "the post-cross phase refuses without order_status and would never replace a leg")
    assert callable(getattr(slotmod, "has_pre_open", None)), (
        "without the pre-open derivation `open-10m` clamps forward past the cross")


def test_a_PRE_OPEN_slot_in_SETTINGS_reaches_the_resolver_the_builder_passes(monkeypatch):
    """The builder must hand the CONSTRUCTOR the resolved ladder, not the built-in.

    kumo-trading-strategies refuses at construction when a trading lane's slots cannot reach the auction and
    checks THAT argument, so passing `BUILTIN_SLOTS` (`("open+5m",)`, after the cross) raises at
    build and under #377 takes every other lane on the node down with it. Bitten by exactly that.

    Whether a given tenant has a pre-open slot configured is a DEPLOYMENT fact — enforced by the
    construction guard and read back by `make up`, not something a unit test can know. What this
    pins is the mechanism: an override in settings survives the resolver the builder calls.
    """
    from strategies import momentum
    from strategies.crsi_short import STRATEGY_ID
    from strategies.decision_slots import BUILTIN_SLOTS

    assert not any(str(x).startswith("open-") for x in BUILTIN_SLOTS[STRATEGY_ID]), (
        "fixture: the built-in must NOT reach the auction, or the override proves nothing")
    import api.settings as settings_mod
    monkeypatch.setattr(settings_mod, "resolve",
                        lambda _domain: {f"{STRATEGY_ID}_SLOTS": ["open-10m", "open+5m"]})
    assert momentum.decision_slots_from_settings(
        STRATEGY_ID, BUILTIN_SLOTS[STRATEGY_ID]) == ("open-10m", "open+5m")


# ------------------------------------------------------------------ fixture properties ---------
def test_the_intent_double_matches_the_INSTALLED_SessionIntent():
    """A double that cannot represent the real intent proves nothing about the lane."""
    import dataclasses
    from kumo_strategies.runtime.nautilus.crsi_short import SessionIntent
    assert {f.name for f in dataclasses.fields(SessionIntent)} == {
        f.name for f in dataclasses.fields(_Intent)}


def test_the_short_order_satisfies_the_execute_plan_contract():
    """`_ShortOrder` must carry every field the executor reads off an order, and `post_trade_qty`
    must be SIGNED — a claim written from an absolute value would read a short as a long."""
    from strategies.crsi_short import _ShortOrder
    o = _ShortOrder(symbol="AAOI", delta=-10.0, target_qty=-10.0, held_qty=0.0, reason="short entry")
    assert o.post_trade_qty == -10.0, "opening a short must leave a NEGATIVE post-trade quantity"
    cover = _ShortOrder(symbol="AAOI", delta=7.0, target_qty=0.0, held_qty=-7.0, reason="cover")
    assert cover.post_trade_qty == 0.0


# ------------------------------------------------------------------ the lane ---------------------
def test_the_lane_SENDS_a_short_entry_as_a_SELL_at_its_own_limit():
    """THE DELIVERABLE. Intent -> sized -> submitted, as a LIMIT DAY SELL."""
    intent = _Intent(enter=("AAOI",), limits={"AAOI": 27.25})
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    res = _run(_gateway(intent=intent, broker=broker, journal=journal))

    assert [(r.side, r.symbol, r.limit_px, r.time_in_force) for r in broker.submitted] == [
        ("SELL", "AAOI", 27.25, "AT_THE_OPEN")], f"submitted {broker.submitted}"
    assert res.submitted == 1
    # 100,000 sleeve / n_slots 20 = 5,000 a name; int(5000 / 27.25) = 183
    assert broker.submitted[0].qty == 183, broker.submitted[0].qty
    assert journal.decision()["detail"]["sized"] == {"AAOI": -183.0}


def test_sizing_is_the_CONFIG_S_OWN_derivation_off_the_SLEEVE_not_the_account():
    """`cfg.slot_notional` is what the backtest uses (`runner_crsi_short.py:243`). Sizing off the
    ACCOUNT is the defect that left QC345-003 unable to fit a single entry for its whole life."""
    from kumo_strategies.strategies.crsi_short.config import CrsiShortConfig
    cfg = CrsiShortConfig()
    intent = _Intent(enter=("TSLQ",), limits={"TSLQ": 11.50})
    broker = _Broker(held={}, prices=dict(PX), equity=5_000_000.0, buys_need_sells_first=False)
    _run(_gateway(intent=intent, broker=broker, journal=_Journal(), sleeve=(40_000.0, 40_000.0)))
    expected = int(cfg.slot_notional(40_000.0) / 11.50)
    assert broker.submitted[0].qty == expected, (
        f"sized {broker.submitted[0].qty}, the config says {expected} — a second sizing derivation")
    assert expected != int(cfg.slot_notional(5_000_000.0) / 11.50), "fixture: account != sleeve"


def test_a_COVER_is_a_BUY_of_exactly_what_THIS_LANE_is_short():
    intent = _Intent(cover={"RGTI": "reversal"}, cover_kind={"RGTI": "reversal"})
    broker = _Broker(held={"RGTI": -40}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    _run(_gateway(intent=intent, broker=broker, journal=journal))
    assert [(r.side, r.symbol, r.qty, r.limit_px) for r in broker.submitted] == [
        ("BUY", "RGTI", 40, None)], "a cover goes MARKET: an unfilled cover leaves a short OPEN"


def test_a_COVER_for_a_name_the_lane_is_NOT_short_is_REFUSED_not_sent():
    """Buying a name we do not owe turns a bookkeeping disagreement into a LONG position — the one
    outcome a short lane must never reach by accident."""
    intent = _Intent(cover={"RGTI": "reversal"})
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    _run(_gateway(intent=intent, broker=broker, journal=journal))
    assert broker.submitted == [], "a cover without a short reached the venue"
    assert journal.coded("cover_without_a_short"), [r for r in journal.rows]


def test_COVERS_GO_BEFORE_ENTRIES_because_a_cover_frees_the_room_an_entry_is_gated_against():
    intent = _Intent(cover={"RGTI": "reversal"}, enter=("AAOI",), limits={"AAOI": 27.25})
    broker = _Broker(held={"RGTI": -40}, prices=dict(PX), buys_need_sells_first=False)
    _run(_gateway(intent=intent, broker=broker, journal=_Journal()))
    assert [r.side for r in broker.submitted] == ["BUY", "SELL"], (
        f"order matters: {[(r.side, r.symbol) for r in broker.submitted]}")


def test_an_ENTRY_WITHOUT_A_LIMIT_is_refused_rather_than_sized_off_a_fallback_price():
    """The limit is the price the rule chose; substituting another makes this a different strategy."""
    intent = _Intent(enter=("AAOI",), limits={})
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    _run(_gateway(intent=intent, broker=broker, journal=journal))
    assert broker.submitted == []
    assert journal.coded("no_entry_limit")


def test_EXIT_ONLY_lifecycle_covers_but_opens_NOTHING():
    intent = _Intent(cover={"RGTI": "reversal"}, enter=("AAOI",), limits={"AAOI": 27.25})
    broker = _Broker(held={"RGTI": -40}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    st = _State(value="EXIT_ONLY", may_submit_entries=False, may_submit_exits=True)
    _run(_gateway(intent=intent, broker=broker, journal=journal, state=st))
    assert [(r.side, r.symbol) for r in broker.submitted] == [("BUY", "RGTI")]
    assert journal.coded("entries_not_permitted")


def test_WITHOUT_execution_deps_the_lane_still_REFUSES_BY_NAME_and_sends_nothing():
    """A build that cannot read a budget or write a claim must not send a short, and must not
    pretend it did. This is the behaviour the lane shipped with and it stays reachable."""
    intent = _Intent(enter=("AAOI",), limits={"AAOI": 27.25})
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)
    journal = _Journal()
    res = _run(_gateway(intent=intent, broker=broker, journal=journal, execution_deps=False))
    assert broker.submitted == [] and res.submitted == 0
    assert "will not submit" in (res.blocked or ""), res.blocked


# ============================================================ the post-cross replacement =========
class _StatusBroker(_Broker):
    """`_Broker` plus the venue's answer about a resting order. Absent from the base deliberately:
    the real `NautilusBroker` has no `order_status` yet either, and the gateway must refuse rather
    than assume when it is missing (that path is pinned below)."""

    def __init__(self, *, statuses=None, **kw):
        super().__init__(**kw)
        self._statuses = dict(statuses or {})

    def order_status(self, client_order_id: str):
        return self._statuses.get(client_order_id)


class _JournalWithAuctionLeg(_Journal):
    """A journal already holding the auction slot's decision row, which is what the post-cross pass
    reads its provenance from."""

    def __init__(self, legs):
        super().__init__()
        self._legs = list(legs)

    async def explain(self, session, slot=None):
        if slot != SLOT:
            return None
        return {"detail": {"sent": self._legs}}


def _leg(symbol="AAOI", coid="CID-1", qty=183, tif="AT_THE_OPEN"):
    return {"symbol": symbol, "side": "SELL", "qty": qty, "delta": -float(qty),
            "target_qty": -float(qty), "held_qty": 0.0, "client_order_id": coid,
            "limit_px": 27.25, "time_in_force": tif, "reason": "short entry"}


def test_an_EXPIRED_auction_leg_is_REPLACED_with_a_DAY_limit_at_THE_SAME_PRICE():
    """The auction ended without a fill, so the intent stands. Same price, deliberately: the
    backtest models ONE limit for the whole session."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _StatusBroker(statuses={"CID-1": "EXPIRED"}, held={}, prices=dict(PX),
                           buys_need_sells_first=False)
    intent = _Intent(enter=("AAOI",), limits={"AAOI": 27.25})
    res = _run(_gateway(intent=intent, broker=broker, journal=journal), slot=POST_SLOT)
    assert [(r.side, r.symbol, r.qty, r.limit_px, r.time_in_force) for r in broker.submitted] == [
        ("SELL", "AAOI", 183, 27.25, "DAY")], f"submitted {broker.submitted}"
    assert res.submitted == 1


@pytest.mark.parametrize("status", ["ACCEPTED", "FILLED", "PARTIALLY_FILLED", "REJECTED"])
def test_a_leg_that_could_DOUBLE_THE_POSITION_is_NEVER_replaced(status):
    """THE SAFETY PROPERTY OF THIS WHOLE PHASE. ACCEPTED is still live IN the auction and a second
    order fills alongside it; FILLED and PARTIALLY_FILLED mean the slot is taken; REJECTED means a
    replacement converts a venue refusal into a silent retry."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _StatusBroker(statuses={"CID-1": status}, held={}, prices=dict(PX),
                           buys_need_sells_first=False)
    intent = _Intent(enter=("AAOI",), limits={"AAOI": 27.25})
    res = _run(_gateway(intent=intent, broker=broker, journal=journal), slot=POST_SLOT)
    assert broker.submitted == [], f"{status} was replaced — a second order can double the position"
    assert res.submitted == 0
    assert journal.coded(f"auction_leg_{status.lower()}")


def test_a_leg_MISSING_FROM_THE_CACHE_is_not_treated_as_gone():
    """Absent is not EXPIRED. `replacement_is_owed` treats them very differently and a missing
    order is 'we do not know', which must never be rendered as 'safe to send another'."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _StatusBroker(statuses={}, held={}, prices=dict(PX), buys_need_sells_first=False)
    res = _run(_gateway(intent=_Intent(enter=("AAOI",), limits={"AAOI": 27.25}),
                        broker=broker, journal=journal), slot=POST_SLOT)
    assert broker.submitted == [] and res.submitted == 0
    assert journal.coded("auction_leg_not_in_cache")


def test_WITHOUT_order_status_the_pass_REFUSES_rather_than_assuming_the_leg_is_gone():
    """The broker has no `order_status` today. Assuming 'gone' would double every live auction leg
    on the first session this ran."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _Broker(held={}, prices=dict(PX), buys_need_sells_first=False)   # no order_status
    res = _run(_gateway(intent=_Intent(enter=("AAOI",), limits={"AAOI": 27.25}),
                        broker=broker, journal=journal), slot=POST_SLOT)
    assert broker.submitted == []
    assert journal.coded("no_order_status")
    assert "status unavailable" in (res.blocked or "")


def test_the_post_cross_pass_NEVER_re_enters_a_name_that_had_no_auction_leg():
    """The replacement carries the AUCTION leg's intent, read from provenance. A name the earlier
    slot never sent must not acquire a position at the later one."""
    journal = _JournalWithAuctionLeg([_leg(symbol="AAOI")])
    broker = _StatusBroker(statuses={"CID-1": "EXPIRED"}, held={}, prices=dict(PX),
                           buys_need_sells_first=False)
    intent = _Intent(enter=("AAOI", "TSLQ"), limits={"AAOI": 27.25, "TSLQ": 11.50})
    _run(_gateway(intent=intent, broker=broker, journal=journal), slot=POST_SLOT)
    assert [r.symbol for r in broker.submitted] == ["AAOI"], (
        "a name with no auction leg was entered at the post-cross slot")


def test_the_BOOK_VETOES_a_replacement_when_the_lane_is_ALREADY_SHORT_that_name():
    """Two derivations of "did the auction leg fill?" — the order status and the book. When they
    disagree, the phase refuses. A fill that reached the book but not the order plane would
    otherwise be replaced, DOUBLING a live short."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _StatusBroker(statuses={"CID-1": "EXPIRED"}, held={"AAOI": -183},
                           prices=dict(PX), buys_need_sells_first=False)
    res = _run(_gateway(intent=_Intent(enter=("AAOI",), limits={"AAOI": 27.25}),
                        broker=broker, journal=journal), slot=POST_SLOT)
    assert broker.submitted == [], "a live short was about to be doubled"
    assert res.submitted == 0
    assert journal.coded("already_short_despite_status")


def test_a_FLAT_book_does_NOT_license_a_send_when_the_status_is_UNKNOWN():
    """The veto is one-directional on purpose. An unpropagated fill looks exactly like no fill."""
    journal = _JournalWithAuctionLeg([_leg()])
    broker = _StatusBroker(statuses={}, held={}, prices=dict(PX), buys_need_sells_first=False)
    _run(_gateway(intent=_Intent(enter=("AAOI",), limits={"AAOI": 27.25}),
                  broker=broker, journal=journal), slot=POST_SLOT)
    assert broker.submitted == [], "a flat book enabled a send on an unknown status"
    assert journal.coded("auction_leg_not_in_cache")
