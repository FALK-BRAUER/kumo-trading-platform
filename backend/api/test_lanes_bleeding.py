"""#1098 — a lane that logs NO DECISION while venue-side stops drain its book is BLEEDING.

Measured on ibkr-paper 2026-09-15..17 (the fixture is those rows, verbatim): MOMENTUM-002 logged
`session outcome — NO DECISION — pool sources stale or failed: ledger_book` at 13:35Z on 09-15 and
09-17, and between them protection sold SM, GRDN and CVE (`terminal: … closed by a protective stop —
this lane did not place that order`, 10 rows for 3 fills). The book went 8 → 2. Every surface said
TRADING. The journal held both facts; nothing joined them.

The fold reads `detail` codes, never `summary` text — `decided: false` on the session-outcome state
row, `by: "protection"` + `phase: "terminal"` on the stop-out order rows — because those are the
shapes every runner writes through the one PgJournal (slot_outcome.py says why).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "lanes_bleeding_1098_ibkr_paper.json"


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _no_decision_sessions(rows, sid):
    return {r["session"] for r in rows if r["strategy_id"] == sid and r["kind"] == "state"
            and isinstance(r["detail"], dict) and r["detail"].get("decided") is False}


def _stop_out_coids(rows, sid):
    return {r["detail"].get("client_order_id") for r in rows if r["strategy_id"] == sid
            and r["kind"] == "order" and r["detail"].get("by") == "protection"
            and r["detail"].get("phase") == "terminal"}


class TestTheFixtureCanExpressTheBug:
    """Assert the fixture's own properties FIRST (CLAUDE.md): a fold over rows that cannot bleed
    proves nothing about the detector."""

    def test_momentum_has_two_no_decision_sessions_and_stop_outs_between_them(self):
        rows = _rows()
        assert _no_decision_sessions(rows, "MOMENTUM-002") == {"2026-09-15", "2026-09-17"}
        assert _stop_out_coids(rows, "MOMENTUM-002") == {
            "PROT-SELL-SM-XNYS-bed80e66", "PROT-SELL-GRDN-XNYS-66aa2070", "PROT-SELL-CVE-XNYS-baebde85"}
        # THE DEDUPE MUST BITE: 10 rows for 3 fills. A row count over-reports by 3.3x.
        assert sum(1 for r in rows if r["strategy_id"] == "MOMENTUM-002" and r["kind"] == "order") == 10

    def test_bctrot_has_only_one_no_decision_session_in_the_window(self):
        # BCTROT's 09-16 slots wrote no row at all (the boot at 14:46Z missed open+5m and nothing
        # journalled close-20m), so by ROWS it has one NO DECISION session — the N=2 boundary.
        rows = _rows()
        assert _no_decision_sessions(rows, "BCTROT-004") == {"2026-09-15"}
        assert len(_stop_out_coids(rows, "BCTROT-004")) == 3

    def test_no_lane_in_the_fixture_entered_anything(self):
        rows = _rows()
        assert not [r for r in rows if r["kind"] == "order" and r["detail"].get("by") != "protection"]
        assert not [r for r in rows if r["kind"] == "decision"]


class TestFold:
    def test_momentum_bleeds_and_bctrot_does_not_on_the_ibkr_paper_rows(self):
        from api.lanes_bleeding import fold_bleeding

        out = {b.strategy_id: b for b in fold_bleeding(_rows(), min_sessions=2)}
        assert set(out) == {"MOMENTUM-002"}, out
        m = out["MOMENTUM-002"]
        # 3 distinct stop fills, not 10 rows; 0 entries; 2 sessions; since the first stop-out.
        assert (m.venue_exits, m.own_entries, m.no_decision_sessions) == (3, 0, 2)
        assert m.since.startswith("2026-09-16T13:46:31")
        assert m.blocked == "pool sources stale or failed: ledger_book"
        assert m.line() == ("MOMENTUM-002: 3 exits, 0 entries, 2 sessions without a decision, "
                              "since 2026-09-16 13:46Z — pool sources stale or failed: ledger_book")

    def test_one_session_is_not_bleeding_but_two_are(self):
        """N counts SESSIONS, not slots (lead, #1098 scope): BCTROT runs two slots a day, so two
        NO DECISION slots can be one bad morning."""
        from api.lanes_bleeding import fold_bleeding

        rows = [r for r in _rows() if r["strategy_id"] == "MOMENTUM-002"]
        one = [r for r in rows if not (r["kind"] == "state" and r["session"] == "2026-09-17")]
        assert not fold_bleeding(one, min_sessions=2)
        assert fold_bleeding(one, min_sessions=1)

    def test_a_stop_out_alone_is_not_bleeding(self):
        """Protection selling a name on a lane that DECIDES is the lane working. Only the pairing
        with NO DECISION is the finding."""
        from api.lanes_bleeding import fold_bleeding

        rows = [r for r in _rows() if r["strategy_id"] == "MOMENTUM-002" and r["kind"] == "order"]
        assert not fold_bleeding(rows, min_sessions=1)

    def test_no_decision_without_a_venue_exit_is_not_bleeding(self):
        """A lane that cannot decide and whose book is untouched is #1097's finding, not this one."""
        from api.lanes_bleeding import fold_bleeding

        rows = [r for r in _rows() if r["strategy_id"] == "MOMENTUM-002" and r["kind"] != "order"]
        assert not fold_bleeding(rows, min_sessions=2)

    def test_an_own_entry_that_reached_terminal_clears_it(self):
        """A lane that BOUGHT in the window is not going to cash."""
        from api.lanes_bleeding import fold_bleeding

        rows = _rows() + [{
            "id": 99999, "ts": "2026-09-17T13:36:00+00:00", "strategy_id": "MOMENTUM-002",
            "session": "2026-09-17", "slot": "open+5m", "symbol": "XYZ", "kind": "order",
            "detail": {"phase": "terminal", "ok": True, "side": "BUY", "client_order_id": "kumo-own1",
                       "position_after": 10},
        }]
        assert "MOMENTUM-002" not in {b.strategy_id for b in fold_bleeding(rows, min_sessions=2)}

    def test_a_sent_then_rejected_entry_does_not_clear_it(self):
        """Lead, #1098 scope: an entry that never landed is not an entry — the #1039 class would
        otherwise read as healthy."""
        from api.lanes_bleeding import fold_bleeding

        rows = _rows() + [{
            "id": 99998, "ts": "2026-09-17T13:36:00+00:00", "strategy_id": "MOMENTUM-002",
            "session": "2026-09-17", "slot": "open+5m", "symbol": "XYZ", "kind": "order",
            "detail": {"phase": "intent", "side": "BUY", "client_order_id": "kumo-own2"},
        }, {
            "id": 99999, "ts": "2026-09-17T13:36:01+00:00", "strategy_id": "MOMENTUM-002",
            "session": "2026-09-17", "slot": "open+5m", "symbol": "XYZ", "kind": "order",
            "detail": {"phase": "result", "ok": False, "side": "BUY", "client_order_id": "kumo-own2"},
        }]
        out = {b.strategy_id: b for b in fold_bleeding(rows, min_sessions=2)}
        assert "MOMENTUM-002" in out and out["MOMENTUM-002"].own_entries == 0

    def test_a_short_lane_bleeds_on_buy_stop_outs(self):
        """The venue exit for a SHORT lane is a BUY (CRSISHORT-006). The predicate is `by:
        protection` + `phase: terminal`, never the word SELL."""
        from api.lanes_bleeding import fold_bleeding

        rows = []
        for s in ("2026-09-15", "2026-09-17"):
            rows.append({"id": len(rows), "ts": f"{s}T13:20:00+00:00", "strategy_id": "CRSISHORT-006",
                         "session": s, "slot": "open-10m", "symbol": None, "kind": "state",
                         "detail": {"decided": False, "blocked": "not warm"}})
        rows.append({"id": 50, "ts": "2026-09-16T15:00:00+00:00", "strategy_id": "CRSISHORT-006",
                     "session": "2026-09-16", "slot": "open-10m", "symbol": "HUBC", "kind": "order",
                     "detail": {"by": "protection", "phase": "terminal", "side": "BUY", "ok": True,
                                "client_order_id": "PROT-BUY-HUBC-1", "position_after": 0}})
        out = fold_bleeding(rows, min_sessions=2)
        assert [b.strategy_id for b in out] == ["CRSISHORT-006"] and out[0].venue_exits == 1

    def test_a_correct_monthly_skip_is_not_a_missing_decision(self):
        """issue 250's row, VERBATIM (lead, 2026-09-17 16:16Z). A monthly lane between
        rebalances writes `decided: false` with `skip.is_rebalance: false` — that is the lane
        working, and its stops draining the book until the next rebalance is by design (memory
        `qc345-is-a-monthly-rebalancer`). Only a NOT-warm / panel-lacks-due skip is a missing
        decision; a `SKIPPED` row is never one."""
        from api.lanes_bleeding import fold_bleeding

        skip = {"kind": "state", "summary": "skipped: not a rebalance session (next 2026-04-01); held 3",
                "session": "2026-03-10",
                "detail": {"state": "SKIPPED", "decided": False, "blocked": "not a rebalance session",
                           "skip": {"warm": True, "panel_has_due": True, "is_rebalance": False},
                           "held": 3, "held_symbols": ["A", "B", "C"],
                           "next_rebalance_on_or_after": "2026-04-01", "skipped_sessions": 1}}
        rows = []
        for i, s in enumerate(("2026-03-10", "2026-03-11")):
            rows.append({**skip, "id": i, "ts": f"{s}T14:00:00+00:00", "strategy_id": "QC345-003",
                         "session": s, "slot": "open+5m", "symbol": None})
        rows.append({"id": 9, "ts": "2026-03-10T18:00:00+00:00", "strategy_id": "QC345-003",
                     "session": "2026-03-10", "slot": "open+5m", "symbol": "A", "kind": "order",
                     "detail": {"by": "protection", "phase": "terminal", "side": "SELL", "ok": True,
                                "client_order_id": "PROT-SELL-A-1", "position_after": 0}})
        assert not fold_bleeding(rows, min_sessions=2)
        # The SAME rows with a not-warm skip (the shape TECHIVOL would write, #1098 block 1) DO bleed.
        for r in rows[:2]:
            r["detail"] = {**r["detail"], "state": "TRADING", "blocked": "not warm",
                           "skip": {"warm": False, "panel_has_due": False, "is_rebalance": True}}
        assert [b.strategy_id for b in fold_bleeding(rows, min_sessions=2)] == ["QC345-003"]

    def test_an_own_exit_that_landed_does_not_clear_a_long_lane(self):
        """MOMENTUM selling a name itself is an EXIT; only an entry (a BUY on a long lane) says the
        lane is not going to cash. The side is judged against the stop-outs' side."""
        from api.lanes_bleeding import fold_bleeding

        rows = _rows() + [{
            "id": 99999, "ts": "2026-09-17T13:36:00+00:00", "strategy_id": "MOMENTUM-002",
            "session": "2026-09-17", "slot": "open+5m", "symbol": "XYZ", "kind": "order",
            "detail": {"phase": "terminal", "ok": True, "side": "SELL", "client_order_id": "kumo-own3",
                       "position_after": 0},
        }]
        assert "MOMENTUM-002" in {b.strategy_id for b in fold_bleeding(rows, min_sessions=2)}

    def test_a_short_lane_is_cleared_by_its_own_landed_sell(self):
        from api.lanes_bleeding import fold_bleeding

        rows = []
        for s in ("2026-09-15", "2026-09-17"):
            rows.append({"id": len(rows), "ts": f"{s}T13:20:00+00:00", "strategy_id": "CRSISHORT-006",
                         "session": s, "slot": "open-10m", "symbol": None, "kind": "state",
                         "detail": {"decided": False, "blocked": "not warm"}})
        rows.append({"id": 50, "ts": "2026-09-16T15:00:00+00:00", "strategy_id": "CRSISHORT-006",
                     "session": "2026-09-16", "slot": "open-10m", "symbol": "HUBC", "kind": "order",
                     "detail": {"by": "protection", "phase": "terminal", "side": "BUY", "ok": True,
                                "client_order_id": "PROT-BUY-HUBC-1", "position_after": 0}})
        assert fold_bleeding(rows, min_sessions=2)
        rows.append({"id": 51, "ts": "2026-09-16T15:30:00+00:00", "strategy_id": "CRSISHORT-006",
                     "session": "2026-09-16", "slot": "open-10m", "symbol": "ABC", "kind": "order",
                     "detail": {"phase": "terminal", "ok": True, "side": "SELL",
                                "client_order_id": "kumo-short-entry", "position_after": -10}})
        assert not fold_bleeding(rows, min_sessions=2)

    def test_a_side_less_landed_row_makes_entries_UNKNOWN_and_does_not_clear(self):
        """9q02jges, #1116 review: momentum.py:90 / qc345.py:203 write terminal rows as
        `{"phase": "terminal", "ok": ok}` — no side, no coid — so paper's MOMENTUM landed five BUY
        fills on 2026-09-14 and the first fold read `0 entries`. A false number. Three states."""
        import inspect

        from api.lanes_bleeding import fold_bleeding
        from strategies import momentum, qc345

        # FIXTURE PROPERTY FIRST, read off the writers: the day either grows a `side`, tighten this.
        for mod in (momentum, qc345):
            src = inspect.getsource(mod)
            assert 'detail={"phase": "terminal", "ok": ok}' in src, mod.__name__
        side_less = {"phase": "terminal", "ok": True}          # verbatim, momentum.py:90
        rows = _rows() + [{
            "id": 99999, "ts": "2026-09-17T13:36:00+00:00", "strategy_id": "MOMENTUM-002",
            "session": "2026-09-17", "slot": "open+5m", "symbol": "UGP", "kind": "order",
            "detail": dict(side_less),
        }]
        out = {b.strategy_id: b for b in fold_bleeding(rows, min_sessions=2)}
        assert "MOMENTUM-002" in out, "a lane whose entries cannot be counted must still be reported"
        m = out["MOMENTUM-002"]
        assert m.own_entries is None
        assert "entries unknown" in m.line() and " 0 entries" not in m.line(), m.line()
        assert m.as_dict()["own_entries"] is None

    def test_a_DUPLICATE_RUN_row_beside_a_real_decision_is_not_a_missing_session(self):
        """#831 (memory `journal-kind-is-load-bearing`): a concurrent run writes `decided: false,
        blocked: "already decided <s>/<slot> (concurrent run)"` NEXT TO the real `decided: true`
        row. Paper's BCTROT read 6 sessions without a decision where 3 were that row. A session
        with ANY decided row is decided — in either write order."""
        from api.lanes_bleeding import fold_bleeding

        def _pair(first_true: bool):
            t = {"id": 1, "ts": "2026-09-08T13:35:00+00:00", "strategy_id": "BCTROT-004",
                 "session": "2026-09-08", "slot": "open+5m", "symbol": None, "kind": "state",
                 "detail": {"state": "TRADING", "decided": True, "submitted": 2}}
            f = {"id": 2, "ts": "2026-09-08T13:35:00.5+00:00", "strategy_id": "BCTROT-004",
                 "session": "2026-09-08", "slot": "open+5m", "symbol": None, "kind": "state",
                 "detail": {"state": "TRADING", "decided": False,
                            "blocked": "already decided 2026-09-08/open+5m (concurrent run)"}}
            rows = [t, f] if first_true else [f, t]
            rows.append({"id": 3, "ts": "2026-09-08T15:00:00+00:00", "strategy_id": "BCTROT-004",
                         "session": "2026-09-08", "slot": "open+5m", "symbol": "APA", "kind": "order",
                         "detail": {"by": "protection", "phase": "terminal", "side": "SELL", "ok": True,
                                    "client_order_id": "PROT-SELL-APA-1", "position_after": 0}})
            return rows

        for order in (True, False):
            assert not fold_bleeding(_pair(order), min_sessions=1), f"first_true={order}"

    def test_the_LATEST_session_must_itself_be_a_missing_decision(self):
        """A lane that could not decide Mon and Tue and DECIDED Wed, with Wed's stop-outs in the
        window, recovered — not bleeding. The reverse (decided Tue, missing Mon and Wed) is."""
        from api.lanes_bleeding import fold_bleeding

        def _state(day, decided, i):
            return {"id": i, "ts": f"2026-09-{day}T13:35:00+00:00", "strategy_id": "MOMENTUM-002",
                    "session": f"2026-09-{day}", "slot": "open+5m", "symbol": None, "kind": "state",
                    "detail": {"state": "TRADING", "decided": decided,
                               **({} if decided else {"blocked": "pool sources stale"})}}
        stop = {"id": 9, "ts": "2026-09-16T15:00:00+00:00", "strategy_id": "MOMENTUM-002",
                "session": "2026-09-16", "slot": "open+5m", "symbol": "SM", "kind": "order",
                "detail": {"by": "protection", "phase": "terminal", "side": "SELL", "ok": True,
                           "client_order_id": "PROT-SELL-SM-1", "position_after": 0}}
        recovered = [_state("14", False, 1), _state("15", False, 2), _state("16", True, 3), stop]
        assert not fold_bleeding(recovered, min_sessions=2)
        relapsed = [_state("14", False, 1), _state("15", True, 2), _state("16", False, 3), stop]
        out = fold_bleeding(relapsed, min_sessions=2)
        assert [(b.strategy_id, b.no_decision_sessions) for b in out] == [("MOMENTUM-002", 2)]

    def test_blocked_is_the_LATEST_missing_session_reason(self):
        from api.lanes_bleeding import fold_bleeding

        rows = [r for r in _rows() if r["strategy_id"] == "MOMENTUM-002"]
        for r in rows:
            if r["kind"] == "state" and r["session"] == "2026-09-15":
                r["detail"] = {**r["detail"], "blocked": "OLDER reason"}
        out = fold_bleeding(rows, min_sessions=2)
        assert out[0].blocked == "pool sources stale or failed: ledger_book"

    def test_every_stop_out_row_in_the_fixture_carries_a_coid(self):
        """The dedupe keys on `client_order_id`; the fallback per-row key is not reached today and
        this says so. Paper 14 d: every `by: protection` terminal row carries one (review)."""
        rows = [r for r in _rows() if r["kind"] == "order" and r["detail"].get("by") == "protection"]
        assert rows and all(r["detail"].get("client_order_id") for r in rows)

    def test_the_sql_projection_and_the_fold_read_the_same_kinds(self):
        """Two derivations of one fact: the query narrows by kind for speed, the fold by kind for
        meaning. If either moves alone the detector goes blind to a whole kind."""
        from api.lanes_bleeding import BLEED_ROWS, READ_KINDS

        sql = str(BLEED_ROWS)
        for kind in READ_KINDS:
            assert f"'{kind}'" in sql, (kind, sql)
        assert "ORDER BY id" in sql and "hours" in sql


class TestScan:
    """`scan_bleeding` is THREE-STATE (lead, #1098 scope): a read that failed is `None` + an error,
    never `[]`. `[]` means read and clean."""

    def test_a_failed_read_is_unreadable_not_clean(self):
        from api.lanes_bleeding import scan_bleeding

        class Boom:
            async def __aenter__(self): raise RuntimeError("pg down")
            async def __aexit__(self, *a): return False

        out = asyncio.run(scan_bleeding(lambda: Boom()))
        assert out["status"] == "unreadable" and out["lanes"] is None
        assert "RuntimeError: pg down" in out["error"]

    def test_a_clean_read_is_an_empty_list(self):
        from api.lanes_bleeding import scan_bleeding

        class Db:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def execute(self, *_a, **_k):
                class R:
                    def __iter__(self): return iter(())
                return R()

        out = asyncio.run(scan_bleeding(lambda: Db()))
        assert out == {"status": "ok", "lanes": [], "error": None}


# ==================================================================================================
# THE SEAM — the fold is worth nothing unless /health carries it (test_app.py drives the endpoint)
# ==================================================================================================
def test_the_HEALTH_MODEL_declares_the_field():
    """An undeclared field is eaten by the DTO silently — the third time (#233/#322/#336)."""
    from api.models import HealthResponse, LanesBleedingDTO

    assert "lanes_bleeding" in HealthResponse.model_fields
    assert LanesBleedingDTO.model_validate({"status": "ok", "lanes": [], "error": None}).lanes == []
    # The unreadable shape round-trips with `lanes: None` — a reader must not get `[]` for it.
    assert LanesBleedingDTO.model_validate({"status": "unreadable", "lanes": None, "error": "x"}).lanes is None
    # `own_entries: None` (entries unknown) must survive the DTO — an `int` field would eat it as 422.
    from api.lanes_bleeding import LaneBleed
    lane = LaneBleed("MOMENTUM-002", 3, None, 2, "2026-09-16T13:46:31+00:00", "x").as_dict()
    assert LanesBleedingDTO.model_validate({"status": "ok", "lanes": [lane]}).lanes[0].own_entries is None


def test_the_field_is_NOT_EXEMPTED_from_the_generic_forwarding_guard():
    """Same guard-on-the-guard `split_divergence` carries: api-computed is not a reason to stop
    checking that /health passes it."""
    import api.test_health_forwards_every_engine_field as guard

    assert "lanes_bleeding" not in guard.NOT_FROM_THE_ENGINE
    assert "lanes_bleeding" in guard._engine_owned_fields()
    assert "lanes_bleeding" in guard.NOT_FROM_THE_BUS


def test_a_BLEEDING_LANE_DEGRADES_the_overall_status_and_an_unreadable_read_does_not():
    """Asserted at the source, like the split-divergence sibling: the status expression must consult
    the field, and must gate on `status == "ok"` so `unreadable` cannot move the banner."""
    import ast
    import inspect

    import api.app as app_mod

    tree = ast.parse(inspect.getsource(app_mod.health).lstrip())
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "HealthResponse")
    rendered = ast.unparse(next(k.value for k in call.keywords if k.arg == "status"))
    assert "bleeding" in rendered, rendered
    assert "'ok'" in rendered.split("bleeding", 1)[1], (
        f"the status consults lanes_bleeding without gating on a successful read: {rendered!r}")


