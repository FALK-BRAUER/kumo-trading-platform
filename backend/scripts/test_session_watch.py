"""The alarm channel must not report a number it cannot support (#390).

WHAT WAS SEEN:

    [heartbeat] 08-20 11:22 ET | 10 held 14 stops

Fourteen protective stops against ten positions reads as duplicate stops on the same shares — the
oversell shape of #245 — and it is what prompted the investigation that found #387. The broker had
NINE, one per symbol, no duplicates.

WHY: the monitor counted protective orders out of `GET /orders`, the cockpit's own cache. The cache
holds orders the venue does not, and it holds an OUO take-profit leg beside each stop. So the count
inflated exactly when cache and broker disagreed — precisely when the number needs to be trustworthy.

This is the THIRD defect in the alarm channel in two days (#380 reported cash as equity; pre-market is
labelled SESSION). #380's framing applies unchanged: a wrong number in the alarm channel is worse than
no number, because it trains the operator to discount the channel.

MEASURED ON THE LIVE ACCOUNT, 2026-08-21, at one instant:

    cache-derived  (old):  17 stop orders across 11 symbols
    broker-derived (new):  14 protected cycles across 8 symbols, 2 unprotected (A, APA), 0 unknown

The 14-vs-8 gap is the second route into the same bug and is pinned below: several cycles can share one
symbol, and a stop is placed against the account's net position in an instrument, not against a cycle.
"""

from __future__ import annotations

from scripts.session_watch import Protection, collect, protection_from_trades


#: A LIVE engine frame, as `/health` carries it since #859: the consumer's own word that a frame is
#: fresh, the engine subsystem up, and a lanes count that is a number. Older tests fed
#: `{"subsystems": []}` — a payload no live engine ever produced — and the #859 gate correctly
#: refuses it, which is the double failing to represent production, not the gate being wrong.
_LIVE_HEALTH = {"status": "ok", "bridge_ok": True,
                "subsystems": [{"name": "engine", "ok": True, "detail": ""}],
                "automated_lanes_registered": 4, "automated_lanes_running": 4,
                "reconcile_drift": [], "protection_divergence": []}


def _cycle(sym, qty=10, protected=True, side="LONG", state="HELD", strategy_id="BCTROT-004"):
    """A trade-cycle row shaped like `TradeDTO`, which declares `side`, `state` and `strategy_id` as
    REQUIRED fields with no defaults.

    They were absent here until 2026-08-22, and their absence hid two things: the netting path's
    HELD-only guard was never exercised (no row ever carried a non-HELD state), and the short
    invariant raised on every row rather than reading it. A double that cannot represent production
    is the bug — the production reader was left strict and this was fixed instead."""
    return {
        "instrument_id": sym, "quantity": qty, "broker_protected": protected,
        "side": side, "state": state, "strategy_id": strategy_id,
        # The rest of TradeDTO's required fields. None of the readers under test touch them, and they
        # are here anyway: the point of a faithful double is that it keeps being faithful as the model
        # grows, so the next reader that DOES touch one finds a plausible value instead of a KeyError.
        "account_id": "PA-TEST", "client_id": "ALPACA-001", "cycle_id": f"{sym}-1",
        "is_capital_deployed": True, "is_engaged": True, "leg_count": 1,
        "opened_ts": 1_787_000_000_000_000_000, "last_event_ts": 1_787_000_600_000_000_000,
        "realized_pnl": 0.0,
    }


def test_the_fixture_carries_every_field_TradeDTO_declares_required():
    """Aimed at the CLASS, not this instance. Four defects have shipped green through doubles that had
    drifted from the model they stand for; this fails the moment TradeDTO gains a required field the
    fixture does not supply, rather than waiting for the next reader to trip over it."""
    from api.models import TradeDTO

    required = {n for n, f in TradeDTO.model_fields.items() if f.is_required()}
    supplied = set(_cycle("AEM.XNYS"))
    assert required - supplied == set(), f"fixture is missing required TradeDTO fields: {required - supplied}"


def test_the_fixture_reproduces_the_over_count():
    """The fixture's own property first.

    If every symbol had exactly one cycle, counting cycles and counting symbols would agree and the
    assertion below could not fail either way. The live account had 14 protected cycles across 8
    symbols; this fixture must carry the same shape or it tests nothing.
    """
    trades = [_cycle("AEM.XNYS"), _cycle("AEM.XNYS"), _cycle("WPM.XNYS")]
    assert len(trades) > len({t["instrument_id"] for t in trades}), "no symbol is duplicated — vacuous"


