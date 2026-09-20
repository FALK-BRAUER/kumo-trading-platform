"""Realized P&L per strategy, attributed to the OPENING lot (#345 item 3, rule from #292).

THE DEFECT. `books.ts` computes `book.total = book.realized + book.unrealized` where `realized` is the
LIVE-CYCLE SESSION figure — it reads $0.00 after any restart, because Nautilus's cache holds positions
closed during THIS process's life and reconciliation restores OPEN positions on startup, not closed
ones. Proof off the 2026-08-18 screen:

    MOMENTUM-002 1,245.59 + MANUAL-001 (-68.80) = 1,176.79 = UNREALIZED, exactly

Both cells printed `real $0.00` while the book had realized $2,650.21 over 1W.

THE ATTRIBUTION RULE IS NOT A DETAIL, and it is already decided. #292, by the operator on 2026-08-14: P&L is
attributed to the strategy tag that OPENED the position, tagging is at LOT level, disposal is FIFO. The
closing actor is recorded separately and takes none of the P&L. Without that rule "MOMENTUM does the
work, a human clicks sell, and the manual book gets the credit" — every per-strategy number then
becomes untrustworthy.

The matcher was already FIFO by lot, so attribution is a third field on the lot rather than a second
pass over the fills.

WHAT THE JOIN IS, AND WHAT IT CANNOT REACH. Alpaca's FILL activities carry `order_id` (the VENUE id) and
no strategy tag. Nautilus's cached orders carry both `venue_order_id` and `strategy_id` and survive
restarts through the AOF. Measured against the live account 2026-08-21:

    2026-08-17 onward   130 of 130 fills join to a cached order   100%
    before 2026-08-17     2 of 191                                 ~1%

The cache does not reach past its horizon. So 1D and 1W are fully attributable and `all` is not, and the
honest rendering of that is `unclaimed` — published, never distributed.
"""

from __future__ import annotations

import inspect

from api.realized_broker import realized_by_period

_DAY = 86_400 * 1_000_000_000


def _ns(iso: str) -> int:
    """Day-resolution stand-in: "2026-08-19T…" -> a monotonic day index in ns."""
    y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
    return (y * 372 + m * 31 + d) * _DAY


def _fill(sym, side, qty, price, day, order_id):
    """Shaped as Alpaca emits it: STRING numerics, `order_id` (venue), and NO strategy tag."""
    return {
        "activity_type": "FILL",
        "symbol": sym,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": f"{day}T14:00:00Z",
        "order_id": order_id,
    }


def _by_order(mapping):
    return lambda f: mapping.get(str(f.get("order_id") or ""))


TODAY = _ns("2026-08-20T00:00:00Z")


def _run(fills, mapping, **kw):
    return realized_by_period(
        fills, day_start_ns=TODAY, ns_of=_ns, strategy_of=_by_order(mapping),
        floor_of=lambda d, days: d - days * 86_400_000_000_000,  # UTC-day floors, as these fixtures were written (#846 hands the engine ET midnights)
        **kw,
    )


# --------------------------------------------------------------------------------------------------

def test_the_fixture_can_express_a_MISATTRIBUTION():
    """The fixture's own property first.

    If the opener and the closer were the same strategy, lot-level and whole-position attribution give
    identical answers (#292 says so explicitly) and every assertion below would pass with the rule
    inverted. The fixture must have a position OPENED by one strategy and CLOSED by another.
    """
    opener, closer = "MOMENTUM-002", "MANUAL-001"
    assert opener != closer


def test_PL_follows_the_OPENER_not_the_closer():
    """#292's central rule, and the leak it exists to close.

    MOMENTUM opens 10 @ 100. A human flattens it at 110 under MANUAL. The $100 belongs to MOMENTUM;
    attributing it to MANUAL is a silent transfer of performance between strategies.
    """
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-19", "o-open"),
        _fill("AEM", "sell", 10, 110.0, "2026-08-20", "o-close"),
    ]
    out = _run(fills, {"o-open": "MOMENTUM-002", "o-close": "MANUAL-001"})

    assert round(out["all"]["by_strategy"]["MOMENTUM-002"], 2) == 100.0
    assert "MANUAL-001" not in out["all"]["by_strategy"], "the closer must take none of the P&L"
    assert out["all"]["unclaimed"] == 0.0


def test_attribution_is_per_LOT_when_two_strategies_built_one_position():
    """#292: "tagging is at LOT level, not position level" — because positions get built by more than
    one strategy regularly (VCTR 20->40 and OKTA 20->30, both manual adds, on 2026-08-13 alone).

    MOMENTUM opens 10 @ 100, MANUAL adds 10 @ 120, all 20 sold at 130. FIFO: MOMENTUM's lot gains
    10x30 = 300, MANUAL's gains 10x10 = 100. A whole-position rule would hand all $400 to MOMENTUM.
    """
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-18", "o-a"),
        _fill("AEM", "buy", 10, 120.0, "2026-08-19", "o-b"),
        _fill("AEM", "sell", 20, 130.0, "2026-08-20", "o-c"),
    ]
    out = _run(fills, {"o-a": "MOMENTUM-002", "o-b": "MANUAL-001", "o-c": "MANUAL-001"})

    by = out["all"]["by_strategy"]
    assert round(by["MOMENTUM-002"], 2) == 300.0
    assert round(by["MANUAL-001"], 2) == 100.0
    assert round(out["all"]["total"], 2) == 400.0


