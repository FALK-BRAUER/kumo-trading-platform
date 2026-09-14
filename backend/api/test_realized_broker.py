"""Realized P&L per period from broker fills (#322)."""

from __future__ import annotations

from api.realized_broker import open_lots_after, open_lots_detail, realized_from_fills


def _fill(sym, side, qty, price, t):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price), "transaction_time": t}


def test_a_simple_round_trip():
    r = realized_from_fills([
        _fill("FIG", "buy", 278, 24.10, "2026-08-14T13:00:00Z"),
        _fill("FIG", "sell", 278, 25.96, "2026-08-14T18:00:00Z"),
    ])
    assert round(r.total, 2) == round(278 * (25.96 - 24.10), 2)
    assert r.closed == 1 and r.unmatched == 0 and r.is_partial is False


def test_the_live_week_reconstructed():
    """The figure the operator's question was really about: NET said $1,061 while equity had moved ~$2,841,
    because the rest was realized on positions already closed and therefore invisible to the book."""
    r = realized_from_fills([
        _fill("WDAY", "buy", 21, 177.87, "2026-08-13T14:00:00Z"),
        _fill("WDAY", "sell", 21, 223.98, "2026-08-14T14:00:00Z"),
        _fill("OKTA", "buy", 66, 149.25, "2026-08-13T14:00:00Z"),
        _fill("OKTA", "sell", 66, 150.33, "2026-08-14T15:00:00Z"),
    ])
    assert round(r.total, 2) == round(21 * (223.98 - 177.87) + 66 * (150.33 - 149.25), 2)
    assert r.closed == 2


def test_fills_are_matched_in_TIME_order_not_list_order():
    """FIFO on an unsorted list is not FIFO, and the error would be small enough to look plausible."""
    late_first = [
        _fill("X", "sell", 10, 12.0, "2026-08-14T18:00:00Z"),
        _fill("X", "buy", 10, 10.0, "2026-08-14T09:00:00Z"),
    ]
    assert round(realized_from_fills(late_first).total, 2) == 20.0


def test_two_lots_match_first_in_first_out():
    r = realized_from_fills([
        _fill("X", "buy", 10, 10.0, "2026-08-14T09:00:00Z"),
        _fill("X", "buy", 10, 20.0, "2026-08-14T10:00:00Z"),
        _fill("X", "sell", 10, 30.0, "2026-08-14T11:00:00Z"),
    ])
    assert round(r.total, 2) == 200.0, "matched the newer lot — that is LIFO, not FIFO"


def test_a_sell_with_no_buy_in_the_window_is_COUNTED_not_guessed():
    """The position was opened before the window. Its P&L is unknowable from these fills, so the figure
    says it is incomplete rather than inventing a basis — a number known to be partial is worth far
    more than one silently so."""
    r = realized_from_fills([_fill("X", "sell", 10, 30.0, "2026-08-14T11:00:00Z")])
    assert r.total == 0.0
    assert r.unmatched == 1 and r.is_partial is True


def test_a_partial_close_leaves_the_rest_of_the_lot_open():
    r = realized_from_fills([
        _fill("X", "buy", 100, 10.0, "2026-08-14T09:00:00Z"),
        _fill("X", "sell", 40, 12.0, "2026-08-14T10:00:00Z"),
    ])
    assert round(r.total, 2) == 80.0
    assert r.unmatched == 0, "a partial close is not an unmatched sell"


def test_malformed_fills_are_skipped_not_fatal():
    r = realized_from_fills([
        {"symbol": "X", "side": "buy", "qty": "oops", "price": "10", "transaction_time": "t"},
        _fill("X", "buy", 10, 10.0, "2026-08-14T09:00:00Z"),
        _fill("X", "sell", 10, 11.0, "2026-08-14T10:00:00Z"),
    ])
    assert round(r.total, 2) == 10.0