def test_protection_is_counted_BY_SYMBOL_not_by_cycle():
    """Three cycles, two symbols, one answer per symbol.

    Counting cycles is how "10 held 14 stops" happens even after the source is fixed: it reports more
    stops than exist, because a protective stop covers the account's net position in an instrument.
    """
    p = protection_from_trades([_cycle("AEM.XNYS"), _cycle("AEM.XNYS"), _cycle("WPM.XNYS")])

    assert p.protected == {"AEM.XNYS", "WPM.XNYS"}
    assert p.held == 2
    assert p.summary() == "2 held 2 protected"


def test_a_symbol_UNPROTECTED_in_any_cycle_is_unprotected():
    """The pessimistic read is the safe one for an alarm.

    A stop covering one cycle's shares does not cover another's. Reporting the symbol as covered
    because one of its cycles is would be the silencing direction, which is the worse one.
    """
    p = protection_from_trades([
        _cycle("APA.XNAS", protected=True),
        _cycle("APA.XNAS", protected=False),
    ])

    assert p.unprotected == {"APA.XNAS"}
    assert p.protected == set(), "a symbol must not appear on both sides"
    assert "UNPROTECTED 1/1: APA.XNAS" in p.alerts()[0]


def test_UNKNOWN_is_reported_and_is_neither_protected_nor_naked():
    """Three-state, because `broker_protected` is three-state.

    `None` means the broker has not been asked. Folding it into "unprotected" cries wolf on every
    startup before the first sweep; folding it into "protected" hides a naked position. Both have
    shipped in this codebase. The only honest answer is to say which one it is.
    """
    p = protection_from_trades([_cycle("BDX.XNYS", protected=None)])

    assert p.unknown == {"BDX.XNYS"}
    assert p.protected == set() and p.unprotected == set()
    assert p.held == 1
    assert any("PROTECTION UNKNOWN 1: BDX.XNYS" in a for a in p.alerts())
    assert "unknown" in p.summary(), "the status line must not read as 1 held 0 protected and nothing else"


def test_a_FLAT_cycle_has_nothing_to_protect():
    """The trades plane carries closed and flat cycles. Counting them inflates `held` and manufactures
    unprotected symbols that hold no shares — CRAK sat flat with an unfilled breakout entry on
    2026-08-21 and must not appear in a coverage alarm."""
    p = protection_from_trades([_cycle("CRAK.ARCX", qty=0, protected=False)])

    assert p.held == 0
    assert p.alerts() == []


def test_a_FAILED_trades_plane_does_NOT_read_as_a_flat_book():
    """#298, and the 2026-08-14 incident it came from: an empty tile stood in for eight held positions.

    A projection that failed returns an empty list. Counted naively that is "0 held", which is a
    confident claim that the book is flat — the most dangerous possible reading. The monitor must
    refuse to count and say why.
    """
    def fetch(path):
        return {
            "/health": dict(_LIVE_HEALTH),
            "/account": {"equity": 100.0, "last_equity": 100.0},
            "/trades": {"status": "failed", "error": "projection raised", "trades": []},
            "/orders": {"orders": []},
        }[path]

    line, alerts = collect(fetch=fetch)

    assert any("protection NOT counted" in a for a in alerts)
    assert any("status=failed" in a for a in alerts)
    assert "0 held 0 protected" in line, "the count is zero because it was refused, and the alert says so"


def test_an_UNREACHABLE_api_is_reported_as_such_and_not_as_zero_positions():
    def fetch(path):
        return {"__err__": "connection refused"}

    line, alerts = collect(fetch=fetch)

    assert line == "DEGRADED"
    assert alerts == ["API UNREACHABLE: connection refused"]


def test_the_monitor_NEVER_derives_protection_from_the_orders_cache():
    """THE REGRESSION GUARD, aimed at the class rather than the instance.

    The bug was not "this filter was wrong"; it was "protection was read from the cache at all". A
    future edit that adds a cache-derived stop count beside the broker-derived one reintroduces the
    disagreement #390 exists to remove. `/orders` may still be read for the `working` count, so the ban
    is on the STOP-classification vocabulary, not on the endpoint.
    """
    import inspect

    from scripts import session_watch

    src = inspect.getsource(session_watch)
    body = src.split('"""', 2)[-1]  # drop the module docstring, which discusses the old approach
    assert "broker_protected" in body
    assert '"STOP"' not in body, "protection is being classified from order type again"
    assert "prot=" not in body.replace("prot=Protection()", ""), "no second protection derivation"


