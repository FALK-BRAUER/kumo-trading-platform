"""The market compass must not depend on which VENDOR the node buys data from (#384, the operator 2026-08-26).

    if self._http is None:
        return
    ...
    page = await self._http.get_bars(ticker, "1Day", start=start, page_token=token)

`self._http` is an `AlpacaHttpClient`, constructed only when `data_provider == "alpaca"`. So the
compass was Alpaca-only, and on any other data provider it returned silently — no payload, no error,
no log. ibkr-paper-retired renders today ONLY because its `KUMO_DATA=alpaca`; an instance taking data from
IBKR would have no market view and would say nothing about it.

That is the same silent-absence this feature was just rescued from: `KUMO_LEDGER_TOOL_TOOLS` could switch
it off, and now a vendor client could too.

NAUTILUS ALREADY SERVES THIS. `Strategy.request_bars(bar_type, start, end, ...)` exists and the engine
already uses it — TECHIVOL logs `RequestBars(bar_type=ZM.XNAS-1-DAY-LAST-EXTERNAL, start=...)` on every
boot. Whichever adapter the node has, Nautilus answers. `bars_to_tuples` is already the converter for
cached Nautilus bars; it predates this and was written for exactly this shape.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).parent / "engine_node.py"


def _refresh_rotation_body() -> str:
    src = SRC.read_text()
    i = src.index("async def _refresh_rotation")
    ends = [x for x in (src.find("\n    def ", i + 10), src.find("\n    async def ", i + 10)) if x > 0]
    return src[i:min(ends)]


def test_the_fixture_can_see_the_refresh():
    body = _refresh_rotation_body()
    assert "build_payload" in body, "the rotation refresh moved — this file is blind"


def test_it_does_NOT_reach_for_a_VENDOR_http_client():
    """THE DEFECT. A vendor client inside a feature that must work on any provider."""
    # CODE, NOT PROSE. The docstring explains what was removed and names `self._http` three times
    # doing it — a substring check flagged that, for the fourth time today. Describing a removed thing
    # is not doing it again, and a check that cannot tell those apart gets suppressed. Walk the AST.
    body = _refresh_rotation_body()
    tree = ast.parse("class X:\n" + "\n".join("    " + l for l in body.splitlines()))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_http" not in attrs, (
        "the compass still fetches through the Alpaca HTTP client — on an IBKR data provider it "
        "returns silently and the market view is absent with no explanation")
    assert "alpaca_bars_to_tuples" not in (attrs | names), (
        "still converting Alpaca REST rows; the bars should come from the Nautilus cache")


def test_it_ASKS_NAUTILUS_for_the_history():
    """Provider-agnostic by construction: whichever adapter the node has, Nautilus answers."""
    body = _refresh_rotation_body()
    assert "request_bars" in body, (
        "nothing requests the rotation history through Nautilus, so the cache will never hold the "
        "400+ daily bars a ratio needs")


def test_it_GRADES_off_the_CACHE():
    body = _refresh_rotation_body()
    assert "cache.bars(" in body and "bars_to_tuples" in body, (
        "the compass does not read the Nautilus cache — that is the one bar source every provider "
        "shares")


def test_the_SOURCE_no_longer_claims_a_vendor():
    """`source` is what an operator reads to know which feed graded the payload. Naming a vendor that
    may not be the one in use is worse than naming none."""
    body = _refresh_rotation_body()
    assert "engine:alpaca-daily" not in body, (
        "the payload still stamps itself `engine:alpaca-daily` regardless of the actual provider")


def test_a_provider_that_cannot_serve_the_history_SAYS_SO():
    """The absence must not be silent — that is the whole lesson of this feature's history. A bare
    `return` is what made `KUMO_LEDGER_TOOL_TOOLS=/nonexistent` look deliberate for days."""
    body = _refresh_rotation_body()
    tree = ast.parse("class X:\n" + "\n".join("    " + l for l in body.splitlines()))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    bare = [n for n in fn.body if isinstance(n, ast.If)
            and any(isinstance(b, ast.Return) and b.value is None for b in n.body)]
    assert not bare, (
        "the refresh still has a top-level guard that returns silently; a provider that cannot serve "
        "the history must produce a payload saying so, like the equity curve now does")