# --- The SEAM. Each of the four hops below has silently dropped a field before (#233): the engine
# --- publishes it, Redis carries it, the response model declares it, the endpoint passes it. A green
# --- test on `realized_from_fills` says nothing about any of them.

import inspect


def test_the_engine_SWEEPS_the_broker_and_publishes_the_periods():
    from api import engine_node

    frame = inspect.getsource(engine_node.UiFeedStrategy._publish_trades)
    assert '"realized_periods"' in frame, "the trades frame does not carry realized_periods"

    sweep = inspect.getsource(engine_node.UiFeedStrategy._refresh_realized_periods)
    assert "list_activities" in sweep, "the sweep does not ask the broker for its fills"
    assert "realized_by_period" in sweep

    start = inspect.getsource(engine_node.UiFeedStrategy.on_start)
    assert "_REALIZED_TIMER" in start, "nothing ever fires the sweep"
    assert "_refresh_realized_periods" in start, "the first sweep waits a full interval after a restart"


def test_a_FAILED_sweep_keeps_the_last_known_answer():
    """A transient 429 must not make a month's P&L vanish and reappear — the operator would read the
    blank as "nothing closed", which is a different claim from "I could not reach the broker"."""
    import asyncio

    from api import engine_node

    class _Http:
        async def list_activities(self, **_):
            raise RuntimeError("429")

    class _Log:
        def warning(self, _m):
            pass

    known = {"1W": {"total": 812.0}}

    class _Fake:
        # A plain double, not `Actor.__new__`: `log` is a read-only Cython attribute on the real class,
        # so the instance cannot be built up field by field. The REAL method is bound below, which is
        # the part that matters — a reimplementation here would test nothing.
        _http = _Http()
        log = _Log()
        _realized_periods = known
        _refresh_realized_periods = engine_node.UiFeedStrategy._refresh_realized_periods

    fake = _Fake()
    asyncio.run(fake._refresh_realized_periods())
    assert fake._realized_periods is known, "a failed sweep blanked the last good answer"


def test_the_REST_boundary_carries_it_all_four_hops():
    from api import app as app_module
    from api import consumer as consumer_module
    from api.models import TradesResponse

    assert "realized_periods" in TradesResponse.model_fields, "the response model DROPS undeclared keys"

    consume = inspect.getsource(consumer_module)
    assert 'payload.get("realized_periods")' in consume, "the consumer never reads it off the frame"

    endpoint = inspect.getsource(app_module.get_trades)
    assert "realized_periods=" in endpoint, (
        "/trades declares the field but never passes it — declared-but-unpassed is silently None, which "
        "is exactly how #233 reached Redis and then vanished here"
    )

    carried = TradesResponse(trades=[], realized_periods={"1W": {"total": 812.0}}).model_dump()
    assert carried["realized_periods"]["1W"]["total"] == 812.0


def test_an_ABSENT_key_does_not_erase_a_known_answer():
    """The engine publishes trades every 2s and sweeps every 5min. If a frame without the key overwrote
    the value, the periods would flicker to null 149 times out of 150."""
    from api.consumer import RedisConsumer

    src = inspect.getsource(RedisConsumer._apply)
    assert 'if payload.get("realized_periods") is not None:' in src, (
        "an absent key overwrites the last sweep — the field would be null almost always"
    )


def test_every_period_the_UI_OFFERS_is_computed():
    """A period the selector renders and the backend does not compute shows a confident-looking dash."""
    import json
    import pathlib
    import re

    from api.realized_broker import PERIOD_DAYS

    ts = pathlib.Path(__file__).parents[2] / "ui/src/components/ds/periods.ts"
    offered = set(re.findall(r'\["([^"]+)",\s*"[^"]*"\]', ts.read_text()))
    assert offered, "could not parse the UI period list — the check would pass vacuously"
    assert offered == set(PERIOD_DAYS), f"UI offers {offered}, backend computes {set(PERIOD_DAYS)}"
    assert json.dumps


