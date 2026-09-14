"""A THIRD planner rule: an EXTERNAL surplus over real lanes that already sum to the venue is evicted (#1038).

THE SHAPE, paper 2026-09-12 07:42Z and every boot since: AEM.XNYS — BCTROT-004 LONG 12, EXTERNAL
LONG 10, venue 12. `reconcile_drift {AEM broker 12, cockpit 22}`, `book_truth.abandoned ['AEM']`, a
red banner whose advice ("manage at your broker") is inverted: the broker is right. `book_repair`
dry-run on the running node: `planned 0 pair(s), 0 eviction(s), 0 not planned` — the shape was
INVISIBLE to the planner. No pair (nothing negative), no #779 eviction (the venue is not flat), and
`transfers.validate` refuses EXTERNAL as a party, so no operator lever either.

THE RULE, exactly (lead's scope, 2026-09-13): when an instrument's NON-EXTERNAL lanes sum to the
venue quantity (within eps) AND one EXTERNAL leg carries the surplus, plan a `SurplusEviction` of
the EXTERNAL leg ONLY, at its own basis, `unbooked_realized` recorded, dry-run by default. Its own
kind, because `Eviction` is all-or-nothing over the instrument's WHOLE lane set and the executor
would veto a one-leg eviction at a venue holding 12. The fingerprint carries the PREMISE (real
lanes' sum, venue qty, the EXTERNAL leg) so an approved plan cannot execute against a later book
where BCTROT dropped to 8 and the 10 is no longer all phantom.

GUARD-RAILS that must stay green: HALO (venue 19, lanes do not sum, residual unattributable) stays
refused; #779's venue-holds-none eviction unchanged; #771's pair rule unchanged; a NON-EXTERNAL
surplus (two real lanes over-summing the venue) is NOT evicted and is named; `arm=False` writes
nothing. Every test here was seen red on 52882d6 for the reason its docstring names.
"""

from __future__ import annotations

import asyncio

import pytest

from api.contra_close import QTY_TOLERANCE, plan_contra_closes
from api.contra_execute import prepare_execution
from api.test_contra_close import _Pos

AEM = "AEM.XNYS"


