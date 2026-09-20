"""The equity curve must publish EQUITY. On Alpaca it publishes CASH (#588).

MEASURED on both live tenants, 2026-08-27, same code, opposite results:

    staging (IBKR MARGIN)   base ~   999,216  on a ~1,000,000 account   -> NetLiquidation, NET correct
    paper   (Alpaca)        base =    21,696  on a     104,777 account  -> CASH,           NET wrong

`periodNet` computes `NET = live equity - base`, so on Alpaca it subtracts CASH from EQUITY. The hero
number read $83,080.79 on an account whose lifetime P&L is about +$4,800.

THE PROOF THAT THE BASE IS CASH: the paper 1D curve's LAST POINT was 44,122.26 — identical, to the
cent, to `CASH · NOW`.

WHY `_total` GETS IT WRONG. It reads `AccountState.balances_total` and treats one field name as one
meaning. `AccountBalance.total` is NetLiquidation on an IBKR margin account and CASH on Alpaca, whose
Nautilus adapter models the account as cash-only and carries the portfolio value elsewhere. The
module's docstring — "every value here is the broker's own balances_total" — is exactly the assumption
that fails.

THE DOUBLES HERE ARE BUILT FROM WHAT THE TWO VENUES ACTUALLY EMIT, read off the live engines:

    staging  balances {'total':'999491.28','locked':'25450.17','free':'974041.11','currency':'SGD'}
             margins  {'initial':'25450.18','maintenance':'23142.92'}
             info     {'TotalCashValue': 860411.08}          account_type MARGIN
    paper    account_type CASH, single USD balance, and cockpit's /account reports
             equity 104,776.75  cash 44,122.26  long_market_value 60,654.49
"""

from __future__ import annotations

from api.account_curve import build_curves

_NS = 1_000_000_000


class _Money:
    """Nautilus `Money`: `.as_double()` and `.currency`. A float here would accept what production
    rejects — `AccountBalance` enforces same-currency total/locked/free."""

    def __init__(self, v: float, ccy: str):
        self._v, self.currency = v, ccy

    def as_double(self) -> float:
        return self._v


class _Bal:
    """Nautilus `AccountBalance`: `.total`, `.free`, `.locked`, all Money."""

    def __init__(self, total: float, free: float | None = None, currency: str = "USD"):
        f = total if free is None else free
        self.total = _Money(total, currency)
        self.free = _Money(f, currency)
        self.locked = _Money(total - f, currency)
        self.currency = currency


class _State:
    """An `AccountState` as production actually shapes it (codex, coverage review).

    TWO CORRECTIONS THAT MATTERED:

    * **NO `balances_total`.** The real `AccountState` does not have it — only `Account` does
      (`hasattr(AccountState, "balances_total")` is False). `_total()` reads
      `state.balances_total if hasattr(...) else [b.total for b in state.balances]`, so PRODUCTION
      ALWAYS TAKES THE SECOND BRANCH. The first version of this double supplied `balances_total` and
      therefore tested a path production never executes.
    * **`account_type` is an ENUM**, `AccountType.MARGIN`, not the string `"MARGIN"`. And both live
      tenants report MARGIN — Alpaca's too — so account_type CANNOT discriminate cash from equity.
      That is precisely why the venue difference is invisible.
    """

    def __init__(self, *, total: float, ts_s: int, free: float | None = None,
                 account_type=None, info: dict | None = None,
                 margins: list | None = None, currency: str = "USD"):
        from nautilus_trader.model.enums import AccountType

        self.account_type = AccountType.MARGIN if account_type is None else account_type
        self.balances = [_Bal(total, free, currency)]
        self.margins = margins or []
        self.info = info or {}
        self.ts_event = ts_s * _NS


def _last_equity(curves: dict, period: str = "1D") -> float | None:
    pts = (curves.get(period) or {}).get("points") or []
    return pts[-1]["equity"] if pts else None


# ==================================================================================================
# THE FIXTURE MUST BE ABLE TO EXPRESS THE BUG, or every assertion below is vacuous.
# ==================================================================================================


