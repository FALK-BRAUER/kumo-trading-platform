"""API contract tests — REST shape + WS tape delivery for the #4 slice.

One Nautilus engine per process: `BacktestEngine` uses native globals that segfault if a second
engine is built in the same interpreter, so the TestClient (and its lifespan) is module-scoped and
shared across tests rather than re-entered per test.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client) -> None:
    body = client.get("/health").json()
    assert body["status"] in ("ok", "degraded")  # degraded if redis/pg aren't up in the test env
    names = {s["name"] for s in body["subsystems"]}
    # `lanes` joined the set in #539: a lane that failed to BUILD has to be able to move `status`,
    # and `status` is "ok" iff every subsystem is ok — so absence had to become a subsystem rather
    # than a field. On 2026-08-25 /health read `ok, 3/3` with QC345-003 missing for a whole session.
    assert names == {"redis", "postgres", "engine", "lanes"}
    # Synthetic node self-reports healthy → its engine subsystem is up regardless of redis/pg.
    engine = next(s for s in body["subsystems"] if s["name"] == "engine")
    assert engine["ok"] is True
    assert "feed_last_tick_ts" in body


def test_external_activity_endpoint_shape(client) -> None:
    # The quarantine plane (#79). Synthetic node is data-only (no exec client) → present + empty.
    resp = client.get("/external-activity")
    assert resp.status_code == 200
    assert resp.json() == {"external": []}


def test_trades_endpoint_shape(client) -> None:
    # The trade-cycle plane (#73). The synthetic node is data-only (no exec client → no cycles), so the
    # contract is present and empty — proves the REST/DTO wiring end-to-end without needing a live broker.
    resp = client.get("/trades")
    assert resp.status_code == 200
    body = resp.json()
    # `status`/`error` ride WITH the rows (#298). They were added because the rows and the projection's
    # health used to travel separately and only the rows survived the hop — so a broken projection reached
    # the UI looking exactly like a flat book, and an empty tile stood in for eight held positions.
    #
    # The synthetic node has no engine, so it reports None rather than inventing a health it cannot know.
    #
    # `realized_session` rides here for the same reason (#233), and its None matters as much as the
    # others': None means "no engine answered" and the tile falls back to summing live cycles, whereas
    # a zeroed dict would assert that nothing closed today — a claim a data-only node cannot make.
    #
    # Asserted as a WHOLE-BODY equality on purpose: this is the contract test that caught the field
    # being added to the model without being passed by the endpoint. A subset check would have let a
    # silently-missing field through, which is precisely how `realized_session` reached Redis and then
    # vanished at the REST boundary while both ends passed their own tests.
    assert body == {"trades": [], "status": None, "error": None, "realized_session": None,
                    "realized_periods": None,
                    # Declared AND passed (#846) — the same guard, two fields on.
                    "realized_periods_swept": None, "realized_legs": None,
                    # ...and a third (#699 a): the per-lane flows for the window identity.
                    "lane_flows": None}


def test_positions_shape(client) -> None:
    body = client.get("/positions").json()
    assert "positions" in body
    # Two-stock demo universe → two LONG positions.
    assert len(body["positions"]) == 2
    by_symbol = {p["instrument_id"]: p for p in body["positions"]}
    assert set(by_symbol) == {"AAPL.XNAS", "MSFT.XNAS"}
    aapl = by_symbol["AAPL.XNAS"]
    assert aapl["side"] == "LONG"
    assert aapl["quantity"] == 100.0
    assert by_symbol["MSFT.XNAS"]["quantity"] == 30.0


def _subscribe(ws, channel: str, params: dict | None = None) -> list[dict]:
    """Send a subscribe control frame, return the four expected event frames (ack→start→snap→end)."""
    ws.send_json({"type": "control", "op": "subscribe", "topic": {"channel": channel, "params": params or {}}})
    return [ws.receive_json() for _ in range(4)]


def test_ws_subscribe_positions(client) -> None:
    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "positions")

    assert ack["event"] == "control_ack" and ack["payload"] == {"op": "subscribe", "status": "ok"}
    assert start["event"] == "status" and start["payload"]["data"]["code"] == "replay_start"
    assert snap["event"] == "data" and snap["payload"]["frame_type"] == "snapshot"
    assert len(snap["payload"]["data"]["positions"]) == 2  # two LONG positions
    assert end["payload"]["data"]["code"] == "replay_end"


def test_ws_subscribe_bars(client) -> None:
    from api.node import N_BARS

    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "bars", {"symbol": "AAPL.XNAS"})

    assert ack["payload"]["status"] == "ok"
    assert snap["payload"]["frame_type"] == "snapshot"
    bars = snap["payload"]["data"]["bars"]
    assert len(bars) == N_BARS  # full history in the snapshot (no tail held back)
    assert {b["instrument_id"] for b in bars} == {"AAPL.XNAS"}
    assert end["payload"]["data"]["code"] == "replay_end"


def test_ws_subscribe_vwaps(client) -> None:
    """Session VWAP plane (#182 follow-up, KPI Phase 2) — synthetic node has no live VWAP stream, so this
    exercises the empty-fallback path (`NodeManager.vwaps() -> []`), not a data-error."""
    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "vwaps")

    assert ack["payload"]["status"] == "ok"
    assert snap["payload"]["frame_type"] == "snapshot"
    assert snap["payload"]["data"] == {"vwaps": []}
    assert end["payload"]["data"]["code"] == "replay_end"


def test_ws_subscribe_today_ranges(client) -> None:
    """Today's Range + Prior Close plane (#182 follow-up, KPI Phase 3) — synthetic node has no Alpaca
    snapshot feed, so this exercises the empty-fallback path (`NodeManager.today_ranges() -> []`)."""
    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "today_ranges")

    assert ack["payload"]["status"] == "ok"
    assert snap["payload"]["frame_type"] == "snapshot"
    assert snap["payload"]["data"] == {"today_ranges": []}
    assert end["payload"]["data"]["code"] == "replay_end"


def test_ws_subscribe_fundamentals(client) -> None:
    """Fundamentals plane (#182 follow-up, KPI Phase 2) — synthetic node has no FMP feed, so this
    exercises the empty-fallback path (`NodeManager.fundamentals() -> []`)."""
    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "fundamentals")

    assert ack["payload"]["status"] == "ok"
    assert snap["payload"]["frame_type"] == "snapshot"
    assert snap["payload"]["data"] == {"fundamentals": []}
    assert end["payload"]["data"]["code"] == "replay_end"


def test_ws_live_push_positions(client, monkeypatch) -> None:
    """After replay_end the per-connection push loop re-pushes the positions snapshot live."""
    from api import app as app_module

    monkeypatch.setattr(app_module, "LIVE_PUSH_INTERVAL", 0.01)  # fast tick for the test

    with client.websocket_connect("/ws/stream") as ws:
        _subscribe(ws, "positions")  # ack, start, snapshot, replay_end
        pushed = ws.receive_json()  # the push loop's next snapshot

    assert pushed["event"] == "data" and pushed["payload"]["frame_type"] == "snapshot"
    assert len(pushed["payload"]["data"]["positions"]) == 2


def test_ws_bars_empty_symbol_is_ok_not_error(client) -> None:
    """A valid bars topic with no data yet returns an EMPTY snapshot (not an error) — the Watch tile
    shows empty and the push loop fills it in, rather than rendering a spurious error."""
    with client.websocket_connect("/ws/stream") as ws:
        ack, start, snap, end = _subscribe(ws, "bars", {"symbol": "TSLA.XNAS"})  # not in the demo universe

    assert ack["payload"]["status"] == "ok"
    assert snap["payload"]["frame_type"] == "snapshot"
    assert snap["payload"]["data"]["bars"] == []


def test_ws_unsubscribe_acks(client) -> None:
    with client.websocket_connect("/ws/stream") as ws:
        _subscribe(ws, "bars", {"symbol": "AAPL.XNAS"})  # ack, start, snapshot, replay_end
        ws.send_json(
            {"type": "control", "op": "unsubscribe", "topic": {"channel": "bars", "params": {"symbol": "AAPL.XNAS"}}}
        )
        ack = ws.receive_json()

    assert ack["event"] == "control_ack" and ack["payload"] == {"op": "unsubscribe", "status": "ok"}


def test_ws_subscribe_unknown_topic(client) -> None:
    with client.websocket_connect("/ws/stream") as ws:
        # A bars topic with no symbol is genuinely unservable → error (vs empty snapshot for a valid one).
        ws.send_json({"type": "control", "op": "subscribe", "topic": {"channel": "bars", "params": {}}})
        ack = ws.receive_json()
        err = ws.receive_json()

    assert ack["event"] == "control_ack" and ack["payload"]["status"] == "error"
    assert ack["payload"]["error"]["code"] == "unknown_topic"
    assert err["event"] == "error" and err["payload"]["data"]["code"] == "subscription_error"


# --- instrument search (#25) --------------------------------------------------


def test_instruments_search_unavailable_503(client) -> None:
    """Synthetic test env has no Alpaca keys → the index isn't built → search degrades to 503, not a crash."""
    from api.app import app

    app.state.search = None  # explicit: no provider/keys
    resp = client.get("/instruments/search", params={"q": "aapl"})
    assert resp.status_code == 503


def test_instruments_search_ranked_results(client) -> None:
    """With an index present, the endpoint returns the ranked InstrumentSearchResponse shape."""
    from api.app import app
    from api.models import InstrumentMatch

    class _FakeIndex:
        async def search(self, q: str, limit: int) -> list[InstrumentMatch]:
            return [InstrumentMatch(instrument_id="AAPL.XNAS", symbol="AAPL", name="Apple Inc.", venue="XNAS")]

    prior = app.state.search
    app.state.search = _FakeIndex()
    try:
        body = client.get("/instruments/search", params={"q": "aapl", "limit": 5}).json()
    finally:
        app.state.search = prior
    assert body["results"][0] == {
        "instrument_id": "AAPL.XNAS",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "venue": "XNAS",
    }


def test_instruments_search_limit_validation(client) -> None:
    """`limit` is bounded 1..50 by the query contract → out-of-range is a 422, not a silent clamp at the edge."""
    assert client.get("/instruments/search", params={"q": "a", "limit": 0}).status_code == 422
    assert client.get("/instruments/search", params={"q": "a", "limit": 51}).status_code == 422


def test_submit_order_returns_command_id(client) -> None:
    r = client.post("/orders", json={
        "instrument_id": "AAPL.XNAS", "side": "BUY", "quantity": 1, "order_type": "market"})
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and "command_id" in b and b["client_order_id"]


def test_command_status_pending_when_unknown(client) -> None:
    # synthetic node has no command channel → any id is pending, never a false reject (#39)
    r = client.get("/commands/does-not-exist")
    assert r.status_code == 200 and r.json()["status"] == "pending"


# --- Symbol pool (#79 follow-on) ----------------------------------------------
def _fake_pool(monkeypatch, rows, sources=()):
    """Stub the Postgres-backed pool module — these tests pin the ENDPOINT contract, not the store."""
    from api import pool as pool_mod

    async def _list(held=None):
        return [dict(r, held=r["symbol"] in (held or set())) for r in rows]

    async def _health():
        return list(sources)

    monkeypatch.setattr(pool_mod, "list_pool", _list)
    monkeypatch.setattr(pool_mod, "source_health", _health)


_ROW = {"symbol": "AFL", "sources": ["ledger_book"], "provenance": "ledger_book",
        "meta": {}, "held": False, "override": None, "reason": None}
_EXCLUDED = {"symbol": "FTNR", "sources": [], "provenance": "blacklisted",
             "meta": {}, "held": False, "override": "exclude", "reason": "no data"}


def test_pool_lists_blacklisted_symbols_but_excludes_them_from_the_count(client, monkeypatch) -> None:
    """A blacklisted name must stay VISIBLE — otherwise the only evidence of the decision is that the
    row vanished — while not counting toward the pool the strategy can actually rank."""
    _fake_pool(monkeypatch, [_ROW, _EXCLUDED])

    body = client.get("/pool").json()

    assert {r["symbol"] for r in body["symbols"]} == {"AFL", "FTNR"}
    assert body["count"] == 1, "count is the RANKABLE pool — the excluded row must not inflate it"
    assert next(r for r in body["symbols"] if r["symbol"] == "FTNR")["override"] == "exclude"


def test_pool_reports_source_health(client, monkeypatch) -> None:
    """A failed source is a hard block on deciding, so it ships with the pool, not buried in a log."""
    _fake_pool(monkeypatch, [_ROW], sources=[
        {"name": "ledger_book", "status": "ok", "symbol_count": 70,
         "refreshed_at": "2026-08-05T18:24:38+00:00", "stale": False, "detail": None}])

    sources = client.get("/pool").json()["sources"]

    assert sources[0]["name"] == "ledger_book" and sources[0]["stale"] is False


def test_clearing_an_unknown_override_kind_is_refused(client, monkeypatch) -> None:
    """`kind` reaches a DELETE path that would silently no-op on a typo, leaving the operator
    believing they had cleared something."""
    _fake_pool(monkeypatch, [_ROW])

    assert client.delete("/pool/override/AFL/unpin").status_code == 422


def test_setting_an_override_returns_the_updated_pool(client, monkeypatch) -> None:
    _fake_pool(monkeypatch, [_ROW])
    called: list[tuple] = []

    async def _set(symbol, kind, reason=""):
        called.append((symbol, kind, reason))

    from api import pool as pool_mod
    monkeypatch.setattr(pool_mod, "set_override", _set)

    body = client.post("/pool/override", json={"symbol": "afl", "kind": "exclude", "reason": "x"})

    assert body.status_code == 200
    assert called == [("afl", "exclude", "x")]
    assert body.json()["count"] == 1


def test_an_invalid_override_kind_is_rejected_by_the_schema(client) -> None:
    assert client.post("/pool/override", json={"symbol": "AFL", "kind": "sell"}).status_code == 422


def test_the_distribution_primitive_is_REACHABLE() -> None:
    """#373 — `distribute_unallocated` was built, tested, and callable by nothing.

    Both halves already existed: `plan_distribution` (proportional to shortfall, conservation and intent
    capped) and `distribute_unallocated` (idempotent, one transaction). Neither had a single production
    caller — `grep` found them referenced only from their own test files. So on 2026-08-19 BCTROT-004 and
    QC345-003 sat at `target 20000, actual 0` with `deployable` pinned at 0, unable to open a position,
    while $45,000.00 sat unallocated and the only route to funding them was waiting for MOMENTUM to sell.

    That is the built-never-executed shape this repo keeps shipping: a mechanism whose own tests pass,
    wired to nothing. The ROUTE is the fix, so the route is what gets pinned — and it is pinned on the
    app's routing table rather than over HTTP, because the behaviour behind it needs Postgres and is
    already covered by `test_budget_distribution_store.py`. Asserting it here through the client would
    only re-test the database.
    """
    from api.app import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/sleeves/distribute" in paths, (
        "no route reaches the distribution primitive — `plan_distribution` and `distribute_unallocated` "
        "remain correct and uncallable, which is the whole of #373"
    )
    route = next(r for r in app.routes if getattr(r, "path", None) == "/sleeves/distribute")
    assert "POST" in route.methods, "distribution moves capital — it cannot be a GET"


def test_the_route_actually_calls_the_primitive_and_returns_what_moved() -> None:
    """The wiring, not the registration. A route that answers 200 and allocates nothing is worse than
    no route: it reports success for capital that never moved.

    Source-level because the call needs a live session; the seam is which function is invoked and
    whether its result is returned rather than discarded — exactly what a passing helper test cannot see.
    """
    import ast
    import inspect
    import textwrap

    from api.app import distribute_unallocated_capital

    src = textwrap.dedent(inspect.getsource(distribute_unallocated_capital))
    called = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "distribute_unallocated"
    ]
    assert called, "the route does not call distribute_unallocated — it is a stub with a docstring"
    assert "allocations" in src and "distributed" in src, (
        "the route does not report what moved; an operator moving capital needs the number back, and a "
        "client that re-derives the total is a second derivation that can disagree with the first"
    )
    assert "run_id" in src, (
        "no run_id reaches the primitive, so its idempotency guard cannot bind and a retried "
        "distribution funds every sleeve twice"
    )


