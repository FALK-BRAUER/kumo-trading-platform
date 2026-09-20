"""The equity curve is read from Nautilus, not fetched from a vendor (#559).

REPLACES `test_equity_curve_absence_is_stated.py`, which pinned the opposite thing: that a stack
with no curve should SAY so. That was the right fix for the wrong problem. The message it defended —

    "this stack's broker publishes no account history — the equity curve comes from the Alpaca
     portfolio-history endpoint, and nothing supplies an equivalent here"

was true of the ARCHITECTURE and false about the world. Measured 2026-08-26:

    paper    trader-PLATFORM-001:accounts:ALPACA-…                280,000 AccountState events
    staging  trader-PLATFORM-STG:accounts:INTERACTIVE_BROKERS-…    22,000 events

`Account.events -> list[AccountState]` is native, provider-agnostic, and persisted by Nautilus. The
IBKR curve was in the cache the whole time and nobody read it, so the honest-sounding explanation was
a claim about a mechanism dressed as an observation — the exact error this repo keeps paying for, and
one I committed while fixing another instance of it.

Operator: "we can't keep the alpaca hardcoding — nothing in nautilus?" There was.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).parent / "engine_node.py"


def _method(name: str) -> str:
    src = SRC.read_text()
    i = src.index(f"def {name}(self)")
    ends = [x for x in (src.find("\n    def ", i + 10), src.find("\n    async def ", i + 10)) if x > 0]
    return src[i:min(ends)]


def test_the_fixture_can_see_the_publisher():
    assert "def _publish_equity_curve_from_cache(self)" in SRC.read_text(), (
        "the curve publisher moved — this file is blind")


def test_it_READS_THE_NAUTILUS_CACHE():
    body = _method("_publish_equity_curve_from_cache")
    assert "cache.accounts()" in body and "events" in body, (
        "the curve is not built from the account's own event history, which is the one source every "
        "venue shares")


def test_it_does_NOT_reach_for_a_VENDOR_client():
    """The whole point. A vendor client here makes the feature venue-specific and silently absent
    everywhere else — which is how staging showed 'No account history yet' for days."""
    body = _method("_publish_equity_curve_from_cache")
    tree = ast.parse("class X:\n" + "\n".join("    " + ln for ln in body.splitlines()))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "_http" not in attrs, "the curve still goes through the Alpaca HTTP client"


def test_the_UNAVAILABLE_CLAIM_IS_GONE():
    """It is now false on every stack. Keeping a never-true branch is worse than deleting it: the next
    reader treats it as a supported state, which is what made 'this instance has no market view' look
    deliberate for days."""
    src = SRC.read_text()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_state_equity_curve_unavailable" not in names, (
        "the 'this broker publishes no account history' claim is back — Account.events supplies one "
        "on every venue, so that sentence cannot be true")


def test_a_FAILURE_keeps_the_last_known_curve():
    """A display plane must never touch the trading loop, and a transient read failure must not blank
    a chart that was correct a second ago."""
    body = _method("_publish_equity_curve_from_cache")
    assert "except Exception" in body and "keeping last known" in body, (
        "a failed read is not quarantined — it will either raise into the engine or blank the tile")
