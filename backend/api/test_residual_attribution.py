"""The −$33.53 residual, measured against the live paper account on 2026-08-23.

MEASURED, not reasoned about (CLAUDE.md: unverifiable by inspection → measure it). The probe swept all
397 activities — 352 FILL, 44 FEE, 1 WH, four pages, terminating on a short page — and found:

    baseline 100,000.00 + fill cashflow −25,605.79 + adjustments −1,000.88  =  73,393.33
    Alpaca's own `cash`                                                        73,393.33

to the cent. THE ACTIVITY RECORD IS COMPLETE. Yet the alarm had been saying, every five minutes for
hours, "Something moved this account that the activity record does not explain."

Where the $33.53 actually is — BETA, per-symbol cost basis:

    2026-08-20 13:35   buy  58 + 19 @ 26.10     → 77 held
    2026-08-20 15:35   sell 2+13+31+18+6+1+5+1 @ 25.04  → 0 held, FLAT
    2026-08-20 16:00   buy  36 + 43 @ 25.24     → 79 held

    FIFO cost of the 79 open:  79 × 25.24        = 1,993.96
    Alpaca `cost_basis`:       79 × 25.664487    = 2,027.49
                                                   ---------
                                                     −33.53

Alpaca's `avg_entry_price` did not reset when the position went flat at 15:35:22 and reopened 25
minutes later; it still blends the 26.10 lot that was sold in full. OUR NUMBER IS THE RIGHT ONE, and
the alarm was blaming our record for the broker's arithmetic.

The engine's 3,312.97 was independently confirmed correct: fill cashflow −25,605.79 plus the FIFO cost
of the genuinely-open lots (30,153.86 less the phantom 1,235.10 PENG lot) = 3,312.97, to the cent.
"""

from __future__ import annotations

import inspect

from api.realized_broker import fill_cashflow, reconcile

#: The BETA fills above, in transaction order. The flat-and-reopen is the whole point of the fixture:
#: without it Alpaca's average and FIFO agree and the test could not fail either way (CLAUDE.md: a test
#: that cannot fail carries no information — assert the fixture's own property first).
BETA = (
    *({"activity_type": "FILL", "symbol": "BETA", "side": "buy", "qty": q, "price": "26.10"} for q in ("58", "19")),
    *({"activity_type": "FILL", "symbol": "BETA", "side": "sell", "qty": q, "price": "25.04"}
      for q in ("2", "13", "31", "18", "6", "1", "5", "1")),
    *({"activity_type": "FILL", "symbol": "BETA", "side": "buy", "qty": q, "price": "25.24"} for q in ("36", "43")),
)


def test_the_fixture_actually_goes_flat_and_reopens() -> None:
    """Without this, FIFO and average agree and nothing below can discriminate."""
    held = 0.0
    flat_after_trading = False
    for f in BETA:
        held += float(f["qty"]) * (1 if f["side"] == "buy" else -1)
        if held == 0:
            flat_after_trading = True
    assert flat_after_trading, "fixture never goes flat — the two cost conventions cannot diverge"
    assert held == 79, "and it must reopen afterwards, or there is no open basis to disagree about"
    # And the reopened lots must be at a DIFFERENT price from the closed ones, or FIFO and average
    # coincide and every assertion below would pass with the bug present.
    prices = {f["price"] for f in BETA if f["side"] == "buy"}
    assert len(prices) > 1, f"all buys at one price {prices} — nothing can diverge"


def test_fill_cashflow_is_the_second_derivation_and_it_is_exact() -> None:
    """Cash is checkable to the CENT, unlike equity-implied realized, which carries the broker's marks.

    −25,605.79 is the live figure. The point is not the number; it is that this quantity and Alpaca's
    `cash` share no code, so their agreement is evidence and their disagreement is missing money.
    """
    assert round(fill_cashflow(BETA), 2) == round(
        -(58 + 19) * 26.10 + 77 * 25.04 - (36 + 43) * 25.24, 2
    )
    # Non-fill rows are NOT cash from fills — they are the `adjustments` term and must not be counted twice.
    with_fee = (*BETA, {"activity_type": "FEE", "net_amount": "-0.42"})
    assert fill_cashflow(with_fee) == fill_cashflow(BETA)