def test_the_status_line_reports_working_orders_SEPARATELY_from_protection():
    """`working` is a cache fact and is fine as one — it answers "what does the engine think it has
    out there". It must simply never be presented as protection, which is what "14 stops" did."""
    def fetch(path):
        return {
            "/health": dict(_LIVE_HEALTH),
            "/account": {"equity": 103066.40, "last_equity": 103327.15},
            "/trades": {"status": "ok", "trades": [_cycle("AEM.XNYS"), _cycle("AEM.XNYS")]},
            "/orders": {"orders": [{"status": "ACCEPTED"}, {"status": "FILLED"}]},
        }[path]

    line, alerts = collect(fetch=fetch)

    assert "1 held 1 protected" in line, "two cycles, one symbol"
    assert "1 working" in line, "FILLED is not working"
    assert "stops" not in line, "the word that made a cache count read as broker protection"
    assert alerts == []


# ==================================================================================================
# A SYMBOL WHOSE CYCLES NET TO ZERO IS NOT HELD (2026-08-21, live paper book).
#
# The alarm channel carried "14 held 12 stops / UNPROTECTED: WHD.XNYS" for most of the session. The
# broker held ZERO WHD. The trades plane had two cycles on it:
#
#     WHD.XNYS  BCTROT-004    LONG   28
#     WHD.XNYS  MOMENTUM-002  SHORT  28
#
# Signed, that is flat, and `_report_reconcile_drift` agreed with the broker exactly. The monitor
# counted both legs as live because it tested `qty != 0` per cycle and never looked at `side`, so a
# genuinely flat symbol was reported naked for half an hour.
#
# This is the #390 shape arriving by a third route: a number that is wrong in the alarm channel trains
# the operator to discount the channel. There is also nothing to protect here — you cannot put a
# protective SELL stop under a position you do not have.
# ==================================================================================================
def _whd_cycle(strategy: str, side: str, qty: float, protected=False) -> dict:
    """Built from what `/trades` ACTUALLY emitted on 2026-08-21, field for field."""
    return {
        "instrument_id": "WHD.XNYS",
        "strategy_id": strategy,
        "side": side,
        "quantity": qty,
        "state": "HELD",
        "broker_protected": protected,
    }


def test_the_fixture_is_genuinely_flat_and_would_look_held_to_a_naive_count():
    # The fixture's own property first. If both legs were the same side, the netting assertion below
    # would pass for the wrong reason and prove nothing.
    rows = [_whd_cycle("BCTROT-004", "LONG", 28.0), _whd_cycle("MOMENTUM-002", "SHORT", 28.0)]
    assert {r["side"] for r in rows} == {"LONG", "SHORT"}
    assert all(r["quantity"] != 0 for r in rows), "every leg is non-zero — a per-cycle count sees two"


def test_a_long_and_short_that_NET_TO_FLAT_is_neither_held_nor_unprotected():
    prot = protection_from_trades([_whd_cycle("BCTROT-004", "LONG", 28.0),
                                   _whd_cycle("MOMENTUM-002", "SHORT", 28.0)])
    assert prot.held == 0, (
        f"reported {prot.held} held for a symbol that nets to zero; the broker had no WHD position"
    )
    assert not prot.unprotected, (
        f"reported {sorted(prot.unprotected)} unprotected — there is no exposure to protect, and this "
        "alert ran in the channel for half a session against a flat book"
    )


def test_a_genuine_net_long_is_STILL_reported_unprotected():
    """The netting must not become a silencer. Unequal legs leave real exposure, and the whole point of
    the monitor is that this still fires."""
    prot = protection_from_trades([_whd_cycle("BCTROT-004", "LONG", 40.0),
                                   _whd_cycle("MOMENTUM-002", "SHORT", 28.0)])
    assert prot.held == 1
    assert prot.unprotected == {"WHD.XNYS"}


def test_an_ordinary_unprotected_long_is_unaffected():
    prot = protection_from_trades([_whd_cycle("BCTROT-004", "LONG", 28.0)])
    assert prot.held == 1 and prot.unprotected == {"WHD.XNYS"}