def test_health_FORWARDS_ownership_violations_rather_than_merely_modelling_them(client, monkeypatch):
    """A lane holding a short is mis-stating its own held size, and `/health` must say so.

    THE TRAP THIS GUARDS is the one named in the endpoint itself: it builds its response from an
    explicit field list, so "a field the model has and nobody passes is empty forever" (#546). A
    model-only change would ship a field that is permanently `[]` and a banner that never fires.

    The condition is the 2026-08-30 incident: eight mirrored shorts, netting to exactly what the
    broker held, so `reconcile_drift` was correctly empty for nine days while four lanes mis-stated
    what they owned. Freshness detectors cannot see this — the frame was current and internally
    consistent the whole time.
    """
    from api.app import app as _app
    from api.models import PositionDTO

    node = _app.state.node
    clean = node.positions()
    assert not any(getattr(p, "side", "") == "SHORT" for p in clean), (
        "precondition broken: the synthetic node already holds a short"
    )
    assert client.get("/health").json().get("ownership_violations", []) == [], (
        "a clean book must report no violations, or the assertion below passes vacuously"
    )

    mirror = [
        PositionDTO(instrument_id="WHD.XNYS", side="LONG", quantity=28.0, avg_px_open=70.57,
                    realized_pnl="0.00 USD", strategy_id="BCTROT-004"),
        PositionDTO(instrument_id="WHD.XNYS", side="SHORT", quantity=28.0, avg_px_open=70.57,
                    realized_pnl="0.00 USD", strategy_id="MOMENTUM-002"),
    ]
    monkeypatch.setattr(type(node), "positions", lambda self: mirror, raising=False)

    body = client.get("/health").json()

    assert "ownership_violations" in body, "/health does not carry the field at all"
    violations = body["ownership_violations"]
    assert violations, "a mirrored short reached /health as a clean book"
    lanes = {v["strategy_id"] for v in violations}
    assert lanes == {"MOMENTUM-002"}, (
        f"expected only the lane holding the short to be named, got {lanes} — naming the stranded "
        f"lane indicts the victim, which is the alarm an operator learns to skip"
    )
    assert violations[0]["instrument_id"] == "WHD.XNYS"
    assert violations[0]["signed_qty"] == -28.0