def test_a_complete_record_is_not_reported_as_a_missing_dollar() -> None:
    """THE LIVE CASE. Cash agrees to the cent; equity-implied is $33.53 out. That is the broker's
    cost basis, and saying "something moved this account" about it is false."""
    rec = reconcile(
        reported_realized=3312.97,
        adjustments=-1000.88,
        equity=103466.29,
        baseline=100000.0,
        unrealized=1120.67,
        cash=73393.33,
        fill_cashflow=-25605.79,
    )
    assert not rec["reconciled"]
    assert round(rec["residual"], 2) == -33.53
    assert round(rec["cash_residual"], 2) == 0.0
    assert rec["verdict"] == "BROKER_BASIS", (
        "the activity record reconciles to the cent — the disagreement is in the broker's lot "
        "accounting, and the alarm must not blame our record for it"
    )


def test_actually_missing_cash_still_reads_as_missing_cash() -> None:
    """The alarm this was built to raise (#345's $999.09) must survive the new verdict.

    Same shape as the live case except the WH row is absent from the record: cash is then $990.46 short
    of what the fills explain, and THAT is a record that does not explain the account.
    """
    rec = reconcile(
        reported_realized=3312.97,
        adjustments=-10.42,          # the FEE rows only — the one WH row never fetched
        equity=103466.29,
        baseline=100000.0,
        unrealized=1120.67,
        cash=73393.33,
        fill_cashflow=-25605.79,
    )
    assert not rec["reconciled"]
    assert round(rec["cash_residual"], 2) == 990.46
    assert rec["verdict"] == "RECORD_INCOMPLETE"


def test_reconciled_when_both_derivations_agree() -> None:
    rec = reconcile(
        reported_realized=3312.97,
        adjustments=-1000.88,
        equity=103466.29 - 33.53,
        baseline=100000.0,
        unrealized=1120.67,
        cash=73393.33,
        fill_cashflow=-25605.79,
    )
    assert rec["reconciled"] and rec["verdict"] == "OK"


def test_the_phantom_lot_detector_has_a_production_caller() -> None:
    """`open_lots_after` found the PENG `sell_short` defect and was then never called by anything.

    Fifth unwired mechanism found in this session (#439 detector, #462 caller argument, #467 acceptor,
    #440 boot gate, this). The pattern is always the same: the thing is written, tested, documented
    with the defect it caught, and no production path drives it — so it catches that defect once, by
    hand, and never again.
    """
    from api import engine_node

    src = inspect.getsource(engine_node)
    assert "open_lots_after" in src, (
        "nothing in the engine drives open_lots_after — a phantom lot means realized is wrong by "
        "exactly that lot's basis, and only this can see it"
    )


# ---------------------------------------------------------------------------------------------------
# THE SEAM. Everything above tests `realized_broker`; none of it drives `_refresh_realized_periods`,
# and that is where the last two defects of this shape lived (#462's caller argument, #467's acceptor).
# A mutation that deleted the `cash=` argument at the call site left every test above green.
# ---------------------------------------------------------------------------------------------------

_T = "2026-08-20T{}Z"


def _fill(sym, side, qty, price, t):
    # Alpaca's own field names and STRING numerics, verbatim from /v2/account/activities.
    return {"id": f"{sym}{t}{qty}", "activity_type": "FILL", "transaction_time": _T.format(t),
            "type": "fill", "price": price, "qty": qty, "side": side, "symbol": sym,
            "order_id": f"o-{sym}", "cum_qty": qty, "order_status": "filled"}


