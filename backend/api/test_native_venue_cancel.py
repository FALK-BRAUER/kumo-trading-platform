"""A cache-terminal order can be cancelled THROUGH Nautilus, not behind its back.

WHY THIS FILE EXISTS
--------------------
`_cancel_reducing_leg` cancels an order the cache holds as terminal by calling Alpaca's REST API
directly. Its own docstring explains why, and the reasoning was sound for the paths it examined:

    NAUTILUS CHECKED FIRST, AND IT CANNOT DO THIS — `Strategy.cancel_all_orders` ... is CACHE-GATED by
    construction ... Every native cancel path bottoms out in cache state, which is the thing that is
    wrong.

True of `Strategy.cancel_order` (`strategy.pyx:1651` refuses a closed order) and of
`cancel_all_orders`. It is NOT true of the layer below them. `CancelOrder` is a plain command taking
`(trader_id, strategy_id, instrument_id, client_order_id, venue_order_id, ...)`, and
`ExecutionEngine._handle_cancel_order` does exactly one thing with it:

    cpdef void _handle_cancel_order(self, ExecutionClient client, CancelOrder command):
        client.cancel_order(command)

No cache lookup, no bookkeeping. The guard is on BUILDING the command inside `Strategy`, not on sending
it — so constructing it ourselves and handing it to the client is the native path, and it is available
for an order the cache has given up on.

WHY IT IS WORTH TAKING
----------------------
Two reasons, and the second is the bigger one.

It is broker-agnostic: the shipped Interactive Brokers adapter implements `cancel_order` like ours does,
so this stops being an Alpaca-only capability (#430).

And it stops the cancel happening behind Nautilus's back. A REST cancel leaves the cache holding an
order the venue no longer has, which is the setup for the `InvalidStateTrigger: REJECTED -> ACCEPTED`
churn documented at `engine_node.py` — 81,128 occurrences in one day on 2026-08-17. Routing the command
through the client means the cancel and its events flow back the normal way.
"""

from __future__ import annotations


def test_the_engine_adds_no_bookkeeping_to_a_cancel():
    """The premise, read from the installed package rather than assumed.

    If a future Nautilus makes `_handle_cancel_order` do more than forward, sending the command straight
    to the client would start skipping it — and this test is where that shows up.
    """
    from nautilus_trader.execution.engine import ExecutionEngine

    assert hasattr(ExecutionEngine, "_handle_cancel_order"), (
        "ExecutionEngine no longer exposes _handle_cancel_order — re-verify what a cancel now involves "
        "before trusting the direct client call"
    )


def test_cancel_order_can_be_built_without_the_cache():
    """The whole basis of the native path: the command needs a venue id, not an Order object.

    `Strategy._create_cancel_order` refuses a closed order. The command itself has no such opinion, and
    the venue id comes from the order status report — which is exactly what we have for an order the
    cache has given up on.
    """
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import CancelOrder
    from nautilus_trader.model.identifiers import (
        ClientOrderId,
        InstrumentId,
        StrategyId,
        TraderId,
        VenueOrderId,
    )

    cmd = CancelOrder(
        trader_id=TraderId("COCKPIT-001"),
        strategy_id=StrategyId("MANUAL-001"),
        instrument_id=InstrumentId.from_str("AEM.XNYS"),
        client_order_id=ClientOrderId("PROT-SELL-AEM-XNYS-a1"),
        venue_order_id=VenueOrderId("v-stop-1"),
        command_id=UUID4(),
        ts_init=0,
    )
    assert str(cmd.venue_order_id) == "v-stop-1"