# ==================================================================================================
# /health MUST NOT DIE BECAUSE ONE CHECK COULD NOT READ ITS INPUT
# ==================================================================================================
def test_health_still_answers_when_the_ownership_check_cannot_READ_the_book(client, monkeypatch):
    """A REGRESSION THIS PR INTRODUCED, found by review.

    `signed_qty_of` RAISES by design on a shape it cannot read — returning 0.0 would report "no
    shorts" for a book it failed to understand, a silent all-clear, which is the exact failure this
    ticket exists to remove. That refusal is right.

    But `/health` called it UNGUARDED, and this endpoint's own prose says the opposite:
    `_inert_contradictions` is documented "NEVER RAISES — a health endpoint that fails because one of
    its checks failed tells an operator nothing about the other checks." One renamed DTO field would
    take down drift display, subsystems, provenance and the banner TOGETHER, and the UI would render
    `apiDown` — telling the operator the cockpit is unreachable when it is running fine and a single
    check is confused. The ALERT path already guards the same call with `_check_failed`; this one did
    not.

    THE DEGRADATION IS ITS OWN CONDITION, not silence. The endpoint answers, `ownership_violations`
    is NULL rather than `[]`, and the failure reports itself. An empty list is a claim that there are
    none; null is "could not tell". Three states, and the middle one is the whole point of the check.
    """
    import api.app as app_mod

    def _explode(_positions):
        raise ValueError("PositionDTO.side became an enum and nothing here knows that")

    # FIXTURE PROPERTY FIRST: the substitute really does raise, or a passing endpoint proves nothing.
    with pytest.raises(ValueError):
        _explode([])

    monkeypatch.setattr(app_mod, "short_violations", _explode)
    resp = client.get("/health")

    assert resp.status_code == 200, "one confused check must not take the endpoint down"
    body = resp.json()
    assert body["ownership_violations"] is None, (
        "unreadable must be UNKNOWN, not an empty list — an empty list claims there are none"
    )
    assert any("ownership" in str(x).lower() for x in (body.get("inert") or [])), (
        f"the failed check must report ITSELF; silence is indistinguishable from clean. "
        f"inert={body.get('inert')}"
    )
    # And the SIBLING checks still answered — the reason the guard exists at all.
    assert body["subsystems"], "the other checks must survive one check failing"