def test_an_UNKNOWN_side_never_lets_a_symbol_be_netted_away():
    """FAIL CLOSED (codex review, 2026-08-21).

    Netting REMOVES a symbol from the alarm, so every input to it is a potential silencer. If a row's
    `side` cannot be read, the symbol's net is not trustworthy and must not be used to silence it.

    THE FIRST VERSION OF THIS TEST COULD NOT FAIL. It used one LONG plus one unreadable row, and with
    the guard disabled the unreadable row defaulted to LONG — 28 + 28 = 56, still non-zero, still
    alarming. Both branches alarmed, so it asserted nothing. The fixture has to make the silencing
    REACHABLE: here the legs sum to exactly zero if the unreadable row is counted as a long, which is
    precisely the state that deletes the symbol from the alarm.
    """
    rows = [
        _whd_cycle("BCTROT-004", "LONG", 28.0),
        _whd_cycle("MOMENTUM-002", "SHORT", 56.0),
        {**_whd_cycle("MYSTERY-000", "LONG", 28.0), "side": None},
    ]
    # The fixture's own property first: counted as a long, these net to EXACTLY zero — the silencing
    # condition really is reachable, so the assertion below can fail.
    naive = 28.0 - 56.0 + 28.0
    assert naive == 0.0
    assert rows[2]["side"] is None

    prot = protection_from_trades(rows)
    assert prot.unprotected == {"WHD.XNYS"}, (
        "a row with an unreadable side was counted into the net, cancelled the real legs to zero and "
        "silenced the alarm for the symbol"
    )


def test_a_nonzero_FLAT_or_CLOSED_row_cannot_cancel_a_live_leg():
    """Codex flagged this as the remaining silencing path: netting reads every row, so if `/trades` ever
    emitted a nonzero FLAT or CLOSED cycle it would cancel a live one. TradeDTO says FLAT means
    quantity == 0 today, so this is a guard against a future change, not a live defect."""
    rows = [_whd_cycle("BCTROT-004", "LONG", 28.0),
            {**_whd_cycle("STALE-999", "SHORT", 28.0), "state": "CLOSED"}]
    prot = protection_from_trades(rows)
    assert prot.unprotected == {"WHD.XNYS"}, (
        "a CLOSED cycle netted against a live HELD one and removed it from the alarm"
    )


# ==================================================================================================
# THE SHORT INVARIANT REACHES THE ALARM (#437 rule 3). `short_violations` living in api/ and passing
# its own tests says nothing about whether anything calls it — that shape broke production five times
# on 2026-08-14 with a green suite. This drives `collect`, the real entry point.
# ==================================================================================================
_WHD_TRADES = {
    "status": "ok",
    "trades": [
        {"instrument_id": "WHD.XNYS", "side": "LONG", "quantity": 28.0, "state": "HELD",
         "strategy_id": "BCTROT-004", "broker_protected": True},
        {"instrument_id": "WHD.XNYS", "side": "SHORT", "quantity": 28.0, "state": "HELD",
         "strategy_id": "MOMENTUM-002", "broker_protected": True},
    ],
}
_OK_HEALTH = dict(_LIVE_HEALTH)     # a LIVE frame, see _LIVE_HEALTH (#859)
_OK_ACCOUNT = {"equity": 103466.29, "last_equity": 103466.29}


def _fetch(trades):
    def go(path):
        return {"/health": _OK_HEALTH, "/account": _OK_ACCOUNT,
                "/trades": trades, "/orders": {"orders": []}}[path]
    return go


def test_a_strategy_holding_a_SHORT_is_alarmed_by_collect():
    """The live 2026-08-21 book. Protection nets to flat and is correctly silent — so WITHOUT this
    rule the whole thing reports `ok`, which is exactly what it did for twelve hours."""
    _line, alerts = collect(fetch=_fetch(_WHD_TRADES))
    assert any("MOMENTUM-002" in a and "WHD" in a for a in alerts), alerts


def test_protection_alone_would_have_reported_this_book_CLEAN():
    """The discriminating half, and the reason the rule is not redundant. Netting removes WHD from the
    protection alarm because the account genuinely holds zero — correct, and blind. If this ever
    starts failing, protection has begun double-reporting and the short rule can be reconsidered."""
    prot = protection_from_trades(_WHD_TRADES["trades"])
    assert prot.alerts() == []
    assert prot.held == 0


def test_a_long_only_book_raises_no_short_alarm():
    clean = {"status": "ok", "trades": [dict(_WHD_TRADES["trades"][0])]}
    _line, alerts = collect(fetch=_fetch(clean))
    assert not [a for a in alerts if "SHORT" in a.upper()], alerts


