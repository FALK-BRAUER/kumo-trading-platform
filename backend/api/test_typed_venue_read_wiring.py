"""The typed venue read must be REACHABLE, not merely written.

WHY THIS FILE EXISTS
--------------------
`_venue_order_reports` asks the execution client for `OrderStatusReport`s — Nautilus's own form, the one
every adapter produces including the shipped Interactive Brokers one. It is the seam that lets a second
broker work without porting the protection and exit paths.

And it does nothing at all unless `_exec_client` is wired, because a `Strategy` has no standard handle
to the execution client. An accessor that silently returns None is indistinguishable from a venue that
cannot be read — the caller treats both as "assume nothing is safe" — so the failure mode of forgetting
this wiring is a permanently degraded exit path that never says so.

That is the "built, configured, deployed, never executed" shape this codebase has hit seven times in a
week, including a route that reported moving $30,000 and committed nothing. So the wiring is asserted at
the seam, not inferred from the accessor existing.
"""

from __future__ import annotations

import ast
import pathlib

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def test_the_builder_hands_the_execution_client_to_the_feed():
    """The seam. `node.kernel` is an instance attribute set in TradingNode.__init__, and the client only
    exists after `node.build()` — so this assignment has exactly one correct place and it is easy to
    drop in a refactor."""
    tree = ast.parse(_ENGINE.read_text())
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_exec_client" for t in n.targets)
    ]
    assert assigns, (
        "nothing assigns `_exec_client`, so `_venue_order_reports` always returns None and every typed "
        "venue read silently degrades to 'the venue cannot be read'"
    )


def test_the_wiring_happens_AFTER_the_node_is_built():
    """Ordering, because before `node.build()` there is no client to hand over and the assignment would
    bind None — which reads exactly like a broker that is down.

    BOUND TO THE AST, NOT TO A SOURCE STRING. This test used to do
    `src.index("_exec_client = node.kernel")`, which is a literal match on ONE spelling of the
    assignment. Fixing the miswire changed that spelling and the test raised `ValueError` instead of
    reporting anything — a guard that breaks when the line it guards is edited is a guard that will be
    deleted by whoever is trying to land the edit. Same prose-matching class this repo has catalogued.
    """
    src = _ENGINE.read_text()
    tree = ast.parse(src)
    build_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "build"
        and getattr(getattr(n.func, "value", None), "id", None) == "node"
    ]
    wire_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_exec_client"
                and getattr(t.value, "id", None) == "feed" for t in n.targets)
    ]
    assert build_lines, "`node.build()` is no longer called here — this test is blind"
    assert wire_lines, "nothing assigns `feed._exec_client` — this test is blind"
    assert min(build_lines) < min(wire_lines), (
        f"the execution client is grabbed at line {min(wire_lines)}, before `node.build()` at line "
        f"{min(build_lines)}, so it is None — and a None client is indistinguishable from an "
        f"unreadable venue at every call site"
    )


def test_an_unwired_client_reads_as_UNKNOWN_never_as_empty():
    """The distinction the whole exit path rests on.

    `None` means "assume nothing is safe" — callers refuse to act. An empty LIST means "nothing is
    resting", which authorises an exit against shares a protective stop may still be holding. Returning
    the wrong one of those is #245.
    """
    import asyncio

    from api.engine_node import UiFeedStrategy

    class _Host:
        _exec_client = None

    got = asyncio.run(UiFeedStrategy._venue_order_reports(_Host()))
    assert got is None, f"an unwired client returned {got!r} — an empty list would authorise a naked exit"


# ---------------------------------------------------------------------------
# THE SEAM, not the accessor. Added because the other cockpit session reviewed
# #445 and asked the question the tests above cannot answer:
#
#   "`_venue_order_reports` returning None is only half — the value is in
#    `if reports is not None:` at both call sites, and what the else branch
#    DOES. If the else path falls back to the raw read, the invariant holds end
#    to end. If it skips and leaves a stale value, then None and [] converge
#    again one layer up and the guarantee is local rather than real."
#
# They were right that it was untested. `test_an_unwired_client_reads_as_UNKNOWN
# _never_as_empty` pins the ACCESSOR and says nothing about any caller — the
# exact "green helper test proves nothing about wiring" shape that broke
# production five times on 2026-08-14. These drive the real caller.
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self):
        self.warnings, self.exceptions = [], []

    def warning(self, msg):
        self.warnings.append(msg)

    def exception(self, msg, exc=None):
        self.exceptions.append(msg)


class _Http:
    """A REST double that RECORDS whether it was consulted.

    The whole question is whether the fallback FIRES, so the double has to be able
    to answer "nobody called me" — a double that merely returns [] cannot tell a
    fallback that ran from one that never happened.
    """

    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []

    async def list_orders(self, **kw):
        self.calls.append(kw)
        return self.rows


class _ExecClient:
    """An execution client that returns a typed list — including the EMPTY one."""

    def __init__(self, reports):
        self.reports = reports

    async def generate_order_status_reports(self, _command):
        return self.reports


def _host(exec_client, http):
    from api.engine_node import UiFeedStrategy

    class _H:
        pass

    h = _H()
    h._exec_client, h._http, h.log = exec_client, http, _Log()
    h.clock = type("_C", (), {"timestamp_ns": staticmethod(lambda: 0)})()
    h._venue_order_reports = lambda: UiFeedStrategy._venue_order_reports(h)
    return h


