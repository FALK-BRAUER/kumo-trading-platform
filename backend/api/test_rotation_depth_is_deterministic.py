"""Two stacks must grade the same market the same way — and the way I first tried was wrong (#384).

MEASURED, both instances on identical code and the identical source:

    :8000  IWM/SPY  OFF  adx=15.8  px=0.3907      GLD/SPY  OFF-wk  adx=32.9
    :8010  IWM/SPY  OFF  adx=43.5  px=0.3907      GLD/SPY  OFF-wk  adx=73.2

Same prices, same verdicts, different ADX.

WHAT I CONCLUDED, AND WHY IT WAS WRONG. I reasoned that Wilder's ADX is recursive from the start of
the series, so different cache depths must produce different values, and truncated each leg to a fixed
bar count. Codex rejected it: THE ADX PERIOD IS 9. Seed effects decay in dozens of bars, not hundreds,
so a 15.8-versus-43.5 gap over identical recent prices cannot come from ancient history. It points at
different recent ratio PATHS — holes, duplicates, or keys that do not match.

The truncation treated a symptom that was not the cause, and produced two failures of its own:

  * `[-N:]` per leg IS NOT ALIGNMENT. It yields `last N(A) ∩ last N(B)`, not "the last N common
    sessions". With N=800 the deep legs were cut and the shallow ones left whole, the intersection
    fell under 400, and axes returned "thin history" on one stack and not the other — the very
    disagreement it was added to remove.
  * With N=700 every leg was exactly 700 and three pairs STILL failed. Equal length, non-overlapping
    keys.

THE REAL FIX IS THE KEY, not the length: `bars_to_tuples` now keys by ET SESSION DATE and dedupes.
This file pins the design that replaced the truncation. The requirement it was written for — two
stacks agree — is unchanged.
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).parent / "engine_node.py"


def _body() -> str:
    src = SRC.read_text()
    i = src.index("async def _refresh_rotation")
    ends = [x for x in (src.find("\n    def ", i + 10), src.find("\n    async def ", i + 10)) if x > 0]
    return src[i:min(ends)]


def test_the_fixture_can_see_the_refresh():
    assert "def bars_for(" in _body(), "the bar accessor moved — this file is blind"


def test_there_is_NO_PER_LEG_TRUNCATION():
    """`last N of A` and `last N of B` are not the same window. Removed, not tuned."""
    body = _body()
    assert "_ROTATION_HISTORY_BARS" not in body, (
        "per-leg truncation is back. It does not align two legs — it intersects two independently "
        "chosen windows, and it hid the real cause (mismatched keys) behind a length argument")


def test_a_ticker_is_re_requested_WHILE_ITS_CACHE_IS_SHORT():
    """"ASKED" IS NOT "HAVE". Marking a ticker requested on the first tick and skipping it forever
    meant a request that never landed was never retried: test-alpaca sat at 0 of 25 axes for fifteen
    minutes with ZERO RequestBars in that window."""
    # THE MECHANISM, NOT THE WORD. The comment above the fix names the removed flag while explaining
    # why it went — the fifth time today a description of a removed thing tripped a check for the
    # thing. Assert on what the code DOES: gate the request on the cache, not on a set of names.
    import ast

    body = _body()
    tree = ast.parse("class X:\n" + "\n".join("    " + l for l in body.splitlines()))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "_rotation_requested" not in attrs, (
        "the once-only request flag is back — a dropped request will never be retried")
    assert "_ROTATION_MIN_BARS" in body and "request_bars" in body, (
        "nothing gates the request on what the cache actually HOLDS, so it cannot self-heal")


def test_SEEDING_is_not_published_as_an_empty_market():
    """`request_bars` is asynchronous, so the cache is legitimately short for the first ticks after a
    recreate. Publishing 0 axes then is the same lie as every other absence in this feature's history —
    an env var that switched it off, a vendor client that returned silently, a broker with no equity
    history rendered as a new account."""
    # THE GUARD, NOT THE WORD. A first version asserted `"seeding" in body` — which survives
    # `if False:` around the whole branch, because the string is still there in dead code. Measured:
    # that mutation left all four tests green. Assert the branch is REACHED, by pinning the condition
    # that reaches it and the list that condition reads.
    import ast

    body = _body()
    tree = ast.parse("class X:\n" + "\n".join("    " + l for l in body.splitlines()))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "short"]
    assert guards, (
        "nothing branches on the short-cache list, so a seeding stack publishes an empty compass — "
        "which reads as a quiet market")
    assert any(isinstance(n, ast.Return) for n in ast.walk(guards[0])), (
        "the seeding branch does not RETURN, so it falls through and grades the short cache anyway")
    assert "self._rotation" in body[body.index("if short:"):], (
        "seeding discards the last good payload instead of keeping it")
