"""The rotation route must pass the payload through, as its own docstring claims (#233/#322/#336 again).

    "The payload is passed through UNCHANGED. The contract belongs to the tool; reshaping it here
     would put a second schema between generator and tile."

Then:

    return {
        "generated": ..., "source": ..., "axes": ..., "errors": ..., "error": None,
    }

FIVE HAND-LISTED KEYS. Everything else the engine publishes is silently dropped, and the docstring
says the opposite — which is why nobody looked.

MEASURED 2026-08-26. Two fields died here:

  * `depth`       — per-ticker bar counts, added to diagnose two stacks grading the same market
                    differently. Reported 0 tickers at the route while the engine had 27.
  * `unavailable` — the reason a provider cannot grade any axis. Added specifically so the absence
                    would stop being silent, and then silenced by the route.

The second is the one that matters: a field whose entire purpose is to make a silent failure loud,
made silent again by a hand-written passthrough that says it is not one. This is the FOURTH time a
published field has been eaten between engine and UI in this codebase.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).parent / "app.py"


def _route_body() -> str:
    src = APP.read_text()
    i = src.index("async def get_rotation()")
    nxt = src.index("\n@app.", i)
    return src[i:nxt]


def test_the_fixture_can_see_the_route():
    assert "node.rotation()" in _route_body(), "the rotation route moved — this file is blind"


def test_the_route_does_not_hand_list_the_keys_it_forwards():
    """THE DEFECT. A hand-written key list drops every field added later, and does it silently."""
    body = _route_body()
    tree = ast.parse("def f():\n" + "\n".join("    " + l for l in body.splitlines()[1:]))
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            # the empty/error returns are allowed to be explicit; the SUCCESS path must not be
            if "axes" in keys and not any(k is None for k in node.value.keys):
                raise AssertionError(
                    f"the success path rebuilds the payload from {sorted(keys)} — anything the engine "
                    f"adds later is dropped, silently, while the docstring says it is passed through")


def test_fields_the_engine_adds_SURVIVE_the_route():
    """The two that were actually lost, named so a future reader knows what this is protecting."""
    body = _route_body()
    assert "**payload" in body, (
        "the route does not spread the engine's payload — `depth` and `unavailable` were both eaten "
        "here, and `unavailable` exists precisely to stop a silent absence being silent")


def test_the_docstring_no_longer_claims_something_it_does_not_do():
    """A comment that reads as a guarantee is worse than none: it is why this went unexamined."""
    body = _route_body()
    if "passed through UNCHANGED" in body:
        assert "**payload" in body, (
            "the docstring claims an unchanged passthrough that the code does not perform")
