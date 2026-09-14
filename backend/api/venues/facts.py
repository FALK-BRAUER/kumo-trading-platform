"""What Alpaca and Interactive Brokers actually do — as data, with provenance.

Read `README.md` first. The short version: doubles are built FROM this, so a double that accepts what
the venue refuses cannot be written by accident.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["ALPACA", "IBKR", "VENUES", "Fact", "Provenance", "VenueFacts", "fact"]


class Provenance(str, Enum):
    """HOW a fact was established. A fact without this cannot be rechecked when it stops being true."""

    MEASURED = "measured"   #: read off the live venue or a running container
    SOURCE = "source"       #: read from the installed package — cannot be wrong about our pinned version
    INCIDENT = "incident"   #: inferred from a production failure, with date and cost


@dataclass(frozen=True)
class Fact:
    """One thing a venue does, and how we know."""

    key: str
    value: object
    how: Provenance
    #: Where to look to recheck it. A file:line, a container command, or the incident date.
    evidence: str
    #: What it cost us, when it cost something. Empty for facts nobody has been bitten by yet.
    cost: str = ""


def fact(key: str, value: object, how: Provenance, evidence: str, cost: str = "") -> Fact:
    return Fact(key=key, value=value, how=how, evidence=evidence, cost=cost)


@dataclass(frozen=True)
class VenueFacts:
    """Everything we know about one venue's behaviour, keyed for lookup."""

    name: str
    facts: tuple[Fact, ...] = field(default_factory=tuple)

    def __getitem__(self, key: str) -> object:
        for f in self.facts:
            if f.key == key:
                return f.value
        raise KeyError(
            f"{self.name} has no recorded fact {key!r}. Do not guess: measure it, then add it here "
            f"with its provenance. A double inventing this value is how six of them lied on "
            f"2026-08-24.")

    def evidence_for(self, key: str) -> Fact:
        for f in self.facts:
            if f.key == key:
                return f
        raise KeyError(key)

    def keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.facts)


# ---------------------------------------------------------------------------------------------
# ALPACA — our own out-of-tree execution client (`api/providers/alpaca/exec_client.py`).
# ---------------------------------------------------------------------------------------------
ALPACA = VenueFacts(
    name="ALPACA",
    facts=(
        fact(
            "balance_total_is", "net_liquidation", Provenance.SOURCE,
            "api/providers/alpaca/exec_client.py — AccountBalance(total=<portfolio_value>, ...)",
            "CHANGED BY #588 (2026-08-27), and this fact is the record of it. It WAS `cash`, which "
            "made the cockpit's equity curve plot 44,121.93 against a real 104,989.31 and put NET-1D "
            "at $72,372.19 on an account that had moved +$669.33. Nautilus's own IBKR adapter has "
            "always written NetLiquidation into this field; ours was the outlier, and the docstring "
            "claiming `AccountState` models cash only was simply untrue for a margin account. "
            "The two venues now AGREE here — see the conformance test, which asserts that.",
        ),
        fact(
            "balance_free_is", "cash", Provenance.SOURCE,
            "api/providers/alpaca/exec_client.py — free and total are BOTH the cash figure, "
            "locked=0",
            "Because free == total here, an `equity != cash` guard is meaningful on Alpaca and "
            "meaningless on IB, where they are different numbers.",
        ),
        fact(
            "account_currencies", ("USD",), Provenance.SOURCE,
            "api/providers/alpaca/exec_client.py — Money(..., USD), single leg",
        ),
        fact(
            "reserves_shares_against_resting_stop", True, Provenance.INCIDENT,
            "kumo-strategies#48 / kumo-cockpit#358 — FSM and VCTR, 2026-07",
            "A plain SELL is refused while a protective stop reserves the quantity: `available: 0` "
            "means UNRESERVED is 0, not that nothing is held. The exit path must cancel the stop "
            "first (`release_for_exit`).",
        ),
        fact(
            "reduce_only_supported", True, Provenance.INCIDENT,
            "venue behaviour relied on by kumo-strategies broker.py:78 (reduce_only=is_exit)",
            "This is what covered for us while `position_id` was never passed on submit, so the "
            "RiskEngine's reduce-only check never ran on EITHER venue. Alpaca carried us; IB does "
            "not. Same key on both venues on purpose — the difference is the value, not the "
            "vocabulary, and a caller reading one and not the other is the bug.",
        ),
        fact(
            "oca_group_immutable_after_submit", True, Provenance.MEASURED,
            "Alpaca docs + probe, 2026-08-23: both legs are submitted together; a leg cannot be "
            "added to a resting order, and the type must always be limit",
        ),
        fact(
            "daily_bars_cover", "rth", Provenance.SOURCE,
            "Alpaca Market Data FAQ 'How are bars aggregated?' (read 2026-08-29): daily bars follow "
            "the SIP update rules, and conditions T (Extended Hours Trade) / U (Extended Trading "
            "Hours) update a DAILY bar's volume but never its open/close or high/low; "
            "api/providers/alpaca/http.py get_bars sends no session parameter",
            "#616: a premarket gap-reversal is absent from the Alpaca daily OHLC and present in the "
            "IBKR one, so two tenants charting one symbol disagree and neither looks wrong.",
        ),
    ),
)


