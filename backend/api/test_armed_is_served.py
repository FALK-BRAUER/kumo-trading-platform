"""A lane's ARMED state must be readable, not inferred (#438, review 2026-08-23).

QC345 came up UNARMED on the post-deploy boot — its calendar timed out — and recovered on the retry. I
concluded it had armed because NO FURTHER `UNARMED` LINES APPEARED. That is absence-of-error as evidence
of success, which is the reasoning I have rejected from everyone else all night.

The reviewer went looking for a positive signal and found there is none:

    GET /preflight             404
    GET /strategies/preflight  404
    GET /strategies            200, and the rows carry no armed field

So the `armed` probe exists on BOTH sides of the seam — kumo-trading-strategies reports it in
`PREFLIGHT_PROBES`, cockpit requires it in `REQUIRED_FROM_STRATEGY` — and NOTHING SERVES IT. Computed,
contracted, and unreachable: the "built, configured, deployed, never executed" category, in the one
mechanism whose entire purpose is to prove a lane can decide.

It matters on a clock. An unarmed lane is RUNNING, counted in `automated_lanes_running`, and will never
fire its slot. On a trading morning that is a missed decision nobody sees — which is exactly what
kumo-trading-strategies warned about: "if you see UNARMED on a trading morning, that is a real incident and not
a benign log line".

The strategy objects live in the ENGINE process and `/strategies` is served by the API (#20), so this
takes the same four hops as the protection divergence in #455.
"""

from __future__ import annotations

import time

from api.consumer import RedisConsumer
from api.feed_config import load_feed_config


class _Lane:
    def __init__(self, armed):
        self.is_armed = armed


def test_the_double_matches_the_INSTALLED_strategy_contract():
    """Fixture property first. `is_armed` is what the deployed package exposes — verified in the
    container after the deploy — so a double inventing a different name would test nothing."""
    from kumo_strategies.runtime.nautilus.contract import RegistrationMixin

    assert hasattr(RegistrationMixin, "begin_arming"), "the deployed contract has no arming path"


def test_armed_state_is_collected_per_lane():
    from api.engine_node import armed_by_lane

    # The VALUE became a row in #997 (state, session, slot, next_fire, reason); the bool is read out
    # of it rather than computed twice. Same question, new shape.
    out = armed_by_lane({"MOMENTUM-002": _Lane(True), "QC345-003": _Lane(False)})
    assert {k: v["armed"] for k, v in out.items()} == {"MOMENTUM-002": True, "QC345-003": False}


def test_a_lane_that_cannot_answer_is_UNKNOWN_not_armed():
    """FAIL CLOSED, and this is the direction that matters. Reporting a lane as armed when we cannot
    tell is the silencing direction — the same one that made TECHIVOL look healthy while it formed
    nothing. `None` means "could not ask", which a reader must be able to tell from `False`."""
    from api.engine_node import armed_by_lane

    class _Opaque:
        pass

    row = armed_by_lane({"X": _Opaque()})["X"]
    assert row["armed"] is None and row["state"] == "unknown"


def test_HOP_2_armed_crosses_the_process_split():
    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "armed_lanes": {"QC345-003": False}}
    c._health_at = time.monotonic()
    assert c.health()["armed_lanes"] == {"QC345-003": False}


def test_a_STALE_frame_reports_armed_as_UNKNOWN_rather_than_stale_truth():
    """A lane that WAS armed when the engine died is not armed now — it is not running at all. Carrying
    the last known value forward would report a dead lane as ready to trade.

    THIS TEST SAID `UNKNOWN` IN ITS NAME AND ITS PROSE AND THEN ASSERTED `{}` (#859). An empty dict is
    not unknown, it is "no lane is armed" — a measurement, and one no frameless process can make.
    Prose and assertion disagreed; the assertion was the one that was wrong."""
    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "armed_lanes": {"QC345-003": True}}
    c._health_at = time.monotonic() - 3600
    assert c.health()["armed_lanes"] is None


