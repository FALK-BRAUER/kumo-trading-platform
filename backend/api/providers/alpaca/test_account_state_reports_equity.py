"""`_report_account_state` must put EQUITY in `AccountBalance.total`, not cash (#588).

THE SEAM, NOT THE UNIT. The bug is not in a helper — it is in what the connector writes into a
Nautilus field, and in the fact that our connector and Nautilus's IBKR connector disagree about what
that field MEANS. So these tests drive the real `_report_account_state` and assert on the real
`AccountBalance` objects it constructs.

WHY total AND NOT SOMETHING ELSE. Measured 2026-08-27:

  - UI equity comes from `account.balances_total()`            (engine_node.py:6138)
  - the equity curve reads `state.balances_total`              (account_curve.py:44)
  - `Portfolio.equity()` is called NOWHERE in this repo or in the pinned kumo-trading-strategies, so its
    `balance.total + Σ unrealized_pnl` margin formula cannot double-count what we put here
  - strategy sizing reads `NautilusBroker.equity()`, which returns `strategy.broker_equity()` — the
    `broker.account` MSGBUS snapshot, a DIFFERENT plane. Order sizes do not move when total does.

Nautilus's own IBKR adapter (`interactive_brokers/execution.py:1667`) puts `NetLiquidation` in
`total` and `FullAvailableFunds` in `free`. That is the convention; ours was the outlier.
"""

from __future__ import annotations

import asyncio

import pytest

from api.providers.alpaca.exec_client import AlpacaExecutionClient

#: Measured off the live paper account, 2026-08-27. `cash` and `portfolio_value` DIFFER, which is
#: what makes the defect reachable at all — a fixture where they agree cannot distinguish the fix
#: from the bug, and "agreement is not connection" is exactly how this shipped.
_LIVE = {
    "id": "master",
    "cash": "44121.93",
    "portfolio_value": "104989.31",
    "equity": "104989.31",
    "buying_power": "346916.39",
    "long_market_value": "60867.38",
    "multiplier": "4",
    "last_equity": "104319.98",
}


class _Http:
    def __init__(self, account: dict):
        self._account = account

    async def get_account(self) -> dict:
        return self._account


class _Clock:
    def timestamp_ns(self) -> int:
        return 1_700_000_000_000_000_000


class _Log:
    def warning(self, *a, **k) -> None: ...
    def error(self, *a, **k) -> None: ...
    def info(self, *a, **k) -> None: ...


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


class _Driver:
    """The five attributes `_report_account_state` actually touches, and nothing else.

    `generate_account_state` is a SPY, not a stand-in for Nautilus: the objects it captures are the
    real `AccountBalance`/`Money` instances production built, so a float where Nautilus demands a
    Decimal still raises inside the code under test rather than being smoothed over here.
    """

    def __init__(self, account: dict):
        self._http = _Http(account)
        self._clock = _Clock()
        self._log = _Log()
        self._msgbus = _Bus()
        self.account_id = None
        self.balances: list = []
        self.margins: list = []
        self.reported: bool | None = None
        self.info: dict = {}

    def _set_account_id(self, account_id) -> None:
        self.account_id = account_id

    def generate_account_state(self, balances, margins, reported, ts_event, info=None) -> None:
        # `info` IS PART OF THE REAL SIGNATURE (`execution/client.pyx:329`, `dict info = None`).
        # The first version of this double omitted it and production raised TypeError — which is the
        # double reporting a defect in itself, not in the code under test.
        self.balances, self.margins, self.reported = balances, margins, reported
        self.info = info or {}


def _drive(account: dict) -> _Driver:
    d = _Driver(account)
    asyncio.run(AlpacaExecutionClient._report_account_state(d))
    return d


# ==================================================================================================
# The fixture's own properties first. A test whose fixture cannot express the bug is vacuous.
# ==================================================================================================

def test_the_fixture_can_tell_cash_apart_from_equity():
    cash, equity = float(_LIVE["cash"]), float(_LIVE["portfolio_value"])
    assert cash != equity, "fixture cannot distinguish the bug from the fix"
    assert equity - cash == pytest.approx(60_867.38, abs=0.01), (
        "the gap IS the book: long_market_value 60,867.38 on 2026-08-27"
    )


