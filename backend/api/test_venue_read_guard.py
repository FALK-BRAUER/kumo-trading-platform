"""A venue order read that FAILED must not be read as a venue that holds no orders (#791).

WHAT HAPPENED. On 2026-09-04, test-alpaca lost the network to Alpaca for about twenty seconds:

    08:45:23Z  DataClient-ALPACA: Alpaca WS closed; reconnecting
    08:45:31Z  Alpaca WS connect failed: ClientConnectorDNSError(stream.data.alpaca.markets)
    08:45:36Z  ExecEngine: Failed to generate order status reports: Cannot connect to host
               paper-api.alpaca.markets:443
    08:46:03Z  33 x OrderRejected(reason='ORDER_NOT_FOUND_AT_VENUE')

`live/execution_engine.py:1574` catches a failed report generation, LOGS it, and `continue`s. The
function's return value cannot express "we could not ask", so a read that FAILED and a venue that
genuinely holds nothing produce the identical empty `venue_reported_ids`.
`_handle_missing_orders_at_venue` then concludes every cached open order is gone, and after
`open_check_missing_retries` (5) at our `open_check_interval_secs` (5.0s) — twenty-five seconds —
rejects them all. They were resting at Alpaca the whole time.

Everything that followed came from that one window: 117,689
`InvalidStateTrigger: REJECTED -> ACCEPTED`, 23 instruments reporting a protective order the engine
could not see, and two phantom position pairs (PATH, TOST) minted when one of the rejected stops
actually filled and the fill could not be applied to an order the cache believed rejected.

THE VENDOR ALREADY KNOWS THE RIGHT ANSWER. The POSITION path took the identical outage in the same
second and skipped every symbol untouched:

    Failed to generate position status reports for venue None: Cannot connect to host ...
      Skipping position reconciliation for PATH.XNYS: failed to query venue position status for XNYS

Unreadable -> touch nothing. Two derivations of one rule inside the adapter, and only one is right.
This guard makes the order path agree with the position path.

THREE STATES, NEVER TWO. `ok` proceeds; `failed` refuses; and NEVER-READ also refuses, because a
check that has not asked yet knows nothing about what the venue holds.
"""

from __future__ import annotations

import asyncio
import inspect


class _Log:
    _LEVELS = ("warning", "info", "error", "debug", "exception")

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def __getattr__(self, name):
        if name in _Log._LEVELS:
            def _rec(msg, *a, **k):
                self.records.append((name, str(msg)))
            return _rec
        raise AttributeError(name)

    @property
    def lines(self):
        return [m for _l, m in self.records]

    def at(self, level):
        return [m for lv, m in self.records if lv == level]


class _Client:
    """An execution client whose venue read can succeed or fail the way production's does.

    Alpaca's failure is an `aiohttp.ClientConnectorError` escaping
    `generate_order_status_reports`; IBKR's (#785) was a `ValueError` out of the Cython identifier.
    Both are plain exceptions escaping the same call, which is why this double raises rather than
    returning a short list — a double that returned `[]` could not tell the two apart, and the
    inability to tell them apart IS the defect.
    """

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def generate_order_status_reports(self, command):
        self.calls += 1
        if self.fail:
            raise ConnectionError(
                "Cannot connect to host paper-api.alpaca.markets:443 ssl:default"
            )
        return []


class _ExecEngine:
    """Stands in for `LiveExecutionEngine`: the two attributes the guard touches, and the bound
    method it wraps."""

    def __init__(self, clients):
        self._clients = dict(clients)
        self._log = _Log()
        self.handled: list[tuple] = []

    async def _handle_missing_orders_at_venue(self, open_order_ids, venue_reported_ids):
        # The real one rejects every id in `open_order_ids - venue_reported_ids`. Recording the call
        # is enough: if it is reached at all with an empty `venue_reported_ids`, the damage follows.
        self.handled.append((set(open_order_ids), set(venue_reported_ids)))


CACHED = {"PROT-SELL-TOST", "PROT-SELL-PATH"}


def _install(engine):
    from api.exec_read_guard import install_venue_read_guard

    return install_venue_read_guard(engine)