# ==================================================================================================
# DELIVERY — the alert half, driven through the real Notifier with a fake transport
# ==================================================================================================
def _alert_svc():
    from api.alerts import AlertsService
    from api.notify import Notifier
    from api.test_alerts import FakeNode, FakeTx

    tx = FakeTx()
    svc = AlertsService(FakeNode(), notifier=Notifier(transport=tx, dedupe_backend="memory",
                                                       settings={"enabled": True}))
    return svc, tx


def test_the_alert_fires_once_per_lane_with_the_rendered_line_and_dedupes():
    from api.lanes_bleeding import LaneBleed

    svc, tx = _alert_svc()
    lane = LaneBleed("MOMENTUM-002", 3, 0, 2, "2026-09-16T13:46:31+00:00",
                     "pool sources stale or failed: ledger_book").as_dict()

    async def _impl():
        return {"status": "ok", "lanes": [lane], "error": None}

    svc._lanes_bleeding_impl = _impl
    asyncio.run(svc._announce_lanes_bleeding())
    asyncio.run(svc._announce_lanes_bleeding())
    assert len(tx.sent) == 1, [a.title for a in tx.sent]
    a = tx.sent[0]
    assert "MOMENTUM-002" in a.title and a.critical
    assert "3 exits, 0 entries, 2 sessions without a decision" in a.body
    assert "ledger_book" in a.body


