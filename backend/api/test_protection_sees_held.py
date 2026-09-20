"""The protection check must be able to SEE a stop the broker is holding — and must not see anything else (#387).

WHY THESE TESTS EXECUTE THE RECONCILER instead of scanning its source. The first version of this file
was six source-text assertions, and review found the hole by mutation: reducing the status set to
`{"held"}` — which reports every ordinarily-stopped position on the book as NAKED — passed this file
AND all 43 tests in `test_protection_reconciler.py`. The tests pinned that terminal states were absent
and `held` present, and nothing pinned that a `new` stop still counted. The cry-wolf direction, which is
half the defect, had no coverage at all.

Three further defects were invisible to source scanning because they are runtime facts about how the
WIDENED fetch flows into consumers that were written for the narrow one:
  * the #269 divergence detector had no status filter and reported every filled or cancelled stop in
    the account's history as "resting and invisible", ERROR, every 60 seconds, including for symbols
    with no position;
  * `_broker_stop_prices` had no status filter, so a superseded `replaced` row could win the client-order-id
    key and SECURED would display a pre-shrink trigger;
  * `_RESTING` and `protection._OPEN_STATUSES` were two derivations of one predicate and disagreed in
    both directions.

The root cause of the retreat to AST was the double: `_Http.list_orders` ignored `status` and returned
the same rows either way, so it could not represent the single venue behaviour at issue. That is fixed
in `test_protection_reconciler.py` and these tests drive the real `_reconcile_protection_inner` through
it. CLAUDE.md: test the seam, not the unit; a double that cannot represent production is the bug.

WHAT IS MEASURED AND WHAT IS NOT. `scripts/probe_held_orders.py` read the paper account on 2026-08-20:
269 orders, 9 returned by the `open` filter, and two resting protective stops invisible to it — CRAK
180 @ 57.06 and APA 231 @ 43.99, both `held`. That the `open` filter hides `held` is measured. Whether
a `held` stop FIRES is NOT, and no test here claims it does; the fix only lets the check tell "no stop"
from "a stop the broker is holding", which today it reports as the first.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from api.engine_node import UiFeedStrategy
from api.protection import _OPEN_STATUSES, is_resting
from api.test_protection_reconciler import _Fake, _Http, _ns, _position, _run, _settings


def _stop(symbol="APA", status="held", coid=None, stop_price=43.99, otype="stop", side="sell"):
    """A protective sell as ALPACA emits it — lowercase status, `client_order_id`, string prices."""
    return {
        "symbol": symbol,
        "side": side,
        "type": otype,
        "status": status,
        "client_order_id": coid or f"PROT-{symbol}-{status}",
        "stop_price": stop_price,
        "submitted_at": "2026-08-20T13:30:00Z",
        "id": f"venue-{symbol}-{status}",
    }


def _reconciled(monkeypatch, *, orders, positions=None, ts_ns=None):
    """Run the REAL reconciler over one broker payload and hand back the double it wrote to."""
    _settings(monkeypatch, enabled=True)
    http = _Http(positions=positions if positions is not None else [_position(symbol="APA", qty="231")],
                 orders=orders)
    fake = _Fake(ts_ns=ts_ns if ts_ns is not None else _ns(10, 0), http=http)
    _run(fake)
    return fake, http


# --------------------------------------------------------------------------------------------------
# The fixture's own property first. A payload that cannot express the bug makes everything below
# vacuous — the kumo-trading-strategies truncation-invariance test passed twice with the look-ahead
# deliberately reintroduced, because its fixture could not violate the invariant either way.
# --------------------------------------------------------------------------------------------------

def test_the_fixture_can_actually_express_the_bug(monkeypatch):
    """The `open` filter must HIDE the held stop, or nothing below discriminates."""
    orders = [_stop(status="held")]
    _settings(monkeypatch, enabled=True)
    http = _Http(positions=[_position(symbol="APA", qty="231")], orders=orders)

    import asyncio

    assert asyncio.run(http.list_orders(status="open")) == [], (
        "the double's `open` filter must not return `held` — that is the measured venue behaviour "
        "(probe_held_orders.py, 2026-08-20) and the whole of #387"
    )
    assert len(asyncio.run(http.list_orders(status="all"))) == 1


# --------------------------------------------------------------------------------------------------
# The defect, both directions.
# --------------------------------------------------------------------------------------------------

def test_a_HELD_stop_at_the_broker_counts_as_protection(monkeypatch):
    """#387 itself. APA held 231 shares behind a `held` stop and the check called the position naked."""
    fake, http = _reconciled(monkeypatch, orders=[_stop(status="held")])

    # KEYED BY (instrument, REDUCING SIDE) since the short-side fix: a SELL protects a LONG and
    # does not protect a SHORT, which an instrument-level set could not say. The property this
    # test pins is unchanged — the stop COUNTS — only its shape moved.
    assert fake._broker_protected == {("APA.XNYS", "SELL")}
    assert ("all", True) in http.order_queries, "the broker must be read with a filter that returns `held`"


