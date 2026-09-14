"""Cash is not equity while anything is held (#382).

WHY THIS FILE EXISTS
--------------------
The health monitor announced, as a CHANGE event:

    [CHANGE] 08-20 00:29 ET closed | eq $77,480.59 day +0.00 | 3 held 3 stops

Equity at that moment was $103,229.30. $77,480.59 was CASH:

    equity             103229.30
    cash                77480.59
    long_market_value   25748.71      77480.59 + 25748.71 = 103229.30

So the alarm channel reported a $25,748 loss — 25% of the book — that never happened, and paired it with
`day +0.00`, which is the other half of the tell.

`_publish_account`'s fallback branch read `equity = cash`, then overwrote it from the Nautilus Portfolio
when that worked. When it did not — the venue-mismatch case this file documents elsewhere, where
Portfolio holds no account for XNYS/XNAS — cash was published under the name `equity`.

WHAT MAKES IT WRONG IS THE ACCOUNT'S OWN CONTRACT. `AccountDTO` states the invariant
`cash + long_market_value == equity`. The fallback satisfied it by omitting the term that falsifies it.

The window is a restart and it self-corrects in seconds. That is what makes it dangerous rather than
cosmetic: it fires on every deploy, and an alarm that cries a 25% loss every deploy is one an operator
stops reading.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.objects import Currency

from api.engine_node import UiFeedStrategy

CASH = 77_480.59
TRUE_EQUITY = 103_229.30
MARKET_VALUE = 25_748.71


class _Account:
    id = SimpleNamespace(get_issuer=lambda: "ALPACA")

    def balance_free(self, _ccy=None):
        return CASH

    def balances_free(self):
        return {USD: CASH}

    def balance_total(self, _ccy=None):
        """CASH, because that is what cockpit's Alpaca client puts in `AccountBalance.total`.

        WITHOUT THIS METHOD THE #382 TEST BELOW PASSED FOR THE WRONG REASON. Trusting `total` on every
        venue — the mutation this file must catch — left it green, because the double simply had no
        such attribute and the read raised instead of being refused. A double that cannot represent
        production cannot discriminate, and an undiscriminating test is the thing it was written to
        prevent (mutation-bitten 2026-08-24).
        """
        return CASH


def _strat(*, positions_open: list, portfolio_equity: float | None):
    """A double built from what production actually holds at this point in a restart.

    `_broker_account` is None because that is the whole scenario: the fallback only runs before the
    broker snapshot has arrived.
    """
    published: list[tuple[str, dict]] = []

    class _Portfolio:
        def equity(self, _venue):
            # RETURNS A DICT, because Nautilus does: `dict[Currency, Money]`. This double returned a
            # bare float, and that is why `equity = float(eq)` in production went unnoticed for as long
            # as it did — against this double it worked, and against Nautilus it raised TypeError into
            # a DEBUG log. Pinned by test_the_double_above_LIES_about_what_Portfolio_equity_returns.
            if portfolio_equity is None:
                raise RuntimeError("no account registered for XNYS")  # what production actually raises
            return {USD: portfolio_equity}

    s = SimpleNamespace(
        # `_publish_account` gained the boot-gate call — #440 had no caller at all until 2026-08-23,
        # which codex found. A host that cannot answer it cannot represent production.
        _run_boot_gate_once=lambda _equity: None,
        _broker_account=None,
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        portfolio=_Portfolio(),
        cache=SimpleNamespace(accounts=lambda: [_Account()], positions_open=lambda: positions_open),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        # production has a msgbus and `_publish_account` now puts the DERIVED snapshot on it
        # so strategies can see it; a host without one cannot represent production.
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    s._publish = lambda kind, payload, **kw: published.append((kind, payload))
    return s, published


def test_the_fixture_reproduces_the_state_that_shipped_the_false_alarm():
    """THE FIXTURE'S OWN PROPERTY FIRST.

    If `portfolio.equity` succeeded in this double, the fallback would never be reached and every
    assertion below would pass against a build that still publishes cash as equity.
    """
    s, _ = _strat(positions_open=[object()], portfolio_equity=None)
    try:
        s.portfolio.equity("ALPACA")
        raise AssertionError("the double no longer reproduces the failing branch")
    except RuntimeError:
        pass
    assert s._broker_account is None, "the broker path would bypass the branch under test"


def test_it_publishes_nothing_rather_than_cash_when_positions_are_held():
    """The bug, exactly. Three positions held, no portfolio equity — the old code published 77,480.59."""
    s, published = _strat(positions_open=[object(), object(), object()], portfolio_equity=None)
    UiFeedStrategy._publish_account(s)
    assert published == [], (
        f"published {published} — cash under the name equity understates the book by the entire market "
        f"value of what is held, which is what produced the phantom $25,748 drop"
    )


def test_a_flat_book_still_reports_cash_as_equity_because_then_it_is_true():
    """The opposite failure: refusing to publish anything at all is not the fix.

    With nothing held, cash IS equity — no estimate involved — and the invariant holds with the term
    stated rather than omitted.
    """
    s, published = _strat(positions_open=[], portfolio_equity=None)
    UiFeedStrategy._publish_account(s)
    assert len(published) == 1
    kind, payload = published[0]
    assert kind == "account"
    assert payload["equity"] == CASH
    assert payload["long_market_value"] == 0.0
    assert payload["cash"] + payload["long_market_value"] == payload["equity"], (
        "AccountDTO's own invariant: cash + long_market_value == equity"
    )


def test_a_PORTFOLIO_derived_equity_is_NEVER_published_for_a_cash_in_total_adapter():
    """THIS TEST REPLACES ONE WHOSE PREMISE WAS IMPOSSIBLE.

    It used to assert that a "working portfolio" publishes real equity while positions are held. No
    adapter we run can produce that. `Portfolio.equity` computes, for a MARGIN account,

        balance.total + Σ unrealized_pnl(open positions)

    and cockpit's Alpaca client sets `AccountBalance(total=cash, ...)`
    (providers/alpaca/exec_client.py:423-425), so on the live paper account it yields

        cash 73,393.33 + unrealized 1,154.73 = 74,548.06     true equity 103,500.35   -> 28.0% LOW

    The old double handed the function a number no adapter could hand it, so the test passed and
    described a system that does not exist. Publishing that figure is #382 arriving by another route,
    on the channel the daily-loss halt anchors on.

    Holding anything, the honest answer is silence.
    """
    s, published = _strat(positions_open=[object()], portfolio_equity=TRUE_EQUITY)
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"], (
        "published a Portfolio-derived equity for an adapter whose balance.total is CASH")


def test_the_warning_is_once_per_outage_not_once_per_tick():
    """The account plane republishes continuously; a per-tick WARN would bury the log it warns in."""
    warnings: list[str] = []
    s, _ = _strat(positions_open=[object()], portfolio_equity=None)
    s.log = SimpleNamespace(warning=lambda m, *a, **k: warnings.append(str(m)))
    for _ in range(5):
        UiFeedStrategy._publish_account(s)
    assert len(warnings) == 1, f"warned {len(warnings)} times across 5 ticks"
    assert "not zero" in warnings[0], "the warning does not say the market value is MISSING, not zero"


# ---------------------------------------------------------------------------------------------
# IBKR: the #382 guard is PERMANENT here, not transient, and the fallback that should have saved
# it cannot run. Both halves measured on staging-ibkr, 2026-08-24.
# ---------------------------------------------------------------------------------------------

IB_NET_LIQUIDATION = 138_402.11   # what IB reports as NetLiquidation -> AccountBalance.total
IB_AVAILABLE_FUNDS = 40_115.02    # FullAvailableFunds -> AccountBalance.free. NOT cash, and NOT equity.


def test_the_double_above_LIES_about_what_Portfolio_equity_returns():
    """The existing double returns a float. Nautilus returns dict[Currency, Money].

    This is why nobody caught that the fallback's `equity = float(eq)` can never succeed: `float()`
    on a dict raises TypeError, the caller swallows it at DEBUG, and equity silently stays None. On
    Alpaca the branch is never reached because the broker snapshot arrives, so the dead code was
    invisible until an IBKR node had nothing else to fall back to.

    Pinned against the INSTALLED package rather than memory — the docstring is the contract.
    """
    from nautilus_trader.portfolio.portfolio import Portfolio

    doc = Portfolio.equity.__doc__ or ""
    assert "dict[Currency, Money]" in doc, (
        "Portfolio.equity's return contract moved; the doubles in this file encode the old one")
    import pytest

    with pytest.raises(TypeError):
        float({"USD": 1.0})   # what the production line actually does with that dict


class _IBAccount:
    """An IB account as the adapter really builds it (execution.py:1668).

    `total` is NetLiquidation and `free` is FullAvailableFunds — so on this venue `total` IS equity,
    which is the opposite of cockpit's Alpaca client, where `total` holds CASH. The double must carry
    that difference or the test cannot tell the two apart.
    """

    id = SimpleNamespace(get_issuer=lambda: "INTERACTIVE_BROKERS")

    #: The account's balance legs, keyed by currency, exactly as `Account.balances_total()` returns
    #: them. NOT `balance_total(USD)`: the live IB paper account reports a SINGLE SGD leg
    #: (`base_currency=None`), so a USD-keyed read finds nothing at all.
    legs = {USD: IB_NET_LIQUIDATION}

    def balance_free(self, ccy=None):
        """RAISES for a currency this account does not hold, because Nautilus does.

        The earlier version returned a number for ANY currency, so it could not express the live
        staging account — SGD only — and it hid a real defect: `_publish_account` reads
        `balance_free(USD)` BEFORE any equity logic and returns silently when that raises. The IB tests
        passed while production returned before reaching the code they were testing.
        """
        if ccy is not None and ccy not in self.legs:
            raise ValueError(f"no {ccy} balance on this account")
        return IB_AVAILABLE_FUNDS

    def balances_free(self):
        """FullAvailableFunds per leg — the SAME currencies `balances_total` reports, because they come
        from one `AccountBalance` per currency (interactive_brokers/execution.py:1671)."""
        return {ccy: IB_AVAILABLE_FUNDS for ccy in self.legs}

    def balances_total(self):
        return dict(self.legs)


def _ib_strat(*, positions_open: list, legs=None):
    published: list[tuple[str, dict]] = []

    class _Portfolio:
        def equity(self, _venue):
            # Production: the account is registered under venue INTERACTIVE_BROKERS while every
            # instrument is XNYS/XNAS, so the venue-keyed lookup finds nothing. Returning an empty
            # dict is what Nautilus does, and it is falsy — the caller's `is not None` check passes it
            # straight into float() regardless.
            return {}

    account = _IBAccount()
    if legs is not None:
        account.legs = legs
    warnings: list[str] = []
    s = SimpleNamespace(
        _run_boot_gate_once=lambda _equity: None,
        _broker_account=None,
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        portfolio=_Portfolio(),
        cache=SimpleNamespace(accounts=lambda: [account], positions_open=lambda: positions_open),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        # production has a msgbus and `_publish_account` now puts the DERIVED snapshot on it
        # so strategies can see it; a host without one cannot represent production.
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda m, *a, **k: warnings.append(str(m))),
    )
    s._publish = lambda kind, payload, **kw: published.append((kind, payload))
    return s, published, warnings


def test_an_ibkr_node_holding_positions_publishes_NET_LIQUIDATION_not_silence():
    """THE STAGING BLOCKER. No account frame -> no equity on the bus -> `broker_equity()` RAISES
    ("refusing to evaluate the daily-loss limit against an unknown equity") -> the lane's session dies
    before it forms an order. Armed, TRADING, correct in every log line, and trading nothing.

    #382 refuses to publish cash as equity while positions are held, which is right on Alpaca where
    the gap is a few seconds after a restart. On IBKR the venue-keyed lookup NEVER succeeds, so the
    refusal is permanent and the node has no equity for as long as it runs.
    """
    s, published, _w = _ib_strat(positions_open=[object()])
    UiFeedStrategy._publish_account(s)

    frames = [p for kind, p in published if kind == "account"]
    assert frames, "an IBKR node holding positions published no account frame at all"
    assert frames[0]["equity"] == IB_NET_LIQUIDATION


def test_it_does_NOT_publish_available_funds_as_equity():
    """The discriminating half, and the failure I would otherwise have shipped.

    My first proposal was `equity = cash + marked positions` with `cash = balance_free(USD)`. On IBKR
    `balance_free` is FullAvailableFunds, not cash — so that sum would have been wrong in a way that
    still looks like a number. codex caught it before it was written.
    """
    s, published, _w = _ib_strat(positions_open=[object()])
    UiFeedStrategy._publish_account(s)
    frames = [p for kind, p in published if kind == "account"]
    assert frames[0]["equity"] != IB_AVAILABLE_FUNDS


def test_a_venue_whose_total_is_CASH_still_refuses_to_publish_it_as_equity():
    """#382 MUST SURVIVE. cockpit's Alpaca client puts CASH in `AccountBalance.total`, so trusting
    `total` everywhere would republish the exact number #382 removed — a 25% phantom loss on every
    deploy, on the channel the daily-loss halt trusts.
    """
    s, published = _strat(positions_open=[object()], portfolio_equity=None)
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"], (
        "an ALPACA account holding positions must publish nothing when equity is unknown")


def test_a_non_usd_leg_is_NOT_published_as_usd_equity():
    """Side-effect guard on my own change.

    `Portfolio.equity` returns dict[Currency, Money] and IB accounts are multi-currency
    (base_currency=None). Reading "whatever is first" would publish a EUR figure under the name
    `equity` — wrong rather than absent, on the channel the daily-loss halt anchors on. Absent is the
    honest answer, and #382 already handles it.
    """
    from nautilus_trader.model.currencies import EUR

    published: list[tuple[str, dict]] = []

    class _Portfolio:
        def equity(self, _venue):
            return {EUR: 91_000.0}          # a real leg, just not the one we report in

    s = SimpleNamespace(
        _run_boot_gate_once=lambda _equity: None,
        _broker_account=None,
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        portfolio=_Portfolio(),
        cache=SimpleNamespace(accounts=lambda: [_Account()], positions_open=lambda: [object()]),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        # production has a msgbus and `_publish_account` now puts the DERIVED snapshot on it
        # so strategies can see it; a host without one cannot represent production.
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    s._publish = lambda kind, payload, **kw: published.append((kind, payload))
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"], (
        "published an account frame from a non-USD leg")


SGD = Currency.from_str("SGD")
IB_SGD_NET_LIQUIDATION = 999_216.15   # the live staging-ibkr account, read off its AccountState


def test_the_real_ib_account_reports_a_single_SGD_leg_and_is_still_published():
    """THE ACTUAL STAGING ACCOUNT. Measured, not assumed:

        AccountState(account_id=INTERACTIVE_BROKERS-DUPTEST02, account_type=MARGIN,
                     base_currency=None,
                     balances=[AccountBalance(total=999_216.15 SGD, locked=9_000.66 SGD,
                                              free=990_215.49 SGD)])

    A USD-keyed read finds nothing here, so the first version of this fix would have published nothing
    and staging would still have been unable to trade — the same silence, one layer down. ni9q2dnz
    said to measure IB's balance rather than infer it from the adapter source; this is why.

    Publishing it unconverted is safe HERE and the reasons are specific: sizing takes
    `limits.allocated_equity` (the strategy's settings target, in USD), and the daily-loss halt
    compares this number against ITSELF at session start, so the unit cancels out of the ratio.
    """
    s, published, warnings = _ib_strat(positions_open=[object()],
                                       legs={SGD: IB_SGD_NET_LIQUIDATION})
    UiFeedStrategy._publish_account(s)
    frames = [p for kind, p in published if kind == "account"]
    assert frames, "the real IB account shape published nothing"
    assert frames[0]["equity"] == IB_SGD_NET_LIQUIDATION
    assert any("SGD" in w and "NOT dollars" in w for w in warnings), (
        f"publishing a non-USD figure under `equity` must say so exactly once: {warnings}")


def test_an_ambiguous_multi_currency_account_publishes_NOTHING():
    """The discriminating half of the currency rule. Two legs and no USD is a GUESS, and a guess on
    the channel the daily-loss halt anchors on is the #382 defect wearing a different unit."""
    s, published, _w = _ib_strat(positions_open=[object()],
                                 legs={SGD: 999_216.15, Currency.from_str("EUR"): 12_000.0})
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"]


def test_ALPACA_IS_NOT_IN_THE_ALLOW_LIST_AND_MUST_NOT_BE():
    """A guard on the allow-list itself, requested by ni9q2dnz, who owns the live Alpaca stack.

    Cockpit's Alpaca client puts CASH in `AccountBalance.total` (providers/alpaca/exec_client.py).
    Measured on the live account 2026-08-24:

        true equity   103,500.35
        cash           73,393.33      <- what `total` holds
        understatement 28,952.29      = 28.0% LOW

    #380 established that this is the channel the DAILY-LOSS HALT anchors on. A 28% phantom drop is
    not a wrong number on a screen; it is a plausible trigger to halt every strategy on the stack at a
    moment when nothing is wrong. Adding ALPACA here to "fix" some other symptom would do exactly
    that, and this test exists so the reason travels with the list rather than living in a review
    comment nobody reads in six months.
    """
    from api.engine_node import _TOTAL_IS_NET_LIQUIDATION

    assert "ALPACA" not in _TOTAL_IS_NET_LIQUIDATION, (
        "ALPACA puts CASH in AccountBalance.total — trusting it as net liquidation republishes the "
        "28.0% understatement #382 removed, on the channel the daily-loss halt anchors on")
    assert _TOTAL_IS_NET_LIQUIDATION == frozenset({"INTERACTIVE_BROKERS"}), (
        "the allow-list grew; every entry must be an adapter whose AccountBalance.total is NET "
        "LIQUIDATION, verified against that adapter's source, not assumed")


def test_alpaca_equity_equal_to_cash_while_holding_is_REFUSED():
    """codex's side effect, and the reason this fix is not simply 'read the dict properly'.

    `Portfolio.equity` starts from `balances_total()` and only ADDS unrealized PnL for positions it
    can PRICE. On Alpaca `total` is cash, so an unpriced book returns exactly the cash figure — and
    publishing it walks straight through the #382 guard, because equity is no longer None.

    `cash + long_market_value == equity` is the account's own contract: holding something,
    equity == cash says the market value term is MISSING, not zero.
    """
    s, published = _strat(positions_open=[object()], portfolio_equity=CASH)
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"], (
        "published cash as equity while positions were held — that is #382, and it was measured at "
        "28.0% low on the live stack")