def test_an_unreadable_journal_counts_as_a_BROKEN_check_and_sends_nothing():
    """`_check_failed` is what makes a silent check report ITSELF after BROKEN_CHECK_POLLS —
    an unreadable read must reach it, never look like a clean poll."""
    svc, tx = _alert_svc()

    async def _impl():
        return {"status": "unreadable", "lanes": None, "error": "RuntimeError: pg down"}

    svc._lanes_bleeding_impl = _impl
    asyncio.run(svc._announce_lanes_bleeding())
    assert tx.sent == []
    n, why = svc._check_failures.get("lanes_bleeding", (0, ""))
    assert n >= 1 and "pg down" in why, dict(svc._check_failures)


def test_the_alert_key_has_ONE_derivation():
    """`LaneBleed.alert_key` and an f-string beside it in alerts.py were two derivations of one
    dedupe key (review). The announce must call the method and hold no literal of its own."""
    import inspect

    from api.alerts import AlertsService
    from api.lanes_bleeding import LaneBleed

    src = inspect.getsource(AlertsService._announce_lanes_bleeding)
    assert "alert_key()" in src
    assert 'f"lanes_bleeding:' not in src and "'lanes_bleeding:" not in src
    assert LaneBleed("X-1", 1, None, 2, "", "").alert_key() == "lanes_bleeding:X-1"


def test_the_alert_is_WIRED_into_the_poll_inside_the_gate():
    import ast
    import inspect

    from api.alerts import AlertsService

    src = inspect.getsource(AlertsService.run)
    assert "_announce_lanes_bleeding" in src
    tree = ast.parse(inspect.getsource(AlertsService.run).lstrip() if not src.startswith(" ") else "class _:\n" + src)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "_announce_lanes_bleeding" in names
