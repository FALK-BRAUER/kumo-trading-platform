"""Every lie a double told on 2026-08-24, replayed against the doubles built from `facts.py`.

Six hand-written doubles produced six confident wrong answers in one session. Each test below is one
of them: it sets up the same situation and asserts the venue-derived double now REFUSES, or answers
the way the venue actually answers.

If any of these ever passes for the wrong reason, the catalogue is wrong — not the test.
"""
from __future__ import annotations

import pytest

from api.venues.doubles import (
    NakedShortRefused,
    SharesReserved,
    UnsubscribedInstrument,
    account_for,
    broker_for,
)
from api.venues.facts import ALPACA, IBKR


class _Ccy:
    """Stands in for a Nautilus Currency, which the account keys on by code."""

    def __init__(self, code):
        self.code = code


USD, SGD = _Ccy("USD"), _Ccy("SGD")


# ---------------------------------------------------------------------------------------------
# LIE 1 — `balance_free` answered ANY currency.
# Cost: hid that `_publish_account` returned three lines above the code its tests covered, so the
# IBKR equity fix was correct and unreachable and staging still could not trade.
# ---------------------------------------------------------------------------------------------

def test_a_currency_the_account_does_not_hold_is_REFUSED():
    ib = account_for("INTERACTIVE_BROKERS", net_liquidation=999_216.15, available_funds=990_215.49)
    with pytest.raises(ValueError, match="no USD balance"):
        ib.balance_total(USD)
    assert ib.balance_total(SGD) == 999_216.15, "the currency it DOES hold must still answer"


def test_the_live_ib_account_is_not_denominated_in_usd():
    """Measured, and the reason a USD-only fix published nothing at all."""
    assert IBKR["account_currencies"] == ("SGD",)
    assert "USD" not in account_for("INTERACTIVE_BROKERS", net_liquidation=1.0).balances_total()


# ---------------------------------------------------------------------------------------------
# LIE 2 — `_Account` had no `balance_total`, so "trust total everywhere" left the file green.
# The doubles cannot omit it, and the two venues disagree about what it MEANS.
# ---------------------------------------------------------------------------------------------

def test_balance_total_means_THE_SAME_THING_on_both_venues_since_588():
    """CROSS-VENUE CONFORMANCE. This test USED to assert the two venues disagreed, and that
    disagreement was the defect (#588): one consumer reading `balance_total` got equity on IBKR and
    cash on Alpaca, so the Home curve was right on staging and wrong on paper.

    The fixture's own property first — the two numbers must differ, or "agreement" is unfalsifiable
    here and a double returning either one would pass.
    """
    alpaca = account_for("ALPACA", cash=73_393.33, net_liquidation=103_500.35)
    ib = account_for("INTERACTIVE_BROKERS", cash=1.0, net_liquidation=999_216.15)
    assert 73_393.33 != 103_500.35 and 1.0 != 999_216.15, "fixture cannot tell cash from net-liq"

    assert alpaca.balance_total(USD) == 103_500.35, "Alpaca must now put NET LIQUIDATION in total"
    assert ib.balance_total(SGD) == 999_216.15, "IB puts NET LIQUIDATION in total"
    assert ALPACA["balance_total_is"] == IBKR["balance_total_is"] == "net_liquidation", (
        "the two venues disagree about balance_total again — that disagreement IS #588, and any "
        "consumer of balances_total is now venue-dependent without saying so")


def test_balance_FREE_still_means_different_things_and_that_is_deliberate():
    """The other half, deliberately NOT unified (#590). `risk/engine.pyx:696` admits orders against
    `balance_free`; on 2026-08-27 Alpaca cash was 44,121.93 against buying_power 346,916.39, so
    aligning this field would loosen the live order gate 7.86x. It is its own decision, with its own
    ticket, and #588 must not smuggle it in."""
    assert ALPACA["balance_free_is"] == "cash", (
        "Alpaca's balance_free moved — that is an ORDER-PATH change, not a display change (#590)")


def test_portfolio_equity_is_STILL_unusable_on_alpaca_but_now_it_OVERSTATES():
    """#588 FLIPPED THE SIGN OF THIS ERROR; it did not remove it, and that is the point.

    `Portfolio.equity` for a MARGIN account is `balance.total + Σ unrealized_pnl`. Both tenants
    declare MARGIN.

        BEFORE #588   total = cash          73,393.33 + 1,154.73 = 74,548.06  vs true 103,500.35
                      -> 28.0% LOW, because the whole book was missing from `total`
        AFTER  #588   total = net-liq      103,500.35 + 1,154.73 = 104,655.08 vs true 103,500.35
                      -> 1.1% HIGH, because net-liq ALREADY contains the unrealized being added

    So the removal of the `Portfolio.equity` fallback (#502) stays correct, for the OPPOSITE reason.
    Both tripwire tests told whoever changed this to "reconsider rather than leave it removed by
    inertia" — this is that reconsideration, written down so the next reader does not repeat it.

    IT IS UNREACHABLE TODAY EITHER WAY: `Portfolio.equity` is called nowhere in this repo or in the
    pinned kumo-strategies (grepped 2026-08-27). This test pins the arithmetic, not a live path.
    """
    a = account_for("ALPACA", cash=73_393.33, net_liquidation=103_500.35)
    unrealized = 1_154.73
    assert a.balance_total(USD) == 103_500.35, "the double did not follow the fact change"
    portfolio_equity = a.balance_total(USD) + unrealized      # the margin formula, on this venue
    overstatement = portfolio_equity / 103_500.35 - 1
    assert round(overstatement, 3) == 0.011, (
        f"{overstatement:.1%} — net-liq already includes the unrealized this formula adds")
    # And the direction is what changed. Asserted so a silent revert to cash cannot pass this file.
    assert portfolio_equity > 103_500.35, "the error must now be an OVERstatement, not an under"