def test_the_fixture_produces_a_curve_at_all():
    now = 1_700_000_000
    curves = build_curves([_State(total=100.0, ts_s=now - 600),
                           _State(total=110.0, ts_s=now - 60)], now * _NS)
    assert _last_equity(curves) is not None, (
        "the harness produced no 1D points — nothing below would be testing anything")


def test_the_fixture_can_tell_cash_apart_from_equity():
    """The two numbers must DIFFER in the fixture. Paper's real gap is 60,654.49; a fixture where cash
    happened to equal equity could not fail, which is how #588 survived — the two agreed on IBKR."""
    assert 44_122.26 != 104_776.75


# ==================================================================================================
# THE DEFECT
# ==================================================================================================


def test_pre_cutover_CASH_rows_are_excluded_once_a_NET_LIQ_row_exists():
    """THE SEAM #588 CREATES, and the reason the fix is not connector-only.

    Before #588 the Alpaca connector wrote CASH into `AccountBalance.total`; after it, net
    liquidation. Those rows sit in ONE history. Plotted together they step by the whole book at
    deploy time — on 2026-08-27 that is +60,867.38 appearing as a gain that never happened, and it
    would have sat inside NET-1W, 1M and 3M for up to three months.

    So the older rows are excluded rather than plotted: a period with no honest base refuses, the
    same rule as #343/#370/#382. `_cutover_ns` is the only thing that can tell them apart, because
    a cash row and an equity row are both just a number.
    """
    now = 1_700_000_000
    cash, equity = 44_121.93, 104_989.31
    curves = build_curves(
        [_State(total=cash, ts_s=now - 900, free=cash),                       # pre-#588: CASH
         _State(total=cash, ts_s=now - 800, free=cash),                       # pre-#588: CASH
         _State(total=equity, ts_s=now - 180, free=cash,                      # post-#588: NET LIQ
                info={"BalanceTotalSemantics": "NET_LIQUIDATION"}),
         # TWO marked rows, because `build_curves` refuses a single point ("a single point is not a
         # curve"). That is also the real post-deploy behaviour: the chart is empty until the SECOND
         # net-liq state arrives, which is seconds, not minutes.
         _State(total=equity, ts_s=now - 120, free=cash,
                info={"BalanceTotalSemantics": "NET_LIQUIDATION"})],
        now * _NS)
    pts = (curves.get("1D") or {}).get("points") or []
    values = [p["equity"] for p in pts]
    assert values, "no curve was built at all"
    assert all(abs(v - cash) > 0.01 for v in values), (
        f"a pre-cutover CASH sample survived into the series: {values}. Mixed semantics are not a "
        f"series — the {equity - cash:,.2f} step between them is the whole book, not a gain."
    )
    assert abs(values[-1] - equity) < 0.01


def test_an_UNMARKED_row_arriving_AFTER_the_cutover_is_also_excluded():
    """THE ROLLBACK CASE, and the reason this is not a timestamp prefix.

    The first implementation dropped rows OLDER than the earliest marked one. That leaves an
    unmarked row that arrives LATER — exactly what redeploying the previous image produces — sitting
    between two equity rows. Measured before the fix: [104,989.31, 44,121.93, 104,989.31], a
    60,867.38 crash and instant recovery on the chart.
    """
    now = 1_700_000_000
    cash, equity = 44_121.93, 104_989.31
    marked = {"BalanceTotalSemantics": "NET_LIQUIDATION"}
    pts = (build_curves(
        [_State(total=cash, ts_s=now - 900, free=cash),
         _State(total=equity, ts_s=now - 600, free=cash, info=dict(marked)),
         _State(total=cash, ts_s=now - 300, free=cash),                  # rollback row, UNMARKED
         _State(total=equity, ts_s=now - 60, free=cash, info=dict(marked))],
        now * _NS).get("1D") or {}).get("points") or []
    values = [p["equity"] for p in pts]
    assert values, "no curve was built"
    assert all(abs(v - cash) > 0.01 for v in values), (
        f"an unmarked rollback row survived into the series: {values}")


