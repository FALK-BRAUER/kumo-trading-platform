"""An exit must be able to take back the shares its own protective stop is holding.

WHY THIS FILE EXISTS
--------------------
On 2026-08-19 at 13:35Z MOMENTUM-002 decided `hold 8 · enter 3 · exit 2` — its first decision since
08-14 — and BOTH exits were refused by the venue:

    FSM 933   rejected: insufficient qty available for order (requested: 933, available: 0)
    VCTR 88   rejected: insufficient qty available for order (requested: 88, available: 0)

The shares were held. `long_market_value` was 74,638.76 at the time. They were RESERVED, by the
position's own `PROT-SELL-*` trailing stop, which claims the full quantity. So the rotation could not
rotate: capital stayed locked in names the strategy had decided to leave, and every other strategy
stayed unfunded behind it. Third live reproduction of this shape (#245, #252, now #358).

WHY THIS IS A ROUTING BUG, NOT A MISSING MECHANISM
--------------------------------------------------
Cockpit already solved this. The exact sequence appears TWICE — `engine_node.py:1681` (manual flatten)
and `engine_node.py:2213` (PEAK) — and both are hardened by two prior incidents:

    await self._cancel_reducing_leg(instrument_id, strategy_id, reducing_side)
    cleared = await self._await_reducing_orders_clear(instrument_id, strategy_id, reducing_side)
    if cleared:
        cleared = await self._await_shares_available(instrument_id, qty)

Neither is reachable from the strategy exit path, which goes `pgrunner._submit` -> `NautilusBroker.
submit` -> `Strategy.submit_order` and never asks anyone to release anything. So this is the seventh
"built, configured, deployed, never executed" instance found this week, and the most expensive one:
the mechanism that would have saved today's rotation was already written, already tested, already
running — for two other callers.

WHAT `release_for_exit` MUST GUARANTEE
--------------------------------------
1. False means SEND NOTHING. The position keeps whatever protects it. Firing an exit on an
   unconfirmed release is exactly how #245 lost five protective stops on 2026-08-12, and
   `_await_shares_available`'s own docstring says so.
2. The reconciler must not re-arm the stop between the cancel and the sell — that reinstates the
   reservation and the exit is refused again, having removed protection for nothing.
3. That suppression must be TTL-BOUNDED. Suppression is a window in which the position is
   deliberately unprotected; a failed exit must not leave it that way forever. This is the one part
   with no prior art in the two working call sites, because neither of them suppresses at all.

Cockpit #358. The matching half (`NautilusBroker.exit`, `pgrunner.py:816`) is kumo-strategies'.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import textwrap
from decimal import Decimal

import pytest

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _func_source(name: str) -> str:
    source = _ENGINE.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} does not exist — this test is blind and must be rewritten")


class _Log:
    def __init__(self):
        self.errors, self.warnings, self.infos = [], [], []

    def error(self, msg, *a):
        self.errors.append(str(msg))

    def warning(self, msg, *a):
        self.warnings.append(str(msg))

    def info(self, msg, *a):
        self.infos.append(str(msg))

    def exception(self, msg, ex):
        """`ex` is REQUIRED, exactly as production requires it.

        `Logger.exception(self, str message, ex)` runs `Condition.not_none(ex, "ex")`
        (common/component.pyx:1564). A double accepting `*a` let the call be written without the
        exception and stay green, while production raised TypeError INSIDE the except handler — so
        `release_for_exit` propagated instead of returning False. `test_protection_reconciler.py` already
        hardened its own `_Log` for this exact defect; this file had regressed against it.
        """
        self.errors.append(f"{msg}|{ex!r}")


class _Clock:
    def __init__(self, now_ns=1_800_000_000_000_000_000):
        self.now = now_ns

    def timestamp_ns(self):
        return self.now


class _Host:
    """A duck-typed `self` carrying exactly what `release_for_exit` touches.

    NOT `UiFeedStrategy.__new__`: `cache` and `clock` are read-only attributes on Nautilus's Cython
    `Actor`, so a stub built that way cannot exist — the price-staleness double proved that the hard
    way. The house rule is to fix the double rather than loosen production, so this binds the REAL
    unbound coroutine to a host that provides only what it reads. If it reaches for anything else,
    this raises rather than silently passing.
    """

    def __init__(self, *, clears=True, shares=True, raises=None, cancel_cost_ns=0):
        self.log, self.clock = _Log(), _Clock()
        self.calls: list[str] = []
        self._clears, self._shares, self._raises = clears, shares, raises
        self._cancel_cost_ns = cancel_cost_ns
        self._t0 = self.clock.timestamp_ns()
        self._exit_suppressed: dict[str, int] = {}
        # The RELEASE WINDOW production opens beside the suppression (#546 part 2) — the observable
        # that survives an exit which never reaches the venue. A host without it cannot run the real
        # `release_for_exit`, which is the point of this double.
        self._exit_windows: dict[str, dict] = {}
        #: Production's `self.id`. UiFeedStrategy is MANUAL-001 (engine_node.py:314), and the protective
        #: stops it builds carry that id — which is the whole point of the owner assertion below.
        self.id = "MANUAL-001"

    def _close_exit_window(self, instrument_id: str) -> None:
        """Production drops the window in the same branch that drops the suppression (#546 part 2);
        a host without it cannot run the real release_for_exit."""
        self._exit_windows.pop(instrument_id, None)

    def _assert_still_standing_off(self, instrument_id, step):
        """THE STANDOFF MUST BE LIVE AT EVERY STEP, not merely at the end.

        Asserting only the final deadline let the extension after a slow cancel be DELETED while the
        suite stayed green — the last extension refreshed it anyway. What matters is the window in
        BETWEEN: if the mark lapses while the release is still working, the reconciler re-arms, the
        shares are reserved again, and the exit is refused with its protection already cancelled.
        """
        # PAST THE ABSOLUTE CEILING THE STANDOFF IS DELIBERATELY OVER. The two guarantees genuinely
        # conflict once a venue hangs for longer than `_EXIT_SUPPRESSION_MAX_S`: the engine cannot both
        # keep standing off and bound how long a position stays unprotected. The ceiling wins, by design
        # — an unbounded naked position is worse than a re-armed stop. Asserting through that case would
        # be demanding behaviour the design deliberately rejects.
        if self.clock.timestamp_ns() - self._t0 > self._EXIT_SUPPRESSION_MAX_S * 1e9:
            return
        until = self._exit_suppressed.get(instrument_id)
        assert until is not None and until > self.clock.timestamp_ns(), (
            f"the standoff had lapsed by the time {step} ran (deadline={until}, "
            f"now={self.clock.timestamp_ns()}) — the reconciler is free to re-arm mid-release"
        )

    def _boom(self, where):
        if self._raises == where:
            raise RuntimeError(f"venue blew up in {where}")

    async def _cancel_reducing_leg(self, instrument_id, strategy_id, reducing_side, proxy=False,
                                   canceller=None):
        # `proxy` mirrors production, which gained it when `release_for_exit` had to declare that it
        # cannot name the asking lane. A double whose signature lags production fails at the CALL —
        # which is loud, and is how this one was found — but a double that quietly accepted **kwargs
        # would have hidden it.
        self.calls.append("cancel")
        # A slow venue burns real time here — one list_orders plus one cancel_order PER RESTING ORDER,
        # none of them bounded by any deadline in production. The double can express that; a fixed clock
        # could not, so the standoff-expiry-under-a-live-release case had no test.
        self.clock.now += self._cancel_cost_ns
        # THE SEAM, asserted at the moment it matters rather than at the end. Moving the mark to AFTER
        # the cancel left every end-state assertion green while reopening the race the mark exists to
        # close: the reconciler is free to fire in between and re-arm.
        assert instrument_id in self._exit_suppressed, (
            "the cancel went out BEFORE the instrument was marked — the reconciler can re-arm in that "
            "window and reinstate the reservation this is about to clear"
        )
        self.owner_seen = strategy_id
        self._boom("cancel")

    async def _await_reducing_orders_clear(self, instrument_id, strategy_id, reducing_side, **kw):
        self.calls.append("await_clear")
        self._assert_still_standing_off(instrument_id, "the cancel-confirm wait")
        self.owner_seen = strategy_id
        self._boom("clear")
        return self._clears

    async def _await_shares_available(self, instrument_id, needed, **kw):
        self.calls.append("await_shares")
        self._assert_still_standing_off(instrument_id, "the share wait")
        self._boom("shares")
        assert isinstance(needed, Decimal), (
            "quantity reached _await_shares_available as "
            f"{type(needed).__name__}; it compares with `>=` against a Decimal from the venue, and a "
            "float there is the Quantity/float seam that killed the whole shrink path once already"
        )
        return self._shares


def _protection(monkeypatch, *, enabled=True, market_open=True):
    """Production refuses to cancel protection it cannot put back, so every test must say which world.

    Both re-arm routes are conditional — the reconciler returns early when the flag is off (default
    False) and again outside RTH — so a release in either state would leave the position naked with
    nothing to restore it.
    """
    import api.engine_node as en

    monkeypatch.setitem(
        __import__("sys").modules, "api.settings",
        type("m", (), {"resolve": staticmethod(lambda d: {"enabled": enabled})}),
    )
    monkeypatch.setattr(en, "_us_market_open", lambda _ns: market_open)


def _bind(**kw) -> _Host:
    from api.engine_node import UiFeedStrategy

    host = _Host(**kw)
    # The REAL constant, not a stand-in. The TTL assertion below is only worth anything if it measures
    # the value production actually uses — a locally invented one would pin the test to itself, which is
    # how a double stops representing the thing it doubles.
    host._EXIT_SUPPRESSION_S = UiFeedStrategy._EXIT_SUPPRESSION_S
    host._EXIT_SUPPRESSION_MAX_S = UiFeedStrategy._EXIT_SUPPRESSION_MAX_S
    host._extend_standoff = lambda *a, **k: UiFeedStrategy._extend_standoff(host, *a, **k)
    host.release_for_exit = lambda *a, **k: UiFeedStrategy.release_for_exit(host, *a, **k)
    return host


def _release(host, qty=933, **kw):
    return asyncio.run(host.release_for_exit("FSM.XNYS", qty, **kw))


# -- the guarantee -----------------------------------------------------------------------------


def test_a_confirmed_release_reports_true_and_frees_the_shares(monkeypatch):
    _protection(monkeypatch)
    host = _bind()
    assert _release(host) is True
    assert host.calls == ["cancel", "await_clear", "await_shares"], (
        f"the release did not follow the sequence both working call sites use: {host.calls}"
    )


def test_an_unconfirmed_cancel_sends_nothing(monkeypatch):
    """The #245 direction. A cancel that is not CONFIRMED has not released anything."""
    _protection(monkeypatch)
    host = _bind(clears=False)
    assert _release(host) is False, (
        "the release reported success while the venue had not confirmed the cancel — an exit sent "
        "here is refused on `available: 0` AND the cancel lands anyway, stripping protection"
    )
    assert "await_shares" not in host.calls, (
        "it went on to wait for shares after the cancel was never confirmed — the two waits are "
        "sequential for a reason, and skipping that ordering is what #252 did"
    )


def test_shares_that_never_free_send_nothing(monkeypatch):
    _protection(monkeypatch)
    host = _bind(shares=False)
    assert _release(host) is False, (
        "reported success while the broker still showed the shares reserved — this is the exact "
        "`available: 0` that refused FSM 933 and VCTR 88"
    )


# -- the part with no prior art ---------------------------------------------------------------


def test_the_reconciler_cannot_rearm_the_stop_while_an_exit_is_in_flight(monkeypatch):
    """Without this the fix defeats itself.

    The protection reconciler runs on a timer. If it re-arms between the cancel and the sell, the
    reservation is back and the exit is refused again — having removed protection for nothing. So the
    release must mark the instrument, and the reconciler must honour the mark.
    """
    _protection(monkeypatch)
    host = _bind()
    _release(host)
    assert "FSM.XNYS" in host._exit_suppressed, (
        "the release never marked the instrument, so the reconciler is free to re-arm the stop "
        "between the cancel and the sell and reinstate the reservation it just cleared"
    )

    # A flag nothing checks is the defect class this whole file is about, so the mark is only half of
    # it. The reconciler's side is asserted by AST in
    # `test_the_reconciler_consults_the_pruner_rather_than_the_raw_dict` — pinned there rather than by a
    # substring here, because matching prose is how two earlier tests in this repo stayed green while
    # the wiring they claimed to guard was deleted.
    tree = ast.parse(textwrap.dedent(_func_source("_reconcile_protection_inner")))
    reads_suppression = any(
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "_active_exit_suppressions"
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    )
    assert reads_suppression, (
        "the reconciler never consults the suppression, so marking it achieves nothing and the stop "
        "re-arms between the cancel and the sell"
    )


def test_the_suppression_is_ttl_bounded_not_permanent(monkeypatch):
    """Suppression is a deliberately unprotected window. A failed exit must not make it permanent.

    Asserts the mark is a DEADLINE in the future rather than a bare presence flag, and that it is
    bounded — an hour-long suppression would be a naked position, which is worse than the bug.
    """
    _protection(monkeypatch)
    # The SUCCESS path. On failure the mark is dropped outright — stronger than bounding it, and pinned
    # by `test_a_failed_release_hands_the_position_straight_back_to_the_reconciler`. What needs bounding
    # is the mark that SURVIVES: an exit is genuinely in flight, the position is deliberately unprotected
    # while it goes out, and that window must close on its own because no operator is watching at 13:35Z.
    host = _bind()
    _release(host)
    until = host._exit_suppressed.get("FSM.XNYS")
    assert isinstance(until, int), (
        f"the suppression mark is {until!r}, not a nanosecond deadline — a bare flag never expires "
        "and leaves the position permanently unprotected the first time an exit fails"
    )
    horizon_s = (until - host.clock.timestamp_ns()) / 1e9
    # DERIVED, not a wide range. `0 < horizon_s <= 120` admitted three orders of magnitude: changing
    # `* 1e9` to `* 1e6` made the standoff 45 MILLISECONDS and stayed green, so the next reconciler tick
    # re-arms mid-release and the exit is refused again with the protection already gone. The floor is
    # the cancel confirm (6s) plus the share wait (10s); the ceiling is that plus margin for the
    # caller's own submit. State the arithmetic so whoever changes it has to argue with the numbers.
    assert 20 <= horizon_s <= 60, (
        f"the standoff is {horizon_s:.3f}s. It must cover _await_reducing_orders_clear (6s) plus "
        f"_await_shares_available (10s) plus the caller's submit, and NOTHING more — every second "
        f"beyond that is a position the reconciler has been told to leave naked"
    )


# -- aimed at the class, not the instance -----------------------------------------------------


def test_the_release_reuses_the_hardened_helpers_rather_than_a_private_variant():
    """The point of this file, stated as an assertion.

    Two prior incidents hardened `_cancel_reducing_leg` / `_await_reducing_orders_clear` /
    `_await_shares_available` — the venue fallback for cache-terminal orders, the reservation-vs-order
    -status distinction, the monotonic deadline. A fourth hand-rolled copy would silently re-lose all
    of it, which is exactly how this system accumulated three reproductions of one bug.
    """
    tree = ast.parse(textwrap.dedent(_func_source("release_for_exit")))
    called = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for helper in ("_cancel_reducing_leg", "_await_reducing_orders_clear", "_await_shares_available"):
        assert helper in called, (
            f"`release_for_exit` does not call `{helper}` — it is hand-rolling a release path beside "
            f"the one two incidents already hardened"
        )


def test_the_fixture_can_see_the_thing_it_judges():
    """An assertion over an empty string passes for the wrong reason."""
    assert _func_source("release_for_exit").strip()
    assert _func_source("_reconcile_protection_inner").strip()


# -- the expiry itself, which the tests above could not reach ---------------------------------
#
# Every test above passed with the expiry comparison DELETED — marks made permanent, positions naked
# for good. The deadline was asserted, the expiry never was, because nothing could drive it without
# standing up the whole reconciler. That is the "a test that cannot fail carries no information" rule
# arriving in the file that cites it. Hence the extracted helper and these three.


def _pruner():
    from api.engine_node import UiFeedStrategy

    host = _bind()
    host._active_exit_suppressions = lambda now_ns: UiFeedStrategy._active_exit_suppressions(host, now_ns)
    return host


def test_an_expired_standoff_stops_holding_protection_back():
    host = _pruner()
    now = host.clock.timestamp_ns()
    host._exit_suppressed = {"FSM.XNYS": now - 1}  # deadline already passed
    assert host._active_exit_suppressions(now) == {}, (
        "an expired standoff still counts, so the reconciler keeps standing off and the position stays "
        "unprotected for good — the exact state a FAILED exit leaves behind"
    )
    assert "FSM.XNYS" not in host._exit_suppressed, "the expired mark was returned-but-not-pruned"


def test_a_live_standoff_still_stands():
    """The fixture must be able to go the other way, or the test above proves only that it returns {}."""
    host = _pruner()
    now = host.clock.timestamp_ns()
    host._exit_suppressed = {"FSM.XNYS": now + 10_000_000_000}
    assert host._active_exit_suppressions(now) == {"FSM.XNYS": now + 10_000_000_000}


def test_the_reconciler_consults_the_pruner_rather_than_the_raw_dict():
    """Reading `_exit_suppressed` directly would skip the prune and reinstate the permanent standoff."""
    tree = ast.parse(textwrap.dedent(_func_source("_reconcile_protection_inner")))
    called = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "_active_exit_suppressions" in called, (
        "the reconciler does not go through the pruner, so an expired standoff can keep a position "
        "unprotected indefinitely"
    )


# -- the six mutations that escaped the first suite -------------------------------------------


@pytest.mark.parametrize("where", ["cancel", "clear", "shares"])
def test_a_release_that_throws_reports_failure_and_sends_nothing(monkeypatch, where):
    """Mutation B: `return False` in the except branch flipped to `return True` and the suite stayed green.

    In production that means the caller submits an exit after a release that threw mid-flight, with the
    cancel possibly already landed — the position is naked AND the exit fires against an unconfirmed
    release. That is #245 verbatim. The double never raised, so the branch had no test at all.
    """
    _protection(monkeypatch)
    host = _bind(raises=where)
    assert _release(host) is False, (
        f"a release that threw in {where} reported success — the caller will send an exit against an "
        f"unconfirmed release while the cancel may already have landed"
    )
    assert host.log.errors, "the failure was silent"


@pytest.mark.parametrize("where", ["cancel", "clear", "shares"])
def test_a_failed_release_hands_the_position_straight_back_to_the_reconciler(monkeypatch, where):
    """The standoff protects an exit in flight. If no exit is going out it protects nothing.

    Leaving the mark behind on failure only delays recovery — up to 45s of standoff plus up to 60s to
    the next tick, on a position whose stop was just cancelled.
    """
    _protection(monkeypatch)
    host = _bind(raises=where)
    _release(host)
    assert "FSM.XNYS" not in host._exit_suppressed, (
        "a FAILED release left the standoff in place, so the reconciler stays blocked from re-arming a "
        "position that now has nothing protecting it and no exit coming"
    )


def test_the_owner_is_the_strategy_that_built_the_stop_not_the_one_exiting(monkeypatch):
    """Mutation: the strategy_id that made half the sequence a no-op.

    Protective stops are built by THIS strategy's order_factory, and this strategy is MANUAL-001
    (engine_node.py:314). The caller used to pass MOMENTUM-002, so `_reducing_orders_open`'s
    `str(o.strategy_id) == strategy_id` filter excluded the very stop being cancelled and
    `_await_reducing_orders_clear` returned True on its first poll having confirmed nothing.
    """
    _protection(monkeypatch)
    host = _bind()
    _release(host)
    # #840: the filter is a SET carrying MANUAL-001 — and the lane too when one is exiting (#748
    # stamps the stop with the lane). Without a lane it is MANUAL-001 alone, as before.
    assert "MANUAL-001" in host.owner_seen, (
        f"the cancel/confirm ran against {host.owner_seen!r}, without MANUAL-001 — an aggregate "
        f"PROT-SELL stop would be missed and the confirm step would wait for nothing (#245, #252)"
    )


def test_a_short_exit_raises_rather_than_silently_cancelling_the_wrong_side(monkeypatch):
    """Mutation I: hardcoding `reducing_side = SELL` stayed green, because nothing passed anything else.

    The BUY branch is dead rather than untested — `_venue_shares_available` returns Alpaca's
    `qty_available` verbatim, which is NEGATIVE for a short, so the share wait can never be satisfied.
    Worse, the old `else` mapped ANY unrecognised value to BUY, which for a long position is the ENTRY
    leg. Raising is the honest answer until shorts are actually handled.
    """
    _protection(monkeypatch)
    host = _bind()
    with pytest.raises(ValueError, match="SELL"):
        _release(host, side="BUY")


@pytest.mark.parametrize(
    "enabled,market_open,why",
    [(False, True, "protection is disabled, so nothing would ever re-arm"),
     (True, False, "the market is closed, so the reconciler returns before placing")],
)
def test_protection_is_never_removed_when_nothing_can_put_it_back(monkeypatch, enabled, market_open, why):
    """Do not cancel a stop this engine cannot restore.

    Both re-arm routes are conditional: the reconciler returns early when `protection.enabled` is False
    (it DEFAULTS to False) and again outside RTH. A release at 15:58 ET, or with the flag off, cancels a
    stop that nothing puts back — naked until the next session, or forever. A missed exit is
    recoverable; that is not.
    """
    _protection(monkeypatch, enabled=enabled, market_open=market_open)
    host = _bind()
    assert _release(host) is False, f"released shares even though {why}"
    assert host.calls == [], f"CANCELLED a protective stop even though {why}"


# -- every OTHER route that places a stop ------------------------------------------------------
#
# Suppressing only the protection reconciler left three routes open: PEAK's arm (engine_node.py:2266)
# and the manager tick's tighten/blow-off paths (5981, 6069, 6393). A PEAK-managed position could get a
# fresh trail resting INSIDE the release window, re-reserving exactly the shares the release had just
# freed — and the exit would then be refused on `available: 0` with its protection already cancelled.
# Every trailing stop in this engine goes through `_submit_trailing_stop`, so that is where the standoff
# is honoured for all of them.


#: The same instant `_Clock` reports, so a deadline built here is comparable with the one the guard reads.
_NOW = _Clock().timestamp_ns()


class _Placer:
    """A host for `_submit_trailing_stop`'s guard, carrying only what the guard itself reads.

    Deliberately provides NOTHING the rest of the function needs: if the guard stops refusing, the call
    proceeds and dies on a missing attribute rather than passing quietly. The double cannot express a
    successful placement, which is exactly the property wanted here.
    """

    def __init__(self, suppressed):
        self.clock = _Clock()
        self._exit_suppressed = dict(suppressed)


def _place(host, instrument_id="FSM.XNYS"):
    from api.engine_node import UiFeedStrategy

    host._active_exit_suppressions = lambda ns: UiFeedStrategy._active_exit_suppressions(host, ns)
    return UiFeedStrategy._submit_trailing_stop(
        host, instrument_id=instrument_id, side="SELL", quantity=933.0,
        trail_bps=750.0, coid="PROT-SELL-FSM-XNAS-deadbeef", manager_id=None,
    )


def test_no_route_can_arm_a_stop_while_that_position_is_releasing_shares():
    from api.engine_node import ExitReleaseInFlight

    host = _Placer({"FSM.XNYS": _NOW + 30_000_000_000})
    with pytest.raises(ExitReleaseInFlight):
        _place(host)


def test_the_guard_is_scoped_to_the_instrument_being_exited():
    """A standoff on one name must not stop every other position being protected.

    Reaches the guard for a DIFFERENT instrument and is expected to fail past it — on a missing
    attribute, because this double cannot place an order. Asserting "not ExitReleaseInFlight" is the
    point: the guard let it through.
    """
    from api.engine_node import ExitReleaseInFlight

    host = _Placer({"FSM.XNYS": _NOW + 30_000_000_000})
    with pytest.raises(Exception) as caught:
        _place(host, instrument_id="AEM.XNYS")
    assert not isinstance(caught.value, ExitReleaseInFlight), (
        "a standoff on FSM blocked protection for AEM — one exit would leave every other position naked"
    )


def test_an_expired_standoff_stops_blocking_placement():
    """The standoff must not outlive its deadline here either, or protection never comes back."""
    from api.engine_node import ExitReleaseInFlight

    host = _Placer({"FSM.XNYS": _NOW - 1})
    with pytest.raises(Exception) as caught:
        _place(host)
    assert not isinstance(caught.value, ExitReleaseInFlight), (
        "an EXPIRED standoff still blocks placement — the position can never be re-armed"
    )


# -- the standoff must outlast a slow venue, but not indefinitely ------------------------------


def test_a_slow_cancel_does_not_let_the_standoff_expire_under_a_live_exit(monkeypatch):
    """The window arithmetic was a guess, and the guess omitted the cancel entirely.

    `_EXIT_SUPPRESSION_S`'s comment claimed 45s covers "the cancel confirm (6s) plus the share wait (10s)
    plus the submit". It does not cover `_cancel_reducing_leg`, which makes one `list_orders` plus one
    `cancel_order` round trip PER RESTING ORDER with no deadline anywhere in this file. Two resting stops
    on a slow venue and the standoff expires while the exit is still in flight — the reconciler re-arms,
    re-reserves the shares the release just freed, and the exit is refused on `available: 0`. #358 caused
    by the fix for #358.
    """
    _protection(monkeypatch)
    # 50s inside the CANCEL ALONE — longer than the whole original 45s window, and well under the 120s
    # ceiling so the standoff is still meant to be standing. The first draft used 40s, which is UNDER
    # the window: the mark had not lapsed, so the fixture could not violate the invariant either way and
    # deleting the extension stayed green. Assert the fixture reaches the bug, not just that it passes.
    slow = 50_000_000_000
    host = _bind(cancel_cost_ns=slow)
    assert _release(host) is True

    remaining_s = (host._exit_suppressed["FSM.XNYS"] - host.clock.timestamp_ns()) / 1e9
    assert remaining_s > 0, (
        f"the standoff had already expired ({remaining_s:.1f}s left) by the time the release finished — "
        f"the reconciler is free to re-arm underneath an exit that is about to be submitted"
    )


def test_the_extension_cannot_run_away_however_slow_the_venue_is(monkeypatch):
    """Extending without a ceiling trades a re-armed stop for an unbounded naked position.

    Pins the ceiling as an ABSOLUTE bound measured from the start of the release, not a per-step reset.
    """
    _protection(monkeypatch)
    host = _bind(cancel_cost_ns=300_000_000_000)  # 300s: far past the ceiling
    started = host.clock.timestamp_ns()
    _release(host)

    until = host._exit_suppressed.get("FSM.XNYS")
    assert until is not None, "the standoff was dropped on a SUCCESSFUL release"
    held_s = (until - started) / 1e9
    assert held_s <= host._EXIT_SUPPRESSION_MAX_S, (
        f"one release held the position off for {held_s:.0f}s, past the {host._EXIT_SUPPRESSION_MAX_S}s "
        f"ceiling — a hung venue would leave it unprotected for as long as it hangs"
    )


def test_the_cancel_confirm_FILTER_and_the_authorisation_identity_are_DIFFERENT_arguments():
    """THE 2026-08 DEFECT, kept impossible by construction (review, kumo-strategies).

    `NautilusBroker.exit`'s docstring records that passing a strategy_id to `release_for_exit` was tried
    and was WRONG:

        "it silently excluded the real order from the cancel-confirm filter and returned True having
         waited for nothing"

    True having waited for nothing is a NAKED POSITION — strictly worse than the #462 it would be
    fixing. That attempt used the id as a FILTER (which resting order to wait on). #462 needs it as an
    AUTHORISATION identity (who is asking). Same name, different argument, opposite consequence.

    `_reducing_orders_open` filters on `str(o.strategy_id) == strategy_id`, and protective stops are
    MANUAL-001's. So the FILTER must stay MANUAL-001 whichever lane is exiting; only the authorisation
    check may read the lane. This asserts the two are separate PARAMETERS, so a future edit cannot
    quietly make one serve both.
    """

    from api.engine_node import UiFeedStrategy

    params = inspect.signature(UiFeedStrategy._cancel_reducing_leg).parameters
    assert "strategy_id" in params, "the filter identity vanished"
    assert "canceller" in params, (
        "there is no separate authorisation identity — if `strategy_id` is being used for BOTH the "
        "cancel-confirm filter and the may-cancel check, threading a lane through re-creates the "
        "'returned True having waited for nothing' defect"
    )

    src = inspect.getsource(UiFeedStrategy.release_for_exit)
    # #840: the filter carries BOTH stamps. MANUAL-001 must never leave it (aggregate stops); the
    # lane must be IN it (since #748 the stop covering a lane's shares carries the lane).
    assert "owner = {str(self.id)} | " in src, (
        "release_for_exit no longer pins MANUAL-001 into the FILTER — an aggregate PROT-SELL stop "
        "would be missed and the wait would confirm nothing (#245, #252)"
    )


def test_a_NAMED_lane_becomes_the_authorisation_identity_and_NOT_the_filter(monkeypatch):
    """The path kumo-strategies is about to start using, driven before they do.

    Three mutations survived without this test — `canceller` falling back to the filter id, the lane
    being ignored entirely, and the FILTER following the lane — because NOTHING passed `strategy_id`
    yet. An acceptor no caller drives is an acceptor no test drives, which is the third time tonight
    the same gap has appeared one layer further out.

    The two identities must move independently:
      * FILTER carries MANUAL-001 AND the lane (#840): MANUAL-001 holds the aggregate stops, and
        since #748 the stop covering a lane's shares carries the lane. A filter with only one of
        them watches nothing in the other regime — the venue guard then refuses the exit.
      * AUTHORISATION becomes the lane, which is what makes #462 enforceable on this path at all.
    """
    _protection(monkeypatch)          # production refuses to cancel what it cannot put back
    host = _bind()
    seen = {}

    async def _record(instrument_id, strategy_id, reducing_side, proxy=False, canceller=None):
        seen.update(filter_id=strategy_id, canceller=canceller, proxy=proxy)

    host._cancel_reducing_leg = _record
    _release(host, strategy_id="MOMENTUM-002")

    # #840: BOTH — MANUAL-001 for aggregate stops, the lane for the stop #748 stamped with it. A
    # filter that REPLACED MANUAL-001 with the lane would miss the aggregate stop; a filter WITHOUT
    # the lane watched nothing on 60 lane exits.
    assert seen["filter_id"] == {"MANUAL-001", "MOMENTUM-002"}, (
        f"the cancel-confirm filter is {seen['filter_id']!r} — it must carry MANUAL-001 AND the lane"
    )
    assert seen["canceller"] == "MOMENTUM-002", (
        f"the lane never reached the authorisation check ({seen['canceller']}) — #462 stays unenforceable"
    )
    assert seen["proxy"] is False, (
        "the proxy concession is still on while the lane IS known — it would keep permitting a lane to "
        "cancel another lane's protective stop, which is the whole defect"
    )


def test_WITHOUT_a_named_lane_the_proxy_concession_stays_ON(monkeypatch):
    """Today's behaviour, and the discriminating half. kumo-strategies does not pass a lane yet, so the
    concession must remain — without it the rule refuses a lane's own protective stop and every exit on
    a protected symbol dies on `available: 0`."""
    _protection(monkeypatch)          # production refuses to cancel what it cannot put back
    host = _bind()
    seen = {}

    async def _record(instrument_id, strategy_id, reducing_side, proxy=False, canceller=None):
        seen.update(filter_id=strategy_id, canceller=canceller, proxy=proxy)

    host._cancel_reducing_leg = _record
    _release(host)

    assert seen["proxy"] is True and seen["canceller"] is None
    assert seen["filter_id"] == {"MANUAL-001"}          # no lane → MANUAL-001 alone (#840)