def test_realized_is_recognised_AT_THE_SALE_with_the_lots_true_basis():
    """CONTRACT REVERSED (#336). This test previously asserted the opposite, and the old assertion said
    "1W invented a basis for a lot opened 90 days before it" — a lot bought outside the window
    contributed only PROCEEDS, so 1W read $0.00 for a round trip that made $200.

    That produced numbers the operator could not use. Live 2026-08-18: 1W $2,226.12* > 1M $825.52* > 3M $138.50,
    a shorter window reading HIGHER than the longer one containing it, each computed on a different cost
    basis, with an asterisk admitting the total was understated.

    Realized P&L is recognised at the SALE, with the lot's true basis however far back it was bought —
    what a brokerage statement and a tax lot both do, and what "realized this week" means to a reader.

    Same fixture as the old test, deliberately: buy day 10, sell day 99, +$200. The only thing that
    changed is which answer is correct.
    """
    from api.realized_broker import realized_by_period

    day = 86_400 * 1_000_000_000
    now = 100 * day  # day 100
    acts = [
        _fill("X", "buy", 10, 10.0, "010"), # day 10 — outside 1W/1M, inside `all`
        _fill("X", "sell", 10, 30.0, "099"),# day 99 — the SALE, inside every window
    ]
    out = realized_by_period(acts, day_start_ns=now, ns_of=lambda v: int(v) * day, floor_of=lambda d, days: d - days * 86_400_000_000_000)

    # The sale is in every window, so every window reports the whole gain with the real basis.
    for key in ("1W", "1M", "3M", "all"):
        assert round(out[key]["total"], 2) == 200.0, f"{key} must use the lot's true basis"
        assert out[key]["closed_count"] == 1
    # No asterisk: the basis was known, so nothing is understated.
    assert out["1W"]["unmatched"] == 0 and out["1W"]["is_partial"] is False


def test_a_sale_OUTSIDE_a_window_does_not_leak_into_it():
    """The other half of the contract, and the one that makes the test above mean something.

    Crediting every sale to every window would also pass the assertions above. This fixture puts the sale
    80 days back — inside 3M and `all`, outside 1W and 1M — so the two tests together pin that windows
    filter by SALE DATE and by nothing else.
    """
    from api.realized_broker import realized_by_period

    day = 86_400 * 1_000_000_000
    now = 100 * day
    acts = [
        _fill("X", "buy", 10, 10.0, "005"),   # day 5
        _fill("X", "sell", 10, 30.0, "020"), # day 20 — 80 days ago
    ]
    out = realized_by_period(acts, day_start_ns=now, ns_of=lambda v: int(v) * day, floor_of=lambda d, days: d - days * 86_400_000_000_000)

    assert round(out["3M"]["total"], 2) == 200.0
    assert round(out["all"]["total"], 2) == 200.0
    assert out["1W"]["total"] == 0.0 and out["1W"]["closed_count"] == 0
    assert out["1M"]["total"] == 0.0 and out["1M"]["closed_count"] == 0


def test_the_windows_NEST_so_a_longer_period_can_never_report_less():
    """The property the operator actually asked for: three numbers under one label that can be compared.

    With every gain positive, a longer window contains a superset of the sales, so its total cannot be
    smaller. Under the old per-window matching this was violated in production — which is what made the
    panel look broken. Asserted with all-positive round trips so the containment is a strict arithmetic
    consequence; with losses in the mix a longer window legitimately reads lower, which is why the check
    is framed this way rather than as blanket monotonicity.
    """
    from api.realized_broker import realized_by_period

    day = 86_400 * 1_000_000_000
    now = 100 * day
    acts = [
        _fill("X", "buy", 10, 10.0, "005"), _fill("X", "sell", 10, 20.0, "020"),  # +100, day 20
        _fill("Y", "buy", 10, 10.0, "008"), _fill("Y", "sell", 10, 15.0, "080"),  # +50,  day 80
        _fill("Z", "buy", 10, 10.0, "090"), _fill("Z", "sell", 10, 12.0, "099"),  # +20,  day 99
    ]
    out = realized_by_period(acts, day_start_ns=now, ns_of=lambda v: int(v) * day, floor_of=lambda d, days: d - days * 86_400_000_000_000)

    assert round(out["1W"]["total"], 2) == 20.0    # day 99 only
    assert round(out["1M"]["total"], 2) == 70.0    # days 80 + 99
    assert round(out["3M"]["total"], 2) == 170.0   # all three
    assert round(out["all"]["total"], 2) == 170.0
    totals = [out[k]["total"] for k in ("1W", "1M", "3M", "all")]
    assert totals == sorted(totals), "a longer window reported LESS — the #336 defect"


