"""An exit may not cancel ANOTHER LANE'S protective stop (#462, same root as #437).

`_cancel_reducing_leg` cancels natively for orders the cache still holds open, then sweeps the VENUE for
the rest — orders the cache has written off but that still reserve shares. The native half is scoped:
`_reducing_orders_open(instrument_id, strategy_id, side)`. The venue half is NOT:
`_venue_reducing_orders(instrument_id, side)` takes no strategy and the loop cancels every row it
returns.

So a lane exiting a shared symbol cancels whatever else is resting on it. Six symbols carry a stop for
more than one owner (AEM, AMGN, BDX, CGAU, WHD, WPM). Same root as #437: an ACCOUNT-scoped read
authorising a STRATEGY-scoped action.

WHY "CANCEL ONLY YOUR OWN" IS THE WRONG FIX, and this is the part that took measuring. Ownership of the
live protective stops, read out of the durable cache 2026-08-22:

    PROT-SELL-AEM-XNYS-68012270    MANUAL-001
    PROT-SELL-AMGN-XNAS-3c2ed0d1   MANUAL-001
    PROT-SELL-BETA-XNYS-3f941a58   MANUAL-001      9 of 10 are MANUAL-001
    PROT-SELL-CGAU-XNYS-320c4ee9   MANUAL-001
    PROT-SELL-WPM-XNYS-692276db    MANUAL-001
    PROT-SELL-BDX-XNYS-3311fe07    MOMENTUM-002    a lane's OWN trailing stop

Account-level protection is placed by the reconciler running under MANUAL-001 — that is #437's decision,
taken deliberately because NETTING position ids are per-strategy and no single correct owner exists for
a stop covering an account net. So the stop reserving the shares is almost never the exiting lane's, and
a rule of "cancel only your own" would refuse every legitimate exit.

THE RULE THAT IS ACTUALLY RIGHT, and it is narrower than either extreme:

    own                      cancel   — it is ours
    account-level protection cancel   — it is the mechanism holding our shares, and #437 made it
                                        account-scoped ON PURPOSE
    another LANE's order     REFUSE   — this is the defect
    owner unknown            REFUSE   — cannot attribute, so cannot justify
"""

from __future__ import annotations

from api.cancel_attribution import OUR_STOP_PREFIXES, PROTECTION_OWNER, may_cancel


def test_the_fixture_matches_the_live_ownership_that_motivated_the_rule():
    """Fixture property first. If MANUAL-001 were not the protection owner the whole rule collapses to
    'cancel your own', so this pins the constant against the reason for it."""
    assert PROTECTION_OWNER == "MANUAL-001"


def test_a_lane_may_cancel_its_OWN_resting_order():
    assert may_cancel(owner="MOMENTUM-002", canceller="MOMENTUM-002") is True


def test_a_lane_may_cancel_ACCOUNT_LEVEL_protection_because_that_is_what_holds_its_shares():
    """The exemption, named to the construction rather than to the module — #437's wording. Without it
    every exit is refused, because 9 of 10 live stops are MANUAL-001's."""
    assert may_cancel(owner=PROTECTION_OWNER, canceller="MOMENTUM-002") is True


def test_a_lane_may_NOT_cancel_ANOTHER_LANES_order():
    """The defect. BCTROT-004 and MOMENTUM-002 both hold six of the same symbols; MOMENTUM exiting BDX
    would cancel BCTROT's own stop and leave that position naked while BCTROT believes it is covered."""
    assert may_cancel(owner="BCTROT-004", canceller="MOMENTUM-002") is False


def test_an_UNATTRIBUTABLE_order_is_REFUSED():
    """Cannot attribute, cannot justify. kumo-strategies' own ids are `kumo-{sha1}` with the owner
    hashed IN and not recoverable OUT, and cockpit's `PROT-SELL-{SYMBOL}-{hash}` carries no owner
    either — so when the cache cannot name it, NEITHER repo's id can. Refusing is safe by
    `release_for_exit`'s own rule: the exit not going out is recoverable, a position left unprotected
    is not."""
    assert may_cancel(owner=None, canceller="MOMENTUM-002") is False
    assert may_cancel(owner="", canceller="MOMENTUM-002") is False