# ==================================================================================================
# SPLIT DIVERGENCE MUST REACH /health WITHOUT TELEGRAM (#817)
#
# These two live HERE rather than beside the rest of #817's tests
# (api/test_split_divergence_is_visible_without_telegram.py) for one reason: a BacktestEngine uses
# native globals that segfault if a second one is built in the same interpreter, so the TestClient
# is module-scoped and there is exactly one per process. A second file with its own client crashes
# the run — measured, not assumed.
# ==================================================================================================
class _ClaimRow:
    """SQLAlchemy-shaped: production reads `r._mapping` (alerts.py:663), not tuple unpacking."""

    def __init__(self, strategy_id, symbol, qty):
        self._mapping = {"strategy_id": strategy_id, "symbol": symbol, "qty": qty}


def test_health_REPORTS_split_divergence_with_notifications_OFF(client, monkeypatch):
    """THE WHOLE OF #817, at the entry point an operator actually reads.

    MEASURED on ibkr-paper 2026-09-09: eleven pairs, 1,504 shares, BCTROT-004 claiming positions the
    engine attributes to EXTERNAL — and nothing on the stack said so, because the only caller of
    `split_divergence` sits inside `if self._enabled():` (alerts.py:1043) and `notifications.enabled`
    is false there. One boolean, the TELEGRAM DELIVERY toggle, decided whether the system looked.

    Every pure and wiring assertion in the sibling file can pass while `/health` calls none of it.
    This is the seam.
    """
    from api import app as app_module
    from api import settings
    from api.app import app as _app
    from api.models import PositionDTO

    assert not (settings.resolve("notifications") or {}).get("enabled"), (
        "notifications are enabled in this environment — this test would prove nothing"
    )

    node = _app.state.node
    assert (client.get("/health").json().get("split_divergence") or {}).get("pairs") == [], (
        "a clean book already reports pairs — the assertion below would pass vacuously"
    )

    book = [
        PositionDTO(instrument_id="AEM.XNYS", side="LONG", quantity=10.0, avg_px_open=215.58,
                    realized_pnl="0.00 USD", strategy_id="BCTROT-004"),
        PositionDTO(instrument_id="AEM.XNYS", side="LONG", quantity=36.0, avg_px_open=215.58,
                    realized_pnl="0.00 USD", strategy_id="EXTERNAL"),
    ]
    monkeypatch.setattr(type(node), "positions", lambda self: book, raising=False)

    async def _claims():
        return [_ClaimRow("BCTROT-004", "AEM", 46.0)]
    monkeypatch.setattr(app_module, "_claim_rows", _claims, raising=False)

    sd = client.get("/health").json()["split_divergence"]
    assert sd["status"] == "ok", f"the surface could not compute: {sd}"
    assert sd["pairs"], "a claim the cache contradicts reached /health as a clean book"
    p = sd["pairs"][0]
    assert (p["symbol"], p["strategy_id"], p["claim"], p["cache"]) == ("AEM", "BCTROT-004", 46.0, 10.0), p