def _read(engine, client_id="ALPACA"):
    client = engine._clients[client_id]
    try:
        return asyncio.run(client.generate_order_status_reports(object()))
    except Exception:
        return None


def _missing(engine, reported=frozenset()):
    asyncio.run(engine._handle_missing_orders_at_venue(CACHED, set(reported)))


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTIES FIRST. Both must pass before AND after the fix.
# ---------------------------------------------------------------------------------------------

def test_the_fixture_can_express_the_bug__an_UNGUARDED_engine_rejects_on_a_FAILED_read():
    """Without the guard, a failed read and a clean-but-empty venue are the same call. If this did
    not hold, every assertion below would be about nothing."""
    engine = _ExecEngine({"ALPACA": _Client(fail=True)})
    assert _read(engine) is None, "the double did not fail the way production fails"
    _missing(engine)
    assert engine.handled == [(CACHED, set())], engine.handled


def test_the_fixture_can_express_the_bug__the_failure_is_an_EXCEPTION_not_a_short_list():
    """`live/execution_engine.py:1574` only drops BaseExceptions. A double that returned `[]` on
    failure would be exercising the wrong branch entirely."""
    client = _Client(fail=True)
    with_exc = None
    try:
        asyncio.run(client.generate_order_status_reports(object()))
    except Exception as exc:  # noqa: BLE001
        with_exc = exc
    assert isinstance(with_exc, Exception)
    assert "paper-api.alpaca.markets" in str(with_exc)


# ---------------------------------------------------------------------------------------------
# THE PROPERTY.
# ---------------------------------------------------------------------------------------------

def test_a_FAILED_read_does_not_reach_the_missing_order_handler():
    """The whole ticket. Killed by installing nothing, or by treating a failed read as ok."""
    engine = _install(_ExecEngine({"ALPACA": _Client(fail=True)}))
    _read(engine)
    _missing(engine)
    assert engine.handled == [], "a failed venue read still rejected the cached orders"


def test_a_SUCCESSFUL_read_still_reaches_it__the_guard_is_not_an_off_switch():
    """A guard that refuses everything is not a guard: fills and cancels would stop surfacing, which
    is the reason `open_check_interval_secs` was turned on at all. Killed by returning early
    unconditionally."""
    engine = _install(_ExecEngine({"ALPACA": _Client()}))
    _read(engine)
    _missing(engine, reported={"PROT-SELL-PATH"})
    assert engine.handled == [(CACHED, {"PROT-SELL-PATH"})]


def test_a_read_that_has_NEVER_RUN_does_not_reach_it_either():
    """Three states, never two. Before the first read the engine knows nothing about what the venue
    holds, and 'not asked' is not 'asked and empty'. Killed by defaulting the state to ok."""
    engine = _install(_ExecEngine({"ALPACA": _Client()}))
    _missing(engine)
    assert engine.handled == []


def test_recovery__a_read_that_SUCCEEDS_after_a_failure_lifts_the_refusal():
    """A latch that never releases turns a 20-second blip into a permanent outage of the very
    mechanism that surfaces fills. Killed by never clearing the failed state."""
    client = _Client(fail=True)
    engine = _install(_ExecEngine({"ALPACA": client}))
    _read(engine)
    _missing(engine)
    assert engine.handled == []

    client.fail = False
    _read(engine)
    _missing(engine, reported={"PROT-SELL-PATH"})
    assert engine.handled == [(CACHED, {"PROT-SELL-PATH"})]


def test_ONE_failing_client_of_two_refuses_the_WHOLE_pass():
    """`venue_reported_ids` is a UNION across clients, so a second client's healthy report cannot
    vouch for the first client's orders — they are simply absent from the union. Killed by requiring
    all clients to fail before refusing."""
    engine = _install(_ExecEngine({"ALPACA": _Client(fail=True), "IB": _Client()}))
    _read(engine, "ALPACA")
    _read(engine, "IB")
    _missing(engine, reported={"PROT-SELL-PATH"})
    assert engine.handled == []


def test_the_refusal_SAYS_SO_at_ERROR_and_names_the_client():
    """A silent skip is indistinguishable from a healthy quiet pass, and this one suppresses a
    safety mechanism — it must be legible in the log that an operator reads after an incident."""
    engine = _install(_ExecEngine({"ALPACA": _Client(fail=True)}))
    _read(engine)
    _missing(engine)
    said = " ".join(engine._log.at("error"))
    assert "ALPACA" in said, engine._log.records
    assert "791" in said, engine._log.records


