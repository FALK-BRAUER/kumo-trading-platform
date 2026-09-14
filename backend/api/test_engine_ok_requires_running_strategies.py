"""`engine_ok` must not be green while no strategy is running (#454, codex round 2).

`engine_ok` was the literal `True`. It answers "did a frame arrive", which is a real question and not
the one an operator is asking. The question they are asking is whether the thing trades.

THE CASE THAT IS ALREADY COVERED, so this does not overclaim: if reconciliation fails at boot,
`kernel.py:1027` returns bare, NO strategy starts — including `UiFeedStrategy`, which publishes the
health frame from `_on_snapshot`. Nothing publishes, the bridge goes stale, and the api already reports
the engine down. That path is fine.

THE CASE THAT IS NOT: a PARTIAL start. The feed is added first and starts; a trading strategy raises in
`on_start` and does not. Health then reports a healthy engine while zero lanes can trade — which is
TECHIVOL-005 and QC345-003 exactly: enabled, funded, scheduled, and not trading, with every surface
green. That is the same shape as #454's protection divergence: the fact existed, nothing carried it.
"""

from __future__ import annotations

from api.engine_node import strategy_run_counts


class _Strat:
    """Shaped like Nautilus's `Strategy` where this code touches it. `is_running` is a real property on
    the installed class — checked against the INSTALLED package rather than assumed, because a double
    built from what the caller wants is how `default_client` returned a ClientId and every typed read
    raised into an unreachable fallback."""

    def __init__(self, running: bool):
        self.is_running = running


def test_the_double_matches_the_installed_Strategy():
    """Fixture property first: if the real class ever loses `is_running`, this fails here rather than
    silently reporting every lane as stopped."""
    from nautilus_trader.trading.strategy import Strategy

    assert hasattr(Strategy, "is_running")


def test_all_running_reports_all_running():
    assert strategy_run_counts({"A": _Strat(True), "B": _Strat(True)}) == (2, 2)


def test_a_PARTIAL_start_is_visible():
    """The live case. QC27 raised in its own path while the feed and the other lanes ran fine — the
    engine was up and one lane could not trade, and nothing said so."""
    assert strategy_run_counts({"A": _Strat(True), "B": _Strat(False)}) == (2, 1)


def test_NO_registered_strategies_is_reported_as_zero_of_zero_not_as_healthy():
    """`(0, 0)` is honest and the caller decides. Returning `(0, 1)` or defaulting to healthy would
    make a node with nothing registered indistinguishable from one trading four lanes."""
    assert strategy_run_counts({}) == (0, 0)


def test_a_strategy_that_cannot_answer_counts_as_NOT_running():
    """Fail closed. A sibling object without `is_running` — a stub, a mock, a future type — must not be
    counted as trading. Reporting a lane as running when we cannot tell is the silencing direction."""
    class _Opaque:
        pass

    assert strategy_run_counts({"A": _Opaque(), "B": _Strat(True)}) == (2, 1)


def test_the_health_frame_carries_BOTH_numbers():
    """Aimed at the routing, not the predicate — the #454 lesson. A count computed and not published is
    the same defect one hop later.

    Both numbers, because a ratio is what makes it readable: `0 of 4` is an outage, `0 of 0` is a node
    with nothing registered, and a single number cannot tell them apart."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "engine_ok" in keys:
                assert "automated_lanes_running" in keys and "automated_lanes_registered" in keys, (
                    f"the health frame carries {sorted(k for k in keys if isinstance(k, str))} — the "
                    f"run counts are computed and never leave the engine"
                )
                return
    raise AssertionError("health frame not found — this test is blind")


# ==================================================================================================
# ROUTING. Same four hops as #454's divergence, and the same hazard: Pydantic drops unknown keys in
# silence, so a field can exist on the frame AND on the model and never be copied between them.
# ==================================================================================================
def test_HOP_2_the_counts_cross_the_process_split():
    import time

    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "last_tick_ts": 1,
                 "automated_lanes_registered": 4, "automated_lanes_running": 2}
    c._health_at = time.monotonic()
    out = c.health()
    assert (out["automated_lanes_registered"], out["automated_lanes_running"]) == (4, 2)


def test_a_STALE_frame_reports_UNKNOWN_rather_than_zero_running():
    """`0 running` from a dead engine is a different claim from `0 running` from a live one, and the
    first is not something we know. `None` is the honest answer; `bridge_ok` already tells a reader
    which case they are in. Reporting 0 would raise a lane-down alarm every time the engine restarts."""
    import time

    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "automated_lanes_registered": 4, "automated_lanes_running": 2}
    c._health_at = time.monotonic() - 3600
    out = c.health()
    assert out["automated_lanes_running"] is None and out["automated_lanes_registered"] is None


def test_HOP_3_the_DTO_does_not_eat_the_counts():
    from api.models import HealthResponse

    r = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0,
                       automated_lanes_registered=4, automated_lanes_running=2)
    assert (r.automated_lanes_registered, r.automated_lanes_running) == (4, 2)
    assert HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0).automated_lanes_running is None


def test_the_ALARM_fires_when_lanes_are_registered_but_none_run():
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from session_watch import collect

    def fetch(path):
        return {
            "/health": {"subsystems": [], "automated_lanes_registered": 4, "automated_lanes_running": 0},
            "/account": {"equity": 1.0, "last_equity": 1.0},
            "/trades": {"status": "ok", "trades": []},
            "/orders": {"orders": []},
        }[path]

    _line, alerts = collect(fetch=fetch)
    assert any("0/4" in a or "0 of 4" in a for a in alerts), alerts


def test_the_alarm_is_SILENT_when_every_registered_lane_runs():
    """The discriminating half — without it, an alarm that always fires passes the test above."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from session_watch import collect

    def fetch(path):
        return {
            "/health": {"subsystems": [], "automated_lanes_registered": 4, "automated_lanes_running": 4},
            "/account": {"equity": 1.0, "last_equity": 1.0},
            "/trades": {"status": "ok", "trades": []},
            "/orders": {"orders": []},
        }[path]

    _line, alerts = collect(fetch=fetch)
    assert not [a for a in alerts if "lane" in a.lower() or "strateg" in a.lower()], alerts