def test_health_STILL_ANSWERS_when_the_CLAIMS_read_fails(client, monkeypatch):
    """Same rule as the ownership check directly above: one confused check must not take the endpoint
    down, and its degradation is its OWN condition rather than silence. A Postgres hiccup must not
    turn eleven divergent pairs into a clean `[]`."""
    from api import app as app_module

    async def _boom():
        raise RuntimeError("postgres said no")
    monkeypatch.setattr(app_module, "_claim_rows", _boom, raising=False)

    resp = client.get("/health")
    assert resp.status_code == 200, "one confused check must not take the endpoint down"
    sd = resp.json()["split_divergence"]
    assert sd["status"] != "ok", "an unreadable claims table reported a clean split"
    assert sd["pairs"] == [], "pairs must be empty when nothing could be computed"
    assert sd["error"], "the failed check must report ITSELF; silence is indistinguishable from clean"
    assert resp.json()["subsystems"], "the other checks must survive this one failing"


def test_health_REPORTS_a_bleeding_lane_and_DEGRADES(client, monkeypatch):
    """#1098 at the entry point an operator reads. The pure fold is pinned in test_lanes_bleeding.py;
    this drives `/health` with the read substituted (the `_claim_rows` idiom) and checks the field,
    the DTO shape and the banner."""
    from api import app as app_module

    before = client.get("/health").json()
    assert (before.get("lanes_bleeding") or {}).get("status") in ("ok", "unreadable"), before.get("lanes_bleeding")
    assert not (before.get("lanes_bleeding") or {}).get("lanes"), (
        "a lane already bleeds in the test env — the assertion below would pass vacuously")

    async def _one():
        from api.lanes_bleeding import LaneBleed
        b = LaneBleed("MOMENTUM-002", 3, 0, 2, "2026-09-16T13:46:31+00:00",
                      "pool sources stale or failed: ledger_book")
        return {"status": "ok", "lanes": [b.as_dict()], "error": None}

    monkeypatch.setattr(app_module, "_lanes_bleeding", _one, raising=False)
    body = client.get("/health").json()
    lb = body["lanes_bleeding"]
    assert lb["status"] == "ok" and [l["strategy_id"] for l in lb["lanes"]] == ["MOMENTUM-002"], lb
    assert lb["lanes"][0]["line"].startswith("MOMENTUM-002: 3 exits, 0 entries, 2 sessions"), lb
    assert body["status"] == "degraded", "a lane going to cash under a TRADING label showed a green banner"


