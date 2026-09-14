"""A correct finding that reaches no operator is not a finding (#454).

On 2026-08-22 an Alpaca 503 made Nautilus mark ELEVEN protective stops REJECTED — every held symbol —
while all eleven rested untouched at the broker. The engine DETECTED that precisely, at ERROR level,
naming each symbol and its consequence:

    PROTECTION DIVERGENCE on AMGN.XNAS: the broker holds 2 resting protective order(s) this engine
    cannot see (...). The cache holds them ['...=REJECTED', '...=REJECTED']. Exits on this position
    will be rejected on `available: 0` unless they cancel at the venue.

And it went nowhere. `grep -rn _protection_divergence` found it in `engine_node.py` and in tests, and
in no other file: not on /health, not on /trades, not in the alarm channel. Every surface an operator
can see reported a healthy, fully protected book for the whole outage.

The analysis was already right. This file is about the four hops between the analysis and a human, and
THE DTO IS THE HAZARD: a field the engine publishes and `models.py` silently drops has bitten three
times (#233, #322, #336). So the test walks all four hops with the real objects rather than asserting
each in isolation.
"""

from __future__ import annotations

import time

from api.consumer import RedisConsumer
from api.feed_config import load_feed_config
from api.models import HealthResponse

_DIVERGENCE = [
    {"instrument_id": "AMGN.XNAS",
     "coids": ["PROT-SELL-AMGN-XNAS-3c2ed0d1", "PROT-SELL-AMGN-XNAS-a15ead44"],
     "cache_holds": ["PROT-SELL-AMGN-XNAS-3c2ed0d1=REJECTED",
                     "PROT-SELL-AMGN-XNAS-a15ead44=REJECTED"]},
]


def test_the_fixture_is_the_shape_engine_node_ACTUALLY_appends():
    """Fixture property first. `engine_node.py:1307` appends exactly these three keys; a double with a
    different shape would let a DTO that drops one of them pass."""
    assert set(_DIVERGENCE[0]) == {"instrument_id", "coids", "cache_holds"}


def test_the_HEALTH_RESPONSE_does_not_silently_eat_the_field():
    """HOP 3, and the one with previous form. Pydantic drops unknown keys in silence — the engine
    publishes, the DTO eats it, and the UI shows nothing with no error anywhere. Three times: #233,
    #322, #336."""
    r = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0,
                       protection_divergence=_DIVERGENCE)
    assert r.protection_divergence, "HealthResponse dropped the divergence — the 4th occurrence"
    assert r.protection_divergence[0].instrument_id == "AMGN.XNAS"
    assert len(r.protection_divergence[0].coids) == 2


def test_the_COIDS_survive_because_the_symbol_alone_is_not_actionable():
    """"AMGN diverges" tells an operator nothing they can act on. WHICH orders, and what the cache
    thinks of them, is the whole content — it is what says whether to cancel at the venue or restart
    the engine."""
    r = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0,
                       protection_divergence=_DIVERGENCE)
    assert r.protection_divergence[0].cache_holds == _DIVERGENCE[0]["cache_holds"]


def test_an_absent_divergence_defaults_to_EMPTY_and_not_to_missing():
    """Every existing caller constructs HealthResponse without this field. A required field would
    break /health outright — turning a monitoring improvement into an outage.

    THAT is what this test protects, and it still holds. The DEFAULT changed from `[]` to `None`
    (#859): a response nobody supplied a divergence for has not observed zero divergences, it has not
    looked. Optional-not-required is the property; the particular empty value never was."""
    r = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0)
    assert r.protection_divergence is None


def _consumer_with(frame: dict) -> RedisConsumer:
    c = RedisConsumer(load_feed_config())
    c._health = frame
    c._health_at = time.monotonic()
    return c


def test_HOP_2_the_consumer_passes_the_field_across_the_process_split():
    """The api cannot reach the engine (#20) — it only sees what rides the health frame over Redis. A
    field the engine publishes and the consumer does not forward dies here, invisibly."""
    h = _consumer_with({"engine_ok": True, "last_tick_ts": 1,
                        "protection_divergence": _DIVERGENCE})
    assert h.health()["protection_divergence"] == _DIVERGENCE


def test_a_STALE_frame_stops_reporting_divergence_just_like_drift():
    """Matches `reconcile_drift`'s existing rule, deliberately. A stale frame must not keep raising an
    alarm after the engine has gone — but it must not report CLEAN either, and `bridge_ok` is what a
    reader checks for that. Two derivations of one policy; they must not diverge."""
    c = _consumer_with({"engine_ok": True, "last_tick_ts": 1, "protection_divergence": _DIVERGENCE})
    c._health_at = time.monotonic() - 3600
    out = c.health()
    # WAS `== []` (#859). This docstring already said a stale frame "must not report CLEAN either,
    # and `bridge_ok` is what a reader checks for that" — while `bridge_ok` did not reach /health at
    # all, so the escape hatch it relied on did not exist. Now the value itself says unknown.
    assert out["protection_divergence"] is None
    assert out["reconcile_drift"] is None, "the two fields no longer share one staleness policy"
    assert out["bridge_ok"] is False, "a reader cannot tell 'no divergence' from 'no engine'"


def test_HOP_1_the_engine_PUBLISHES_it_on_the_health_frame():
    """Source. `_protection_divergence` is recomputed every protection pass and lived only in a log
    line — the analysis was right and reached nobody. Asserted on the source text because the frame is
    built inside a long method with a live node; the point is that the key is on the frame at all."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "reconcile_drift" in keys and "engine_ok" in keys:
                assert "protection_divergence" in keys, (
                    "the health frame carries reconcile_drift but not protection_divergence — the "
                    "finding stops at the engine and no operator surface can ever show it"
                )
                return
    raise AssertionError("health frame not found — this test is blind")


def test_HOP_4_the_api_populates_the_response_from_the_observed_frame():
    """The last hop, and the easiest to forget: the field can exist on the frame AND on the model and
    still never be copied between them."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "app.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HealthResponse":
            names = {k.arg for k in node.keywords}
            assert "protection_divergence" in names, (
                f"/health builds HealthResponse with {sorted(names)} — the divergence is on the frame "
                f"and on the model, and nothing carries it across"
            )
            return
    raise AssertionError("HealthResponse construction not found — this test is blind")


def test_the_ALARM_reports_divergence_and_says_exits_will_be_rejected():
    """The end of the chain. A field on a JSON endpoint nobody polls is the same failure one hop
    later — `session_watch` is what actually reaches a human."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from session_watch import collect

    def fetch(path):
        return {
            # A LIVE frame (#859): the monitor refuses a payload with no engine frame before it
            # reads any list, so the divergence must arrive on a frame the engine actually sent.
            "/health": {"status": "degraded", "bridge_ok": True,
                        "subsystems": [{"name": "engine", "ok": True, "detail": ""}],
                        "automated_lanes_registered": 4, "automated_lanes_running": 4,
                        "protection_divergence": _DIVERGENCE},
            "/account": {"equity": 103466.29, "last_equity": 103466.29},
            "/trades": {"status": "ok", "trades": []},
            "/orders": {"orders": []},
        }[path]

    _line, alerts = collect(fetch=fetch)
    hit = [a for a in alerts if "AMGN" in a]
    assert hit, f"divergence raised no alert: {alerts}"
    assert "available: 0" in hit[0] or "reject" in hit[0].lower(), (
        f"the alert does not say what it costs the operator: {hit[0]}"
    )