def test_MANUAL_itself_may_cancel_its_own_protection():
    """The reconciler re-arms by cancelling and replacing; it must not be refused by its own rule."""
    assert may_cancel(owner=PROTECTION_OWNER, canceller=PROTECTION_OWNER) is True


def test_the_rule_is_not_simply_ALWAYS_TRUE():
    """Discriminating: an implementation returning True unconditionally passes four of the tests above.
    This states the whole truth table in one place so that cannot happen quietly."""
    table = {
        ("MOMENTUM-002", "MOMENTUM-002"): True,
        (PROTECTION_OWNER, "MOMENTUM-002"): True,
        ("BCTROT-004", "MOMENTUM-002"): False,
        ("QC345-003", "TECHIVOL-005"): False,
        (None, "MOMENTUM-002"): False,
    }
    got = {k: may_cancel(owner=k[0], canceller=k[1]) for k in table}
    assert got == table, got


# ==================================================================================================
# OWNER ALONE IS TOO WIDE (review, kumo-cockpit-ibkr session). `may_cancel` on the owner only lets any
# lane cancel ANY MANUAL-001 order — including a discretionary sell the operator placed by hand, which is not
# protection and which nothing would put back.
#
# The prefix is NOT the answer either, and the evidence against it is stronger than the evidence for.
# #242: a bracket leg carries a client id ALPACA minted, not ours — so `PROT-SELL` holds for orders WE
# construct and provably not for orders the venue constructs on our behalf. Keying on it alone makes
# every bracketed position unexitable.
#
# BOTH, therefore: the owner must be the protection construction AND the order must be protective by
# TYPE. `PROTECTIVE_TYPES` is already bilingual across the raw and typed vocabularies, and a
# discretionary exit is a market or limit order, never a stop.
# ==================================================================================================
from api.cancel_attribution import may_cancel_order


def _row(order_type="trailing_stop", coid="PROT-SELL-AEM-XNYS-abc"):
    return {"order_type": order_type, "client_order_id": coid}


def test_the_fixture_uses_a_type_PROTECTIVE_TYPES_actually_contains():
    """Fixture property first: if the type were absent from the set, every allow below would be vacuous
    and the rule would read as 'refuse everything', which passes for the wrong reason."""
    from api.protection import PROTECTIVE_TYPES

    assert _row()["order_type"] in PROTECTIVE_TYPES


def test_account_level_PROTECTION_may_be_cancelled_by_an_exiting_lane():
    assert may_cancel_order(owner=PROTECTION_OWNER, canceller="MOMENTUM-002", row=_row()) is True


def test_a_DISCRETIONARY_sell_owned_by_MANUAL_may_NOT_be_cancelled():
    """THE HOLE OWNER-ALONE LEAVES. The operator trades this account by hand under MANUAL-001. A market or
    limit sell he placed is not protection, nothing re-arms it, and a lane exiting the same symbol
    would have cancelled it."""
    assert may_cancel_order(owner=PROTECTION_OWNER, canceller="MOMENTUM-002",
                            row=_row(order_type="limit", coid="MANUAL-SELL-1")) is False


def test_a_lane_may_still_cancel_its_OWN_order_whatever_the_type():
    """Own orders are not restricted by type — a lane cancelling its own resting limit is its business,
    and narrowing that would refuse legitimate self-management."""
    assert may_cancel_order(owner="MOMENTUM-002", canceller="MOMENTUM-002",
                            row=_row(order_type="limit")) is True


def test_a_protective_order_owned_by_ANOTHER_LANE_is_still_refused():
    """Type does not widen the rule. BDX carries MOMENTUM-002's own trailing stop; BCTROT exiting BDX
    must not cancel it just because it is protective."""
    assert may_cancel_order(owner="BCTROT-004", canceller="MOMENTUM-002", row=_row()) is False


def test_a_bracket_leg_with_an_ALPACA_MINTED_id_is_judged_on_TYPE_not_on_the_prefix():
    """#242: the venue mints ids for bracket legs, so ours is absent. Judging on the prefix would call
    this unattributable and refuse — making every bracketed position unexitable. The type is what
    carries here."""
    assert may_cancel_order(owner=PROTECTION_OWNER, canceller="MOMENTUM-002",
                            row=_row(coid="8f2c1e77-3a4b-4f0e-9d21-6b5a0c9e4d13")) is True