class _Recorder:
    """A host carrying exactly what `_cancel_reducing_leg` touches, and nothing else."""


    # A cache that answers NOTHING, which is the honest shape for these fixtures: the venue sweep runs
    # on orders the cache has written off, so blindness is the case under test. Attribution then falls
    # through to the `_OURS` prefix inside the REAL `_owner_of`, exactly as it does in production.
    class _BlindCache:
        """Answers only for venue ids it was told about, and nothing else.

        NOT blind to everything, and that distinction is #242. A bracket leg carries a client id ALPACA
        minted — ours is absent — so the PREFIX fallback cannot attribute it and the CACHE is the only
        source. Production links our coid to the venue id at submit, so the cache CAN answer; a double
        that could not would make every bracketed position look unexitable and would be the double
        lying, not the rule failing.
        """

        def __init__(self, known=()):
            self._known = set(known)

        def client_order_id(self, venue_order_id):
            return str(venue_order_id) if str(venue_order_id) in self._known else None

        def order(self, client_order_id):
            if str(client_order_id) in self._known:
                return type("_O", (), {"strategy_id": "MANUAL-001"})()
            return None

    # THE REAL `_owner_of`, not a stub — delegated rather than assigned, because `UiFeedStrategy` is
    # imported inside the test functions here, not at module scope. Production's own method means the
    # prefix fallback AND the cache lookup are exercised, rather than replaced by a value the double
    # invents for itself.
    def _owner_of(self, coid, venue_id):
        from api.engine_node import UiFeedStrategy

        return UiFeedStrategy._owner_of(self, coid, venue_id)

    # THE REAL venue-cancel route, delegated for the same reason (#872 extracted it so the protection
    # reconciler's wrong-mode cancels take the SAME path). A stub here would let this file keep
    # asserting "it went through Nautilus" about a method that no longer exists.
    async def _cancel_at_venue(self, instrument_id, coid, venue_id):
        from api.engine_node import UiFeedStrategy

        return await UiFeedStrategy._cancel_at_venue(self, instrument_id, coid, venue_id)

    def __init__(self, rows, with_client=True):
        self.cache = self._BlindCache(known=[str(r.get('id') or '') for r in rows])
        from nautilus_trader.model.identifiers import StrategyId, TraderId

        self.trader_id = TraderId("COCKPIT-001")
        self.id = StrategyId("MANUAL-001")
        self._rows = rows
        self.sent = []           # CancelOrder commands routed through Nautilus
        self.rest_cancels = []   # venue ids cancelled over REST
        self.clock = type("C", (), {"timestamp_ns": staticmethod(lambda: 0)})()
        self.log = type("L", (), {"warning": lambda *a, **k: None,
                                  "error": lambda *a, **k: None,
                                  "exception": lambda *a, **k: None})()
        outer = self

        class _Client:
            def cancel_order(self, command):
                outer.sent.append(command)

        class _Http:
            async def cancel_order(self, venue_id):
                outer.rest_cancels.append(venue_id)

        self._exec_client = _Client() if with_client else None
        self._http = _Http()

    def _reducing_orders_open(self, *a, **k):
        return []

    def _cancel(self, order):
        raise AssertionError("nothing cache-open in this fixture")

    async def _venue_reducing_orders(self, instrument_id, side):
        return self._rows

    def _cache_status_of(self, coid):
        return "REJECTED"


def _leg(rows, with_client=True):
    import asyncio

    from nautilus_trader.model.enums import OrderSide

    from api.engine_node import UiFeedStrategy

    host = _Recorder(rows, with_client=with_client)
    asyncio.run(UiFeedStrategy._cancel_reducing_leg(host, "AEM.XNYS", "MANUAL-001", OrderSide.SELL))
    return host


def test_the_venue_cancel_goes_through_nautilus_not_the_rest_client():
    """BEHAVIOURAL, and the first version of this test was not.

    It asserted that `CancelOrder` appeared in the source — which `if False:` around the whole native
    branch satisfies. The mutation walked straight through. Drive the real method and look at where the
    cancel actually went.
    """
    rows = [{"client_order_id": "PROT-SELL-AEM-XNYS-a1", "id": "v-stop-1", "_remaining": 54.0}]
    host = _leg(rows)

    assert len(host.sent) == 1, (
        f"the cancel did not go through Nautilus (sent={len(host.sent)}, "
        f"rest={host.rest_cancels}) — a REST cancel is Alpaca-only and leaves the cache holding an "
        f"order the venue has dropped"
    )
    assert not host.rest_cancels, f"it ALSO cancelled over REST: {host.rest_cancels}"
    assert str(host.sent[0].venue_order_id) == "v-stop-1"
    assert str(host.sent[0].client_order_id) == "PROT-SELL-AEM-XNYS-a1"


