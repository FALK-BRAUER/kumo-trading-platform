"""`GET /market/rotation` — serve the rotation payload, compute nothing (#351).

WHY THE COCKPIT DOES NOT COMPUTE THIS
-------------------------------------
The Ichimoku stack that grades a rotation is `ledger-tool:tools/rotation_read.py`, and it already grades
single names with the same weekly-governs/daily-times hierarchy. Reimplementing it in TypeScript would
be a second derivation of one fact, and the two would disagree the first time either changed — the
failure this repo has now measured several times. So the tool emits the contract (`--json --out`) and
the cockpit serves the file.

That makes the endpoint's whole job "read a file that may not be there", and the interesting cases are
all absence: no file yet, a half-written file, a stale one. None of them may look like data.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import app


def _client() -> TestClient:
    return TestClient(app)


def test_the_route_exists_and_is_a_GET() -> None:
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/market/rotation" in paths, (
        "no route serves the rotation payload — the tile has nothing to render and #351 cannot start"
    )
    route = next(r for r in app.routes if getattr(r, "path", None) == "/market/rotation")
    assert "GET" in route.methods, "reading a rotation payload is not a mutation"


# ==================================================================================================
# THE SOURCE CHANGED, THE PROPERTIES DID NOT (#384).
#
# These three used to write a `rotation.json` into a tmp_path and point `KUMO_ROTATION_PATH` at it. The
# route no longer reads a file — the engine publishes the payload and this serves it — so the fixtures
# are a fake node instead. What is asserted is unchanged: absence must not render as an empty market, a
# malformed payload must be reported rather than raised, and a good one must pass through untouched.
#
# 2026-08-21: "it should not feed from a file!!!!"
# ==================================================================================================


class _Node:
    def __init__(self, payload):
        self._payload = payload

    def rotation(self):
        return self._payload


def _serve(payload) -> dict:
    """Run the real route against a node returning `payload`."""
    import asyncio

    from api import app as app_module

    original = getattr(app_module.app.state, "node", None)
    app_module.app.state.node = _Node(payload)
    try:
        return asyncio.run(app_module.get_rotation())
    finally:
        if original is not None:
            app_module.app.state.node = original


def test_a_MISSING_payload_reports_absence_rather_than_an_empty_rotation() -> None:
    """"There is no rotation right now" and "nobody has looked" are different claims. Rendering the
    first for the second turns a dead feed into a calm market — the same rule as an unpriced symbol
    that must not render as flat (#356)."""
    out = _serve({})

    assert out["axes"] == []
    assert out["generated"] is None
    assert out["error"], "absence must state itself"


def test_a_MALFORMED_payload_is_reported_not_raised() -> None:
    """The file version's analogue was a half-written file. A publisher bug must not take the tile
    down, and must not read as data either."""
    out = _serve({"generated": "2026-08-21T01:00:00+00:00", "axes": "not-a-list"})

    assert out["axes"] == []
    assert out["error"], "a bad shape must be reported"


def test_a_GOOD_payload_is_passed_through_UNCHANGED() -> None:
    """The contract belongs to the tool. Reshaping here would put a second schema between generator and
    tile, and the tile would end up pinned to whichever of the two drifted last."""
    payload = {
        "generated": "2026-08-21T01:21:04+00:00",
        "source": "engine:alpaca-daily",
        "axes": [{"pair": "XLI/XLU", "verdict": "\U0001f7e1 ON-wk", "adx": 24.5}],
        "errors": [],
    }

    out = _serve(payload)

    assert out["axes"] == payload["axes"]
    assert out["source"] == "engine:alpaca-daily"
    assert out["generated"] == payload["generated"]
    assert out["error"] is None
