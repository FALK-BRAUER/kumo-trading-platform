"""Test doubles BUILT FROM `facts.py`, so they cannot quietly disagree with the venue.

Every double here refuses what the real venue refuses. That is the whole point: on 2026-08-24 six
hand-written doubles each invented their own idea of the venue and every one produced a confident,
plausible, wrong answer. A double you configure by hand is a second implementation of the venue, and
two implementations of one thing drift.

Use these in tests instead of a `SimpleNamespace`. If a double here cannot express what you need,
MEASURE the venue and add the fact — do not widen the double.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from api.venues.facts import VENUES, VenueFacts

__all__ = [
    "NakedShortRefused",
    "SharesReserved",
    "UnsubscribedInstrument",
    "VenueAccount",
    "VenueBroker",
    "account_for",
    "broker_for",
]


class NakedShortRefused(AssertionError):
    """A sell larger than the position. On a book with no short permission this is not an order."""


class UnsubscribedInstrument(AssertionError):
    """Nautilus refuses a symbol the node never subscribed — including one the lane HOLDS."""


class SharesReserved(AssertionError):
    """Alpaca: `available: 0` means UNRESERVED is 0, not that nothing is held."""


@dataclass
class VenueAccount:
    """An account that answers like the venue's, including where it REFUSES to answer.

    Built from facts rather than configured: `balance_total` means cash on Alpaca and net liquidation
    on IB, and the double will not let a test pretend otherwise.
    """

    venue: VenueFacts
    #: {currency: amount}. Defaults to the venue's own currency layout, which is USD for Alpaca and
    #: SGD for the live IB paper account — a USD-keyed read finds NOTHING there.
    legs: dict = field(default_factory=dict)
    net_liquidation: float = 0.0
    cash: float = 0.0
    available_funds: float = 0.0

    def __post_init__(self):
        if not self.legs:
            self.legs = {c: self._total_for(c) for c in self.venue["account_currencies"]}

    def _total_for(self, _ccy: str) -> float:
        return self.net_liquidation if self.venue["balance_total_is"] == "net_liquidation" else self.cash

    def _free_for(self, _ccy: str) -> float:
        return self.available_funds if self.venue["balance_free_is"] == "full_available_funds" else self.cash

    def _require(self, ccy) -> str:
        name = getattr(ccy, "code", None) or str(ccy)
        if name not in self.legs:
            # Nautilus RAISES for a currency the account does not hold. A double that answered anyway
            # hid a defect three lines above the code its tests were covering (2026-08-24).
            raise ValueError(
                f"no {name} balance on this {self.venue.name} account; it holds "
                f"{sorted(self.legs)} — see facts.account_currencies")
        return name

    def balance_total(self, ccy=None):
        return self.legs[self._require(ccy)] if ccy is not None else next(iter(self.legs.values()))

    def balances_total(self) -> dict:
        return dict(self.legs)

    def balance_free(self, ccy=None):
        self._require(ccy) if ccy is not None else None
        return self._free_for(next(iter(self.legs)))

    def balances_free(self) -> dict:
        return {c: self._free_for(c) for c in self.legs}


@dataclass
class VenueBroker:
    """A broker that records instead of sending, and refuses what the venue refuses."""

    venue: VenueFacts
    #: {symbol: qty} the ACCOUNT holds — the broker's book, never the claims table.
    account_positions: dict = field(default_factory=dict)
    #: {symbol: qty} attributed to THIS strategy by the venue's own position ids.
    own_positions: dict = field(default_factory=dict)
    #: Symbols the node actually subscribed. Nautilus refuses anything else, held or not.
    subscribed: set = field(default_factory=set)
    #: Symbols whose quantity is reserved by a resting protective order (Alpaca).
    reserved: set = field(default_factory=set)
    submitted: list = field(default_factory=list)
    #: reduce_only flags the venue DROPPED — non-empty means the flag protected nothing.
    dropped_reduce_only: list = field(default_factory=list)

    def submit(self, symbol: str, side: str, qty: float, *, reduce_only: bool = False):
        if self.subscribed and symbol not in self.subscribed:
            raise UnsubscribedInstrument(
                f"{symbol} is not a subscribed instrument — Nautilus refuses it even though the lane "
                f"holds {self.own_positions.get(symbol, 0):g}. Subscribe universe UNION held.")
        if reduce_only and not self.venue["reduce_only_supported"]:
            # NOT AN ERROR — that is exactly the danger. IB neither honours nor rejects it, so the
            # order goes out unprotected and the test can assert on what was dropped.
            self.dropped_reduce_only.append(symbol)
        if side.upper() == "SELL":
            held = float(self.own_positions.get(symbol, 0.0))
            if qty > held:
                raise NakedShortRefused(
                    f"SELL {qty:g} {symbol} against {held:g} held on {self.venue.name}. "
                    + ("reduce_only would NOT save this: the venue drops it silently."
                       if not self.venue["reduce_only_supported"] else ""))
            if symbol in self.reserved and self.venue["reserves_shares_against_resting_stop"]:
                raise SharesReserved(
                    f"{symbol} is reserved by a resting protective order — `available: 0` means "
                    f"UNRESERVED is 0, not that nothing is held. Cancel the stop first.")
        self.submitted.append({"symbol": symbol, "side": side.upper(), "qty": qty})
        return {"ok": True, "id": f"DOUBLE-{len(self.submitted)}"}

    def positions(self) -> dict:
        return dict(self.account_positions)

    def strategy_positions(self) -> dict:
        return dict(self.own_positions)


def account_for(venue_name: str, **kw) -> VenueAccount:
    return VenueAccount(venue=VENUES[venue_name], **kw)


def broker_for(venue_name: str, **kw) -> VenueBroker:
    return VenueBroker(venue=VENUES[venue_name], **kw)