def test_flat_and_priced_or_not_the_alpaca_path_never_derives_equity_itself():
    """Cover for the side effect of removing the branch: the FLAT case must still work.

    Flat, cash IS equity and no estimate is involved — that path is untouched and is the one legitimate
    way this fallback publishes for a cash-in-total adapter.
    """
    s, published = _strat(positions_open=[], portfolio_equity=TRUE_EQUITY)
    UiFeedStrategy._publish_account(s)
    frames = [p for kind, p in published if kind == "account"]
    assert frames and frames[0]["equity"] == CASH, (
        "flat: equity is cash, and it must still be published")


# ---------------------------------------------------------------------------------------------
# PREMISE PINS. Each of these encodes a fact this file's REASONING depends on. If one changes,
# the conclusions above stop following and somebody must revisit them — which is the whole point:
# the 28.0% argument is only valid while Alpaca puts cash in `total`.
# ---------------------------------------------------------------------------------------------

def test_premise_the_alpaca_client_now_puts_NET_LIQUIDATION_in_balance_total():
    """THE PREMISE MOVED (#588), and this test is the reconsideration its predecessor demanded.

    It used to assert `total=cash` and warn that if that ever changed, `Portfolio.equity` "may become
    usable for Alpaca and the branch removed above should be reconsidered rather than left removed by
    inertia". It changed. It was reconsidered. `Portfolio.equity` is STILL not usable:

        margin formula   balance.total + Σ unrealized_pnl
        total is now     net liquidation, which ALREADY contains that unrealized

    so it now OVERSTATES by roughly the unrealized instead of understating by the book. The removal
    stands, for the opposite reason. Arithmetic pinned in
    `venues/test_doubles_refuse_what_venues_refuse.py`.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent / "providers" / "alpaca" / "exec_client.py"
    text = src.read_text()
    assert "AccountBalance(total=cash," not in text, (
        "Alpaca is writing CASH into AccountBalance.total again — that is #588 reintroduced")
    assert "AccountBalance(total=total," in text and "portfolio_value" in text


def test_premise_portfolio_equity_adds_unrealized_for_margin_accounts():
    """The other half of the 28.0% argument, read off the installed package rather than remembered."""
    from nautilus_trader.portfolio.portfolio import Portfolio

    doc = Portfolio.equity.__doc__ or ""
    assert "balance.total + Σ unrealized_pnl(open positions)" in doc.replace("``", ""), (
        "Portfolio.equity's margin formula changed; the cash-in-total reasoning must be re-derived")


# ---------------------------------------------------------------------------------------------
# SIDE-EFFECT COVER. Things this change could plausibly have broken, asserted directly.
# ---------------------------------------------------------------------------------------------

def test_the_broker_snapshot_branch_is_untouched_and_wins():
    """The live Alpaca stack takes this branch on every tick. It must not consult the account at all.

    The double's account RAISES on every accessor, so any read of it fails the test loudly rather
    than quietly returning a plausible number.
    """
    class _Exploding:
        id = SimpleNamespace(get_issuer=lambda: "ALPACA")

        def balance_free(self, _ccy):
            raise AssertionError("the broker-snapshot branch must not read the account")

        def balances_total(self):
            raise AssertionError("the broker-snapshot branch must not read the account")

        def balances_free(self):
            raise AssertionError("the broker-snapshot branch must not read the account")

    published: list[tuple[str, dict]] = []
    s = SimpleNamespace(
        _run_boot_gate_once=lambda _equity: None,
        _broker_account={"equity": 103_500.35, "cash": 73_393.33, "buying_power": 4.0,
                         "multiplier": 4.0, "long_market_value": 30_107.02, "last_equity": 103_466.29,
                         "ts": 7},
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        portfolio=SimpleNamespace(equity=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no"))),
        cache=SimpleNamespace(accounts=lambda: [_Exploding()], positions_open=lambda: [object()]),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        # production has a msgbus and `_publish_account` now puts the DERIVED snapshot on it
        # so strategies can see it; a host without one cannot represent production.
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    s._publish = lambda kind, payload, **kw: published.append((kind, payload))
    UiFeedStrategy._publish_account(s)
    frames = [p for kind, p in published if kind == "account"]
    assert frames and frames[0]["equity"] == 103_500.35
    assert frames[0]["last_equity"] == 103_466.29, "the broker's own prior close must pass through"


def test_exactly_one_account_frame_per_call():
    """Cover: a fallback that falls through into the publish below would emit two frames, and the UI
    would flicker between two different equities without either being wrong on its own."""
    s, published, _w = _ib_strat(positions_open=[object()], legs={SGD: IB_SGD_NET_LIQUIDATION})
    UiFeedStrategy._publish_account(s)
    assert len([p for kind, p in published if kind == "account"]) == 1


def test_the_foreign_currency_warning_is_once_per_outage_not_once_per_tick():
    """Cover: this runs on every account update. `Cannot calculate unrealized PnL` once flooded 93% of
    the log at 2s intervals and buried the only signal that tells a working strategy from a stuck one.
    """
    s, published, warnings = _ib_strat(positions_open=[object()], legs={SGD: IB_SGD_NET_LIQUIDATION})
    for _ in range(5):
        UiFeedStrategy._publish_account(s)
    assert len([w for w in warnings if "SGD" in w]) == 1, warnings
    assert len([p for kind, p in published if kind == "account"]) == 5, (
        "warning once must NOT mean publishing once — the frame is per tick")


def test_the_boot_gate_receives_the_published_equity_and_only_when_there_is_one():
    """Cover: `should_run` refuses an equity-less snapshot and the flag is spent on first use. If the
    IB path published without calling the gate, preflight would never run on that node."""
    seen: list = []
    s, published, _w = _ib_strat(positions_open=[object()], legs={USD: IB_NET_LIQUIDATION})
    s._run_boot_gate_once = seen.append
    UiFeedStrategy._publish_account(s)
    assert seen == [IB_NET_LIQUIDATION]

    seen2: list = []
    s2, published2, _w2 = _ib_strat(positions_open=[object()], legs={})
    s2._run_boot_gate_once = seen2.append
    UiFeedStrategy._publish_account(s2)
    assert seen2 == [], "no equity means the gate must not be spent on it"


def test_an_account_with_no_balances_publishes_nothing_and_does_not_raise():
    """Cover: an IB node's first frames arrive before the account summary completes."""
    s, published, _w = _ib_strat(positions_open=[object()], legs={})
    UiFeedStrategy._publish_account(s)
    assert not [p for kind, p in published if kind == "account"]