def test_the_ORDER_TYPE_KEY_the_venue_actually_uses_is_read():
    """Alpaca returns `type`; our own frames emit `order_type`. A reader checking only one calls half
    the rows untyped — the same bilingual problem `is_resting` already solves for status."""
    assert may_cancel_order(owner=PROTECTION_OWNER, canceller="MOMENTUM-002",
                            row={"type": "trailing_stop", "client_order_id": "x"}) is True


def test_the_ORDER_CACHE_cannot_be_configured_to_EVICT_orders():
    """Q2 from review, pinned rather than commented. `_owner_of` resolves from the cache, and the venue
    sweep exists FOR orders the cache has written off — so the rule depends on a terminal order staying
    IN the cache. The installed `CacheConfig` has no order-eviction knob (`bar_capacity` and
    `tick_capacity` are market data), but `flush_on_start=True` would silently turn this rule into
    'refuse everything' on the first boot after the edit, and it would look like an exit bug."""
    from nautilus_trader.cache.config import CacheConfig

    # `.dict()`, not `.model_fields` — CacheConfig is not a pydantic BaseModel in the pinned version,
    # and asserting against an API it does not have is how a guard silently tests nothing. Read the
    # INSTALLED object, which cannot be wrong about itself.
    fields = set(CacheConfig().dict())
    assert not {f for f in fields if "order" in f and "capacity" in f}, (
        f"CacheConfig gained an order-capacity knob ({sorted(fields)}) — attribution can now lose an "
        f"order and this rule would refuse every cancel"
    )
    assert "flush_on_start" in fields, "flush_on_start vanished — re-check what clears the order cache"


# ==================================================================================================
# ATTRIBUTION NEEDS A SECOND SOURCE (review, kumo-cockpit-ibkr session).
#
# The venue sweep exists FOR orders the cache has written off, so `_owner_of` returning None there is
# not an edge case — it is the CENTRAL case. A rule that refuses on None turns the sweep off precisely
# where it was needed, which is what broke PEAK's re-arm: `PKW-ours-but-stuck` is stuck BECAUSE the
# cache cannot answer for it.
#
# The codebase already states the predicate, at engine_node.py:5719:
#
#     #: Client-order-id prefixes this system places protective stops under. A stop we placed is one we
#     #: may cancel and replace when a manager arms; anything else is not ours to touch.
#     _OURS = ("PROT-", "PKW-", "PK-", "FL-")
#
# Four conventions, not the two I had reasoned about. #242 still cuts against the prefix as a SOLE key
# — a bracket leg carries an id ALPACA minted — and this does not use it as one: it is the FALLBACK
# when the primary source is blind, and the type check still applies on top.
# ==================================================================================================
from api.cancel_attribution import owner_from_prefix


def test_the_prefix_list_here_is_the_one_engine_node_ACTUALLY_uses():
    """Two derivations of one fact. A private copy of the prefixes would drift the day a fifth is added
    — and a fifth has been added twice already, going two -> four."""
    from api.engine_node import _OURS

    assert set(_OURS) == set(OUR_STOP_PREFIXES), (
        f"engine_node places stops under {sorted(_OURS)} and attribution knows "
        f"{sorted(OUR_STOP_PREFIXES)} — an order under the difference is unattributable and refused"
    )


def test_every_prefix_the_system_places_resolves_to_the_protection_owner():
    for pfx in OUR_STOP_PREFIXES:
        assert owner_from_prefix(f"{pfx}whatever") == PROTECTION_OWNER, pfx


def test_a_PKW_order_the_cache_cannot_answer_for_is_still_attributable():
    """THE CASE THAT BROKE PEAK. `PKW-` is minted at engine_node.py:2560 and submitted through
    `self._submit_trailing_stop`, where `self` is `UiFeedStrategy` — which IS MANUAL-001. So it is
    account-level protection even when the cache has lost it."""
    assert owner_from_prefix("PKW-ours-but-stuck") == PROTECTION_OWNER