# ==================================================================================================
# A JSON `null` BODY IS NOT AN ERROR, AND IT IS NOT A DICT (observed live 2026-08-22 17:21 ET).
#
# During the post-deploy boot `/account` answered 200 with the body `null` — the engine had no account
# snapshot yet. `_get` returned None, and `"__err__" in account` raised TypeError, so the ALARM ITSELF
# died at the exact moment it was needed: mid-restart, with three of four strategies not running.
#
# The monitor reported `session_watch raised: ...` rather than a health line, which is the right
# direction — loud, not silently green — but it meant no protection, short or claims check ran for the
# whole window.
# ==================================================================================================
def test_a_null_body_is_normalised_to_an_ERROR_not_left_as_None():
    """`json.load` on the body `null` returns None, which is not a dict and not an error dict. Every
    reader in `collect` does `"__err__" in x`, so None raises rather than reporting."""
    from scripts.session_watch import _normalise

    out = _normalise(None)
    assert isinstance(out, dict) and "__err__" in out
    assert "null" in out["__err__"].lower() or "None" in out["__err__"]


def test_a_LIST_body_is_normalised_too():
    """`/trades` and `/orders` can answer with a bare list. Same failure one endpoint over."""
    from scripts.session_watch import _normalise

    assert "__err__" in _normalise([])


def test_a_real_dict_passes_through_UNCHANGED():
    """The discriminating half — a normaliser that wrapped everything would make every endpoint look
    unreachable and the alarm would report a total outage on a healthy stack."""
    from scripts.session_watch import _normalise

    body = {"equity": 1.0, "last_equity": 1.0}
    assert _normalise(body) is body