def test_health_STILL_ANSWERS_when_the_JOURNAL_read_fails_and_does_not_degrade_for_it(client, monkeypatch):
    """Three states: a failed read is `unreadable` with `lanes: None`, never `[]` — and it does not
    move the banner by itself (lead, #1098 scope)."""
    from api import app as app_module

    async def _boom():
        return {"status": "unreadable", "lanes": None, "error": "RuntimeError: postgres said no"}

    monkeypatch.setattr(app_module, "_lanes_bleeding", _boom, raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200
    lb = resp.json()["lanes_bleeding"]
    assert lb["status"] == "unreadable" and lb["lanes"] is None and lb["error"], lb
    # Whatever else the test env degrades on, THIS field must not be the reason: recompute the
    # banner's other inputs from the same body.
    body = resp.json()
    others_ok = (all(s["ok"] for s in body["subsystems"]) and not body.get("unpriced_positions")
                 and body.get("feed_stale") is not True and not (body.get("split_divergence") or {}).get("pairs"))
    if others_ok:
        assert body["status"] == "ok", "an unreadable journal degraded the banner on its own"


# ==================================================================================================
# THE WINDOW-BASE ENDPOINT — the seam, not the assembler (#699 read path)
# ==================================================================================================
def test_the_endpoint_answers_and_carries_BOTH_the_map_and_the_unreadable_list(client, monkeypatch):
    """THE SEAM, driven through HTTP. The assembler is tested above; this proves the endpoint calls
    it and FORWARDS BOTH FIELDS. A response model that declares a field nobody passes is silently
    empty forever — #546's shape, and this endpoint builds its object field by field."""
    import api.app as app_mod
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"MOMENTUM-002": 100.0}, "all": None},
                           unreadable=("2026-07-31",))

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    body = client.get("/pnl/unrealized-base").json()

    assert body["by_period"]["1W"]["MOMENTUM-002"] == 100.0
    assert body["by_period"]["all"] is None, "a period with no base is an explicit null, not absent"
    assert body["unreadable"] == ["2026-07-31"], (
        "the outage list must reach the caller — without it a failed read is indistinguishable from "
        "an uncaptured day, which is the distinction this whole payload exists to preserve"
    )


def test_the_endpoint_uses_an_ET_date_not_the_containers_local_one(client, monkeypatch):
    """Session dates in the table are ET. The container runs UTC, and after 20:00 ET they are
    DIFFERENT DAYS — so a local date would ask for tomorrow's base all evening and get nothing, every
    evening, silently. The same trap `venue_hours` documents and `eod_hook` was caught by."""
    import api.app as app_mod
    from api.pnl_base import WindowBases

    seen = {}

    async def _fake(store, *, today):
        seen["today"] = today
        return WindowBases()

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    client.get("/pnl/unrealized-base")

    from datetime import datetime
    from zoneinfo import ZoneInfo

    assert seen["today"] == datetime.now(ZoneInfo("America/New_York")).date()


def test_a_FAILING_assembler_does_not_500_the_endpoint(client, monkeypatch):
    """It reads a database. A read that raises must answer with everything UNKNOWN rather than an
    error page — the tile then draws em dashes, which is correct, instead of showing nothing at all
    and leaving an operator unable to tell a broken endpoint from a broken stack."""
    import api.app as app_mod

    async def _boom(store, *, today):
        raise ConnectionError("postgres went away")

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _boom)
    resp = client.get("/pnl/unrealized-base")
    assert resp.status_code == 200
    body = resp.json()
    # FIXTURE PROPERTY FIRST. `all()` over an EMPTY dict is True, so a mutant returning `{}` on
    # failure passed this — every period must be PRESENT and null, not absent. An absent key is
    # indistinguishable from a serialisation fault; an explicit null is a statement.
    from api.realized_broker import PERIOD_DAYS

    assert set(body["by_period"]) == set(PERIOD_DAYS), (
        f"every period must appear even on failure; got {sorted(body['by_period'])}"
    )
    assert all(v is None for v in body["by_period"].values())
    # THE FAILURE REPORTS ITSELF, AND IN THE RIGHT FIELD. `unreadable` promises DATES — a day to
    # backfill — while an assembly failure is a different operator action entirely: restart or debug
    # the api. Putting "assembly failed: TypeError" in a list documented as dates is a lie about its
    # own type, so they are separate fields.
    assert body["error"], "an assembly failure must report itself"
    assert "ConnectionError" in body["error"]
    assert body["unreadable"] == [], "a whole-assembly failure names no specific date to backfill"


# --- #922: the liquidate route enqueues the command with the leash and the provenance --------------------

def test_liquidate_route_enqueues_liquidate_lane_with_leash_and_provenance(client, monkeypatch) -> None:
    sent = []

    async def send_command(ctype, payload):
        sent.append((ctype, payload))
        return "cmd-922"
    monkeypatch.setattr(client.app.state.node, "send_command", send_command, raising=False)
    r = client.post("/strategies/MOMENTUM-002/liquidate", json={
        "expected_positions": 2, "expected_total_qty": 176, "invoked_by": "operator", "reason": "market broke"})
    assert r.status_code == 200, r.text
    assert sent == [("liquidate_lane", {"strategy_id": "MOMENTUM-002", "expected_positions": 2,
                                        "expected_total_qty": 176.0, "invoked_by": "operator", "reason": "market broke"})]
    assert r.json()["command_id"] == "cmd-922"