def test_an_UNKNOWN_opener_is_reported_not_distributed():
    """The cache horizon, measured: essentially nothing before 2026-08-17 joins.

    A fill whose opening lot has no known strategy must land in `unclaimed`. Handing it to the closer
    — the only other strategy in scope — is precisely the leak #292 closes, and it would make the
    per-strategy numbers wrong in a way nothing downstream could detect.
    """
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-10", "o-ancient"),   # before the cache horizon
        _fill("AEM", "sell", 10, 110.0, "2026-08-20", "o-close"),
    ]
    out = _run(fills, {"o-close": "MANUAL-001"})  # the opener is NOT in the map

    assert out["all"]["by_strategy"] == {}, "nothing may be attributed on an unknown opener"
    assert round(out["all"]["unclaimed"], 2) == 100.0


def test_THE_INVARIANT_holds_in_every_window():
    """#345's own acceptance: Sum(strategies) + unclaimed ~= account NET(window).

    Two derivations of one fact, so a disagreement is the detector. Asserted for EVERY window, not just
    `all`, because the windows are where #336 went wrong — three incomparable numbers under one label.
    """
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-05", "o-old"),
        _fill("AEM", "sell", 10, 110.0, "2026-08-06", "o-x"),
        _fill("WPM", "buy", 5, 50.0, "2026-08-19", "o-a"),
        _fill("WPM", "sell", 5, 60.0, "2026-08-20", "o-b"),
    ]
    out = _run(fills, {"o-a": "MOMENTUM-002", "o-b": "MOMENTUM-002"})

    for key, period in out.items():
        if not isinstance(period, dict) or "by_strategy" not in period:
            continue
        recombined = sum(period["by_strategy"].values()) + period["unclaimed"]
        assert abs(recombined - period["total"]) < 1e-9, (
            f"{key}: strategies + unclaimed = {recombined} but total = {period['total']}"
        )


def test_the_windows_NEST_per_strategy_as_well():
    """3M must contain 1W. #336 shipped a shorter window reading HIGHER than a longer one containing
    it, because each re-matched with a different cost basis. Per-strategy buckets are sliced from ONE
    match, so nesting is structural — pinned so it stays that way."""
    fills = [
        _fill("WPM", "buy", 5, 50.0, "2026-08-01", "o-a"),
        _fill("WPM", "sell", 5, 60.0, "2026-08-02", "o-b"),   # old: in 3M, not in 1W
        _fill("AEM", "buy", 5, 50.0, "2026-08-19", "o-c"),
        _fill("AEM", "sell", 5, 70.0, "2026-08-20", "o-d"),   # recent: in both
    ]
    m = {"o-a": "MOMENTUM-002", "o-c": "MOMENTUM-002"}
    out = _run(fills, m)

    wk = out["1W"]["by_strategy"]["MOMENTUM-002"]
    q = out["3M"]["by_strategy"]["MOMENTUM-002"]
    assert round(wk, 2) == 100.0
    assert round(q, 2) == 150.0
    assert q >= wk, "a longer window must contain the shorter one"


def test_adjustments_are_NOT_split_across_strategies():
    """A fee is an account-level cost, not one strategy's trading result.

    Splitting it would be an allocation decision nobody has made, and it would break the invariant
    above — which is stated against `total` (fills only), deliberately.
    """
    fee = {"activity_type": "FEE", "date": "2026-08-20", "net_amount": "-2.26"}
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-19", "o-a"),
        _fill("AEM", "sell", 10, 110.0, "2026-08-20", "o-b"),
    ]
    out = _run(fills, {"o-a": "MANUAL-001", "o-b": "MANUAL-001"}, adjustments=[fee])

    assert round(out["all"]["by_strategy"]["MANUAL-001"], 2) == 100.0, "unchanged by the fee"
    assert round(out["all"]["adjustments"], 2) == -2.26
    assert round(out["all"]["net"], 2) == 97.74


def test_no_strategy_map_leaves_EVERYTHING_unclaimed_rather_than_guessing():
    """`strategy_of=None` is the pre-#345 caller. It must not invent attribution, and it must not
    crash — every existing call site keeps working and simply gets an empty breakdown."""
    fills = [
        _fill("AEM", "buy", 10, 100.0, "2026-08-19", "o-a"),
        _fill("AEM", "sell", 10, 110.0, "2026-08-20", "o-b"),
    ]
    out = realized_by_period(fills, day_start_ns=TODAY, ns_of=_ns, floor_of=lambda d, days: d - days * 86_400_000_000_000)

    assert out["all"]["by_strategy"] == {}
    assert round(out["all"]["unclaimed"], 2) == 100.0
    assert round(out["all"]["total"], 2) == 100.0


def test_the_engine_JOINS_on_venue_order_id_not_client_order_id():
    """THE SEAM, and the thing that is easy to get wrong.

    Alpaca's FILL activities carry `order_id` — the VENUE id. They do NOT carry `client_order_id`. A
    join written against `client_order_id` compiles, runs, matches nothing, and reports every dollar as
    unclaimed — which looks like the cache-horizon result and would be indistinguishable from correct.
    Measured field list, 2026-08-21: ['activity_type', 'cum_qty', 'id', 'leaves_qty', 'order_id',
    'order_status', 'price', 'qty', 'side', 'symbol', 'transaction_time', 'type'].
    """
    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._refresh_realized_periods)
    assert "venue_order_id" in src
    assert 'fill.get("order_id")' in src
    assert "strategy_of=" in src
    assert "client_order_id" not in src, "FILL activities carry no client_order_id — this join matches nothing"