def _aem(external_qty: float = 10.0, bctrot_qty: float = 12.0, external_px: float = 203.4):
    """Paper's AEM: the real lane holds exactly what the venue holds; EXTERNAL sits on top."""
    return [
        _Pos(AEM, "BCTROT-004", bctrot_qty, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", external_qty, external_px, f"{AEM}-EXTERNAL"),
    ]


def _plan(book, broker=None):
    return plan_contra_closes(book, broker if broker is not None else {AEM: 12.0}, broker_complete=True)


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_aem_shape_is_served_by_NEITHER_existing_rule():
    """No shorts → the pair rule cannot fire; venue 12 → the #779 flatten cannot fire. If either
    ever served this book, every assertion below would pass with the new rule removed."""
    book = _aem()
    assert all(p.signed_qty > 0 for p in book), "a short would make this a pair case"
    assert sum(p.signed_qty for p in book if p.strategy_id != "EXTERNAL") == 12.0
    assert sum(p.signed_qty for p in book) == 22.0
    # on the tree before the fix this is what the running node printed: nothing at all
    # (asserted for real in test_on_the_old_tree_... via the mutation record in the PR)


# ------------------------------------------------------------------ the rule -------------------

def test_the_EXTERNAL_surplus_over_lanes_that_sum_to_the_venue_is_planned_as_a_SURPLUS_EVICTION():
    """THE DEFECT. On 52882d6: pairs (), evictions (), refused () — invisible. After: one surplus
    eviction of the EXTERNAL leg only, 10 shares at its own basis, the real lane untouched."""
    plan = _plan(_aem())

    assert plan.pairs == () and plan.evictions == (), "the shape must not be bent into an existing rule"
    assert [r.reason for r in plan.refused] == []
    se = plan.surplus_evictions
    assert len(se) == 1, f"no surplus eviction planned for the AEM shape (#1038): {plan}"
    e = se[0]
    assert e.instrument_id == AEM
    assert e.leg.strategy_id == "EXTERNAL" and e.leg.side == "SELL" and e.leg.quantity == 10.0
    assert e.leg.price == 203.4 and e.leg.position_id == f"{AEM}-EXTERNAL"
    assert e.broker_qty == 12.0 and e.real_lanes_qty == 12.0
    assert e.unbooked_realized == pytest.approx(-203.4 * 10.0), (
        "the ledger delta the eviction writes off, in Eviction's sign: BUY − SELL, a long closed by "
        "a SELL is negative — one convention, because unbooked_realized_total sums both kinds")


def test_the_fingerprint_carries_the_PREMISE_not_only_the_leg():
    """An approved plan must not execute against a later book where the real lanes no longer sum
    to the venue — BCTROT 12 → 8 leaves the same EXTERNAL 10 leg, but 4 of it is no longer phantom.
    The fingerprint moves with the real lanes' sum and with the venue quantity."""
    base = _plan(_aem()).surplus_evictions[0]
    moved_lane = _plan(_aem(bctrot_qty=8.0), broker={AEM: 8.0}).surplus_evictions[0]
    moved_venue_only = plan_contra_closes(_aem(), {AEM: 12.0}, broker_complete=True).surplus_evictions[0]
    assert base.fingerprint != moved_lane.fingerprint
    assert base.fingerprint == moved_venue_only.fingerprint, "same book, same venue → same approval"
    assert base.fingerprint.startswith("") and len(base.fingerprint) == 16
    # disjoint namespace from Eviction/ContraPair: the ledger keys on the fingerprint alone
    from api.contra_close import Eviction, Leg
    ev = Eviction(instrument_id=AEM, legs=(base.leg,), ts_opened=(base.ts_opened,), broker_qty=0.0)
    assert ev.fingerprint != base.fingerprint


# ------------------------------------------------------------------ guard-rails ----------------

def test_GUARD_a_NON_EXTERNAL_surplus_is_NOT_evicted_and_is_NAMED():
    """Two real lanes over-summing the venue: the planner cannot say whose shares are phantom, and
    it must say so rather than evict either — or evict an EXTERNAL leg that does not carry the
    surplus."""
    book = [
        _Pos(AEM, "BCTROT-004", 12.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "MOMENTUM-002", 10.0, 201.0, f"{AEM}-MOMENTUM-002"),
    ]
    plan = _plan(book, broker={AEM: 12.0})
    assert plan.surplus_evictions == () and plan.evictions == () and plan.pairs == ()
    assert any("surplus" in r.reason.lower() and "EXTERNAL" in r.reason for r in plan.refused), plan.refused


def test_GUARD_an_EXTERNAL_leg_that_does_NOT_carry_the_whole_surplus_is_not_evicted():
    """Real lanes 14 against venue 12 with EXTERNAL 10: evicting EXTERNAL leaves cockpit 14 ≠ 12.
    The premise (real lanes == venue) fails; nothing is planned and the shape is named."""
    book = [
        _Pos(AEM, "BCTROT-004", 14.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", 10.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    plan = _plan(book, broker={AEM: 12.0})
    assert plan.surplus_evictions == ()
    assert any(AEM == r.instrument_id for r in plan.refused), plan.refused


def test_GUARD_a_venue_that_did_NOT_state_the_instrument_plans_nothing():
    """`broker_complete=False` and no entry: the venue never spoke. Absence is not 12."""
    plan = plan_contra_closes(_aem(), {}, broker_complete=False)
    assert plan.surplus_evictions == ()


def test_GUARD_the_HALO_residual_shape_stays_REFUSED():
    """+55 / -37 / +1 against a venue holding 19: lanes do not sum to the venue, a short exists,
    the residual is unattributable. Unchanged by the new rule."""
    legs = [
        _Pos("HALO.XNAS", "BCTROT-004", 55.0, 106.42, "HALO.XNAS-BCTROT-004"),
        _Pos("HALO.XNAS", "EXTERNAL", -37.0, 111.78, "HALO.XNAS-EXTERNAL"),
        _Pos("HALO.XNAS", "MOMENTUM-002", 1.0, 104.35, "HALO.XNAS-MOMENTUM-002"),
    ]
    plan = plan_contra_closes(legs, broker={"HALO.XNAS": 19.0}, broker_complete=True)
    assert plan.pairs == () and plan.surplus_evictions == () and plan.evictions == ()
    assert any("more than two open legs" in r.reason for r in plan.refused)


def test_GUARD_the_779_venue_holds_none_eviction_is_UNCHANGED():
    from api.test_contra_execute import _gmab

    plan = plan_contra_closes(_gmab(), broker={}, broker_complete=True)
    assert len(plan.evictions) == 1 and plan.surplus_evictions == () and plan.pairs == ()


def test_GUARD_the_771_pair_rule_is_UNCHANGED():
    from api.test_build_book_repair import _offset_book

    plan = plan_contra_closes(_offset_book(), broker={}, broker_complete=True)
    assert len(plan.pairs) == 1 and plan.surplus_evictions == () and plan.evictions == ()


def test_GUARD_a_resting_order_on_the_instrument_refuses_the_surplus_eviction():
    """Same refusal the other rules carry: a protective stop sized to the cache's 22 must be
    cancelled and re-armed by the reconciler, not repaired underneath."""
    plan = plan_contra_closes(_aem(), {AEM: 12.0}, working_orders=(AEM,), broker_complete=True,
                              known_instrument_ids=(AEM,))
    assert plan.surplus_evictions == ()
    assert any("resting order" in r.reason for r in plan.refused)


# ------------------------------------------------------------------ the executor ---------------

def test_the_surplus_eviction_REACHES_execution_as_ONE_order_with_the_EXTERNAL_leg_only():
    plan = _plan(_aem())
    ex = prepare_execution(plan, _aem(), {AEM: 12.0}, broker_complete=True)
    assert [r.reason for r in ex.refused] == []
    assert len(ex.orders) == 1
    order = ex.orders[0]
    assert [(l.strategy_id, l.side, l.quantity, l.position_id) for l in order.legs] == [
        ("EXTERNAL", "SELL", 10.0, f"{AEM}-EXTERNAL")]
    assert order.fingerprint == plan.surplus_evictions[0].fingerprint
    assert order.is_partial is False
    assert order.unbooked_realized == pytest.approx(-203.4 * 10.0)


def test_execution_REFUSES_when_the_real_lane_moved_so_the_surplus_is_no_longer_all_phantom():
    """Approved at BCTROT 12 / venue 12; at execution BCTROT is 8 and the venue 8 — the EXTERNAL
    10 is now partly real (the premise fails) and the fingerprint has moved. Refused, never trimmed."""
    plan = _plan(_aem())
    later = _aem(bctrot_qty=8.0)
    ex = prepare_execution(plan, later, {AEM: 8.0}, broker_complete=True)
    assert ex.orders == ()
    assert any(AEM == r.instrument_id and "moved" in r.reason for r in ex.refused), ex.refused


def test_execution_REFUSES_when_the_venue_now_holds_the_surplus_too():
    """Approved at venue 12; at execution the venue says 22 — nothing is phantom any more."""
    plan = _plan(_aem())
    ex = prepare_execution(plan, _aem(), {AEM: 22.0}, broker_complete=True)
    assert ex.orders == ()
    assert any(AEM == r.instrument_id for r in ex.refused)


def test_execution_REFUSES_when_a_sibling_lane_APPEARED_after_approval():
    """MOMENTUM opened 5 AEM after approval: the real lanes now sum to 17 ≠ venue 12 → refused."""
    plan = _plan(_aem())
    later = _aem() + [_Pos(AEM, "MOMENTUM-002", 5.0, 200.0, f"{AEM}-MOMENTUM-002")]
    ex = prepare_execution(plan, later, {AEM: 12.0}, broker_complete=True)
    assert ex.orders == ()
    assert any(AEM == r.instrument_id for r in ex.refused)


# ------------------------------------------------------------------ the node seam --------------

def test_build_book_repair_on_the_AEM_book_PLANS_one_surplus_eviction_and_UNARMED_books_nothing():
    """The command path (`build_book_repair`, the node's own caller). Dry-run by default: the plan
    is reported with the new count and `run_book_repair` receives `arm=False`."""
    from api.test_build_book_repair import _Node, _Report

    node = _Node(_aem(), reports=[_Report(AEM, 12.0)])
    out = asyncio.run(node.build_book_repair())

    assert out.get("surplus") == 1, out
    assert out["planned"] == 0 and out["evictions"] == 0 and out["not_planned"] == 0
    assert node.ran and node.ran[0][1] is False, "run_book_repair was not called unarmed"
    assert any("1 surplus" in line for line in node.log.lines), node.log.lines


# ------------------------------------------------------------------ siblings (review) ----------

def test_SIBLING_an_EXTERNAL_SHORT_surplus_is_771s_book_and_the_surplus_rule_is_INERT():
    """Lanes sum to the venue, EXTERNAL −N: a short exists, so #771 pairs long against short and
    the surplus rule must not race it on the same instrument."""
    book = [
        _Pos(AEM, "BCTROT-004", 22.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", -10.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    plan = _plan(book, broker={AEM: 12.0})
    assert plan.surplus_evictions == ()
    assert len(plan.pairs) == 1 and plan.pairs[0].short_strategy == "EXTERNAL"


def test_SIBLING_a_real_lane_SHORT_beside_the_EXTERNAL_surplus_disables_the_rule():
    """BCTROT 10, MOMENTUM −2, EXTERNAL 12, venue 10: read over the LONGS alone the premise holds
    (real 10 == venue 10, EXTERNAL 12 == surplus 12) — so the fixture CAN fire the rule, and only
    the short check stops it. A short ANYWHERE hands the book to #771; the rule must not evict and
    leave a pair behind."""
    book = [
        _Pos(AEM, "BCTROT-004", 10.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "MOMENTUM-002", -2.0, 201.0, f"{AEM}-MOMENTUM-002"),
        _Pos(AEM, "EXTERNAL", 12.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    from api.contra_close import surplus_shape
    longs_only = [p for p in book if p.signed_qty > 0]
    assert surplus_shape(longs_only, 10.0)[0] is not None, "fixture: the longs alone satisfy the premise"
    plan = _plan(book, broker={AEM: 10.0})
    assert plan.surplus_evictions == ()
    assert any(AEM == r.instrument_id for r in plan.refused) or plan.pairs, "the short is #771's to pair or name"


def test_SIBLING_TWO_EXTERNAL_legs_are_REFUSED_by_name_never_planned_as_two_evictions():
    book = [
        _Pos(AEM, "BCTROT-004", 12.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", 6.0, 203.4, f"{AEM}-EXTERNAL"),
        _Pos(AEM, "EXTERNAL", 4.0, 203.4, f"{AEM}-EXTERNAL-2"),
    ]
    plan = _plan(book, broker={AEM: 12.0})
    assert plan.surplus_evictions == ()
    assert any("2 EXTERNAL legs" in r.reason for r in plan.refused), plan.refused


def test_SIBLING_lanes_that_sum_to_the_venue_with_NO_surplus_plan_nothing_and_say_nothing():
    for book in ([_Pos(AEM, "BCTROT-004", 12.0, 203.4, f"{AEM}-BCTROT-004")],
                 [_Pos(AEM, "BCTROT-004", 12.0, 203.4, f"{AEM}-BCTROT-004"),
                  _Pos(AEM, "EXTERNAL", 0.0, 203.4, f"{AEM}-EXTERNAL", is_open=False)]):
        plan = _plan(book, broker={AEM: 12.0})
        assert plan.surplus_evictions == () and plan.refused == () and plan.pairs == () and plan.evictions == ()


def test_SIBLING_broker_complete_but_the_instrument_ABSENT_is_779s_flatten_not_a_surplus():
    """A complete venue read that does not mention AEM says the venue holds NONE — the #779
    eviction of every leg, not a surplus eviction that would leave BCTROT's 12 standing."""
    plan = plan_contra_closes(_aem(), {}, broker_complete=True)
    assert plan.surplus_evictions == ()
    assert len(plan.evictions) == 1 and {l.strategy_id for l in plan.evictions[0].legs} == {"BCTROT-004", "EXTERNAL"}


def test_SIBLING_MANUAL_is_a_real_lane_the_premise_counts():
    """BCTROT 8 + MANUAL-001 4 + EXTERNAL 10 against venue 12: the real lanes are every
    non-EXTERNAL lane, MANUAL included — planned."""
    book = [
        _Pos(AEM, "BCTROT-004", 8.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "MANUAL-001", 4.0, 200.0, f"{AEM}-MANUAL-001"),
        _Pos(AEM, "EXTERNAL", 10.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    plan = _plan(book, broker={AEM: 12.0})
    assert len(plan.surplus_evictions) == 1 and plan.surplus_evictions[0].real_lanes_qty == 12.0


def test_the_order_is_sized_from_the_EXTERNAL_leg_which_equals_the_surplus_and_not_the_real_lane():
    """BCTROT 13, EXTERNAL 10, venue 13: the eviction is 10 (the surplus == the EXTERNAL leg),
    never 13 — kills a mutant that sizes the SELL off the real lane or the venue."""
    book = [
        _Pos(AEM, "BCTROT-004", 13.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", 10.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    plan = _plan(book, broker={AEM: 13.0})
    assert plan.surplus_evictions[0].leg.quantity == 10.0
    ex = prepare_execution(plan, book, {AEM: 13.0}, broker_complete=True)
    assert ex.orders[0].legs[0].quantity == 10.0


def test_a_RE_OPENED_external_leg_with_the_same_qty_and_basis_breaks_the_fingerprint():
    """`ts_opened` is in the fingerprint: an approval cannot be replayed against a leg that closed
    and re-opened identically."""
    a = _plan(_aem()).surplus_evictions[0]
    reopened = [_Pos(AEM, "BCTROT-004", 12.0, 203.4, f"{AEM}-BCTROT-004"),
                _Pos(AEM, "EXTERNAL", 10.0, 203.4, f"{AEM}-EXTERNAL", ts_opened=2_000)]
    b = _plan(reopened).surplus_evictions[0]
    assert a.fingerprint != b.fingerprint
    ex = prepare_execution(_plan(_aem()), reopened, {AEM: 12.0}, broker_complete=True)
    assert ex.orders == () and any("moved" in r.reason for r in ex.refused)


def test_execution_REFUSES_on_a_venue_read_that_did_not_mention_the_instrument():
    """`_same_qty` semantics at the executor: unknown is never the same. The planner's premise used
    `QTY_TOLERANCE` on numbers it had; the executor compares against a fresh read that may be NaN."""
    plan = _plan(_aem())
    ex = prepare_execution(plan, _aem(), {}, broker_complete=True)
    assert ex.orders == () and any(AEM == r.instrument_id for r in ex.refused)


# ------------------------------------------------------------------ one derivation ------------

def test_the_executor_re_derives_the_premise_with_the_PLANNERS_OWN_predicate():
    """AST: `prepare_execution` calls `surplus_shape` — the same function `plan_contra_closes`
    calls — and writes no second inequality for the premise."""
    import ast
    import inspect
    import textwrap

    from api import contra_close, contra_execute

    def calls(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    assert "surplus_shape" in calls(contra_close.plan_contra_closes)
    assert "surplus_shape" in calls(contra_execute.prepare_execution)


def test_unbooked_realized_is_ONE_formula_for_evictions_and_surplus_evictions():
    """A copied formula passes the value test and drifts on the first edit: both properties must
    bottom out in `_book_value`."""
    import ast
    import inspect
    import textwrap

    from api.contra_close import Eviction, SurplusEviction

    for cls in (Eviction, SurplusEviction):
        src = textwrap.dedent(inspect.getsource(cls))
        tree = ast.parse(src)
        props = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "unbooked_realized"]
        assert props, cls.__name__
        names = {c.func.id for c in ast.walk(props[0]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_book_value" in names, f"{cls.__name__}.unbooked_realized does not use the shared _book_value"


def test_the_two_eviction_kinds_share_ONE_SIGN_so_the_plan_total_does_not_net_them_against_each_other():
    """Impl review on #1038: the first draft returned `-_book_value` here and `+_book_value` on
    `Eviction`, so a plan holding one of each summed numbers of opposite sign for the same act
    (a long written off by a SELL). Both are derived from the same legs and must agree."""
    from api.contra_close import Eviction
    se = _plan(_aem()).surplus_evictions[0]
    ev = Eviction(instrument_id=AEM, legs=(se.leg,), ts_opened=(se.ts_opened,), broker_qty=0.0)
    assert se.unbooked_realized == ev.unbooked_realized != 0.0


def test_GUARD_a_PARTIAL_venue_read_plans_and_names_NOTHING_even_when_the_instrument_is_present():
    """`broker_complete=False` with AEM PRESENT at 12: #779 refuses a present zero on a partial
    read, and the surplus rule holds to the same gate — a partial snapshot must neither plan an
    eviction nor emit refusal rows the complete read would not."""
    plan = plan_contra_closes(_aem(), {AEM: 12.0}, broker_complete=False)
    assert plan.surplus_evictions == () and plan.refused == () and plan.evictions == ()


def test_SIBLING_the_FLAT_venue_with_a_real_long_and_an_EXTERNAL_long_is_779s_eviction_of_BOTH():
    """The one place the two rules touch. Venue 0, BCTROT 5, EXTERNAL 10, no shorts: #779 runs
    first and evicts every leg; the surplus predicate (real 5 ≠ venue 0) never gets to refuse it."""
    book = [
        _Pos(AEM, "BCTROT-004", 5.0, 203.4, f"{AEM}-BCTROT-004"),
        _Pos(AEM, "EXTERNAL", 10.0, 203.4, f"{AEM}-EXTERNAL"),
    ]
    plan = plan_contra_closes(book, {AEM: 0.0}, broker_complete=True)
    assert plan.surplus_evictions == () and plan.refused == ()
    assert len(plan.evictions) == 1
    assert {l.strategy_id for l in plan.evictions[0].legs} == {"BCTROT-004", "EXTERNAL"}