def test_collect_SURVIVES_a_null_account_and_still_reports_the_rest():
    """The live case, end to end. A missing account must not take protection and claims reporting down
    with it — those are the checks that matter during a restart."""
    def fetch(path):
        return {"/health": dict(_LIVE_HEALTH), "/account": None,
                "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}[path]

    line, alerts = collect(fetch=fetch)
    assert isinstance(line, str)
    assert any("account" in a.lower() for a in alerts), alerts


# -- an engine that has told us nothing (#859) ----------------------------------------------------
def _no_frame_health() -> dict:
    """What `/health` says on the #859 branch when no engine frame has arrived or the bridge is
    stale: `status` degraded, `bridge_ok` False, every list-valued field None (UNKNOWN, not empty),
    lanes count None. Measured 2026-09-11 00:34:45–00:35:14 SGT across a paper recreate: three such
    windows, each rendered as a clean bill of health by every reader that keyed on the lists."""
    return {
        "status": "degraded", "bridge_ok": False,
        "subsystems": [{"name": "engine", "ok": False, "detail": "no fresh engine health frames on the bus"},
                       {"name": "lanes", "ok": None, "detail": ""},
                       {"name": "redis", "ok": True, "detail": ""}, {"name": "postgres", "ok": True, "detail": ""}],
        "reconcile_drift": None, "ownership_violations": None, "protection_divergence": None,
        "naked_after_reject": None, "unpriced_positions": None, "failed_requests": None,
        "automated_lanes_registered": None, "automated_lanes_running": None, "feed_last_tick_ts": None,
    }


def test_the_no_frame_fixture_is_UNKNOWN_everywhere_not_empty():
    """Fixture property first: every list the monitor reads is None, and status is not ok. If any of
    them were [], the test below could pass for the wrong reason."""
    h = _no_frame_health()
    assert h["status"] != "ok" and h["bridge_ok"] is False
    for k in ("reconcile_drift", "protection_divergence", "naked_after_reject", "failed_requests"):
        assert h[k] is None, k


def test_an_engine_that_has_told_us_NOTHING_is_refused_before_any_list_is_read():
    """The monitor keyed on `if health.get("reconcile_drift"):` — a None reads exactly like an empty
    list, so an absent engine produced no drift alert, a protection tally, an equity line and "ok".
    It must refuse the way it refuses an unreachable API: a DEGRADED verdict naming that the book is
    UNKNOWN, before any list is consulted, and no figure derived from that payload."""
    fake = {"/health": _no_frame_health(),
            "/account": {"equity": 101_339.07, "last_equity": 102_656.85},
            "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}
    line, alerts = collect(fetch=lambda path: fake[path])
    assert line == "DEGRADED", f"a payload with no engine frame produced a status line: {line!r}"
    assert alerts and any("unknown" in a.lower() and "engine" in a.lower() for a in alerts), alerts
    assert not any("RECONCILE DRIFT" in a for a in alerts), "a None drift list must not be read as anything"
    assert not any("eq $" in a for a in alerts) and "eq $" not in line, "no equity/protection figure may be derived from an absent frame"


def test_a_payload_WITHOUT_the_lanes_key_is_refused_and_says_ABSENT_not_null():
    """Absent is not null (#859): a live api always carries `automated_lanes_running`; a payload
    without it is an older or truncated api. Refused the same way — no frame either way — but the
    refusal names which, because "the sender never had the field" and "the sender said unknown"
    are different facts. Also pins the corollary a reader will ask about: during a bridge flap this
    gate suppresses the divergence alarm, so a QUIET boot window is guaranteed, not informative."""
    h = _no_frame_health(); del h["automated_lanes_running"]; h["status"] = "ok"; h["bridge_ok"] = True
    h["subsystems"] = [{"name": "engine", "ok": True, "detail": ""}]
    h["protection_divergence"] = [{"instrument_id": "AAPL.XNAS", "coids": ["c1"]}]
    fake = {"/health": h, "/account": {"equity": 1.0, "last_equity": 1.0}, "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}
    line, alerts = collect(fetch=lambda p: fake[p])
    assert line == "DEGRADED" and "ABSENT" in alerts[0], (line, alerts)
    assert not any("PROTECTION DIVERGENCE" in a for a in alerts), "no list is read once the frame is judged absent"


def test_a_STALE_bridge_with_status_ok_is_still_refused():
    """`bridge_ok False` is the consumer's own word that the frame it holds is old. Status alone is
    not the gate: a reader must refuse on either."""
    h = _no_frame_health(); h["status"] = "ok"
    fake = {"/health": h, "/account": {"equity": 1.0, "last_equity": 1.0}, "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}
    line, alerts = collect(fetch=lambda path: fake[path])
    assert line == "DEGRADED" and any("unknown" in a.lower() for a in alerts), (line, alerts)


def test_a_DEGRADED_but_FRAMED_engine_still_has_every_check_RUN_against_it():
    """The other direction of the gate, and the one the no-frame fixture cannot discriminate: a LIVE
    engine whose status is "degraded" for a benign reason (unpriced positions, a refused protection
    request) must still get drift, protection and lanes checked. A gate on `status != "ok"` alone
    would pass every no-frame test above and silently blind the monitor at exactly the moment
    protection legitimately refuses (review of #883, mutant B)."""
    h = {"status": "degraded", "bridge_ok": True,
         "subsystems": [{"name": "engine", "ok": True, "detail": ""}],
         "automated_lanes_registered": 4, "automated_lanes_running": 3,
         "unpriced_positions": ["FOO.XNAS"],
         "reconcile_drift": [{"instrument_id": "AAPL.XNAS", "engine": 10, "broker": 0}],
         "protection_divergence": [{"instrument_id": "AAPL.XNAS", "coids": ["c1", "c2"]}]}
    assert h["status"] != "ok" and h["bridge_ok"] is True and h["automated_lanes_running"] is not None
    fake = {"/health": h, "/account": {"equity": 100.0, "last_equity": 99.0},
            "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}
    line, alerts = collect(fetch=lambda p: fake[p])
    assert line != "DEGRADED", (line, alerts)
    assert any("RECONCILE DRIFT" in a for a in alerts), alerts
    assert any("PROTECTION DIVERGENCE" in a for a in alerts), alerts
    assert any("AUTOMATED LANES NOT RUNNING 3/4" in a for a in alerts), alerts


def test_the_refusal_names_EVERY_down_subsystem_not_only_the_engine():
    """The old per-subsystem loop sat after the gate and became unreachable when the engine is down,
    so redis "timeout" and postgres "connection refused" vanished from the one line the operator
    reads (review of #883). The refusal carries them."""
    h = _no_frame_health()
    h["subsystems"] = [{"name": "engine", "ok": False, "detail": "no fresh engine health frames on the bus"},
                       {"name": "redis", "ok": False, "detail": "timeout"},
                       {"name": "postgres", "ok": False, "detail": "connection refused"},
                       {"name": "lanes", "ok": None, "detail": ""}]
    fake = {"/health": h, "/account": {"equity": 1.0, "last_equity": 1.0}, "/trades": {"status": "ok", "trades": []}, "/orders": {"orders": []}}
    line, alerts = collect(fetch=lambda p: fake[p])
    assert line == "DEGRADED"
    joined = " | ".join(alerts)
    assert "redis" in joined and "timeout" in joined and "postgres" in joined and "connection refused" in joined, alerts