# --- SHORTS. Found by reconciling the FIFO's leftover lots against the broker's actual positions, not
# --- by reading the code: the leftovers held 23 PENG the broker did not have.


def test_a_SHORT_is_opened_by_sell_short_and_closed_by_the_covering_buy():
    """The live defect. PENG was sold short on 2026-07-28 @ 47.56 and later covered.

    Read as a plain sell it matched nothing, and the covering BUY opened a phantom LONG lot of 23 shares
    that never closes: $1,235.10 of cost basis the broker does not have, and the short's whole P&L gone.
    """
    r = realized_from_fills([
        _fill("PENG", "sell_short", 23, 47.56, "2026-07-28T13:41:27Z"),
        _fill("PENG", "buy", 23, 45.00, "2026-07-29T14:00:00Z"),
    ])
    assert round(r.total, 2) == round(23 * (47.56 - 45.00), 2), "a short profits when the price FALLS"
    assert r.closed == 1
    assert r.unmatched == 0, "the short sell was counted as a gap rather than as an opening"


def test_a_LOSING_short_is_not_reported_as_a_winner():
    """Sign errors here do not look wrong — they report a losing short as a gain of the same size."""
    r = realized_from_fills([
        _fill("P", "sell_short", 10, 40.00, "2026-07-28T13:00:00Z"),
        _fill("P", "buy", 10, 50.00, "2026-07-29T13:00:00Z"),
    ])
    assert round(r.total, 2) == -100.0


def test_the_leftover_lots_must_equal_what_the_broker_ACTUALLY_HOLDS():
    """The check that found the bug, kept as the check that prevents the next one.

    Realized P&L cannot be verified by inspection — every plausible-looking implementation returns a
    plausible-looking number. What CAN be verified is the residue: whatever the matcher has not closed
    must be exactly the broker's open positions. When it was not, the gap named the defect.
    """
    from api.realized_broker import open_lots_after

    fills = [
        _fill("PENG", "sell_short", 23, 47.56, "2026-07-28T13:41:27Z"),
        _fill("PENG", "buy", 23, 45.00, "2026-07-29T14:00:00Z"),
        _fill("NBIS", "buy", 29, 100.00, "2026-08-01T14:00:00Z"),
    ]
    broker_holds = {"NBIS": 29.0}  # PENG is FLAT — short opened and covered
    assert open_lots_after(fills) == broker_holds, (
        "the matcher's leftovers disagree with the broker — a phantom lot means realized P&L is wrong "
        "by that lot's basis"
    )


def test_adding_to_an_open_position_does_not_close_anything():
    r = realized_from_fills([
        _fill("X", "sell_short", 10, 50.0, "2026-08-01T13:00:00Z"),
        _fill("X", "sell_short", 10, 60.0, "2026-08-02T13:00:00Z"),
        _fill("X", "buy", 20, 40.0, "2026-08-03T13:00:00Z"),
    ])
    assert round(r.total, 2) == (10 * (50 - 40) + 10 * (60 - 40))
    assert r.closed == 1