def test_installing_twice_does_not_double_wrap():
    engine = _ExecEngine({"ALPACA": _Client()})
    _install(engine)
    once = engine._handle_missing_orders_at_venue
    _install(engine)
    assert engine._handle_missing_orders_at_venue is once


def test_the_guard_RETURNS_the_engine_so_the_wiring_reads_as_one_line():
    engine = _ExecEngine({"ALPACA": _Client()})
    assert _install(engine) is engine


# ---------------------------------------------------------------------------------------------
# THE SEAM and THE CONFIG.
# ---------------------------------------------------------------------------------------------

def test_the_NODE_installs_the_guard_after_it_builds():
    """A correct guard nothing installs is the defect with extra steps — the shape this repo has
    shipped three times (`_lane_symbols` defined and never called, #606 wired into one of two
    providers, `claimed_symbols=` passed to a constructor that had no such parameter).

    Asserts the call is REACHED from the module that builds the node, resolving the name rather than
    grepping for a string a dead definition would satisfy.
    """
    import ast

    import api.engine_node as en

    tree = ast.parse(inspect.getsource(en))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "install_venue_read_guard" in called, (
        "engine_node builds the node but never installs the #791 guard — a failed venue read would "
        "still reject every resting order"
    )
    from api.exec_read_guard import install_venue_read_guard

    assert callable(install_venue_read_guard)


def test_EVERY_config_that_turns_the_POLL_ON_also_widens_the_retry_budget():
    """The second half, and the one that was vacuous.

    `open_check_missing_retries` defaults to 5, and at `open_check_interval_secs=5.0` that is
    TWENTY-FIVE SECONDS before the engine disowns every resting order — less than the outage on
    2026-09-04. Nautilus's own default for the interval is `None`, i.e. the check is OFF, so this
    window is one this repo opted into for fast fill surfacing.

    THE FIRST VERSION OF THIS TEST PASSED OVER THE DEFECT. `engine_node` builds `LiveExecEngineConfig`
    TWICE — a durable one and the fallback the non-durable path actually uses — and the fix set the
    field on only the first. The test took `min()` across the configs that MENTIONED the field, so
    the one that did not mention it was invisible to it. Found by codex, and it is the
    "anchor on something absent" shape: a change that lands on one of two constructions does nothing,
    quietly, while its test reports green.

    So this asserts a PROPERTY OVER ALL of them: any config that turns polling on must also state
    the budget, and every such config must tolerate at least two minutes.
    """
    import ast

    import api.engine_node as en

    tree = ast.parse(inspect.getsource(en))
    configs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "LiveExecEngineConfig":
            kw = {
                k.arg: getattr(k.value, "value", None)
                for k in node.keywords
                if k.arg in ("open_check_interval_secs", "open_check_missing_retries")
            }
            configs.append((node.lineno, kw))

    polling = [(ln, kw) for ln, kw in configs if kw.get("open_check_interval_secs")]
    assert len(polling) >= 2, (
        f"expected both LiveExecEngineConfig constructions to enable polling; found {configs!r} — "
        f"if one was removed this guard is now watching less than it thinks"
    )
    for lineno, kw in polling:
        assert kw.get("open_check_missing_retries") is not None, (
            f"engine_node.py:{lineno} turns order polling on but does not set "
            f"open_check_missing_retries, so Nautilus's default of 5 applies — a 25s tolerance for "
            f"an unreachable venue, which is the #791 incident"
        )
        tolerance = kw["open_check_missing_retries"] * kw["open_check_interval_secs"]
        assert tolerance >= 120, (
            f"engine_node.py:{lineno} tolerates only {tolerance:.0f}s of an unreachable venue "
            f"before disowning resting orders; the 2026-09-04 outage lasted ~20s and 120s is the floor"
        )