# ---------------------------------------------------------------------------------------------
# LIE 3 — `positions()` read the CLAIMS table, which goes stale.
# Cost: two lanes formed 156 BETA against 79 held, and 22 XLV against zero — a naked short.
# ---------------------------------------------------------------------------------------------

def test_a_sell_larger_than_the_position_is_REFUSED():
    b = broker_for("INTERACTIVE_BROKERS", own_positions={"BETA": 79}, account_positions={"BETA": 79})
    with pytest.raises(NakedShortRefused, match="BETA"):
        b.submit("BETA", "SELL", 156)
    assert b.submitted == [], "nothing may be recorded when the venue would have refused"


def test_a_sell_of_a_symbol_that_is_not_held_at_all_is_REFUSED():
    """XLV: claimed 11 by two lanes, held by neither."""
    b = broker_for("INTERACTIVE_BROKERS", own_positions={}, account_positions={})
    with pytest.raises(NakedShortRefused):
        b.submit("XLV", "SELL", 11)


def test_selling_exactly_what_is_held_is_allowed():
    """The discriminating half — a double that refused everything would pass the two tests above."""
    b = broker_for("INTERACTIVE_BROKERS", own_positions={"BETA": 79}, account_positions={"BETA": 79})
    b.submit("BETA", "SELL", 79)
    assert b.submitted == [{"symbol": "BETA", "side": "SELL", "qty": 79}]


# ---------------------------------------------------------------------------------------------
# LIE 4 — `strategy_positions()` returned {}, so the runner believed the book was empty and formed
# eight fresh BUYS instead of the exits it actually produces.
# ---------------------------------------------------------------------------------------------

def test_the_account_book_and_this_lane_s_share_are_different_questions():
    b = broker_for(
        "ALPACA",
        account_positions={"BETA": 79, "WHD": 56},   # what the BROKER holds, all lanes together
        own_positions={"BETA": 79},                  # what THIS lane is attributed
    )
    assert b.positions() != b.strategy_positions(), (
        "a double where these are the same cannot express a shared account, which is the only "
        "situation own_ceiling exists for")
    with pytest.raises(NakedShortRefused):
        b.submit("WHD", "SELL", 56)                  # held by the ACCOUNT, not by this lane


# ---------------------------------------------------------------------------------------------
# THE IBKR EXPOSURE — reduce_only is neither honoured nor rejected.
# ---------------------------------------------------------------------------------------------

def test_ib_DROPS_reduce_only_silently_and_the_double_records_it():
    """Not an error, which is exactly the danger: the order goes out unprotected and nothing says so."""
    b = broker_for("INTERACTIVE_BROKERS", own_positions={"AEM": 46}, account_positions={"AEM": 46})
    b.submit("AEM", "SELL", 46, reduce_only=True)
    assert b.dropped_reduce_only == ["AEM"], (
        "IB neither honours nor refuses reduce_only; a test relying on it for protection is relying "
        "on nothing")


def test_alpaca_does_not_drop_it():
    """The discriminating half: if BOTH venues dropped it, the flag would be meaningless everywhere
    and the fact would not be worth recording."""
    b = broker_for("ALPACA", own_positions={"AEM": 46}, account_positions={"AEM": 46})
    b.submit("AEM", "SELL", 46, reduce_only=True)
    assert b.dropped_reduce_only == []


# ---------------------------------------------------------------------------------------------
# THE SUBSCRIPTION GAP — measured on TECHIVOL-005, 2026-08-21: 8 orders, 8 errors.
# ---------------------------------------------------------------------------------------------

def test_selling_a_held_name_that_was_never_subscribed_is_REFUSED():
    b = broker_for(
        "ALPACA",
        own_positions={"XLV": 11}, account_positions={"XLV": 11},
        subscribed={"AAPL", "MSFT"},          # the universe, which no longer contains XLV
    )
    with pytest.raises(UnsubscribedInstrument, match="XLV"):
        b.submit("XLV", "SELL", 11)


def test_subscribing_what_we_hold_makes_the_same_exit_legal():
    """The fix in one assertion: universe UNION held."""
    b = broker_for(
        "ALPACA",
        own_positions={"XLV": 11}, account_positions={"XLV": 11},
        subscribed={"AAPL", "MSFT", "XLV"},
    )
    b.submit("XLV", "SELL", 11)
    assert b.submitted[-1]["symbol"] == "XLV"


# ---------------------------------------------------------------------------------------------
# ALPACA'S RESERVATION — `available: 0` means UNRESERVED is 0, not that nothing is held.
# ---------------------------------------------------------------------------------------------

def test_alpaca_refuses_a_sell_while_a_protective_stop_reserves_the_shares():
    b = broker_for("ALPACA", own_positions={"AEM": 46}, account_positions={"AEM": 46},
                   reserved={"AEM"})
    with pytest.raises(SharesReserved, match="AEM"):
        b.submit("AEM", "SELL", 46)


def test_ib_does_not_reserve_shares_that_way():
    """Why kumo-cockpit#430 is venue-conditional rather than a bug to fix everywhere."""
    assert IBKR["reserves_shares_against_resting_stop"] is False
    assert ALPACA["reserves_shares_against_resting_stop"] is True, (
        "if Alpaca stops reserving, `release_for_exit` and #430's venue split both need rechecking")
    b = broker_for("INTERACTIVE_BROKERS", own_positions={"AEM": 46},
                   account_positions={"AEM": 46}, reserved={"AEM"})
    b.submit("AEM", "SELL", 46)   # reserved on IB is not a refusal
    assert b.submitted[-1]["symbol"] == "AEM"
