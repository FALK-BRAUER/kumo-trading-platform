"""The boot gate must hand each lane a BROKER, and must not re-probe forever (#515, round three).

WHAT THE GATE ACTUALLY SAID the first time it ever ran on Alpaca, 2026-08-25 08:37:52:

    PREFLIGHT DEGRADED BCTROT-004:
        equity: raised: AttributeError("'dict' object has no attribute 'equity'");
        owned:  raised: AttributeError("'dict' object has no attribute 'strategy_positions'")

`_run_boot_gate_once` passed `broker=self._broker_account`. That is the ACCOUNT FRAME — a plain dict
off the msgbus — and every lane's `preflight(broker)` calls `broker.equity()`,
`broker.strategy_positions()` and `broker.last_price(sym)`. Methods of a `NautilusBroker`, not keys
of a dict. So the gate has never produced a valid verdict on any tenant, since the day it got a
caller.

AND THE EARLIER "RACE" WAS THIS BUG WEARING A FRIENDLIER EXPLANATION. On staging the same line read
`'NoneType' object has no attribute 'equity'`, because on an IBKR node nothing publishes
`broker.account` so the dict is None. I read that as "the lane's broker had not been attached yet",
wrote it into `boot_gate.should_run`'s docstring as a startup race, and built the DEGRADED-retries
change (17f5c41) on top of it. Two different symptoms of ONE wrong argument, and I explained the
first one away instead of following it.

THE RETRY THEN TURNED A WRONG ANSWER INTO A LOOP. `_publish_account` runs on every account update,
and a DEGRADED verdict is retryable, so the four ERROR lines above repeated every ~2 seconds. An
alarm that fires continuously is one an operator silences, which is how the next real one gets
missed.

WHAT THIS PINS
  1. Each lane is probed with ITS OWN broker — the object whose `equity()` exists — not with a
     shared account dict and not with None.
  2. Retries while degraded are BOUNDED, so a permanently-degraded lane reports and then stops.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.boot_gate import BootGateState, run_boot_gate, should_run


class _Broker:
    """What a lane's `preflight` actually calls. A dict has none of these, which is the whole bug."""

    def equity(self):
        return 100_000.0

    def strategy_positions(self):
        return {"AAA": 3}

    def last_price(self, _sym):
        return 10.0


def _lane(broker, *, probes_ok=True):
    """A lane shaped like the real ones: `preflight(broker)` plus `_runner.broker`."""
    seen: list = []

    def preflight(b):
        seen.append(b)
        from api.preflight_runner import Probe
        # EVERY probe the real lanes emit, in the real order — `armed`, `equity`, `owned`, `price`
        # (qc27_rotation.preflight). A double that emitted only two would be judged MISSING on the
        # other two and report DEGRADED for a reason that has nothing to do with the broker, which
        # is a double that cannot represent production.
        return [
            Probe("armed", True),
            Probe("equity", b.equity()),
            Probe("owned", b.strategy_positions()),
            Probe("price", b.last_price("AAA")),
            Probe("universe", {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}}),
        ]

    lane = SimpleNamespace(preflight=preflight, _runner=SimpleNamespace(broker=broker))
    lane.seen = seen
    return lane


def test_the_fixture_reproduces_the_failure_when_handed_a_DICT():
    """THE FIXTURE'S OWN PROPERTY FIRST. If this double tolerated a dict, every assertion below would
    pass over the exact defect it exists to catch."""
    lane = _lane(_Broker())
    with pytest.raises(AttributeError, match="'dict' object has no attribute 'equity'"):
        lane.preflight({"equity": 100_000.0})


def test_each_lane_is_probed_with_ITS_OWN_broker_not_a_shared_account_dict():
    """THE DEFECT. The account frame is not a broker, and neither is None."""
    b1, b2 = _Broker(), _Broker()
    lanes = {"AAA-001": _lane(b1), "BBB-002": _lane(b2)}

    # PLATFORM PROBES SUPPLIED, because cockpit supplies them in production. Omitting them makes
    # every lane DEGRADED on "cockpit did not supply this probe", which would mask the very thing
    # under test — a green broker read hidden behind an unrelated red.
    degraded = run_boot_gate(lanes, broker={"equity": 100_000.0},
                             platform={"lifecycle": "TRADING", "budget": 20_000.0,
                                       "slot_size": 5},
                             enabled_ids=list(lanes))

    assert lanes["AAA-001"].seen == [b1], (
        f"the lane was probed with {lanes['AAA-001'].seen!r}, not its own broker. Passing the "
        f"account dict is what made every probe raise "
        f"AttributeError(\"'dict' object has no attribute 'equity'\") on 2026-08-25"
    )
    assert lanes["BBB-002"].seen == [b2], "lanes must not share one broker"
    assert not degraded, f"a healthy lane was reported degraded: {degraded}"