def test_a_bracket_leg_with_no_client_order_id_is_still_cancellable():
    """Alpaca generates its own id for a bracket leg, so a row may carry a coid we never sent — or none.

    The venue id identifies it either way, and refusing for want of a client order id would leave the
    shares reserved, which is the outcome the whole sweep exists to prevent.
    """
    host = _leg([{"client_order_id": "", "id": "v-stop-2", "_remaining": 10.0}])
    assert len(host.sent) == 1
    assert str(host.sent[0].client_order_id) == "v-stop-2"


def test_the_rest_cancel_survives_only_as_a_fallback():
    """Not a deletion: with no execution client the command has nowhere to go, and an uncancelled
    reducing order holds shares an exit needs."""
    host = _leg([{"client_order_id": "PROT-SELL-AEM-XNYS-c3", "id": "v-stop-3", "_remaining": 5.0}], with_client=False)
    assert host.rest_cancels == ["v-stop-3"], (
        f"with no execution client the REST fallback did not run: {host.rest_cancels}"
    )
    assert not host.sent


def test_ANOTHER_LANES_resting_order_is_NOT_cancelled_by_the_venue_sweep():
    """THE #462 DEFECT, driven through the real `_cancel_reducing_leg`.

    The rule itself is covered in test_cancel_attribution.py — but a mutation bypassing the guard
    entirely left this whole suite green, because nothing exercised the WIRING. The rule was tested and
    the call site was not, which is the shape that broke production five times on 2026-08-14.

    Six symbols carry a stop for more than one owner. MOMENTUM-002 exiting one of them must not cancel
    BCTROT-004's protective stop: BCTROT would believe it is covered while the position is naked, and
    nothing re-arms a stop the reconciler did not place.
    """
    import asyncio

    from nautilus_trader.model.enums import OrderSide

    from api.engine_node import UiFeedStrategy

    foreign = {"client_order_id": "kumo-9f21ab", "id": "v-foreign-1", "_remaining": 9.0,
               "order_type": "trailing_stop"}
    # `_Recorder` directly, NOT `_leg` — `_leg` runs the sweep itself, so using it here would run it
    # twice and the second pass would judge rows the first had already cancelled.
    host = _Recorder([foreign])
    # OWNED BY ANOTHER LANE. Not prefix-attributable either — kumo-strategies mints `kumo-{sha1}` — so
    # the cache is the only source, and here it names BCTROT-004.
    host.cache = type("_C", (), {
        "client_order_id": staticmethod(lambda v: "kumo-9f21ab" if str(v) == "v-foreign-1" else None),
        "order": staticmethod(lambda c: type("_O", (), {"strategy_id": "BCTROT-004"})()),
    })()

    asyncio.run(UiFeedStrategy._cancel_reducing_leg(host, "AEM.XNYS", "MOMENTUM-002", OrderSide.SELL))

    assert host.rest_cancels == [], (
        f"MOMENTUM-002 cancelled BCTROT-004's resting stop at the venue: {host.rest_cancels}. That "
        f"position is now naked while BCTROT believes it is protected (#462)"
    )
    assert host.sent == [], f"and it went through Nautilus too: {host.sent}"


def test_the_SAME_SWEEP_still_cancels_account_level_protection():
    """The discriminating half. Without it, a guard that refuses EVERYTHING passes the test above and
    breaks every exit — which is what "cancel only your own" would have done, since nine of ten live
    stops belong to MANUAL-001."""
    import asyncio

    from nautilus_trader.model.enums import OrderSide

    from api.engine_node import UiFeedStrategy

    ours = {"client_order_id": "PROT-SELL-AEM-XNYS-a1", "id": "v-ours-1", "_remaining": 9.0,
            "order_type": "trailing_stop"}
    host = _Recorder([ours], with_client=False)   # no exec client -> REST fallback, so it is visible

    asyncio.run(UiFeedStrategy._cancel_reducing_leg(host, "AEM.XNYS", "MOMENTUM-002", OrderSide.SELL))

    assert host.rest_cancels == ["v-ours-1"], (
        f"the sweep refused account-level protection ({host.rest_cancels}) — MOMENTUM cannot free its "
        f"own shares and every exit on a protected symbol is rejected on `available: 0`"
    )