def test_the_driver_actually_reaches_the_connector():
    """If `_report_account_state` never ran, every assertion below would be vacuously true."""
    d = _drive(_LIVE)
    assert d.reported is True, "generate_account_state was never called"
    assert d.balances, "no balances were generated"
    assert str(d.account_id) == "ALPACA-master"


# ==================================================================================================
# THE DEFECT
# ==================================================================================================

def test_total_is_EQUITY_not_cash():
    """THE BUG. `total = cash` made the Home equity curve plot the cash balance.

    On 2026-08-27 that put the account at 44,121.93 against a real 104,989.31, and because
    `NET = equity - base_value` subtracts a base built from the same wrong series, NET · 1D read
    $72,372.19 on an account that had actually moved +$669.33.
    """
    total = _drive(_LIVE).balances[0].total
    assert float(total) == pytest.approx(104_989.31, abs=0.01), (
        f"total is {float(total):,.2f}. That is the CASH balance. Equity is portfolio_value = "
        f"104,989.31 — what Nautilus's IBKR adapter puts in this same field as NetLiquidation."
    )


def test_free_is_STILL_cash_because_the_risk_engine_gates_on_it():
    """DELIBERATELY UNCHANGED, and this test exists so it stays that way (#590).

    `risk/engine.pyx:696` admits orders against `account.balance_free()`. On 2026-08-27 cash was
    44,121.93 and buying_power 346,916.39 — aligning `free` with IBKR's `FullAvailableFunds` would
    have loosened the live order gate 7.86x on a tenant with KUMO_ORDERS_ARMED=true. A display fix
    must not carry that.
    """
    free = _drive(_LIVE).balances[0].free
    assert float(free) == pytest.approx(44_121.93, abs=0.01), (
        f"free moved to {float(free):,.2f}. It must stay cash — see #590."
    )


def test_cash_survives_so_nothing_downstream_has_to_re_derive_it():
    """Once `total` stops being cash, cash has no other home on this plane. IBKR keeps it in
    `info["TotalCashValue"]` (`execution.py:1667`); we follow that rather than inventing a key."""
    b = _drive(_LIVE).balances[0]
    locked = float(b.total) - float(b.free)
    assert locked == pytest.approx(60_867.38, abs=0.01), (
        "total - free should be the market value of the book"
    )


def test_a_SHORT_book_does_not_produce_a_negative_locked():
    """codex, scope review: cash can EXCEED net-liq (shorts, or a debit balance), and `locked` is
    not meaningfully negative. The clamp is deliberate; this pins it so it is not lost."""
    short = dict(_LIVE, cash="120000.00", portfolio_value="100000.00", equity="100000.00")
    d = _drive(short)
    b = d.balances[0]
    assert float(b.locked) >= 0.0, f"locked went negative: {float(b.locked)}"
    # Nautilus enforces this on construction; assert it here so the reason survives the next edit.
    assert float(b.total) - float(b.locked) == pytest.approx(float(b.free), abs=0.01)
    # FREE is what clamps, and it clamps DOWN — the order gate gets the smaller number (#590).
    assert float(b.free) == pytest.approx(100_000.00, abs=0.01), (
        "free must clamp to net-liq when cash exceeds it, never above"
    )
    # The real condition stays observable rather than being erased by the clamp.
    assert d.info["RawLockedCashValue"] == pytest.approx(-20_000.00, abs=0.01)


@pytest.mark.parametrize("bad,why", [
    ({"portfolio_value": "0", "equity": "0"}, "zero is not a degraded equity, it is a wrong one"),
    ({"portfolio_value": "", "equity": ""}, "empty strings must name the missing input, not raise Decimal"),
    ({"portfolio_value": "abc", "equity": "abc"}, "unparseable must not become a number"),
    ({"portfolio_value": None, "equity": None}, "absent"),
])
def test_an_UNUSABLE_equity_raises_and_says_which_input_was_missing(bad, why):
    """MEASURED, not imagined. The first implementation used `account.get(a) or account.get(b)` and
    `portfolio_value="0"` published total=0.00 AND free=0.00 — a zero account, silently, on the
    channel the daily-loss halt reads. An `or` chain cannot tell absent from unparseable from zero.
    """
    with pytest.raises(ValueError, match="no usable equity"):
        _drive(dict(_LIVE, **bad))
    assert why