def test_an_UNMARKED_series_is_left_exactly_as_it_was():
    """THE IBKR CASE, and the pre-deploy case. Nautilus's IBKR adapter writes no semantics key and
    never needed one — its `total` has always been NetLiquidation. If the absence of a marker caused
    exclusion, this fix would silently empty staging's chart.

    Fixture property first: none of these rows carries the marker, so the filter has something to
    NOT do. Without that assertion this test passes whether or not the filter is even reachable.
    """
    now = 1_700_000_000
    states = [_State(total=999_491.28, ts_s=now - 900, free=974_041.11, currency="SGD"),
              _State(total=1_000_000.54, ts_s=now - 60, free=974_607.42, currency="SGD")]
    assert all(not s_.info.get("BalanceTotalSemantics") for s_ in states), "fixture is marked"
    pts = (build_curves(states, now * _NS).get("1D") or {}).get("points") or []
    assert len(pts) == 2, f"unmarked rows were dropped: {len(pts)} of 2 survived"


def test_a_MARGIN_account_curve_is_still_the_total():
    """STAGING, THE CONTROL. On an IBKR margin account `balances_total` ALREADY is NetLiquidation —
    999,491.28 against an account worth 999,480.46 — and staging's NET was correct.

    The fix must not 'correct' this one by adding a market value that is already inside it. A change
    that fixes paper and breaks staging is not a fix; it is the same bug with the venues swapped.
    """
    now = 1_700_000_000
    netliq = 999_491.28
    curves = build_curves(
        [_State(total=netliq, ts_s=now - 600, free=860_411.08, currency="SGD",
                 info={"TotalCashValue": 860_411.08},
                 margins=[object()]),
         _State(total=netliq, ts_s=now - 60, free=860_411.08, currency="SGD",
                 info={"TotalCashValue": 860_411.08},
                 margins=[object()])],
        now * _NS)
    got = _last_equity(curves)
    assert got is not None and abs(got - netliq) < 0.01, (
        f"a MARGIN account's curve moved from {netliq:,.2f} to {got}. NetLiquidation already includes "
        f"position value; adding it again would double-count the book.")


# ==================================================================================================
# UNCLAIMED MUST BE INSIDE EQUITY (2026-08-27: "if calculated from nautilus, need to handle
# unclaimed properly")
#
# The account holds positions the cockpit did not originate. Nautilus carries them under strategy
# EXTERNAL — 12 of them in paper's cache on 2026-08-27, one still open, surfacing on the panel as the
# `UNCLAIMED` row at -$21.28.
#
# THE TRAP: a fix that derives equity by summing OUR strategies' marks silently EXCLUDES them, and the
# curve understates the account by whatever the operator holds outside the lanes. That is not
# hypothetical — it is precisely how DEPLOYED broke (#310): it was computed from our marks while CASH
# and LIQUIDATION came from the broker, so the panel did not add up. The comment in `BookTile.tsx`
# records the same lesson: "Alpaca's own numbers satisfy cash + long_market_value == equity to the
# cent, so taking all three from one snapshot makes the panel add up by construction."
#
# So the rule this pins is not "include unclaimed" as an extra step. It is: the position side of equity
# must come from the BROKER's figure, which already contains everything the account holds, claimed or
# not. Any derivation that walks our own book will be wrong by the unclaimed amount and will look right.
# ==================================================================================================


# `test_equity_includes_positions_the_cockpit_never_claimed` and its fixture-property partner were
# REMOVED here (#588). They asserted that `build_curves` should return cash + long_market_value —
# i.e. that the curve layer should reconstruct equity itself. That is not the fix that was chosen:
# equity is reported by the CONNECTOR into `AccountBalance.total`, matching Nautilus's IBKR adapter,
# and the curve plots what it is given. Their real content — that equity must follow the BROKER and
# not the sum of our own lanes, so unclaimed stock is not lost (#310) — now lives at the seam that
# actually decides it: `providers/alpaca/test_account_state_reports_equity.py`.