def test_a_FOREIGN_id_stays_unattributable():
    """The discriminating half. If every id resolved to MANUAL-001 the fallback would be a rubber
    stamp, and an order we did NOT place would become cancellable on both paths."""
    assert owner_from_prefix("kumo-3f9a1c") is None          # kumo-strategies' own convention
    assert owner_from_prefix("8f2c1e77-3a4b-4f0e") is None   # an id ALPACA minted (#242)
    assert owner_from_prefix("") is None


# ==================================================================================================
# THE THIRD SHAPE, AND IT REFUSED THE ONLY STOP ON THE BOOK (review, kumo-cockpit-ibkr session).
#
#     owner = MOMENTUM-002     the lane's OWN trailing stop
#     canceller = MANUAL-001   because release_for_exit does `owner = str(self.id)` and the feed IS
#                              MANUAL-001 — every exit, from every lane, presents as MANUAL
#
# Through the rule: not self, not the protection owner -> REFUSED. So MOMENTUM exiting its OWN BDX
# position, cancelling its OWN stop, was refused and 55 shares stayed reserved.
#
# Neither seam test could see it: one asserts MOMENTUM must not cancel BCTROT's, the other that the
# sweep still cancels MANUAL's. The breaking case is owner=LANE, canceller=MANUAL, and there was no
# fixture for it. Nine of ten stops were MANUAL-owned only because the FEED placed them; the moment a
# lane places its own, this fires.
#
# ROOT: `release_for_exit` CANNOT KNOW WHICH LANE IS ASKING. `NautilusBroker.exit` calls
# `feed.release_for_exit(str(iid), int(req.qty))` and its docstring says so deliberately. Until the lane
# is threaded through, the feed is a PROXY — acting for a lane it cannot name — and the rule is told
# that explicitly rather than inferring it.
# ==================================================================================================
def test_a_lanes_OWN_stop_reached_through_the_exit_path_is_NOT_refused():
    """The live case: PROT-SELL-BDX-XNYS-3311fe07, owned by MOMENTUM-002, 55 shares."""
    assert may_cancel_order(owner="MOMENTUM-002", canceller=PROTECTION_OWNER,
                            row=_row(coid="PROT-SELL-BDX-XNYS-3311fe07"),
                            canceller_is_proxy=True) is True


def test_the_PROXY_concession_is_limited_to_PROTECTIVE_orders():
    """The concession is wide — it permits any owner — so the type check is the only thing holding it.
    A discretionary sell must still be refused even when the feed is acting on a lane's behalf."""
    assert may_cancel_order(owner="MOMENTUM-002", canceller=PROTECTION_OWNER,
                            row=_row(order_type="limit"), canceller_is_proxy=True) is False


def test_WITHOUT_the_proxy_flag_the_rule_stays_STRICT():
    """The manager and PEAK paths pass the REAL lane, so they get the strict rule and #462 is fixed
    there. Only the exit path — which cannot name the lane — asks for the concession."""
    assert may_cancel_order(owner="MOMENTUM-002", canceller="BCTROT-004", row=_row()) is False
    assert may_cancel_order(owner="MOMENTUM-002", canceller=PROTECTION_OWNER, row=_row()) is False


def test_the_PROXY_concession_is_the_NAMED_GAP_in_462_and_is_asserted_as_such():
    """DELIBERATE AND WRITTEN DOWN. With the proxy flag, a lane exiting through `release_for_exit` can
    still cancel ANOTHER lane's protective stop — which is #462 itself, unfixed on that one path.

    It is not closable cockpit-side: the exit path is handed no strategy_id, so "MOMENTUM exiting its
    own" and "MOMENTUM exiting BCTROT's" arrive identical. The durable fix is threading `req.strategy_id`
    through `NautilusBroker.exit`, which is cross-repo. This test exists so the gap cannot be mistaken
    for closed, and FAILS the day the lane is threaded — at which point delete the flag."""
    assert may_cancel_order(owner="BCTROT-004", canceller=PROTECTION_OWNER,
                            row=_row(), canceller_is_proxy=True) is True, (
        "the proxy concession no longer permits a foreign protective stop — if the lane is now threaded "
        "through release_for_exit, DELETE canceller_is_proxy and this test"
    )