def test_HOP_4_the_api_populates_the_response_from_the_observed_frame():
    """SURVIVED THE FIRST SWEEP. Deleting the `app.py` line changed no test, because the DTO test drives
    the model directly and the alarm test drives `session_watch` directly — so the one hop between them
    was covered by neither. A field can exist on the frame AND on the model and never be copied across;
    that is the hop that is easiest to forget and the only one nothing else touches."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "app.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HealthResponse":
            names = {k.arg for k in node.keywords}
            missing = {"automated_lanes_registered", "automated_lanes_running"} - names
            assert not missing, (
                f"/health builds HealthResponse without {sorted(missing)} — the counts are on the "
                f"frame and on the model, and nothing carries them across"
            )
            return
    raise AssertionError("HealthResponse construction not found — this test is blind")


def test_MANUAL_is_deliberately_NOT_counted_and_the_NAME_says_so():
    """THE DEFECT WAS THE NAME, NOT THE NUMBER.

    Five strategies log RUNNING — MANUAL, MOMENTUM, BCTROT, QC345, TECHIVOL — while the counter reads
    4/4, because MANUAL-001 is discretionary: always live, never scheduled, and never passed to
    `register_strategy` (engine_node registers bctrot, momentum, qc345, qc27 and nothing else).

    Counting it would make `4/5` the HEALTHY state, which makes a lane-down alarm unreadable. So the
    exclusion is right and the old name `strategies_*` was not falsifiable by a reader — the author of
    the field misread his own 4/4 as "all five running", in a message to the one person able to catch
    it. This pins the reason so the next person does not "fix" the number.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    # UNPARSE THE WHOLE EXPRESSION. A first version took `.attr`, which is `"id"` for every call
    # (`register_strategy(momentum.id, momentum)`), so the set was `{"id"}` and no lane name could ever
    # appear — the assertion below could not fail and a mutation registering MANUAL sailed through.
    registered = {
        ast.unparse(n.args[0])
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "register_strategy"
        and n.args
    }
    assert len(registered) >= 4, f"expected the four automated lanes, saw {sorted(registered)}"
    assert any("momentum" in r.lower() for r in registered), (
        f"the scan is not seeing lane names at all: {sorted(registered)}"
    )
    assert not any("manual" in r.lower() for r in registered), (
        f"MANUAL is now registered as a sibling ({registered}) — it would be counted as an automated "
        f"lane, making 4/5 the healthy state and the alarm unreadable"
    )


def test_no_field_named_strategies_running_survives_anywhere():
    """Aimed at the class: a half-completed rename leaves the engine publishing one name while the DTO
    reads another, and Pydantic drops the mismatch in SILENCE — the #233/#322/#336 shape. Four hops had
    to change together, so this asserts none of them kept the old name."""
    import pathlib

    root = pathlib.Path(__file__).parent
    stale = []
    for f in ("engine_node.py", "consumer.py", "models.py", "app.py",
              "../scripts/session_watch.py"):
        text = (root / f).read_text()
        if "strategies_running" in text or "strategies_registered" in text:
            stale.append(f)
    assert stale == [], f"{stale} still use the old field name — the rename is half applied"