# ---------------------------------------------------------------------------------------------
# IBKR — Nautilus's native adapter (`nautilus_trader/adapters/interactive_brokers/`).
# ---------------------------------------------------------------------------------------------
IBKR = VenueFacts(
    name="INTERACTIVE_BROKERS",
    facts=(
        fact(
            "balance_total_is", "net_liquidation", Provenance.SOURCE,
            "adapters/interactive_brokers/execution.py — "
            "total = Money(self._account_summary[currency]['NetLiquidation'], cur)",
            "The convention, and since #588 Alpaca matches it. It was the OPPOSITE until then, which "
            "is how a rule written for one venue was wrong for the other in the direction that looks "
            "like a number. Kept as a fact so the agreement stays asserted rather than assumed.",
        ),
        fact(
            "balance_free_is", "full_available_funds", Provenance.SOURCE,
            "adapters/interactive_brokers/execution.py — free = FullAvailableFunds",
            "NOT cash. A `cash + marked positions` estimate built on it is wrong and still looks like "
            "a number — codex caught this before it was written, 2026-08-24.",
        ),
        fact(
            "account_currencies", ("SGD",), Provenance.MEASURED,
            "AccountState on the live paper account, 2026-08-24: base_currency=None, "
            "balances=[AccountBalance(total=999_216.15 SGD, locked=9_000.66 SGD, free=990_215.49 SGD)]",
            "A USD-keyed read finds NOTHING on this account. The first version of the equity fix "
            "would have published nothing and staging still would not have traded.",
        ),
        fact(
            "account_venue_differs_from_instrument_venue", True, Provenance.MEASURED,
            "Portfolio: 'no account registered for venue=Venue(XNYS)' while the account is "
            "INTERACTIVE_BROKERS-DUPTEST02, 2026-08-24",
            "Instruments carry XNYS/XNAS/ARCX from the Alpaca data feed; the account is IB's. Any "
            "`portfolio.account(instrument.venue)` returns None for every instrument, forever. On "
            "Alpaca the two coincide, which is why nothing surfaced it for months.",
        ),
        fact(
            "reduce_only_supported", False, Provenance.SOURCE,
            "grep -rn reduce_only adapters/interactive_brokers/ -> 0 matches; okx, bybit, kraken have 2 "
            "files each. execution.py DOES raise for post_only and TrailingOffsetType.",
            "Not supported AND not refused — set on the Nautilus order, never read, never sent. It is "
            "the only thing between an exit and a naked short if the position moves between decision "
            "and fill, and IB paper permits shorts.",
        ),
        fact(
            "client_id_lock", True, Provenance.MEASURED,
            "error 326 'Unable to connect as the client id is already in use', 2026-08-24",
            "After a recreate the gateway holds the dead engine's session. The new engine retried for "
            "4 minutes and never recovered; only restarting the gateway cleared it — while `verify` "
            "PASSED, because it checks the API, not the exec client.",
        ),
        fact(
            "order_ref_truncated_at_last_colon", True, Provenance.SOURCE,
            "adapters/interactive_brokers/execution.py — order_ref = execution.orderRef.rsplit(':', 1)[0]",
            "IB appends its own suffix. Any client_order_id format carrying meaning after a colon "
            "loses it silently on the fill.",
        ),
        fact(
            "oca_group_immutable_after_submit", True, Provenance.MEASURED,
            "IB error 10326 'OCA group revision is not allowed' — Nautilus issue #2978",
        ),
        fact(
            "reserves_shares_against_resting_stop", False, Provenance.INCIDENT,
            "kumo-cockpit#430 — IB does not reserve shares against a resting order the way Alpaca "
            "does, which is why the `_await_shares_available` wait is venue-conditional rather than "
            "a bug to remove everywhere",
            "Recorded as FALSE rather than omitted: an absent fact and a false one read the same to a "
            "human and differently to `facts[...]`, which refuses the absent one. #430's narrow skip "
            "depends on this being false, so it belongs in the catalogue where it can be rechecked.",
        ),
        fact(
            "daily_bars_cover", "rth", Provenance.SOURCE,
            "api/providers/ibkr.py build_data (#875): RthDailyIBDataClientFactory rewrites use_rth "
            "to True for DAY aggregations on the IB client's get_historical_bars and "
            "subscribe_historical_bars; use_regular_trading_hours=False still governs every other "
            "bar type. WAS 'extended' under #616 (the installed adapter forwards the one config flag "
            "into every bar request, data.py:625); IB useRTH=1 daily bars equal Alpaca 1Day to the "
            "cent, extended highs sit up to 14% above them (#875 measurement)",
            "#616: the flag exists for the live price plane (prices must move outside RTH) and "
            "daily HISTORY inherits it as a side effect, so IBKR daily OHLC folds pre/post-market "
            "in while Alpaca's does not — same symbol, same window, two plausible answers.",
        ),
    ),
)

VENUES = {v.name: v for v in (ALPACA, IBKR)}