def test_a_lane_with_no_broker_of_its_own_is_DEGRADED_FOR_THAT_REASON():
    """UNPROBEABLE IS NOT HEALTHY — but it must be degraded for the RIGHT reason.

    THIS TEST WAS VACUOUS AND CODEX CAUGHT IT (2026-08-25). It used `preflight=lambda b: []` and an
    empty `platform`, so the lane was degraded because every probe was MISSING — it would have
    passed identically with the broker resolution completely reverted. It asserted the outcome the
    gate produces for almost any broken lane, which is no evidence about brokers at all.

    So: a lane that WOULD report healthy probes if it had a broker, and a platform that supplies its
    three. The only thing left that can degrade it is the missing broker, and the reason is asserted
    rather than the bare fact of degradation.
    """
    healthy_probes = _lane(_Broker())          # emits armed/equity/owned/price against a real broker
    lane = SimpleNamespace(preflight=healthy_probes.preflight,
                           _runner=SimpleNamespace(broker=None))

    degraded = run_boot_gate({"CCC-003": lane}, broker=None,
                             platform={"lifecycle": "TRADING", "budget": 20_000.0, "slot_size": 5},
                             enabled_ids=["CCC-003"])

    assert degraded, "a lane with no broker probed CLEAN — unprobeable is not healthy"
    assert degraded[0][0] == "CCC-003"
    assert "broker" in degraded[0][1].lower(), (
        f"degraded for some other reason, so this proves nothing about broker resolution: "
        f"{degraded[0][1]!r}")


def test_retries_while_degraded_are_BOUNDED():
    """`_publish_account` runs on every account update. Unbounded retry means an ERROR every ~2s.

    Measured on paper 2026-08-25: the same four PREFLIGHT DEGRADED lines repeated continuously. An
    alarm that fires forever is one an operator silences.

    UPDATED FOR #604: bounded in RATE, no longer in TOTAL. This test used to pin `fired == 5` —
    exactly the permanence #604 measured: the five consecutive attempts were spent by ~boot+10s,
    before the first bar could arrive, so a price-less boot verdict could never be corrected for
    the process life (TECHIVOL-005, 2026-08-27). The bound this test defends is the RATE — five
    consecutive fast probes, then a doubling update-count gap — never a probe on every update, and
    never a full stop either. The thinning schedule itself is pinned in `test_boot_gate.py`.
    """
    state = BootGateState()

    fires = []
    for i in range(50):
        if should_run(state, equity=100_000.0):
            fires.append(i)
            state.degraded = [("AAA-001", "still broken")]   # never recovers

    assert len(fires) > 1, (
        "a DEGRADED verdict never retried — 17f5c41 exists because a lane may still be coming up")
    assert fires[:5] == [0, 1, 2, 3, 4], (
        f"the first _MAX_DEGRADED_ATTEMPTS=5 probes must fire on consecutive updates — a lane "
        f"still coming up is caught fast: {fires}")
    late = fires[5:]
    assert late, (
        "#604 returned: past the fast attempts the gate stopped forever, so a verdict measured "
        "at the one moment prices cannot exist would again be permanent")
    gaps = [b - a for a, b in zip(fires[4:], late)]
    assert min(gaps) >= 8, (
        f"a late retry fired within 8 updates of the previous probe — that is the every-~2s ERROR "
        f"line this bound exists to prevent: {fires}")


def test_a_CLEAN_verdict_is_still_final():
    """The property 17f5c41 kept, which the bound must not cost: re-probing a healthy stack learns
    nothing and would eventually report a transient as news."""
    state = BootGateState()

    fired = sum(1 for _ in range(20) if should_run(state, equity=100_000.0))

    assert fired == 1, f"a clean verdict re-probed {fired} times"