def test_the_watcher_RE_RAISES__it_records_and_never_absorbs():
    """The module promises "records, never absorbs", and nothing proved it.

    A watcher that set FAILED and returned None instead of re-raising would satisfy every refusal
    test in this file — and would also change what Nautilus sees: `execution_engine.py:1574` gathers
    with `return_exceptions=True` and drops a failed client's reports from the union. Swallowing it
    would silently promote a failed read to an empty successful one INSIDE the vendor, which is the
    defect this file exists for, reintroduced by its own fix.
    """
    from api.exec_read_guard import install_venue_read_guard

    engine = install_venue_read_guard(_ExecEngine({"ALPACA": _Client(fail=True)}))
    raised = None
    try:
        asyncio.run(engine._clients["ALPACA"].generate_order_status_reports(object()))
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, ConnectionError), f"the watcher absorbed the failure: {raised!r}"
    assert "paper-api.alpaca.markets" in str(raised), "the original error was replaced"


def test_a_CANCELLED_read_counts_as_failed__Nautilus_drops_BaseException_too():
    """`asyncio.gather(..., return_exceptions=True)` at `execution_engine.py:1571` returns ANY
    BaseException, and `:1574` drops all of them from the union. On Python 3.13 `CancelledError` is a
    BaseException, so recording only `Exception` would leave a cancelled read looking like a venue
    that answered "nothing" — the precise confusion this guard exists to end.
    """
    from api.exec_read_guard import install_venue_read_guard

    class _Cancelled(_Client):
        async def generate_order_status_reports(self, command):
            raise asyncio.CancelledError()

    engine = install_venue_read_guard(_ExecEngine({"ALPACA": _Cancelled()}))
    client = engine._clients["ALPACA"]
    try:
        asyncio.run(client.generate_order_status_reports(object()))
    except BaseException:  # noqa: BLE001 — re-raised, as it must be
        pass

    # DISCRIMINATE, or this test cannot fail. Narrowing the watcher to `except Exception` leaves the
    # outcome UNSET, and an unset outcome refuses too — via the never-read branch. So asserting
    # `handled == []` passes either way and proves nothing: the mutation survived exactly this
    # assertion. The claim is that a cancel is recorded as FAILED, so assert the recorded value and
    # the reason given, not merely that something was refused.
    from api.exec_read_guard import _FAILED, _OUTCOME

    assert getattr(client, _OUTCOME, None) == _FAILED, (
        "a cancelled read was not recorded as failed — it reads as 'never asked', which refuses for "
        "the wrong reason and clears the moment any other read succeeds"
    )
    _missing(engine)
    assert engine.handled == [], "a cancelled read was treated as a venue holding nothing"
    said = " ".join(engine._log.at("error"))
    assert "FAILED" in said, engine._log.records


def test_a_client_REGISTERED_AFTER_the_install_is_still_watched():
    """Nautilus registers and deregisters execution clients at runtime
    (`execution/engine.pyx:465`, `:507`) and builds `venue_reported_ids` from whatever is registered
    at the moment of the check. The first version of this guard snapshotted `_clients` at install, so
    a client added later could fail, be dropped by Nautilus, and never be noticed. Killed by reading
    the snapshot instead of the live registry."""
    engine = _install(_ExecEngine({"ALPACA": _Client()}))
    _read(engine)

    engine._clients["IB"] = _Client(fail=True)          # registered after the guard went on
    _read(engine, "IB")
    _missing(engine)
    assert engine.handled == [], "a late-registered client's failed read was not seen"


def test_a_DEREGISTERED_client_does_not_pin_the_refusal_forever():
    """The mirror. A failed client that Nautilus then removes (`execution/engine.pyx:586`) must stop
    counting — otherwise one blip on a client that no longer exists disables missing-order
    resolution for the life of the process. Killed by keeping the outcome in a dict the guard owns
    rather than on the client itself."""
    engine = _install(_ExecEngine({"ALPACA": _Client(), "IB": _Client(fail=True)}))
    _read(engine, "ALPACA")
    _read(engine, "IB")
    _missing(engine)
    assert engine.handled == []

    del engine._clients["IB"]                            # deregistered
    _read(engine, "ALPACA")
    _missing(engine, reported={"PROT-SELL-PATH"})
    assert engine.handled == [(CACHED, {"PROT-SELL-PATH"})], "a departed client still pinned refusal"