def test_an_unreadable_venue_FALLS_BACK_rather_than_answering_nothing_is_resting():
    """None must reach the caller as "ask another way", never as "nothing is there".

    This is the half the accessor test cannot see. If the else branch ever became a
    bare `return None` or a `return []`, the accessor test above stays green and the
    exit path starts authorising sells against shares a protective stop is holding —
    #245, restated.
    """
    import asyncio

    from api.engine_node import UiFeedStrategy

    http = _Http(rows=[])
    h = _host(None, http)  # no execution client -> typed read is UNKNOWN
    asyncio.run(UiFeedStrategy._venue_reducing_orders(h, "WHD.XNYS", "sell"))

    assert http.calls, (
        "the typed read returned None and the caller did NOT fall back to the REST read — None and "
        "[] have converged one layer up, so the UNKNOWN/empty distinction is local to the accessor "
        "and buys nothing at the seam that matters"
    )
    assert http.calls[0].get("paginate") is True, (
        f"the fallback read was not paginated ({http.calls[0]!r}) — a truncated list answers "
        f"'nothing is resting' for an order it simply never received"
    )
    assert h.log.warnings, "the fallback was silent; a venue-specific degradation must say so"


def test_a_readable_venue_that_is_genuinely_EMPTY_does_not_fall_back():
    """The other direction, and the one that makes the test above mean something.

    Without this, `assert http.calls` would pass for a caller that ALWAYS hits REST —
    which would make the typed read decorative. An empty TYPED list is a real answer:
    the venue was read, nothing is resting. It must be believed, not re-asked.
    """
    import asyncio

    from api.engine_node import UiFeedStrategy

    http = _Http(rows=[{"symbol": "WHD", "side": "sell", "status": "new", "qty": "28"}])
    h = _host(_ExecClient([]), http)  # client present, venue genuinely empty
    got = asyncio.run(UiFeedStrategy._venue_reducing_orders(h, "WHD.XNYS", "sell"))

    assert http.calls == [], (
        "an EMPTY typed read fell through to the REST path — the typed read is decorative if its "
        "answer is only believed when non-empty, and the fallback's Alpaca-specific parse would then "
        "run on every venue"
    )
    assert got == [], f"a genuinely empty venue must report nothing resting, got {got!r}"


# ---------------------------------------------------------------------------
# THE DOUBLE AGREED WITH MY CODE INSTEAD OF WITH PRODUCTION. Found by codex.
#
# `feed._exec_client = node.kernel.exec_engine.default_client` looked right and
# was wrong: Nautilus types that property `ClientId | None` — it returns the
# IDENTIFIER. `ClientId` has exactly one public member, `value`.
#
# The assignment SUCCEEDS, so the surrounding `try` never fired. Every typed read
# then raised AttributeError inside the CALLER's try, which aborted before the
# `if reports is not None` fallback — so the REST fallback was unreachable code
# and the native venue cancel silently did nothing. The whole PR was inert.
#
# The tests above did not catch it because `_ExecClient` implements
# `generate_order_status_reports`. A double built from what the CALLER needs
# rather than from what production SUPPLIES cannot fail this way. These bind the
# real `ClientId`.
# ---------------------------------------------------------------------------


def test_a_ClientId_is_not_an_execution_client():
    """The premise, asserted against the installed Nautilus rather than remembered.

    If a future version ever gives `ClientId` these methods, this file's reasoning changes and this
    test says so instead of quietly passing.
    """
    from nautilus_trader.model.identifiers import ClientId

    assert not hasattr(ClientId, "generate_order_status_reports"), (
        "ClientId now has generate_order_status_reports — the wiring guard below is measuring nothing"
    )
    assert not hasattr(ClientId, "cancel_order"), "ClientId now has cancel_order — re-read the wiring"


def test_the_wiring_REFUSES_a_handle_that_cannot_answer():
    """A handle that cannot be read must become None, not be kept and fail at the call site.

    Those two outcomes look identical in a log and are opposite in meaning: `None` routes to the REST
    fallback, while a kept-but-useless handle raises inside the caller's try and takes the fallback
    down with it. That is the difference between "degraded" and "dead".
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.joinpath("engine_node.py").read_text()
    tree = ast.parse(src)
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_exec_client" for t in n.targets)
    ]
    assert assigns, "nothing assigns `_exec_client` any more — this test is blind"

    # The value assigned must never be `<...>.default_client` directly: that is the ClientId.
    for node in assigns:
        v = node.value
        assert not (isinstance(v, ast.Attribute) and v.attr == "default_client"), (
            "`_exec_client` is assigned `default_client`, which Nautilus documents as `ClientId | "
            "None` — the identifier, not the client. ClientId has one member, `value`. Every typed "
            "read raises AttributeError inside the caller's try, which aborts BEFORE the REST "
            "fallback can run, so the fallback is unreachable and the venue cancel silently no-ops"
        )

    guarded = "generate_order_status_reports" in src.split("_exec_client = _client")[0][-2000:]
    assert guarded, (
        "the resolved handle is not capability-checked before being stored — a handle that cannot "
        "answer must be refused so callers see UNKNOWN, not an exception at the call site"
    )