# ---------------------------------------------------------------------------------------------
# Round four. The first fix resolved `_runner.broker` by NAME, and only QC27 uses that name.
# ---------------------------------------------------------------------------------------------


def test_the_broker_is_found_by_CAPABILITY_not_by_attribute_name():
    """MEASURED ON PAPER, 2026-08-25 10:14:21, with the name-based fix DEPLOYED:

        TECHIVOL-005   equity/owned PASSED                 <- QC27SessionRunner.broker (dataclass field)
        BCTROT-004     equity: 'dict' has no 'equity'      <- SessionGateway._broker
        MOMENTUM-002   equity: 'dict' has no 'equity'      <- SessionGateway._broker
        QC345-003      equity: 'dict' has no 'equity'      <- QC345SessionGateway._broker

    One lane fixed, three still broken, and the deploy still VERIFIED. `getattr(runner, "broker")`
    is a guess about a NAME; `qc345.py:131` reads `self._broker, self._limits = broker, limits`.

    Guessing names is how this returns the next time an adapter spells it differently. The gate needs
    an object that can answer `equity()` — so ask THAT, and accept whatever holds it.
    """
    from api.boot_gate import _broker_of

    b = _Broker()

    class _NameIsUnderscored:
        def __init__(self):
            self._broker = b

    lane = SimpleNamespace(preflight=lambda x: [], _runner=_NameIsUnderscored())

    assert _broker_of(lane, {"equity": 1.0}) is b, (
        "a runner storing its broker as `_broker` fell back to the account dict — three of our four "
        "lanes do exactly that"
    )


def test_an_object_without_a_CALLABLE_equity_is_never_accepted_as_a_broker():
    """The predicate must be `callable(x.equity)`, not `hasattr(x, "equity")`.

    THE FIRST VERSION OF THIS TEST WAS VACUOUS AND A MUTATION PROVED IT. It used a dict
    `{"equity": 1.0}`, and `getattr(dict_instance, "equity")` is None under BOTH predicates — so
    swapping `callable(...)` for `is not None` left all eight tests green. The mutation killed its
    neighbours and spared its target, which is the one reliable outside signature of a test that
    measures nothing.

    Discriminating it needs an object where `equity` EXISTS but is not callable — a value where a
    method belongs. That is a real shape: a broker double built from an account snapshot, or an
    adapter exposing equity as a property returning a float rather than a bound method.
    """
    from api.boot_gate import _broker_of

    class _EquityIsAValueNotAMethod:
        equity = 100_000.0          # `hasattr` says yes; `equity()` would raise TypeError

    lane = SimpleNamespace(preflight=lambda x: [],
                           _runner=SimpleNamespace(broker=_EquityIsAValueNotAMethod()))

    assert _broker_of(lane, None) is None, (
        "an object whose `equity` is a VALUE was accepted as a broker. `preflight` calls "
        "`broker.equity()`, so this raises TypeError at probe time — the same class of failure as "
        "the account dict, one layer subtler"
    )

    # And the account dict itself, which is the shape that actually shipped.
    dict_lane = SimpleNamespace(preflight=lambda x: [],
                                _runner=SimpleNamespace(broker={"equity": 1.0}))
    assert _broker_of(dict_lane, None) is None, "the account dict was accepted as a broker"


def test_a_lane_whose_broker_cannot_be_found_says_WHY_rather_than_raising_four_times():
    """Unprobeable is not healthy — and must not be an AttributeError storm either.

    Before this, an unresolvable broker produced four `AttributeError` probe lines per lane, per
    attempt. The lane should be degraded once, with a reason an operator can act on.
    """
    lane = SimpleNamespace(preflight=lambda b: [], _runner=SimpleNamespace(broker=None))

    degraded = run_boot_gate({"DDD-004": lane}, broker={"equity": 1.0},
                             platform={"lifecycle": "TRADING", "budget": 1.0, "slot_size": 1},
                             enabled_ids=["DDD-004"])

    assert degraded, "a lane with no resolvable broker probed CLEAN"
    why = degraded[0][1]
    assert "broker" in why.lower(), f"the reason does not name the broker: {why!r}"
    assert "AttributeError" not in why, (
        f"degraded by an exception rather than a diagnosis: {why!r}")