#: BETA's real 2026-08-20 sequence plus the real PENG short round trip of 2026-07-28 → 2026-08-03.
#: The short is here so `fill_cashflow` is exercised on a `sell_short`, which credits cash exactly as a
#: `sell` does and which a naive `side == "sell"` would debit instead — an $2,187.76 error on 23 shares.
_FILLS = [
    _fill("PENG", "sell_short", "23", "47.56", "13:41:27.410390"),
    _fill("PENG", "buy", "23", "53.70", "13:41:28.410390"),
    _fill("BETA", "buy", "58", "26.10", "13:35:06.047900"),
    _fill("BETA", "buy", "19", "26.10", "13:35:07.047900"),
    _fill("BETA", "sell", "77", "25.04", "15:35:22.890888"),
    _fill("BETA", "buy", "36", "25.24", "16:00:10.432325"),
    _fill("BETA", "buy", "43", "25.24", "16:00:11.579799"),
]
#: A real FEE row: no `qty`, no `price`, `net_amount` as a string. Production emits nothing else.
_FEE_ROW = {"id": "fee1", "activity_type": "FEE", "activity_sub_type": "CAT", "date": "2026-08-20",
            "created_at": "2026-08-21T00:05:43.079941Z", "net_amount": "-0.42",
            "description": "CAT fee", "status": "executed", "currency": "USD"}

#: Derived, not asserted — so the fixture stays arithmetically honest if a fill is ever edited.
_CASHFLOW = -(77 * 26.10) + 77 * 25.04 - 79 * 25.24 + 23 * 47.56 - 23 * 53.70   # −2,216.80
_CASH = 100000.0 - 2216.80 - 0.42
_BETA_MV = 79 * 24.74
#: Alpaca's stale average — 79 × 25.664487, still blending the 26.10 lot it sold in full at 15:35:22.
_BETA_ALPACA_COST = 2027.49


def _drive(positions, monkeypatch):
    """Run the REAL `_refresh_realized_periods` against a double built from production payloads."""
    import asyncio

    from api import engine_node

    equity = _CASH + _BETA_MV

    class _Http:
        async def list_activities(self, **_):
            return [*_FILLS, _FEE_ROW]

        async def get_account(self):
            return {"equity": f"{equity:.2f}", "cash": f"{_CASH:.2f}"}

        async def list_positions(self):
            return positions

    class _Log:
        def __init__(self):
            self.warnings = []

        def warning(self, m):
            self.warnings.append(str(m))

    class _Clock:
        def timestamp_ns(self):
            return 1755734400_000_000_000   # 2026-08-21, so the whole fixture is before "today"

    class _Cache:
        def orders(self):
            return []

    class _Fake:
        # The real method is bound; `log` is read-only Cython on the real class so it cannot be built up.
        _http = _Http()
        log = _Log()
        clock = _Clock()
        cache = _Cache()
        _realized_periods: dict = {}
        _refresh_realized_periods = engine_node.UiFeedStrategy._refresh_realized_periods

    monkeypatch.setattr(engine_node, "_ACCOUNT_BASELINE", 100000.0)
    fake = _Fake()
    asyncio.run(fake._refresh_realized_periods())
    return fake


_HELD = [{"symbol": "BETA", "qty": "79", "cost_basis": f"{_BETA_ALPACA_COST}",
          "market_value": f"{_BETA_MV:.2f}", "unrealized_pl": f"{_BETA_MV - _BETA_ALPACA_COST:.2f}",
          "avg_entry_price": "25.664487", "current_price": "24.74"}]


def test_the_engine_publishes_the_cash_arm_and_calls_it_a_basis_gap(monkeypatch) -> None:
    """The live case, driven through the production method rather than around it."""
    fake = _drive(_HELD, monkeypatch)
    rec = fake._realized_periods["reconciliation"]
    assert round(rec["residual"], 2) == -33.53, "the fixture must reproduce the live gap"
    assert rec["cash_residual"] is not None, "the caller stopped passing the cash arm"
    assert round(rec["cash_residual"], 2) == 0.0
    assert rec["verdict"] == "BROKER_BASIS"
    said = " ".join(fake.log.warnings)
    assert "BROKER_BASIS" in said and "COMPLETE" in said, (
        "the operator must be told the record is complete; the old text said the opposite"
    )
    assert "does not show" not in said, "still accusing the record of hiding a movement"