def test_liquidate_route_refuses_a_body_without_provenance_or_leash(client) -> None:
    r = client.post("/strategies/MOMENTUM-002/liquidate", json={"expected_positions": 2, "expected_total_qty": 176, "invoked_by": "", "reason": "x"})
    assert r.status_code == 422
    r = client.post("/strategies/MOMENTUM-002/liquidate", json={"invoked_by": "operator", "reason": "x"})
    assert r.status_code == 422


def test_the_endpoint_serves_the_NET_TERMS_from_the_bases_the_flows_frame_and_the_manifest_coverage(client, monkeypatch):
    """#699 a, THE SEAM. `market_value` and `net` must reach the caller; `net` is assembled from
    THREE sources the endpoint alone holds together — the bases (Postgres), the engine's `lane_flows`
    (the trades frame via the consumer) and the manifest coverage (Postgres). A declared field nobody
    passes is silently None forever (#546's shape)."""
    from datetime import date

    import api.app as app_mod
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"MOMENTUM-002": 250.0}, "all": None},
                           market_value={"1W": {"MOMENTUM-002": 2000.0}, "all": None},
                           base_date={"1W": "2026-09-04", "all": None})

    class _Cov:
        async def coverage(self, since: str):
            _seen["since"] = since
            return {"first_observed": {"MOMENTUM-002": "2026-08-31"},
                    "captured": {"2026-09-04", "2026-09-05", "2026-09-07", "2026-09-08", "2026-09-09",
                                 "2026-09-10", "2026-09-11", "2026-09-12"} | {
                                 (date(2026, 9, 14) - __import__("datetime").timedelta(days=n)).isoformat()
                                 for n in range(0, 400)}}

    _seen = {}
    # THE CLOCK IS PINNED, NOT READ (#1106). The coverage above is anchored on a literal 2026-09-14;
    # the route used to read `datetime.now(ET)` INLINE — its own copy of `_today()` — so the window
    # grew past the fixture's coverage one day at a time and this test went red on 09-15 on its own,
    # `partial: 'not captured: 2026-09-15, 2026-09-16'`. A test that passes only on the day it was
    # written is a clock. FIXTURE PROPERTY FIRST: the pinned day is inside the coverage and the wall
    # clock's day is not, or pinning changes nothing and the test can go green by the calendar again.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    pinned = date(2026, 9, 14)
    captured = __import__("asyncio").run(_Cov().coverage("2026-09-04"))["captured"]
    assert pinned.isoformat() in captured
    wall = datetime.now(ZoneInfo("America/New_York")).date()
    assert wall > pinned and wall.isoformat() not in captured, (
        f"the fixture covers {wall}; this test can no longer prove the pin matters — move the anchor")
    monkeypatch.setattr(app_mod, "_today", lambda: pinned)
    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    monkeypatch.setattr(app_mod, "EodObservationStore", lambda: _Cov())
    monkeypatch.setattr(app_mod.app.state.node, "trades_flows",
                        lambda: {"by_day": {"MOMENTUM-002": {"2026-09-04": 999.0, "2026-09-10": -1000.0}},
                                 "earliest": "2026-08-17", "horizon_days": 100, "error": None},
                        raising=False)
    body = client.get("/pnl/unrealized-base").json()

    assert body["market_value"]["1W"] == {"MOMENTUM-002": 2000.0}
    assert body["net"]["1W"]["MOMENTUM-002"] == {"mv_base": 2000.0, "invested": -1000.0, "partial": None}, body["net"]
    assert body["net"]["all"] is None
    assert _seen["since"] <= "2026-09-04", "coverage must be read back to (at least) the oldest base"


def test_the_endpoint_serves_net_UNKNOWN_when_the_engine_has_published_no_flows(client, monkeypatch):
    import api.app as app_mod
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"MOMENTUM-002": 250.0}}, market_value={"1W": {"MOMENTUM-002": 2000.0}},
                           base_date={"1W": "2026-09-04"})

    class _Cov:
        async def coverage(self, since):
            return {"first_observed": {}, "captured": set()}

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    monkeypatch.setattr(app_mod, "EodObservationStore", lambda: _Cov())
    monkeypatch.setattr(app_mod.app.state.node, "trades_flows", lambda: None, raising=False)
    body = client.get("/pnl/unrealized-base").json()
    assert body["net"]["1W"]["MOMENTUM-002"]["invested"] is None
    assert body["net"]["1W"]["MOMENTUM-002"]["mv_base"] == 2000.0


def test_a_FAILING_coverage_read_costs_the_net_terms_NOT_the_bases(client, monkeypatch):
    """Two Postgres reads; the second failing must not blank the first, and must not fabricate
    coverage (which would render every window as un-partial)."""
    import api.app as app_mod
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"MOMENTUM-002": 250.0}}, market_value={"1W": {"MOMENTUM-002": 2000.0}},
                           base_date={"1W": "2026-09-04"})

    class _Cov:
        async def coverage(self, since):
            raise ConnectionError("manifest unreadable")

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    monkeypatch.setattr(app_mod, "EodObservationStore", lambda: _Cov())
    monkeypatch.setattr(app_mod.app.state.node, "trades_flows", lambda: None, raising=False)
    body = client.get("/pnl/unrealized-base").json()
    assert body["by_period"]["1W"] == {"MOMENTUM-002": 250.0}
    assert body["net"] == {p: None for p in body["net"]}, "no coverage → no net terms, every period null, not fabricated"
    assert "coverage" in (body["error"] or "")


