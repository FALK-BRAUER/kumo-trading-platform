"""Historical bar requests leave at the rate the PROVIDER declares (#617).

THE DEFECT, measured on staging-ibkr 2026-08-28 00:2x SGT, market open:

    requests in one 45s window   744          = 7 bar types x ~106 instruments
    next two 45s windows           0, 0       <- a periodic BURST, not a spiral
    bars received WITH data        0          in 15 minutes
    empty <Bar[]> responses    2,232          in 15 minutes
    errors                         0
    162 (contention)               0

IBKR's documented historical pacing is ~60 requests per 10 minutes. We fire 744 at once. **IB does
not error — it answers with EMPTY ARRAYS**, so the logs are clean and nothing works.

The peer put the shape better than I did: *a rate limiter that refuses by returning success is
indistinguishable from a venue with no data.* Same family as absence reported as an all-clear.

The loop is closed: a series with no bars stays short, so the next tick asks again, and every answer
is empty because the batch is 120x the allowance.

    short of bars -> request -> IB returns empty (over pacing) -> still short -> request ...

`_refresh_rotation`'s comment calls this "self-healing and idempotent: a satisfied ticker is never
re-requested, and an unsatisfied one is retried every tick". That is TRUE, and benign at Alpaca's
limits, and it guarantees permanent starvation at IBKR's.

THE SEAM IS #619's, WHICH IS MERGED AND WORKS. The provider declares its own limit on
`DataClientSpec`; the engine honours it. No venue name above the connector, and a third adapter is
paced correctly by declaring a number. A provider that declares nothing is NOT paced — same default
direction as #619 and for the same reason: a foreign limit silently throttling a feed is worse than
no limit at all.

NOT A SLEEP, NOT A THREAD. `clock.set_timer` is Nautilus-native and already used at engine_node:1044.
Blocking the Nautilus thread is what #598 was: a synchronous 6.4 MB fetch parked `node.run()`, py-spy
showed MainThread in `ssl.read`, and nothing published on EITHER tenant.
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def test_the_fixture_reflects_the_measured_burst():
    """FIXTURE PROPERTY FIRST. The numbers below are the point: if the declared rate were not far
    BELOW the request count, pacing could not bind and every assertion here would pass against an
    unpaced engine."""
    granularities, instruments = 7, 106
    assert granularities * instruments == 742          # ~744 measured, incl. the compass
    assert 6.0 < granularities * instruments, "the fixture cannot exceed IBKR's ~6/min allowance"


def test_the_provider_declares_its_own_historical_rate():
    """THE SEAM. Not a constant in the engine — that is exactly how an Alpaca plan cap came to ration
    an IBKR node (#619). A limit belongs to whoever imposes it."""
    from api.providers.base import DataClientSpec

    assert "historical_requests_per_minute" in DataClientSpec.__dataclass_fields__, (
        "DataClientSpec does not declare a historical request rate, so pacing would have to be a "
        "hardcoded constant reachable from every provider (#617)"
    )


def test_a_provider_that_declares_NOTHING_is_not_paced(monkeypatch):
    """Alpaca must be UNCHANGED. Its REST history is generous and throttling it would trade a real
    outage on the trading tenant for a display gap on the other."""
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    from api.providers.alpaca.data_client import build_data

    assert build_data({}).historical_requests_per_minute == float("inf")


def test_IBKR_declares_a_FINITE_rate():
    """The venue's real limit, declared by its own adapter — and it is a limit IB enforces by
    answering EMPTY rather than erroring, which is why nothing detected it for a full session."""
    from api.providers.ibkr import build_data

    rate = build_data({}).historical_requests_per_minute
    assert rate != float("inf"), "IBKR declares no historical pacing, so the burst is unbounded"
    assert 0 < rate <= 60, f"{rate}/min is not IBKR's documented ~60 per 10 minutes"


def test_build_node_PASSES_THE_RATE_to_the_strategy():
    """THE WIRING, and the half that would ship green. #619's first test survived a full
    reintroduction of the defect 8/8 because the fixture injected the budget instead of deriving it —
    proving only that the consumer respects a value it is HANDED, never that the right one arrives.

    Source-read with docstrings stripped: a live node cannot be built in a unit test, and a comment
    naming the field would otherwise satisfy a raw grep — a trap this repo fell into last night.
    """
    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.build_node)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    assert "historical_requests_per_minute=spec.historical_requests_per_minute" in ast.unparse(fn), (
        "build_node does not pass the provider's declared rate, so the constructor default paces "
        "every provider — the #619 defect in a second place"
    )


