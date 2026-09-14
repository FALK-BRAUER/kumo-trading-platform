"""No execution client may reach the venue without the budget gate (#782).

THE DEFECT THIS EXISTS FOR. `may_submit` had exactly one call site, in the Alpaca exec client, and
Alpaca is the only exec client this repo owns. staging-ibkr runs the SHIPPED Nautilus IBKR adapter,
so the gate was in nobody's path there: BCTROT-004 was allocated 100,000 and reached 118,967.75
because nothing ever checked. `budget.py` and `budget_gate.py` were fully written, fully tested, and
dead on that instance.

AIM AT THE CLASS, NOT AT IBKR. Enumerating vendors is what failed — a list of "clients that need the
gate" is a list that goes stale the day a third venue arrives. These tests assert the PROPERTY: every
execution client this repo hands to Nautilus is gated, whoever wrote it.
"""

from __future__ import annotations

import asyncio
import types


class _Denied(Exception):
    pass


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, msg, *a):
        self.lines.append(str(msg))

    warning = info = error = debug = exception = _rec


class _Clock:
    def timestamp_ns(self):
        return 1_000


class _Order:
    def __init__(self):
        self.strategy_id = "BCTROT-004"
        self.instrument_id = "AEM.XNYS"
        self.client_order_id = "coid-1"
        self.quantity = 10
        self.side = types.SimpleNamespace(name="BUY")
        self.price = 100.0


class _Cache:
    def positions_open(self, strategy_id=None, instrument_id=None):
        return []

    def quote_tick(self, iid):
        return None

    def trade_tick(self, iid):
        return None


class _VendorClient:
    """Stands in for a client this repo did not write. Its `_submit_order` is the venue."""

    def __init__(self):
        self._cache = _Cache()
        self._log = _Log()
        self._clock = _Clock()
        self.reached_venue = 0
        self.denied = []

    async def _submit_order(self, command):
        self.reached_venue += 1

    def generate_order_denied(self, **kw):
        self.denied.append(kw)


def _install(client):
    from api.providers.gated_exec import install_budget_gate
    return install_budget_gate(client)


def test_the_fixture_reaches_the_venue_WITHOUT_the_gate():
    """FIXTURE PROPERTY FIRST. If the bare double could not reach the venue, every assertion below
    would pass with the gate removed."""
    c = _VendorClient()
    asyncio.run(c._submit_order(types.SimpleNamespace(order=_Order())))
    assert c.reached_venue == 1


def test_an_order_over_budget_is_DENIED_and_never_reaches_the_venue():
    """The whole point. Killed by not installing the gate, or by delegating before checking."""
    import api.budget_guard as bg

    c = _install(_VendorClient())
    real = bg.budget_allows

    async def refuse(order, **kw):
        return (False, "BCTROT-004 has 0 of budget left and this order needs 1,000",
                {"target": 100000.0, "deployed": 118967.75, "room": 0.0})

    bg.budget_allows = refuse
    try:
        asyncio.run(c._submit_order(types.SimpleNamespace(order=_Order())))
    finally:
        bg.budget_allows = real
    assert c.reached_venue == 0, "an over-budget order reached the venue"
    assert len(c.denied) == 1
    assert c.denied[0]["client_order_id"] == "coid-1"
    # The DERIVATION travels with the refusal, not just the verdict.
    assert any("118967" in ln or "118,967" in ln or "deployed" in ln for ln in c._log.lines)


def test_an_allowed_order_still_reaches_the_venue():
    """A gate that blocks everything is not a gate. Killed by returning early unconditionally."""
    import api.budget_guard as bg

    c = _install(_VendorClient())
    real = bg.budget_allows

    async def allow(order, **kw):
        return (True, "", {})

    bg.budget_allows = allow
    try:
        asyncio.run(c._submit_order(types.SimpleNamespace(order=_Order())))
    finally:
        bg.budget_allows = real
    assert c.reached_venue == 1
    assert c.denied == []


def test_installing_twice_does_not_double_the_gate():
    """Idempotent: a second install would run the check twice and double every refusal log, making
    the count meaningless."""
    c = _install(_install(_VendorClient()))
    import api.budget_guard as bg
    real = bg.budget_allows
    calls = []

    async def count(order, **kw):
        calls.append(1)
        return (True, "", {})

    bg.budget_allows = count
    try:
        asyncio.run(c._submit_order(types.SimpleNamespace(order=_Order())))
    finally:
        bg.budget_allows = real
    assert len(calls) == 1


# `test_EVERY_exec_client_spec_this_repo_builds_is_gated` lived here until #1030. It walked
# `pkgutil.iter_modules`, which yields the `alpaca` PACKAGE but never enters it — so the Alpaca spec at
# `providers/alpaca/exec_client.py` was outside its reach and "EVERY" was true of one venue. The
# class-level guard is now `test_ownership_gate_both_venues.test_EVERY_exec_client_factory_declares_BOTH_gates`,
# a recursive walk that asserts BOTH markers. One walker, not two.


def test_installing_the_gate_SAYS_SO():
    """A gate that leaves no trace cannot be verified on a live stack, only assumed — and #782 is
    exactly a control everyone believed was running. Killed by removing the install log."""
    c = _install(_VendorClient())
    assert any("budget gate INSTALLED" in ln for ln in c._log.lines), c._log.lines