def test_OUR_STOP_PREFIXES_covers_every_prefix_cockpit_ACTUALLY_MINTS():
    """AGAINST UNIFORM WRONGNESS, not against drift.

    `test_the_prefix_list_here_is_the_one_engine_node_ACTUALLY_uses` compares two copies of the list —
    and kumo-strategies demonstrated today why that is not enough: their
    `test_every_default_strategy_id_agreed` was GREEN while all three defaults agreed perfectly on the
    WRONG lane. A guard on the CONSISTENCY of a thing cannot see that the thing is uniformly wrong.

    So this derives the answer from a third place: the f-strings where cockpit actually mints a
    protective client order id.

        engine_node.py:140   f"PROT-{side}-..."
        engine_node.py:1862  f"FL-{cid[:20]}"
        engine_node.py:2565  f"PKW-{cid[:20]}"
        engine_node.py:6890  f"PK-{manager_id[:20]}"

    MEASURED AGAINST THE VENUE, 2026-08-22: of 119 protective sell orders in account history, 31 carry
    ids matching NONE of these — and all 31 are UUID-shaped. That is #242, not a gap: a native bracket
    request has no per-leg client-id field, so Alpaca MINTS ITS OWN for the protective legs. Those are
    unattributable by prefix BY CONSTRUCTION and the cache is their only source, which is why
    `_owner_of` tries the cache first and why the venue-id path exists at all.

    So the number to carry is not "the list is incomplete" — it is that FOR ROUGHLY A QUARTER OF
    PROTECTIVE STOPS, CACHE ATTRIBUTION IS THE ONLY ATTRIBUTION. If the cache ever loses them, those
    orders are refused, and that is the residual risk this rule ships with.
    """
    import ast
    import pathlib
    import re

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    minted = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                m = re.match(r"^([A-Z]{2,4}-)", head.value)
                if m:
                    minted.add(m.group(1))
    assert minted, "no minted prefixes found — this test is blind"

    #: Minted prefixes that are NOT protective stops, each with the reason. Default-deny: a new prefix
    #: must be classified here or it fails, which is the moment someone asks whether it needs
    #: attributing. Checked by reading each site, 2026-08-22:
    #:
    #:   MGR-  a TAG on an order, not a client order id (engine_node.py:4063)
    #:   PKB-  a command_id — PEAK breakeven, internal, never reaches the venue (7181)
    #:   PKT-  a command_id — PEAK trim (7141)
    #:   PYR-  a command_id — pyramid (7505)
    #:   PY-   pyramid entry coid; an ENTRY is not protective and must not be cancellable as one
    #:   PYA-  pyramid add, same
    #:   SRW-  stop-reenter WATCH phase — "never places an order itself, kept only for protocol
    #:         conformance" (6023), so no order ever carries it
    #:   SRR-  stop-reenter RE-ENTRY — a BUY that reopens a position, not a protective sell (6148)
    #:   RPR-  an INTERNAL book-repair leg (#744, `_book_repair_leg`). It never reaches a venue, so
    #:         there is nothing at the broker to attribute or cancel — and it must NOT be classified
    #:         protective, because these legs are stamped with the OWNING LANE while
    #:         `owner_from_prefix` answers PROTECTION_OWNER (MANUAL-001) for everything in
    #:         OUR_STOP_PREFIXES. Putting it there would re-attribute the repair to the very lane
    #:         whose mis-stamping caused the damage.
    NOT_PROTECTIVE = {"MGR-", "PKB-", "PKT-", "PYR-", "PY-", "PYA-", "SRW-", "SRR-", "RPR-"}

    unclassified = minted - set(OUR_STOP_PREFIXES) - NOT_PROTECTIVE
    assert not unclassified, (
        f"cockpit mints ids under {sorted(unclassified)} and nothing here says whether they are "
        f"protective. If they are, add them to OUR_STOP_PREFIXES — otherwise a stop under that prefix "
        f"is unattributable and REFUSED whenever the cache cannot answer. If they are not, list them in "
        f"NOT_PROTECTIVE with the reason"
    )
    stale = NOT_PROTECTIVE - minted
    assert not stale, (
        f"{sorted(stale)} are no longer minted anywhere — remove them, or the exclusion list stops "
        f"being a list of decisions and starts waving through the next real one"
    )