def test_EVERY_bar_request_goes_through_the_PACED_path():
    """AIMED AT THE CLASS. Three call sites issue historical bar requests — the initial load, the
    refetch heal loop, and the compass. One unpaced site reproduces the whole burst.

    Found this exact shape earlier today: `_lane_symbols` was DEFINED and called by two of three
    lanes, and deleting the third call left the suite 304/304 green. A seam most callers use is not
    a seam.
    """
    import api.engine_node as mod

    #: The ONLY two methods allowed to call `request_bars` directly — the paced enqueue (which
    #: passes straight through when the provider declares no rate) and the drain. Every other caller
    #: must go through the queue.
    sanctioned = {"_request_bars_paced", "_drain_bar_requests"}

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.UiFeedStrategy)))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name in sanctioned:
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]                       # the docstring may legitimately NAME the call
        if not body:
            continue
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))
        if "self.request_bars(" in code:
            offenders.append(node.name)
    assert not offenders, (
        f"these methods bypass the paced queue: {offenders}. One unpaced site is the whole burst "
        f"back — 744 requests answered with 2,232 empty arrays and no error (#617)"
    )
    assert "_request_bars_paced(" in ast.unparse(tree)


def test_the_drain_uses_the_NAUTILUS_CLOCK_not_a_sleep_or_a_thread():
    """#598 IS WHAT HAPPENS OTHERWISE. A synchronous 6.4 MB fetch on the Nautilus thread parked
    `node.run()`: py-spy showed MainThread in `ssl.read`, log frozen, 1.4% CPU, nothing published on
    BOTH tenants. A sleep in the drain is the same outage with a different cause.

    `clock.set_timer` is native and already used at engine_node:1044, so it also works in backtest —
    which a wall-clock sleep would not.
    """
    import api.engine_node as mod

    # SCOPED TO THE DRAIN, not the whole class. The first version asserted `time.sleep` appeared
    # nowhere in `UiFeedStrategy` and failed against a correct implementation: two pre-existing
    # sleeps live in the Redis WRITER thread, which is not the Nautilus thread and is allowed to
    # block. An assertion that fails for the wrong reason reads as proof and is worse than none —
    # the third time today I have caught myself widening a check past the property it means.
    def _body(fn) -> str:
        """Source with the docstring stripped.

        The drain's own docstring says "Not a sleep and not a thread" — so a raw-source check for
        "sleep" matches the sentence EXPLAINING the property and fails against a correct
        implementation. That is the fourth time today an assertion has been satisfied, or defeated,
        by prose adjacent to the code it meant.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        node = tree.body[0]
        if node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant):
            node.body = node.body[1:]
        return ast.unparse(node)

    for fn in (mod.UiFeedStrategy._drain_bar_requests, mod.UiFeedStrategy._request_bars_paced):
        assert "sleep" not in _body(fn), f"{fn.__name__} blocks the Nautilus thread"



def test_a_PACED_provider_actually_SCHEDULES_the_drain():
    """THE MOST DANGEROUS MUTATION OF THE SET, and a grep could not catch it.

    If the drain is never scheduled the queue fills and never empties: no bars at all, which is worse
    than the burst and looks deliberate. Asserting the strings `set_timer` and `_BAR_DRAIN_TIMER`
    appear in `on_start` DID NOT BITE when the registration was disabled — the text survives inside
    the dead branch. A name being present is not the same as a line running.

    So drive the registration and record what was asked for.
    """
    from types import SimpleNamespace

    import api.engine_node as mod

    timers: list[str] = []

    def _register(rate: float) -> list[str]:
        timers.clear()
        host = SimpleNamespace(
            _hist_rate=rate,
            clock=SimpleNamespace(set_timer=lambda name, **kw: timers.append(name)),
            _drain_bar_requests=lambda *a, **k: None,
        )
        mod.UiFeedStrategy._schedule_bar_drain(host)
        return list(timers)

    assert mod._BAR_DRAIN_TIMER in _register(6.0), (
        "a provider that declared a rate never scheduled the drain — every historical request would "
        "queue forever and no bars would ever arrive (#617)"
    )
    # ...and an UNPACED provider must not register one: passthrough means there is nothing to drain,
    # and a timer firing on an empty queue every 10s forever is pure noise on the trading tenant.
    assert mod._BAR_DRAIN_TIMER not in _register(float("inf"))


def test_a_STILL_SHORT_series_is_ALREADY_bounded_and_that_is_load_bearing_here():
    """PACING ALONE DOES NOT BREAK THE LOOP, it only slows it.

    Two things must hold together: requests leave at the declared rate, AND a series still short
    after a COMPLETED request must not be asked for again on the next tick — otherwise the paced
    queue refills exactly as fast as it drains and starvation is permanent, just quieter.

    I ADDED A SECOND COOLDOWN FOR THIS AND THEN DELETED IT. `_healed` / `_HEAL_COOLDOWN_NS` already
    does precisely this job (engine_node:682, :1332, "don't spam a series the feed genuinely can't
    complete") — and I had independently picked the same 300 seconds, which is how close two
    derivations of one rule sit before they drift apart.

    So this test does not pin new behaviour. It pins that the EXISTING bound is what makes pacing
    sufficient rather than merely slower, so nobody weakens it without meeting #617.
    """
    import api.engine_node as mod

    assert mod._HEAL_COOLDOWN_NS >= 60 * 1_000_000_000, (
        f"_HEAL_COOLDOWN_NS is {mod._HEAL_COOLDOWN_NS / 1e9:.0f}s — too short to bound a series the "
        f"venue cannot complete, so the paced queue refills as fast as it drains (#617)"
    )
    src = inspect.getsource(mod.UiFeedStrategy._on_refetch)
    assert "_HEAL_COOLDOWN_NS" in src and "_healed" in src, (
        "the still-short bound left `_on_refetch`; pacing alone does not close the loop (#617)"
    )


def test_on_start_ACTUALLY_CALLS_the_drain_registration():
    """THE WIRING, and the third time today this exact gap has appeared.

    `_schedule_bar_drain` can be perfect and drivable while nothing invokes it — deleting the call
    from `on_start` left the suite green. The same shape as `_lane_symbols`, which momentum DEFINED
    and never called, and as #619's budget, which the fixture injected instead of deriving.

    A method being correct says nothing about it running.
    """
    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.UiFeedStrategy.on_start)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) \
            and isinstance(fn.body[0].value.value, str):
        fn.body = fn.body[1:]                      # a docstring naming it must not satisfy this
    assert "_schedule_bar_drain()" in ast.unparse(fn), (
        "on_start never registers the paced drain — every historical request queues forever and no "
        "bars arrive, which is worse than the burst and looks deliberate (#617)"
    )


def test_LIVE_subscription_is_NOT_chained_behind_the_paced_backfill():
    """MEASURED ON STAGING, one deploy after #617 shipped.

        live bar events / 25m    0        RequestBars    150
        trade ticks / 25m        0        SubscribeBars  150  = 6.0/min, the pacing exactly

    `subscribe_bars` used to hang off `request_bars`'s callback. Pacing the historical requests
    therefore paced the LIVE subscriptions too — roughly five hours before a lane could see a live
    price, and BCTROT-004 could not arm at all because warmup needs 101 bars it had not received.

    THE PREMISE THIS TEST ORIGINALLY CARRIED WAS WRONG, and #836 corrected it with a measurement.
    It said "a streaming subscription does not consume IB's historical allowance". On IB it is one:
    the shipped adapter's `subscribe_historical_bars` issues `reqHistoricalData(keepUpToDate=True)`
    for every bar size but 5s, and 297 of them in one second on 2026-09-09 was most of the burst
    that left IB silent for the session. What this test PROTECTS is the intent — live data must
    not wait behind the whole backfill — and #836 keeps it by PRIORITY rather than by exemption:
    the live subscription is queued AHEAD of every history request, never chained behind one.

    So the assertion is now the ordering, not the synchrony: on a paced provider a live
    subscription for a symbol leaves before that symbol's own history does. The synchronous form is
    still pinned for an UNPACED provider in `test_boot_does_not_burst_the_venue.py`.
    """
    import types

    from nautilus_trader.model.identifiers import InstrumentId

    from api.test_boot_does_not_burst_the_venue import _probe

    s = _probe(rate=6.0)
    s._after_definition(InstrumentId.from_str("AAPL.XNAS"))
    for _ in range(8):
        s._drain_bar_requests()
    kinds = [k for k, bt in s.calls if "AAPL.XNAS-1-MINUTE" in bt]
    assert kinds and kinds[0] == "subscribe", (
        f"the live subscription left AFTER the history request for the same symbol: {s.calls[:4]} — "
        f"live data is chained behind the backfill again (#617), now by queue order"
    )