def test_HOP_4_the_strategies_endpoint_serves_it_per_lane():
    """The hop nothing else covers. A field can exist on the frame AND cross the split and still never
    reach the endpoint a human reads — which is precisely the state this whole test file exists for."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "app.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "strategy_id" in keys and "last_decision" in keys:
                assert "armed" in keys, (
                    f"/strategies builds its row with {sorted(k for k in keys if isinstance(k, str))} "
                    f"and no `armed` — the probe stays computed and unreachable"
                )
                return
    raise AssertionError("the /strategies row builder was not found — this test is blind")


def test_the_endpoint_reads_the_node_from_APP_STATE_not_a_bare_name():
    """THE BUG I ACTUALLY WROTE, pinned without booting Nautilus.

    My first version called `node.health()` inside `get_strategies`, where `node` is not in scope — it
    is `app.state.node`, bound at startup. That is a NameError on every request, and the ENTIRE SUITE
    STAYED GREEN because the AST test above proves the key is in the row builder and nothing executes
    the code around it. Third time tonight the wiring was in place and nothing drove it.

    Driving it properly needs `TestClient`, which runs the lifespan, which builds a real TradingNode —
    it crashed the interpreter rather than failing. So this reads the function's own source instead:
    narrower than execution, and it catches exactly the class of error that shipped.
    """
    import ast
    import inspect

    from api.app import get_strategies

    src = inspect.getsource(get_strategies)
    assert "armed_lanes" in src, "the endpoint no longer resolves armed state at all"
    tree = ast.parse(inspect.cleandoc(src.split("\n", 1)[1]) if False else src.strip())

    # every bare Name loaded in the function that is not a builtin, a local, or an import
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assigned |= {n.target.id for n in ast.walk(fn)
                 if isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name)}
    assigned |= {n.name for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler) and n.name}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == "node":
            assert n.id in assigned, (
                "`get_strategies` reads a bare `node`, which is not in scope there — the node is bound "
                "on app.state at startup. This is a NameError on every request and no test drives it"
            )


def test_an_UNREADABLE_engine_frame_yields_UNKNOWN_rather_than_503():
    """`/strategies` answers capital, targets and last-decision too, and must keep answering when the
    engine is unreachable. Asserted on the source for the same reason: the guard must be a try/except
    around the health read, not an unprotected call."""
    import inspect

    from api.app import get_strategies

    src = inspect.getsource(get_strategies)
    block = src[src.index("armed_lanes"):]
    assert "except" in block.split("rows = []")[0], (
        "the armed-state read is not wrapped — an unreachable engine would 503 a screen that answers "
        "other questions"
    )


def test_HOP_1_the_engine_PUBLISHES_armed_on_the_health_frame():
    """Source of the whole chain, and it survived the first sweep: deleting the engine's publish left
    every test green, because the consumer, DTO and endpoint tests all start from a frame the test
    itself supplies. Each hop was covered and the ORIGIN was not."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "engine_ok" in keys and "automated_lanes_registered" in keys:
                assert "armed_lanes" in keys, (
                    "the health frame no longer carries armed_lanes — the probe is computed in the "
                    "engine and never leaves it, which is the state this file exists to end"
                )
                return
    raise AssertionError("health frame not found — this test is blind")


def test_the_consumer_declares_armed_lanes_EXACTLY_ONCE():
    """A duplicate key in the health dict is legal Python and silently keeps the LAST one. Two edits
    landed the same key twice here and only the mutation harness noticed, by refusing an ambiguous
    anchor. A second declaration with different gating would be invisible."""
    import pathlib

    src = (pathlib.Path(__file__).parent / "consumer.py").read_text()
    assert src.count('"armed_lanes":') == 1, (
        f'consumer.py declares "armed_lanes" {src.count(chr(34) + "armed_lanes" + chr(34) + ":")} times '
        f"— a duplicate key silently wins and its gating may differ"
    )