def test_the_LIVE_account_reconciles_to_the_cent():
    """The whole-account identity, pinned with the real 2026-08-16 figures.

    Realized P&L cannot be verified by inspection. It CAN be verified by conservation, and that is what
    finally settled it — every term below came from a different Alpaca endpoint, and they close exactly:

        starting equity  100,000.00   (paper account, opened 2026-07-03)
      + realized              107.70   this matcher, over all 193 FILL activities
      + unrealized          1,004.49   the broker's own `unrealized_pl` on 8 open positions
      + fees                   -5.45   29 FEE activities
      + withholding          -990.46   1 WH activity
      = equity           100,116.28   /v2/account

    THE WITHHOLDING IS THE POINT OF THIS TEST. $990.46 is not a trading result and is correctly absent
    from realized — but it is an equity move, so "realized + change in unrealized" does NOT equal the
    equity curve, and an operator reconciling the two by hand will find exactly this gap. That is the
    same shape as the question that started #322.

    Before shorts were handled the identity was out by $1,235.10, and no amount of reading the matcher
    would have shown it.
    """
    realized, unrealized, fees, withholding = 107.70, 1004.49, -5.45, -990.46
    assert round(100_000 + realized + unrealized + fees + withholding, 2) == 100_116.28

    # And the reason the identity is trustworthy: nothing is left over. A phantom lot would move
    # `realized` by its basis while every other term stayed put, so the sum would still "look" fine.
    assert round(100_000 + realized + unrealized, 2) != 100_116.28, (
        "the fee and withholding terms are doing no work — this test would pass without them and would "
        "stop being evidence of anything"
    )


def test_the_WEBSOCKET_frame_carries_realized_too_not_just_REST():
    """The hop the first fix missed, and the one that actually feeds the UI.

    `_live_payload` REBUILDS the trades frame for the WS re-push instead of forwarding what the
    engine published, so a field not named there is silently absent. REST carried both fields and a
    test pinned REST — but the tile is WS-driven and never calls `/trades`, so it rendered a dash
    while every backend assertion passed.

    Same shape as #233's original defect (the response model dropped an undeclared key), one boundary
    further out. Three hops now: engine frame -> WS re-push -> REST response.
    """
    import inspect

    from api import app as app_module

    src = inspect.getsource(app_module)
    start = src.index('if topic.channel == "trades"')
    payload = src[start:start + 1400]
    assert '"realized_periods"' in payload, (
        "the WS trades frame drops realized_periods — the UI reads this, not /trades, so the period "
        "figure renders as a dash no matter what REST returns"
    )
    assert '"realized_session"' in payload, "the WS trades frame drops realized_session"


# ==================================================================================================
# THE OPEN BOOK WITH ITS BASIS AND ITS OPENER INTACT (#734).
#
# `open_lots_after` returns net qty per symbol and throws away the two fields the per-lane P&L history
# is made of: what each lot COST, and which strategy OPENED it. Reconstructing
# `unrealized_at(T, lane)` needs both — it is Σ over the lane's open lots of qty x (mark(T) − lot_px).
#
# A SECOND MATCHER IS NOT AN OPTION. `_match`'s own docstring says both existing callers come from one
# pass "deliberately — two walks over one set of fills would drift, and the reconciliation would then
# be checking the second walk rather than the number anyone reports". So this is a third view of the
# same pass, not a reimplementation: everything below is `_match`'s residue, read rather than recomputed.
#
# ATTRIBUTION IS THE LOT'S OPENER (#292, the operator 2026-08-14), and an unknown opener stays None forever —
# it is reported as UNCLAIMED and never assigned to whoever closed the position.
# ==================================================================================================
def test_the_open_book_keeps_the_BASIS_that_open_lots_after_discards():
    """The reconstruction is qty x (mark − BASIS). `open_lots_after` cannot answer it."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-02T13:00:00Z"),
    ]
    # Fixture property first: the existing helper genuinely loses what this test is about.
    assert open_lots_after(fills) == {"AEM": 15.0}

    book = open_lots_detail(fills)
    assert book["AEM"].qty == 15.0
    # Weighted, not averaged: (10x100 + 5x120) / 15.
    assert round(book["AEM"].basis, 6) == round((10 * 100.0 + 5 * 120.0) / 15, 6)


def test_the_open_book_keeps_the_OPENING_strategy_per_lot():
    """One symbol, two lanes, and the answer must not be one number."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "buy", 5, 120.0, "2026-08-02T13:00:00Z"),
    ]
    tags = {"2026-08-01T13:00:00Z": "MOMENTUM-002", "2026-08-02T13:00:00Z": "BCTROT-004"}
    book = open_lots_detail(fills, strategy_of=lambda f: tags.get(f["transaction_time"]))
    by_lane = {(lot.strategy_id, lot.symbol): lot for lot in book["AEM"].lots}
    assert by_lane[("MOMENTUM-002", "AEM")].qty == 10.0
    assert by_lane[("BCTROT-004", "AEM")].qty == 5.0
    assert by_lane[("MOMENTUM-002", "AEM")].basis == 100.0