def test_a_present_but_EMPTY_portfolio_value_falls_through_to_equity():
    """The one case that SHOULD degrade: Alpaca omits one field but supplies the other."""
    b = _drive(dict(_LIVE, portfolio_value="")).balances[0]
    assert float(b.total) == pytest.approx(104_989.31, abs=0.01)


def test_cash_EQUAL_to_equity_is_a_flat_book_not_an_error():
    """A funded account holding nothing. locked must be exactly zero, and free must stay cash."""
    b = _drive(dict(_LIVE, cash="104989.31")).balances[0]
    assert float(b.locked) == pytest.approx(0.0, abs=0.01)
    assert float(b.free) == pytest.approx(104_989.31, abs=0.01)


def test_the_old_missing_equity_case_still_raises():
    """A fallback here would be a silent wrong answer: reporting cash as equity is the very defect,
    and #382 already established that publishing no number beats publishing a known-wrong one."""
    blind = {k: v for k, v in _LIVE.items() if k not in ("portfolio_value", "equity")}
    with pytest.raises(Exception):
        _drive(blind)


def test_a_refused_snapshot_at_CONNECT_does_not_stop_the_client_connecting():
    """THE BLAST RADIUS OF THE RAISE, and it is not obvious from the raise alone.

    `_report_account_state` is called from two places: the refresh loop, which wraps it, and
    `_connect`, which did NOT. Once the function started raising on an unusable equity, a bad payload
    at boot would propagate out of `_connect` and the Alpaca exec client would never connect —
    the same shape as the reconciliation failure that already made a node inert once.

    A node that will not start is worse than a chart that is briefly empty. So `_connect` logs an
    ERROR and continues; the refresh loop retries. What it must never do is publish cash as equity.
    """
    import inspect

    from api.providers.alpaca import exec_client as mod

    src = inspect.getsource(mod.AlpacaExecutionClient._connect)
    assert "await self._report_account_state()" in src
    # The call must sit inside a try, or a refused snapshot takes the whole client down.
    call = src.index("await self._report_account_state()")
    assert "try:" in src[:call], (
        "_connect calls _report_account_state OUTSIDE a try — an unusable account payload at boot "
        "now stops the exec client connecting entirely (#588)"
    )
    assert "self._log.error" in src[call:call + 600], "the degraded path must report itself LOUDLY"


@pytest.mark.parametrize("poison", ["NaN", "nan", "Infinity", "-Infinity"])
def test_a_NON_FINITE_equity_is_refused_not_compared(poison):
    """THE THIRD TIME A NON-FINITE NUMBER HAS WALKED THROUGH A GUARD IN THIS STACK.

    `Decimal("NaN")` parses fine, so it survives the try/except around the parse — and then
    `Decimal("NaN") > 0` RAISES InvalidOperation from a line that reads like a plain comparison.
    Infinity is worse: it compares True and would be accepted as an equity.

    kumo-trading-strategies 43c6d3e is the same bug in the other direction: `broker_equity` returned NaN and
    `nan <= 0` is False, so the daily-loss halt — the one automatic stop the momentum family has —
    could never fire. Check finiteness; never just the sign.
    """
    with pytest.raises(ValueError, match="no usable equity"):
        _drive(dict(_LIVE, portfolio_value=poison, equity=poison))


def test_a_NON_FINITE_cash_does_not_poison_the_order_gate():
    """`cash` decides `free`, which is what `risk/engine.pyx:696` admits orders against. A NaN there
    would raise inside the clamp comparison; an Infinity would clamp `free` to total silently."""
    b = _drive(dict(_LIVE, cash="NaN")).balances[0]
    assert float(b.free) == pytest.approx(0.0, abs=0.01), "a non-finite cash must not become free"
    assert float(b.total) == pytest.approx(104_989.31, abs=0.01), "equity is still readable"