def test_the_endpoint_feeds_the_qty_invariant_from_the_POSITIONS_plane_signed_by_side(client, monkeypatch):
    """#1072 b: `lane_net_terms(qty_now=…)` gets `{lane: {instrument: signed qty}}` built from
    `node.positions()` — SHORT negative, FLAT rows dropped. Without it every lane on a scoping frame
    is refused ("positions plane unreadable"), so this is the seam that makes the invariant live."""
    import api.app as app_mod
    from api.models import PositionDTO
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"BCTROT-004": 1.0}}, market_value={"1W": {"BCTROT-004": 1000.0}},
                           instruments={"1W": {"BCTROT-004": ["B.XNYS"]}}, qty_at={"1W": {"BCTROT-004": {"B.XNYS": 10.0}}},
                           base_date={"1W": "2026-09-04"})

    class _Cov:
        async def coverage(self, since):
            return {"first_observed": {"BCTROT-004": "2026-08-31"}, "captured": set()}

    seen = {}
    real = app_mod.lane_net_terms

    def _spy(bases, flows, *, coverage, today, qty_now=None):
        seen["qty_now"] = qty_now
        return real(bases, flows, coverage=coverage, today=today, qty_now=qty_now)

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    monkeypatch.setattr(app_mod, "EodObservationStore", lambda: _Cov())
    monkeypatch.setattr(app_mod, "lane_net_terms", _spy)
    monkeypatch.setattr(app_mod.app.state.node, "trades_flows", lambda: {
        "by_day": {"BCTROT-004": {"2026-09-09": 500.0}}, "internal": {}, "kernel_fills": [],
        "touched": {"BCTROT-004": {"2026-09-09": ["B.XNYS"]}},
        "net_qty": {"BCTROT-004": {"2026-09-09": {"B.XNYS": 5.0, "S.XNYS": -4.0}}},
        "earliest": "2026-08-17", "horizon_days": 100, "error": None}, raising=False)
    monkeypatch.setattr(app_mod.app.state.node, "positions", lambda: [
        PositionDTO(instrument_id="B.XNYS", side="LONG", quantity=15.0, strategy_id="BCTROT-004", avg_px_open=1.0, realized_pnl="0.00 USD"),
        PositionDTO(instrument_id="S.XNYS", side="SHORT", quantity=4.0, strategy_id="BCTROT-004", avg_px_open=1.0, realized_pnl="0.00 USD"),
        PositionDTO(instrument_id="F.XNYS", side="FLAT", quantity=0.0, strategy_id="BCTROT-004", avg_px_open=1.0, realized_pnl="0.00 USD"),
    ], raising=False)
    body = client.get("/pnl/unrealized-base").json()
    assert seen["qty_now"] == {"BCTROT-004": {"B.XNYS": 15.0, "S.XNYS": -4.0}}
    # B: 10 → 15 by +5; S: 0 → −4 by −4 (the sign is what makes the short consistent) → vouched.
    assert body["net"]["1W"]["BCTROT-004"]["invested"] == 500.0, body["net"]


def test_a_RAISING_positions_plane_is_a_refusal_not_a_flat_book(client, monkeypatch):
    """Review Q4: the unreadable case must be driven by a positions() that RAISES, not an injected
    None — and with no frame qty_now to fall back on, every lane on a scoping frame is refused."""
    import api.app as app_mod
    from api.pnl_base import WindowBases

    async def _fake(store, *, today):
        return WindowBases(by_period={"1W": {"BCTROT-004": 1.0}}, market_value={"1W": {"BCTROT-004": 1000.0}},
                           instruments={"1W": {"BCTROT-004": ["B.XNYS"]}}, qty_at={"1W": {"BCTROT-004": {"B.XNYS": 10.0}}},
                           base_date={"1W": "2026-09-04"})

    class _Cov:
        async def coverage(self, since):
            return {"first_observed": {"BCTROT-004": "2026-08-31"}, "captured": set()}

    def _boom():
        raise RuntimeError("redis gone")

    monkeypatch.setattr(app_mod, "unrealized_base_by_period", _fake)
    monkeypatch.setattr(app_mod, "EodObservationStore", lambda: _Cov())
    monkeypatch.setattr(app_mod.app.state.node, "trades_flows", lambda: {
        "by_day": {"BCTROT-004": {"2026-09-09": 500.0}}, "internal": {}, "kernel_fills": [],
        "touched": {"BCTROT-004": {"2026-09-09": ["B.XNYS"]}}, "net_qty": {"BCTROT-004": {"2026-09-09": {"B.XNYS": 5.0}}},
        "qty_now": None, "earliest": "2026-08-17", "horizon_days": 100, "error": None}, raising=False)
    monkeypatch.setattr(app_mod.app.state.node, "positions", _boom, raising=False)
    body = client.get("/pnl/unrealized-base").json()
    t = body["net"]["1W"]["BCTROT-004"]
    assert t["invested"] is None and "positions" in t["partial"]