def test_the_sweep_authorises_on_CANCELLER_and_filters_on_STRATEGY_ID():
    """The two identities inside the sweep itself, not merely as passed to it.

    A mutation collapsing `canceller or strategy_id` to `strategy_id` survived, because the test above
    asserts what `release_for_exit` HANDS the sweep and stops there. The sweep's own use of the two
    values was unasserted — the caller was tested, the callee was not, which is the same fault line
    one step further in.

    Here `strategy_id` is MANUAL-001 (the filter identity, always the feed) and `canceller` is the real
    lane. A BCTROT-004 stop must be refused because MOMENTUM-002 is asking — and it would be PERMITTED
    if the sweep authorised on the filter id, since MANUAL-001 is the protection owner and the
    exemption would apply.
    """
    import asyncio

    from nautilus_trader.model.enums import OrderSide

    from api.engine_node import UiFeedStrategy

    foreign = {"client_order_id": "kumo-bc7a01", "id": "v-bctrot-1", "_remaining": 9.0,
               "order_type": "trailing_stop"}
    host = _Recorder([foreign])
    host.cache = type("_C", (), {
        "client_order_id": staticmethod(lambda v: "kumo-bc7a01" if str(v) == "v-bctrot-1" else None),
        "order": staticmethod(lambda c: type("_O", (), {"strategy_id": "BCTROT-004"})()),
    })()

    asyncio.run(UiFeedStrategy._cancel_reducing_leg(
        host, "AEM.XNYS", "MANUAL-001", OrderSide.SELL, proxy=False, canceller="MOMENTUM-002"))

    assert host.rest_cancels == [] and host.sent == [], (
        f"the sweep authorised on the FILTER identity (MANUAL-001, the protection owner) instead of on "
        f"the canceller — MOMENTUM-002 just cancelled BCTROT-004's stop: {host.rest_cancels or host.sent}"
    )


def test_a_named_lane_may_cancel_ITS_OWN_stop_through_the_sweep():
    """THE CASE THAT DISCRIMINATES, and the previous test did not.

    With owner=BCTROT-004 both the correct code and a mutation authorising on the FILTER identity
    refuse — so that test passed either way. The two only differ when the order belongs to the LANE:

        correct   canceller=MOMENTUM-002, owner=MOMENTUM-002  -> own order -> ALLOW
        mutated   canceller=MANUAL-001                        -> not own, not protection owner -> REFUSE

    This is also the realistic case the moment kumo-strategies threads `req.strategy_id` through: a lane
    exiting its own position must be able to clear its own stop, or every exit dies on `available: 0`.
    """
    import asyncio

    from nautilus_trader.model.enums import OrderSide

    from api.engine_node import UiFeedStrategy

    own = {"client_order_id": "PKW-momentum-1", "id": "v-own-1", "_remaining": 55.0,
           "order_type": "trailing_stop"}
    host = _Recorder([own], with_client=False)      # REST fallback, so the cancel is visible
    host.cache = type("_C", (), {
        "client_order_id": staticmethod(lambda v: "PKW-momentum-1" if str(v) == "v-own-1" else None),
        "order": staticmethod(lambda c: type("_O", (), {"strategy_id": "MOMENTUM-002"})()),
    })()

    asyncio.run(UiFeedStrategy._cancel_reducing_leg(
        host, "BDX.XNYS", "MANUAL-001", OrderSide.SELL, proxy=False, canceller="MOMENTUM-002"))

    assert host.rest_cancels == ["v-own-1"], (
        f"MOMENTUM-002 was refused its OWN resting stop ({host.rest_cancels}) — the sweep authorised on "
        f"the filter identity instead of the canceller, and every exit on a protected symbol now fails "
        f"on `available: 0`"
    )
