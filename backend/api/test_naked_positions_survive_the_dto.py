"""The naked-position report must survive the DTO, not merely be published (#546, review round 2).

FOURTH INSTANCE of `pydantic-dto-drops-published-fields` (#233/#322/#336). The engine published
`naked_after_reject` on its health frame; `HealthResponse` has no such field and `/health` builds
its response from an explicit field list, so pydantic dropped it silently. Proven against the
running paper stack on 49e8881:

    curl :8000/health -> naked_after_reject present: False

So both #546 halves — the rejection escalation and the window sweep — wrote to a list nothing
could read. The first version's test ast-grepped the PUBLISHER for the string and passed while its
own docstring claimed a consumer property that was false: it was structurally incapable of
catching this. These tests pin the CONSUMER.
"""

from __future__ import annotations


def test_the_response_MODEL_carries_the_field():
    from api.models import HealthResponse

    assert "naked_after_reject" in HealthResponse.model_fields, (
        "HealthResponse has no naked_after_reject — pydantic drops whatever the engine publishes"
    )


def test_the_ENDPOINT_forwards_it_from_the_engine_frame():
    """The model having the field is not the endpoint filling it: /health constructs
    HealthResponse from an explicit list, so a field nobody passes defaults to empty forever."""
    import ast
    import inspect
    import textwrap

    import api.app as app_mod

    src = inspect.getsource(app_mod)
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "HealthResponse"]
    assert calls, "no HealthResponse construction found — this test is measuring nothing"
    assert any(any(kw.arg == "naked_after_reject" for kw in c.keywords) for c in calls), (
        "/health never forwards naked_after_reject — the engine's report dies in the DTO"
    )


def test_a_frame_carrying_one_SURVIVES_serialisation():
    """End to end through the model: a report the engine published must still be there after the
    round trip that dropped it."""
    from api.models import HealthResponse

    r = HealthResponse(
        status="ok",
        subsystems=[],
        feed_last_tick_ts=0,
        naked_after_reject=[{"instrument_id": "AAPL.XNAS", "reason": "rejected", "ts": 1}],
    )
    assert r.model_dump()["naked_after_reject"][0]["instrument_id"] == "AAPL.XNAS"


def test_the_CONSUMER_projection_forwards_it_too():
    """THERE ARE TWO ALLOWLISTS, not one (review round 3, found while verifying the round-two fix).

    The engine publishes onto its health frame; `RedisConsumer.health()` re-projects that frame
    into an EXPLICIT dict; `/health` then builds `HealthResponse` from ANOTHER explicit field list.
    Adding the field to the model and the endpoint closes the second gate only — the report still
    dies at the first. Verified in the running paper container: the runtime node is RedisConsumer,
    not NodeManager, and its projection is a literal dict of named keys.

    This is why the first fix's test was worthless: it asserted against NodeManager, a class
    production does not use.
    """
    import ast
    import inspect
    import textwrap

    from api.consumer import RedisConsumer

    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(RedisConsumer.health))))
    assert "naked_after_reject" in src, (
        "the consumer's health projection drops naked_after_reject — the engine's report never "
        "reaches /health no matter what the DTO carries"
    )


def test_the_test_ANCHORS_ON_THE_RUNTIME_NODE_TYPE():
    """The round-two fix asserted `hasattr(NodeManager, 'health')` — true, and irrelevant: the API
    process runs a RedisConsumer. An anchor on a class production does not instantiate is not an
    anchor. Whatever `create_node()` returns is the thing that must answer."""
    from api.node_factory import create_node

    node = create_node()
    assert hasattr(node, "health"), f"{type(node).__name__} cannot answer health()"