def test_an_ORDINARY_resting_stop_STILL_counts_as_protection(monkeypatch):
    """THE MUTATION THAT ESCAPED THE FIRST SUITE.

    Narrowing the predicate to `{"held"}` reports every normally-stopped position on the book as naked
    and passed 43 tests. This is the assertion that bites it: the common case must survive the fix for
    the rare one. `new` is what an ordinary GTC protective stop rests as.
    """
    fake, _ = _reconciled(monkeypatch, orders=[_stop(status="new")])

    # KEYED BY (instrument, REDUCING SIDE) since the short-side fix: a SELL protects a LONG and
    # does not protect a SHORT, which an instrument-level set could not say. The property this
    # test pins is unchanged — the stop COUNTS — only its shape moved.
    assert fake._broker_protected == {("APA.XNYS", "SELL")}


def test_a_TERMINAL_stop_is_never_counted_as_protection(monkeypatch):
    """The dangerous direction, and the near-miss that widening the fetch created.

    This account carried 86 CANCELLED protective sells on 2026-08-20. Counting them would render every
    position covered — a safety claim that is wrong, which is strictly worse than the alarm being fixed.
    """
    terminal = ["filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "calculated"]
    for status in terminal:
        fake, _ = _reconciled(monkeypatch, orders=[_stop(status=status)])
        assert fake._broker_protected == set(), f"{status!r} protects nothing and must not appear"


def test_done_for_day_and_calculated_are_POST_completion_states_not_protection(monkeypatch):
    """Both reviewers flagged these independently, and they were in the first fix's set.

    Alpaca: `calculated` is settlement bookkeeping on an order already completed for the day;
    `done_for_day` will not execute again until the next session. A position whose only stop is either
    one is exposed for the rest of the session while the checker reports it secured — the silencing
    direction. Pinned by name because they were shipped once and would be re-added by the same reasoning.
    """
    for status in ("done_for_day", "calculated"):
        assert not is_resting(status), f"{status!r} cannot fire today and is not protection"


def test_pending_cancel_IS_still_protection(monkeypatch):
    """The other half of the disagreement: `_OPEN_STATUSES` had it, the hand-written set did not.

    Alpaca frees the shares on a CONFIRMED cancel and a cancel can be rejected, so the order is live
    until then. With the two predicates disagreeing, the SECURED badge read NAKED while the order placer
    treated the position as covered — so the alarm fired and nothing was ever done about it.
    """
    fake, _ = _reconciled(monkeypatch, orders=[_stop(status="pending_cancel")])

    # KEYED BY (instrument, REDUCING SIDE) since the short-side fix: a SELL protects a LONG and
    # does not protect a SHORT, which an instrument-level set could not say. The property this
    # test pins is unchanged — the stop COUNTS — only its shape moved.
    assert fake._broker_protected == {("APA.XNYS", "SELL")}


# --------------------------------------------------------------------------------------------------
# The consumers that the widened fetch broke. None of these were reachable by scanning source.
# --------------------------------------------------------------------------------------------------

def test_the_divergence_detector_ignores_the_order_HISTORY(monkeypatch):
    """#269's detector had no status filter and became a klaxon the moment the fetch widened.

    Payload: one live held stop on a position we hold, plus three terminal stops — two of them on CRAK,
    which has NO POSITION AT ALL. Before the fix this produced two divergence entries and an ERROR for
    CRAK every 60 seconds. On the real account that is one row per symbol ever traded, and the genuine
    REJECTED-in-cache/open-at-venue signal the detector exists for becomes one row in ninety.
    """
    orders = [
        _stop(symbol="APA", status="held", coid="PROT-SELL-APA-live"),
        _stop(symbol="APA", status="canceled", coid="PROT-SELL-APA-old"),
        _stop(symbol="CRAK", status="filled", coid="PROT-SELL-CRAK-done"),
        _stop(symbol="CRAK", status="expired", coid="PROT-SELL-CRAK-exp"),
    ]
    fake, _ = _reconciled(monkeypatch, orders=orders)

    reported = {d["instrument_id"] for d in fake._protection_divergence}
    assert "CRAK.XNYS" not in reported, "a filled and an expired stop on a symbol we do not hold is not a divergence"
    assert all("CRAK" not in e for e in fake.log.errors), "and it must not log ERROR either"
    # The live one IS a genuine divergence — the cache holds no such order — so the detector must still fire.
    assert reported == {"APA.XNYS"}
    apa = next(d for d in fake._protection_divergence if d["instrument_id"] == "APA.XNYS")
    assert apa["coids"] == ["PROT-SELL-APA-live"], "only the resting order, not the cancelled one beside it"


def test_broker_stop_prices_carries_only_LIVE_triggers(monkeypatch):
    """SECURED reads its trigger from this map, and it had no status filter.

    Stale keys are mostly inert because lookups are by client order id — except on collision. An Alpaca
    replace leaves the original as `replaced` and the client order id is what survives the handoff
    (`exec_client.py:817`), so two rows share one key, the response is newest-first, and the dict
    comprehension lets the OLDER `replaced` row win. SECURED then shows the pre-shrink trigger on every
    stop the oversize path has ever modified.
    """
    orders = [
        _stop(symbol="APA", status="held", coid="PROT-APA", stop_price=43.99),
        _stop(symbol="APA", status="replaced", coid="PROT-APA", stop_price=41.00),
        _stop(symbol="CRAK", status="canceled", coid="PROT-CRAK", stop_price=50.00),
    ]
    fake, _ = _reconciled(monkeypatch, orders=orders)

    assert fake._broker_stop_prices == {"PROT-APA": 43.99}, (
        "the superseded `replaced` row must not win the key, and a cancelled stop's trigger must not "
        "be in the map at all"
    )


# --------------------------------------------------------------------------------------------------
# One predicate, not two.
# --------------------------------------------------------------------------------------------------

def test_the_badge_and_the_placer_use_the_SAME_predicate():
    """CLAUDE.md: when a check exists in two places, pin that they use the same one.

    `_broker_protected` (the SECURED badge) and `plan_protection` (which decides whether to PLACE) both
    answer "is this order live at the venue". They were two sets and disagreed in both directions —
    `pending_cancel` in one, `done_for_day`/`calculated` in the other — which produced a badge that said
    NAKED while nothing acted, and a badge that said SECURED while a duplicate stop went on. This
    asserts the reconciler no longer defines its own.
    """
    src = inspect.getsource(UiFeedStrategy._reconcile_protection_inner)
    assert "_RESTING" not in src, "the second definition is back; import `is_resting` instead"
    assert src.count("_is_resting(") >= 3, (
        "all three consumers — _broker_protected, _broker_stop_prices and the divergence detector — "
        "must use it; the first fix filtered only the first and the other two shipped broken"
    )


def test_the_predicate_normalises_BOTH_vocabularies():
    """Alpaca returns lowercase; our own engine frames emit Nautilus's uppercase `status.name`.

    A raw lowercase comparison against `_OPEN_STATUSES` matches nothing, which reports the entire book
    naked. This is a live hazard, not a hypothetical: the set is uppercase and the payload is not.
    """
    assert is_resting("held") and is_resting("HELD")
    assert is_resting("pending_replace") and is_resting("PENDING_REPLACE")
    assert not is_resting("") and not is_resting(None)
    assert "PENDING_REPLACE" in _OPEN_STATUSES, (
        "a replace leaves the original live and reserving shares until the new order is accepted — "
        "omitting it undercounts the reservation AND lets a duplicate stop go on"
    )


# --------------------------------------------------------------------------------------------------
# The reads that must not be able to truncate.
# --------------------------------------------------------------------------------------------------

def test_both_broker_order_reads_are_paginated_and_unnarrowed():
    """`status="all"` is one 500-row page, newest-first, and the account was at 269 and climbing.

    Past 500 the OLDEST rows silently vanish — and a long-lived GTC protective stop is the oldest thing
    in the list. Because this one read is the coverage oracle for BOTH `_broker_protected` AND
    `plan_protection`, a dropped stop yields `protective_quantity=0` and `reserved_quantity=0` together
    and the reconciler places a SECOND stop on top of a resting one. Under `status="open"` that could
    not happen (9 rows); widening the fetch is what made pagination load-bearing.
    """
    for method in (UiFeedStrategy._reconcile_protection_inner, UiFeedStrategy._venue_reducing_orders):
        src = inspect.getsource(method)
        assert 'list_orders(status="open")' not in src, f"{method.__name__} cannot see `held` orders"
        assert "paginate=True" in src, f"{method.__name__} may silently read a truncated order list"


def test_the_probe_exists_and_says_what_it_cannot_answer():
    """This repo measures venue semantics rather than reasoning about them (CLAUDE.md).

    The probe is what turned "the open filter probably hides held orders" into a number. It must keep
    stating the limit of what it measured — Alpaca's `/v2/orders` returns CURRENT status only, with no
    state history, so no read of it can show that a given order was ever `held`.
    """
    probe = Path(__file__).resolve().parents[1] / "scripts" / "probe_held_orders.py"
    text = probe.read_text()
    assert "read-only" in text.lower()
    assert "current status" in text.lower(), "the probe must name the endpoint's limitation, not imply it"


def test_a_BUY_stop_protecting_a_SHORT_is_COUNTED(monkeypatch):
    """The false NEGATIVE, at the producer — and tonight's live path.

    The set was built from SELL orders alone, so a BUY stop correctly protecting a short was never
    counted at all and the position reported NAKED. Staging's `RDN.XNYS` is SHORT 72 with protection
    armed today; the first correct BUY stop placed there would have shown as unprotected.

    The consumer-side tests inject a protected set directly, so they cannot see this — a mutation
    restoring `side == "sell"` here survived all of them. This drives the PRODUCER.
    """
    fake, _ = _reconciled(monkeypatch, orders=[_stop(status="new", side="buy")])

    assert fake._broker_protected == {("APA.XNYS", "BUY")}, (
        "a BUY protective stop was not counted — a short's protection reduces by BUYING, and "
        "leaving it out reports every correctly protected short as naked"
    )


def test_a_SELL_and_a_BUY_stop_on_ONE_instrument_are_kept_APART(monkeypatch):
    """A mirror pair carries both: a SELL protecting the real long and a BUY protecting the phantom
    short. The set must hold them separately, or one leg borrows the other's protection — which is
    the false positive this whole change exists to remove."""
    fake, _ = _reconciled(monkeypatch,
                          orders=[_stop(status="new"), _stop(status="new", side="buy")])
    assert fake._broker_protected == {("APA.XNYS", "SELL"), ("APA.XNYS", "BUY")}