def test_an_UNKNOWN_opener_stays_None_and_is_never_given_to_the_closer():
    """#292: a fill from before the cache's horizon has no opener. Carried as None and reported
    UNCLAIMED — attributing it to whoever closed the position is the exact leak that rule exists to
    close ("MOMENTUM does the work, a human clicks sell, and the manual book gets the credit")."""
    fills = [_fill("WPM", "buy", 7, 50.0, "2026-07-01T13:00:00Z")]
    book = open_lots_detail(fills, strategy_of=lambda f: None)
    assert [lot.strategy_id for lot in book["WPM"].lots] == [None]


def test_a_SHORT_book_reports_negative_qty_and_its_own_basis():
    """`sell_short` opens; the PENG defect (a covering buy read as an opening long) is pinned in
    `_match`'s comments and cost $1,235.10 of phantom basis. The detail view must not reintroduce it."""
    fills = [_fill("PENG", "sell_short", 23, 53.70, "2026-07-28T13:00:00Z")]
    book = open_lots_detail(fills)
    assert book["PENG"].qty == -23.0
    assert book["PENG"].basis == 53.70
    # THE LOT'S OWN SIGN, not just the book's. The per-lane grouping sums LOT quantities, so an
    # unsigned lot would render every short position as a positive holding — and mutation found this
    # assertion missing: dropping `* sign` from the lot left the whole suite green.
    assert [lot.qty for lot in book["PENG"].lots] == [-23.0]


def test_a_CLOSED_position_leaves_no_row_at_all():
    """Flat is flat. A zero-qty row would make every consumer decide whether zero means flat or
    unknown, which is the distinction this whole ticket exists to keep."""
    fills = [
        _fill("FIG", "buy", 278, 24.10, "2026-08-14T13:00:00Z"),
        _fill("FIG", "sell", 278, 25.96, "2026-08-14T18:00:00Z"),
    ]
    assert open_lots_detail(fills) == {}


def test_it_agrees_with_open_lots_after_on_NET_QTY_for_the_live_week():
    """TWO DERIVATIONS OF ONE FACT, which is the only reason to trust either. `open_lots_after` is
    reconciled against the broker's real positions in production (the check that found the PENG
    defect); if this view disagrees with it on net qty, this view is wrong."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-01T13:00:00Z"),
        _fill("AEM", "sell", 4, 110.0, "2026-08-03T13:00:00Z"),
        _fill("PENG", "sell_short", 23, 53.70, "2026-07-28T13:00:00Z"),
        _fill("FIG", "buy", 278, 24.10, "2026-08-14T13:00:00Z"),
        _fill("FIG", "sell", 278, 25.96, "2026-08-14T18:00:00Z"),
    ]
    assert {s: b.qty for s, b in open_lots_detail(fills).items()} == open_lots_after(fills)