def test_the_engine_names_a_lot_the_account_does_not_hold(monkeypatch) -> None:
    """The PENG shape: the matcher's leftovers must be checked against the account, every sweep."""
    fake = _drive([], monkeypatch)
    assert fake._realized_periods["reconciliation"]["phantom_lots"] == {"BETA": (79.0, 0.0)}
    assert any("MATCHER LOTS DISAGREE WITH THE ACCOUNT" in w for w in fake.log.warnings)


def test_a_phantom_free_sweep_stays_quiet(monkeypatch) -> None:
    """Or the alarm fires on the normal path and gets muted before it matters."""
    fake = _drive(_HELD, monkeypatch)
    assert fake._realized_periods["reconciliation"]["phantom_lots"] == {}
    assert not any("MATCHER LOTS DISAGREE" in w for w in fake.log.warnings)


def test_a_non_fill_row_that_carries_a_price_is_still_not_a_fill() -> None:
    """No Alpaca type does this today — FEE and WH carry `net_amount` and no `qty`/`price`, measured
    2026-08-23. That is exactly why the guard is a NEGATIVE definition (`!= FILL`) and not a list: the
    row Alpaca adds next is the one that would be double-counted, once as cash and again as a fill.
    """
    from api.realized_broker import fill_cashflow as fc

    invented = {"activity_type": "CFEE", "qty": "10", "price": "5.00", "side": "sell",
                "net_amount": "-50.00"}
    assert fc([*_FILLS, invented]) == fc(_FILLS)


def test_a_split_moves_no_cash_and_must_not_read_as_nothing_to_see(monkeypatch) -> None:
    """VALUE LEAVES WITHOUT CASH MOVING — 59sh1zl1, reviewing #472, and they were right.

    "Cash is the movement and everything else is a valuation" is true of the CASH side and false of the
    SHARE side. A split, reverse split, merger-with-exchange, spinoff or symbol change alters QUANTITY
    with no cash activity at all. Then:

        cash_residual  ~0      -> cash_ok TRUE
        equity arm     out     -> ok FALSE
        verdict                -> "BROKER_BASIS", i.e. "the record is COMPLETE"

    and it is not complete: `open_lots_after(fills)` still holds the PRE-split quantity, so every basis
    derived from it is wrong. Here BETA 2-for-1 — the account holds 158, our fills say 79.

    The mechanism that KNOWS is already in this PR: `phantom_lots` diverges the instant a split lands.
    It was computed AFTER `reconcile` had already returned its verdict and `reconcile` never saw it —
    two derivations of one question not consulting each other, which is the rule this whole session is
    about. So the verdict now takes the lot check as an input.
    """
    split = [{"symbol": "BETA", "qty": "158", "cost_basis": f"{_BETA_ALPACA_COST}",
              "market_value": f"{_BETA_MV:.2f}", "unrealized_pl": f"{_BETA_MV - _BETA_ALPACA_COST:.2f}",
              "avg_entry_price": "12.832", "current_price": "12.37"}]
    fake = _drive(split, monkeypatch)
    rec = fake._realized_periods["reconciliation"]
    assert rec["phantom_lots"] == {"BETA": (79.0, 158.0)}, "the fixture must actually diverge the lots"
    assert round(rec["cash_residual"], 2) == 0.0, "and cash must still reconcile, or this proves nothing"
    assert rec["verdict"] == "LOTS_DIVERGE", (
        "cash reconciling does NOT make the record complete — the share side is stale, and that is a "
        "different repair from either of the other two branches"
    )
    said = " ".join(fake.log.warnings)
    assert "COMPLETE" not in said, "still telling the operator there is nothing to see"
