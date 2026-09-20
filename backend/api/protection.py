"""Protective-stop sizing and the audit that finds positions without one (#239).

The book has been running with almost nothing resting at the broker. On 2026-08-13, 11 of 12 positions
had no protective sell order at all — $89,879 of market value, about 91% of the account. When the engine
went down for thirteen hours on 2026-08-11, none of it was protected, because every stop this system
places is engine-driven.

A stop that lives at the venue keeps working when we do not. That is the whole point, and it is why the
order type here is a TRAILING stop rather than a fixed one: it ratchets up on the broker's side with no
involvement from us, so "constantly updated" needs no update mechanism to go wrong. Alpaca recomputes the
stop from its own high-water mark on every print (measured 2026-08-12: the hwm even survives a replace).

WHAT THIS DOES NOT COVER, and the docs are explicit about it: **a trailing stop does not trigger outside
regular trading hours.** So this protects against an engine outage DURING the session, which is the case
that actually happened, but an overnight gap is untouched by it — the stop elects at the next open, at
whatever the market reopens at. Daily ATR is used to size it precisely because it includes those gaps, but
sizing for a risk is not the same as covering it. Anything that reports "the book is protected" has to
mean "during RTH".

Width is per symbol, from the symbol's own ATR. Measured against the live book on 2026-08-13:

    VFLO   ATR  1.64%          NBIS   ATR 11.18%

A single percentage cannot serve both — 2.5% is 1.5x ATR on VFLO and 0.22x on NBIS, which would sell NBIS
on an ordinary hour. The same measurement fixed the multiple: at 1.5x ATR every position on the book sits
at least 6x its worst 95th-percentile 5-minute range from its stop, so a brief engine outage or a normal
intraday swing cannot reach it.

Pure. Placement is a separate concern and is not done here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

#: Float slack for quantity comparisons — shares can arrive fractional.
_QTY_EPS = 1e-9

#: The protection policies a lane may carry (#872). A CLOSED set: an unrecognised mode is a mode the
#: planner cannot dispatch on, and defaulting one silently is how a lane ends up with a policy nobody
#: chose. The schema's enum is the other half of this; `LaneProtection` refuses anything else outright.
#:
#:   trail        today's peak-relative trailing stop — the high-water mark ratchets at the venue.
#:   entry_floor  a FIXED stop at the lane's own entry minus k x ATR. Measured over 6 independent
#:                60-session periods (issue 139) at -10.7pp against the trail's -82.5pp for
#:                QC345-003, whose exit rule is its own rebalance rather than a stop.
#:   none         no broker-side stop at all. For a lane whose config carries its exit rule; the
#:                exposure report says OPTED-OUT, which is neither "covered" nor "naked".
PROTECTION_MODES = frozenset({"trail", "entry_floor", "none"})

#: Settings key suffix for a lane's policy, e.g. `QC345-003_protection`. LITERAL keys, not a map:
#: `_strip_additional` (settings/store.py) keeps only declared `properties`, so a map of lane ids is
#: rewritten to `{}` before validation and reads as a dead-but-authoritative knob (#574/#581, the same
#: shape). `strategies.schema.json` already uses literal lane keys for exactly this reason.
LANE_PROTECTION_SUFFIX = "_protection"

#: The client-order-id family this reconciler mints. ONE derivation, shared with
#: `engine_node._protection_coid` — `wrong_mode` cancels only orders THIS mechanism placed, and a
#: private copy of the prefix here would be free to drift from the one that mints the ids, which is
#: how a cancel sweep starts eating a bracket leg or a manual sell.
PROTECTION_COID_PREFIX = "PROT-"


@dataclass(frozen=True)
class LaneProtection:
    """One lane's protection policy: which kind of stop, and how wide.

    `atr_multiple` is None when the lane does not override the domain-level width. That is a
    CONSIDERED fallback, not a lazy one (CLAUDE.md): the domain multiple is the policy for every lane
    that has not asked for its own, and "inherit" is a genuinely correct answer rather than a guess at
    a missing one. It is `None`, never `0.0`, so "not set" can never be read as "no width".
    """

    mode: str
    atr_multiple: float | None = None
    #: WHERE the answer came from (#1029, #965: "absent key and declared-none must remain
    #: distinguishable"). One of `PROTECTION_SOURCES`:
    #:   `settings` — the operator WROTE `<lane>_protection` in the tenant's file;
    #:   `declared` — the file is silent and this is the lane's own stance from the registry;
    #:   `default`  — the file is silent and the registry does not know the lane (a blank leg,
    #:                `EXTERNAL`): `DEFAULT_LANE_PROTECTION`.
    #: KEYWORD-ONLY AND REQUIRED. A default here would be a real identity nobody chose: a hand-built
    #: `LaneProtection(mode="none")` in any `mode_of` double would render as "the operator wrote it"
    #: the moment the row exists. `lane_modes` stamps every path — pinned by
    #: `test_lane_modes_stamps_a_source_on_every_path` — and every other constructor says what it is.
    source: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.mode not in PROTECTION_MODES:
            raise ValueError(
                f"unknown protection mode {self.mode!r} — must be one of {sorted(PROTECTION_MODES)}"
            )
        if self.atr_multiple is not None and not (self.atr_multiple > 0):
            raise ValueError(f"atr_multiple must be positive or None, got {self.atr_multiple!r}")
        if self.source not in PROTECTION_SOURCES:
            raise ValueError(
                f"unknown protection source {self.source!r} — must be one of {sorted(PROTECTION_SOURCES)}"
            )


#: The provenance a `LaneProtection` can carry. CLOSED, like the modes: the readback row renders it.
PROTECTION_SOURCES = frozenset({"settings", "declared", "default"})

#: The policy for a lane the REGISTRY does not know and the settings file does not name — a blank leg,
#: `EXTERNAL`, a lane id from a stale cache. Trail is what every lane did before #872 and what the
#: schema's per-lane defaults still say for the trail lanes, so this is the SAME answer arrived at
#: twice rather than a second opinion. A REGISTERED lane never lands here: its own stance does (#1029).
DEFAULT_LANE_PROTECTION = LaneProtection(mode="trail", source="default")


def lane_modes(cfg: dict, declared: dict | None = None) -> Callable[[str], LaneProtection]:
    """Resolved `protection` settings -> the `mode_of` callable `plan_protection` takes.

    ONE derivation of "what does this lane's key mean", shared by the engine and by every test, so the
    settings shape and the planner's reading of it cannot drift apart.

    THREE ANSWERS, IN ORDER (#1029):
      1. the operator's key, when the tenant's file carries `<lane>_protection`      -> `settings`
      2. the lane's own stance from the registry (`StrategyEntry.protection`)        -> `declared`
      3. `DEFAULT_LANE_PROTECTION`, for an id the registry does not know              -> `default`

    `declared` is the RAW coerced file (`api.settings.declared("protection")`): what the operator
    actually wrote, with no schema defaults filled. It is needed because `resolve()` fills EVERY
    per-lane schema default (store.py `_extend_with_default`), so in a resolved `cfg` an operator's
    `{"mode": "none"}` and a filled default `{"mode": "none"}` are byte-identical — and #965 requires
    the two to stay distinguishable. When `declared` is None the keys in `cfg` are taken as declared:
    that is the shape every hand-built caller passes, and it keeps their answers what they were.
    The schema's per-lane default is NOT read here; it must AGREE with the registry stance, and
    `test_lane_protection_is_declared_by_the_lane.py` pins that it does.
    """
    written = cfg if declared is None else declared

    def mode_of(lane: str) -> LaneProtection:
        raw = (written or {}).get(f"{lane}{LANE_PROTECTION_SUFFIX}")
        if isinstance(raw, dict):
            mode = str(raw.get("mode") or "")
            if mode in PROTECTION_MODES:
                multiple = raw.get("atrMultiple")
                return LaneProtection(
                    mode=mode,
                    atr_multiple=float(multiple) if isinstance(multiple, (int, float)) else None,
                    source="settings",
                )
            # A mode the planner cannot dispatch on is unreachable on the production path — `declared()`
            # is `_coerced_file`, which DROPS an enum-invalid key before this callable sees it, so an
            # unusable written value arrives here as an absent one. A hand-built dict can still carry
            # one, and it must degrade the same way rather than raise inside a 60 s protection tick:
            # fall through and answer as if unwritten, stamped `declared`, which is what coercion
            # would have made of it.
        # Function-local on purpose: the registry imports `PROTECTION_MODES` from this module at import
        # time, so a module-level import here would be a cycle (pinned by
        # `test_the_registry_imports_clean_in_a_fresh_interpreter`). `by_id` reads the registry at
        # CALL time — its `entries` default used to bind the tuple at definition, the default-argument
        # shape that made `_j`'s slot invisible (CLAUDE.md), and is fixed at the source.
        from api.strategy_registry import by_id

        entry = by_id(lane) if lane else None
        if entry is not None:
            return LaneProtection(mode=entry.protection, source="declared")
        return DEFAULT_LANE_PROTECTION

    return mode_of


def true_ranges(bars: list) -> list[float]:
    """True range per bar, oldest first. TR is the greater of the bar's own range and its gap from the
    previous close, so an overnight gap is not mistaken for a quiet session."""
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        high, low = float(b["h"]), float(b["l"])
        tr = high - low if prev_close is None else max(high - low, abs(high - prev_close), abs(low - prev_close))
        out.append(tr)
        prev_close = float(b["c"])
    return out


def atr(bars: list, lookback: int = 14) -> float | None:
    """SIMPLE mean true range over the last `lookback` completed sessions, or None without enough history.

    A plain average, NOT Wilder's smoothing — conventional ATR decays old shocks exponentially while this
    drops them abruptly out of the window (codex review). For sizing a protective stop that is arguably
    the more honest behaviour: a volatility regime that ended a fortnight ago should stop widening the
    stop, not linger in it. But it is a different number from Wilder's, and callers should not assume they
    match.

    None means "do not guess" — a symbol we cannot measure gets no automatic stop rather than an
    arbitrary one, the same "wait for real data, never fabricate" rule the managers follow.
    """
    # lookback + 1: the FIRST true range in any series has no previous close to gap from, so a window of
    # exactly `lookback` bars silently drops the oldest session's overnight gap. (codex review, Medium.)
    if len(bars) < lookback + 1:
        return None
    window = true_ranges(bars)[-lookback:]
    return sum(window) / len(window)


@dataclass(frozen=True)
class TrailWidth:
    """A stop width, plus why it came out that way — the clamp reason matters more than the number.

    Hitting the ceiling is not a detail: it means the symbol's normal movement is wider than we are
    willing to risk, so the stop is protecting less than a full ATR of it. That is a signal the position
    is too large for its volatility, and it should surface rather than be silently applied.
    """

    bps: int
    pct: float
    atr_pct: float
    clamped: str | None  # "floor" | "ceiling" | None

    @property
    def placeable(self) -> bool:
        """False when the width hit the CEILING (codex review, High).

        A floor clamp is fine — the stop is merely wider than the symbol strictly needs. A ceiling clamp
        is not: it means the symbol's normal movement is larger than policy allows us to sit through, so
        the order would protect less than one ATR and give false comfort. Placement must refuse it unless
        an operator explicitly overrides, rather than quietly under-protecting.
        """
        return self.clamped != "ceiling"


def trail_width(
    price: float,
    atr_value: float,
    *,
    multiple: float = 1.5,
    min_pct: float = 1.0,
    max_pct: float = 15.0,
) -> TrailWidth | None:
    """Stop width for one symbol, in basis points, clamped to the configured band.

    Returns None when it cannot be computed — a non-positive price or ATR — rather than defaulting.
    """
    if price <= 0 or atr_value <= 0:
        return None
    atr_pct = atr_value / price * 100
    raw = atr_pct * multiple
    clamped: str | None = None
    pct = raw
    if pct < min_pct:
        pct, clamped = min_pct, "floor"
    elif pct > max_pct:
        pct, clamped = max_pct, "ceiling"
    # Basis points, rounded HALF AWAY FROM ZERO to match the UI's `Math.round` — the same JS/Python
    # rounding seam that already bit the PEAK settings validator.
    import math

    return TrailWidth(bps=math.floor(pct * 100 + 0.5), pct=pct, atr_pct=atr_pct, clamped=clamped)


#: Order types that actually PROTECT a position. A take-profit limit reduces the position too, but it
#: sits ABOVE the market and does nothing on the way down — counting it as protection is how a naked
#: position hides. NBIS carried exactly this shape on 2026-08-12: a stop AND a limit, and the limit
#: outlived the stop.
PROTECTIVE_TYPES = frozenset({"stop", "stop_limit", "trailing_stop", "STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET"})

#: The same set, SPLIT by what the two lane modes place — because `wrong_mode` has to answer "is this
#: resting order the kind this lane's policy asks for". Both vocabularies again (Alpaca's lowercase and
#: Nautilus's upper), for the reason `is_resting` gives: one of them matches nothing, silently.
_TRAILING_TYPES = frozenset({"trailing_stop", "TRAILING_STOP_MARKET"})
_FIXED_STOP_TYPES = frozenset({"stop", "stop_limit", "STOP_MARKET", "STOP_LIMIT"})


@dataclass(frozen=True)
class Unprotected:
    """A held position whose protective coverage is short of the quantity held.

    `uncovered` is the shortfall, not the whole position: a 1-share stop on 100 shares leaves 99 exposed,
    and reporting that row as fully naked would be as misleading as reporting it as fully protected.
    """

    instrument_id: str
    strategy_id: str
    quantity: float
    market_value: float
    covered: float = 0.0

    @property
    def uncovered(self) -> float:
        return max(0.0, abs(self.quantity) - self.covered)

    @property
    def uncovered_notional(self) -> float:
        """Current market value of the exposed portion.

        NOT "loss at risk" (codex review). For a long it bounds the loss; for a SHORT the downside is
        unbounded and this understates it. It answers "how much exposure has no stop behind it", which is
        the question the audit exists to answer.
        """
        if not self.quantity:
            return 0.0
        return abs(self.market_value) * (self.uncovered / abs(self.quantity))


#: Open statuses in BOTH vocabularies this code meets (codex review, Medium). Alpaca's REST returns
#: lowercase names; our own engine frames emit Nautilus's `order.status.name` in upper case, and
#: `pending_new` there is `SUBMITTED`. An audit that knew only one of the two would ignore live
#: protective stops from the other and report a covered position as naked — or the reverse.
_OPEN_STATUSES = frozenset({
    "NEW", "ACCEPTED", "HELD", "PARTIALLY_FILLED", "PENDING_NEW", "ACCEPTED_FOR_BIDDING",
    "SUBMITTED", "PENDING_UPDATE", "PENDING_CANCEL", "PENDING_REPLACE", "TRIGGERED",
})
#: `PENDING_REPLACE` added for #387. An Alpaca replace leaves the original order live until the new one
#: is accepted, so the venue is still reserving its shares. Omitting it made the reservation count too
#: low (an exit sized against free shares that were not free) AND let the reconciler place a SECOND stop
#: on top of one that was merely being modified. Both directions of the same missing status.


def is_resting(status: object) -> bool:
    """Is this order still live at the venue — in EITHER vocabulary this code meets?

    THE SINGLE DEFINITION (#387 review). This predicate existed twice: here, and as a hand-written
    `_RESTING` set in `engine_node`'s protection reconciler. The two disagreed in BOTH directions, and
    each direction was a live defect:

      * `pending_cancel` was here and missing there -> the SECURED badge read NAKED while the order
        placer treated the position as covered, so nothing was ever done about it. Alpaca frees the
        shares on CONFIRMED cancel, and a cancel can be rejected; the order is live until then.
      * `done_for_day` and `calculated` were there and (rightly) missing here -> the badge claimed
        SECURED *and* a second stop went on. Both are Alpaca POST-completion states: `calculated` is
        settlement bookkeeping on a finished order and `done_for_day` cannot execute again today.
        Counting either as protection is the silencing direction, which is the worse one.

    CLAUDE.md: two derivations of one fact will disagree. They did. This is the one derivation.

    Alpaca's REST returns lowercase names; our own engine frames emit Nautilus's `order.status.name` in
    upper case (where `pending_new` is `SUBMITTED`). Normalising here is what lets one set serve both —
    a raw lowercase comparison against this set matches NOTHING, which would report an entire book naked.
    """
    return str(status or "").upper() in _OPEN_STATUSES


def _same_instrument(order_symbol: str, instrument_id: str) -> bool:
    """Does this order belong to this instrument?

    Compares the full id, or the id with only its VENUE suffix removed. Splitting on the first dot would
    turn `BRK.B.XNYS` into `BRK` and let an unrelated BRK stop count as protection for BRK.B — tickers
    contain dots. (codex review, High.)
    """
    if order_symbol == instrument_id:
        return True
    ticker = instrument_id.rsplit(".", 1)[0] if "." in instrument_id else instrument_id
    return order_symbol == ticker


def protective_orders(orders: list, instrument_id: str, reducing_side: str) -> list[dict]:
    """The OPEN protective orders on one leg, with their remaining quantity as `_remaining`.

    Split out of `protective_quantity` so the sum and the orders behind it cannot disagree — two
    derivations of one fact eventually do, and here the disagreement would be "the audit says covered while
    the resize path finds nothing to shrink".
    """
    out: list[dict] = []
    for o in orders:
        if not _same_instrument(str(o.get("symbol") or o.get("instrument_id") or ""), instrument_id):
            continue
        if str(o.get("side", "")).lower() != reducing_side.lower():
            continue
        if str(o.get("type") or o.get("order_type") or "") not in PROTECTIVE_TYPES:
            continue
        if str(o.get("status", "")).upper() not in _OPEN_STATUSES:
            continue
        try:
            qty = float(o.get("qty") or o.get("quantity") or 0)
            filled = float(o.get("filled_qty") or 0)
        except (TypeError, ValueError):
            continue
        remaining = max(0.0, qty - filled)
        if remaining > 0:
            out.append({**o, "_remaining": remaining})

    # Deduped by the VENUE's identifier, not by Python object identity — this function builds a fresh dict
    # per input, so `id()` always differs and an identity check protects nothing (codex review, Medium;
    # my first fix made exactly that mistake). A payload that repeats one order would otherwise be counted
    # as several, inflating measured coverage and then "correcting" one order several times over.
    seen: set[str] = set()
    deduped: list[dict] = []
    for o in out:
        key = str(o.get("id") or o.get("client_order_id") or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(o)
    return deduped


def protective_quantity(orders: list, instrument_id: str, reducing_side: str) -> float:
    """How many shares of `instrument_id` are covered by resting PROTECTIVE orders.

    Three filters, each of which a naked position has hidden behind at some point:

    * TYPE — only stops count. A take-profit limit reduces the position but sits above the market and
      does nothing on the way down.
    * SIDE — it must reduce the position, not add to it.
    * OPEN — a filled, cancelled or expired order protects nothing. NBIS's take-profit EXPIRED at the
      close on 2026-08-12 while still appearing in the order list.

    Delegates to `protective_orders` so the total and the orders behind it are one derivation.
    """
    return sum(o["_remaining"] for o in protective_orders(orders, instrument_id, reducing_side))


def reserved_quantity(orders: list, instrument_id: str, reducing_side: str) -> float:
    """Shares locked by ANY open reducing-side order, protective or not (#287).

    Alpaca reserves shares against every resting sell, including a take-profit LIMIT that protects nothing
    on the way down. So `PROTECTIVE_TYPES` is right to exclude that limit from COVERAGE and irrelevant to
    AVAILABILITY — the two questions have different answers on the same order, and conflating them is what
    produced:

        PROT-SELL-OKTA: 403 insufficient qty available (requested: 68, available: 2)

    Same reservation mechanism as #245, seen from the other side: not a cancel racing a submit, but two
    different claims on one share.

    Delegates to `reserving_orders` so the total and the orders behind it are one derivation — same rule
    as `protective_quantity`/`protective_orders`, and for the same reason: a caller that must CANCEL the
    reservation needs the orders, and a total that disagreed with them would cancel the wrong set.
    """
    return sum(o["_remaining"] for o in reserving_orders(orders, instrument_id, reducing_side))


def reserving_orders(orders: list, instrument_id: str, reducing_side: str) -> list[dict]:
    """Every OPEN order at the venue that RESERVES shares on this leg, with `_remaining` per order.

    Deliberately unfiltered by type, unlike `protective_orders`. Alpaca reserves against any resting
    reducing order — a protective stop, a take-profit limit, an operator's working sell — and the question
    "what must be cancelled before I can send an exit" is about the reservation, not about what protects.

    THIS IS BROKER TRUTH AND IT HAS NO CACHE-BASED EQUIVALENT. The engine holds orders as REJECTED that
    Alpaca reports `new`: a submit whose HTTP call failed after the venue accepted it lands in a state
    Nautilus treats as TERMINAL, so every later `OrderAccepted` from reconciliation is discarded
    (`InvalidStateTrigger: REJECTED -> ACCEPTED`, 81,128 times on 2026-08-17). Those orders vanish from
    `cache.orders_open()` while still holding the shares. Ask the cache and the answer is zero reserved;
    submit against it and the venue answers `insufficient qty available (requested: 29, available: 0)`.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for o in orders:
        if not _same_instrument(str(o.get("symbol") or o.get("instrument_id") or ""), instrument_id):
            continue
        if str(o.get("side", "")).lower() != reducing_side.lower():
            continue
        if str(o.get("status", "")).upper() not in _OPEN_STATUSES:
            continue
        # Deduped by the VENUE's identifier, never by object identity — this builds a fresh dict per
        # input, so `id()` always differs and an identity check protects nothing (the #239 codex finding).
        key = str(o.get("id") or o.get("client_order_id") or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        try:
            qty = float(o.get("qty") or o.get("quantity") or 0)
            filled = float(o.get("filled_qty") or 0)
        except (TypeError, ValueError):
            continue
        remaining = max(0.0, qty - filled)
        if remaining > 0:
            out.append({**o, "_remaining": remaining})
    return out


def orders_from_reports(reports: list) -> list[dict]:
    """Nautilus `OrderStatusReport`s -> the order shape this module's readers already consume.

    ONE PARSE INSTEAD OF TWO. `providers/alpaca/exec_client.py` already turns the broker's payload into
    typed reports; `engine_node` was re-parsing the same bytes by hand in Alpaca's vocabulary. This is
    the adapter that lets every reader here take the typed source without being rewritten, which is what
    makes a second broker possible — the shipped Interactive Brokers adapter produces the same reports.

    Emits BOTH spellings of the aliased keys (`qty`/`quantity`, `type`/`order_type`) because the readers
    accept either, and emitting one would make this adapter a third vocabulary rather than a bridge.

    Order TYPE is Nautilus's name (`TRAILING_STOP_MARKET`), which `PROTECTIVE_TYPES` already accepts —
    it holds both vocabularies deliberately. Any private copy of that set holding only Alpaca's spellings
    will silently match nothing here, which is why `engine_node` no longer keeps one.
    """
    out: list[dict] = []
    for r in reports:
        if r is None:
            continue
        qty = float(r.quantity)
        filled = float(r.filled_qty)
        out.append({
            "id": str(r.venue_order_id) if r.venue_order_id is not None else "",
            "client_order_id": str(r.client_order_id) if r.client_order_id is not None else "",
            "instrument_id": str(r.instrument_id),
            "symbol": r.instrument_id.symbol.value,
            "side": r.order_side.name.lower(),
            "status": r.order_status.name,
            "type": r.order_type.name,
            "order_type": r.order_type.name,
            "qty": qty,
            "quantity": qty,
            "filled_qty": filled,
            # `stop_price` is what the reconciler reads to decide whether a resting order is a stop at
            # all. `trigger_price` is the typed equivalent and carries the same number.
            "stop_price": float(r.trigger_price) if r.trigger_price is not None else None,
        })
    return out


def reserving_orders_typed(reports: list, instrument_id: str, reducing_side: str) -> list[dict]:
    """`reserving_orders`, reading Nautilus `OrderStatusReport`s instead of Alpaca JSON.

    THE SAME QUESTION, ASKED OF THE TYPED SOURCE. `engine_node` reads the broker by calling Alpaca's REST
    API and parsing the JSON by hand — while the same payload is already parsed, one layer down, into
    `OrderStatusReport` (`providers/alpaca/exec_client.py:1042`). That is the form Nautilus defines and
    every adapter produces, including the shipped Interactive Brokers one, so reading it here is what
    makes a second broker possible without porting this file.

    Semantics are deliberately identical to `reserving_orders` and pinned by a test that runs both over
    the same rows and compares order-for-order. Where they could drift:

      status      `_OPEN_STATUSES` already speaks Nautilus's vocabulary — `is_resting` was written to
                  handle "EITHER vocabulary this code meets" — so `order_status.name` matches directly.
                  Alpaca's `held` maps to ACCEPTED, which is why a HELD bracket leg is still seen. That
                  mapping IS the #387 fix and a test asserts it rather than trusting it.
      identity    deduped by `venue_order_id`, the venue's own identifier, exactly as the raw path does.
      remaining   `quantity - filled_qty`, floored at zero. Both are `Quantity`, so the subtraction is
                  exact rather than float-parsed out of strings.

    Returns dicts, not reports, so callers already written against `reserving_orders` need no change —
    the migration is of the SOURCE, not of every consumer at once. `_remaining` carries the same meaning.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in reports:
        if r is None:
            continue
        if not _same_instrument(str(r.instrument_id), instrument_id):
            continue
        if r.order_side.name.lower() != reducing_side.lower():
            continue
        if r.order_status.name.upper() not in _OPEN_STATUSES:
            continue
        key = str(r.venue_order_id) if r.venue_order_id is not None else str(r.client_order_id or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        remaining = max(0.0, float(r.quantity) - float(r.filled_qty))
        if remaining > 0:
            out.append({
                "id": str(r.venue_order_id) if r.venue_order_id is not None else "",
                "client_order_id": str(r.client_order_id) if r.client_order_id is not None else "",
                "instrument_id": str(r.instrument_id),
                "symbol": r.instrument_id.symbol.value,
                "side": r.order_side.name.lower(),
                "status": r.order_status.name,
                "type": r.order_type.name,
                "qty": float(r.quantity),
                "filled_qty": float(r.filled_qty),
                "_remaining": remaining,
            })
    return out


def _signed_qty(position: dict) -> float:
    """Signed quantity, honouring an explicit `side` when the DTO carries one.

    Our `PositionDTO` reports `side` as LONG/SHORT with a POSITIVE `quantity`, so inferring direction from
    the sign alone read every short as long — a short with a valid BUY stop would be reported naked while
    a SELL stop counted as protection. (codex review, High.)
    """
    qty = abs(float(position.get("quantity") or 0))
    side = str(position.get("side") or "").upper()
    if side == "SHORT":
        return -qty
    if side == "LONG":
        return qty
    # No side field — fall back to the sign as given.
    return float(position.get("quantity") or 0)


def lane_quantities(cache_positions) -> dict[tuple[str, str], dict[str, float]]:
    """`{(instrument_id, LONG/SHORT): {lane: signed_qty}}` from the engine's own open positions.

    The cache is the ONLY thing that knows which sleeve holds what — the broker does not, which is why
    `broker_rows` stamps every row `strategy_id: ""`. That blank is what collapses two lanes into one
    leg, leaves `intent.strategy_id` empty in production, and sends the arming path to `stamp_for`,
    which refuses a multi-holder instrument outright. Measured on an Alpaca paper instance 2026-09-08: CRAK, LAND, PAGP
    and GMAB each had ONE lane's slice covered and the other's naked — 433 shares with no stop.
    """
    out: dict[tuple[str, str], dict[str, float]] = {}
    for pos in cache_positions or ():
        if not hasattr(pos, "is_open") or not pos.is_open:
            continue
        lane = str(getattr(pos, "strategy_id", "") or "")
        if not lane:
            continue
        try:
            qty = float(pos.signed_qty)
        except (TypeError, ValueError):
            continue
        if qty != qty or abs(qty) <= _QTY_EPS:
            continue
        key = (str(getattr(pos, "instrument_id", "")), "LONG" if qty > 0 else "SHORT")
        out.setdefault(key, {})[lane] = out.setdefault(key, {}).get(lane, 0.0) + qty
    return out


def lane_entries(cache_positions) -> dict[tuple[str, str], float]:
    """`{(instrument_id, lane): avg_px_open}` from the engine's own open positions (#872).

    THE ENTRY A FLOOR IS MEASURED FROM, and it has to come from here rather than from the broker. Under
    NETTING each lane's position is `{instrument}-{strategy_id}` and carries its OWN `avg_px_open`; the
    broker publishes one account-level `avg_entry_price` per symbol, which mixes every lane holding it.
    On a shared name that account number puts one lane's floor at another lane's cost basis — and the
    two look identical on a book where only one lane holds the symbol, which is exactly the condition
    under which the wrong source is invisible.

    Reads the same rows as `lane_quantities` and applies the same filters, deliberately kept as two
    functions returning two shapes rather than one returning a bundle: a caller that needs quantities
    must not silently acquire entries, and vice versa.

    A position with no usable entry is ABSENT from the map, never present as 0.0 — the planner's
    `no_lane_entry` refusal is the answer for that, and a zero would be a floor at -k x ATR.
    """
    out: dict[tuple[str, str], float] = {}
    for pos in cache_positions or ():
        if not getattr(pos, "is_open", False):
            continue
        lane = str(getattr(pos, "strategy_id", "") or "")
        if not lane:
            continue
        try:
            entry = float(getattr(pos, "avg_px_open", None))
        except (TypeError, ValueError):
            continue
        if entry != entry or entry <= 0:      # NaN or non-positive: not an entry, and not a zero either
            continue
        out[(str(getattr(pos, "instrument_id", "")), lane)] = entry
    return out


def attribute_rows_to_lanes(rows: list, by_lane: dict, *, eps: float = 1e-6) -> list[dict]:
    """Split each aggregate broker row into PER-LANE rows — but only where the split is provable.

    THE BROKER NET IS THE ONLY HARD ANCHOR (ADR 0001). A cache split is an unverified claim, so it is
    used ONLY when it sums to what the broker actually holds. Where it does not, the aggregate row is
    returned UNCHANGED, lane still "", and the caller keeps exactly today's behaviour.

    Three states, not two: split reconciles -> per-lane rows; split disagrees -> aggregate row; no
    split known at all -> aggregate row. The middle case is the one that must not silently become the
    first, because a wrong split rests a stop against shares a lane does not hold, and on trigger that
    sells into a position that never existed — the phantom mint this whole module exists to stop.
    """
    out: list[dict] = []
    for row in rows or ():
        iid = str(row.get("instrument_id") or "")
        side = str(row.get("side") or "").upper()
        held = abs(float(row.get("quantity") or 0))
        # LONG ONLY, matching `stamp_for`. A short's protection reduces by BUYING, and neither this
        # split nor the stamp reasons about side — naming a lane for a BUY is a guess about which
        # position that BUY closes. A short row therefore stays aggregate and keeps today's refusal.
        lanes = ((by_lane or {}).get((iid, side)) or {}) if side == "LONG" else {}
        total = sum(abs(v) for v in lanes.values())
        # `eps` is absolute-share slack, not a ratio: quantities are whole shares from two sources that
        # should agree exactly, so anything above rounding noise is a real disagreement, not drift.
        if not lanes or abs(total - held) > eps:
            out.append(row)
            continue
        mv = float(row.get("market_value") or 0)
        for lane, qty in sorted(lanes.items()):
            share = abs(qty) / total if total else 0.0
            out.append({**row,
                        "quantity": abs(qty),
                        "strategy_id": lane,
                        "market_value": mv * share})
    return out


def unprotected_positions(positions: list, orders: list, lane_of=None) -> list[Unprotected]:
    """Held positions whose protective coverage falls short of what is held.

    Presence is NOT enough (codex review, High). A 1-share stop on a 100-share position, or a take-profit
    limit standing in for a stop, would suppress the row entirely and report `$0 unprotected` while
    almost all the downside sat uncovered. Coverage is compared against quantity, and the shortfall is
    what gets reported.

    Deliberately judged per INSTRUMENT, not per strategy. The broker does not know about our sleeves, and
    a stop resting on the instrument protects the shares whoever claims them — this answers "is the book
    covered", not "is the attribution tidy".

    IMPORTANT, and not fixed by anything here: a trailing stop does NOT trigger outside regular trading
    hours. This audit measures RTH protection. An overnight gap is not covered by any of it.
    """
    # Keyed by instrument AND direction (codex review, High). Two sleeves holding the same name on
    # OPPOSITE sides net to zero, which would make both vanish from the audit — and each needs a stop on
    # its own reducing side, so coverage cannot be pooled across them either.
    by_leg: dict[tuple[str, str], list] = {}
    for p in positions:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        by_leg.setdefault((str(p.get("instrument_id")), "sell" if qty > 0 else "buy"), []).append(p)

    out: list[Unprotected] = []
    for (iid, reducing_side), rows in by_leg.items():
        held = sum(_signed_qty(r) for r in rows)
        resting = protective_orders(orders, iid, reducing_side)
        covered = sum(o["_remaining"] for o in resting)

        # PER-LANE COVERAGE, when — and only when — every resting stop on this leg can be attributed.
        #
        # Pro-rata is the wrong answer whenever the lanes are unevenly covered, and it is wrong in the
        # dangerous direction: on paper 2026-09-08 CRAK held 32 (MOMENTUM, fully covered) + 32 (BCTROT,
        # naked). Pro-rata calls each lane half-covered, so it rests 16 MORE for the lane that already
        # had 32 — 48 of coverage against 32 held, which on trigger sells shares that are not there and
        # FLIPS the lane short. That is the hazard `protective_stamp.py` names as the reason the
        # per-lane split "cannot be activated alone".
        #
        # An order whose lane we cannot name makes the whole leg unattributable — NOT merely
        # unattributed. Falling back to pro-rata for the leg keeps today's behaviour, which is
        # conservative here: it can under-claim coverage and refuse, never over-claim and oversize.
        lanes_of_rows = {str(r.get("strategy_id") or "") for r in rows}
        per_lane: dict[str, float] | None = None
        if lane_of is not None and "" not in lanes_of_rows:
            attributed: dict[str, float] = {}
            for o in resting:
                lane = lane_of(str(o.get("client_order_id") or ""))
                if not lane:
                    attributed = {}
                    break
                attributed[lane] = attributed.get(lane, 0.0) + o["_remaining"]
            else:
                per_lane = attributed

        if per_lane is None and covered >= abs(held):
            continue

        for r in rows:
            qty = _signed_qty(r)
            lane = str(r.get("strategy_id") or "")
            if per_lane is not None:
                lane_covered = per_lane.get(lane, 0.0)
                if lane_covered >= abs(qty) - _QTY_EPS:
                    continue
            else:
                share = abs(qty) / abs(held) if held else 0
                lane_covered = covered * share
            out.append(
                Unprotected(
                    instrument_id=iid,
                    strategy_id=lane,
                    quantity=qty,
                    market_value=float(r.get("market_value") or 0),
                    covered=lane_covered,
                )
            )
    return out


@dataclass(frozen=True)
class StopIntent:
    """One protective stop this reconciler intends to rest at the broker.

    TWO KINDS, and they carry different fields (#872). A `trail` intent is a trailing stop whose
    trigger the VENUE recomputes from its own high-water mark — it has a width (`trail_bps`) and no
    absolute price. An `entry_floor` intent is a fixed STOP_MARKET at `lane entry - k x ATR`, so it has
    an absolute `trigger_px` and no width. `__post_init__` refuses every other combination, because a
    consumer dispatching on `kind` and finding the wrong fields has to guess — and the guess here is a
    0-bps trailing stop, which fires on the next print.
    """

    instrument_id: str
    side: str          # the REDUCING side: SELL for a long, BUY for a short
    quantity: float
    trail_bps: int
    atr_pct: float
    clamped: str | None
    #: THE LANE WHOSE SHARES THIS COVERS, and the reason this type is no longer sleeve-blind (#748).
    #: Under NETTING the execution engine derives a fill's position as
    #: `PositionId(f"{instrument_id}-{fill.strategy_id}")`, and `fill.strategy_id` is the ORDER's.
    #: A stop stamped with the submitting display strategy resolves to a position that has never
    #: existed, so a reduce-only fill lands on NO position and the poll fabricates the difference —
    #: the phantom mint. The intent has to name its owner before the order can.
    strategy_id: str
    #: "trail" | "entry_floor" — which of the two shapes above this is. Defaulted so every existing
    #: construction keeps meaning what it meant.
    kind: str = "trail"
    #: The absolute trigger, for `entry_floor` only. Computed ONCE, here, and never recomputed: a
    #: later pass may modify the resting order's QUANTITY and must never re-send a price.
    trigger_px: float | None = None

    def __post_init__(self) -> None:
        if self.kind == "trail":
            if self.trigger_px is not None:
                raise ValueError("a trail intent has no absolute trigger — the venue computes it")
            if self.trail_bps <= 0:
                raise ValueError(f"a trail intent needs a positive width, got {self.trail_bps}")
        elif self.kind == "entry_floor":
            if self.trigger_px is None:
                raise ValueError("an entry_floor intent IS its trigger — None cannot be placed")
            if self.trail_bps != 0:
                raise ValueError("an entry_floor intent has no trailing width")
        else:
            raise ValueError(f"unknown stop kind {self.kind!r}")


#: Every reason this planner may decline to rest a stop — a CLOSED set (#872). `/health.failed_requests`
#: renders these verbatim to an operator, and a reason invented at a call site is a string nobody can
#: search for. Adding one here is the deliberate act; `Refusal` refuses anything else.
REFUSAL_REASONS = frozenset({
    "no_price",                 # no last price, so no width and no placeability check
    "no_atr",                   # cannot measure the symbol — "wait for real data, never fabricate"
    "ceiling",                  # the trail width hit the policy ceiling: less than one ATR of cover
    "reserved_by_other_order",  # every uncovered share is locked behind another resting order
    "opted_out",                # the lane's mode is `none` — its exit rule is its own, not a stop
    "lane_unattributable",      # blank lane on an instrument some lane has a per-lane policy on
    "floor_below_market",       # the floor sits AT OR ABOVE the last price; the venue rejects it
    "no_lane_entry",            # entry_floor with no `avg_px_open` for this lane's own position
})


@dataclass(frozen=True)
class Refusal:
    """A position deliberately left unprotected, and why.

    This type exists because of how misattribution works. A case classified as an exception drops out of
    the accounting, and its exposure lands in whichever bucket is left over — here, "protected". So a
    symbol we refuse to size is not silently skipped: it is reported, and its notional stays in
    `uncovered_notional`. Correct handling makes the term KNOWN, not zero.
    """

    instrument_id: str
    reason: str        # one of REFUSAL_REASONS — closed, and enforced
    uncovered_notional: float
    #: The lane the leg was attributed to, when it was. None = the row was aggregate (no lane could be
    #: named) — an honest absence, distinct from "" (#1029).
    lane: str | None = None
    #: WHERE the policy that produced this refusal came from (`LaneProtection.source`), for the
    #: refusals a lane's POLICY caused — `opted_out`, and the entry-floor family. None for a refusal no
    #: policy was involved in (`no_price`, `lane_unattributable`). This is what makes #965's "absent
    #: key vs declared none" visible on a surface rather than true only in memory: the standing
    #: `failed_requests` row renders it, so an operator can see whether SMHGLD's opt-out is the lane's
    #: own declaration or a key someone wrote.
    source: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in REFUSAL_REASONS:
            raise ValueError(
                f"undeclared refusal reason {self.reason!r} — add it to REFUSAL_REASONS with a comment "
                "saying what an operator seeing it should do"
            )


@dataclass(frozen=True)
class Oversize:
    """Protective coverage that EXCEEDS the position behind it (codex review, Critical).

    A stop sized for more shares than are held does not merely over-protect: when it triggers it sells
    shares that are not there, so the position over-liquidates and FLIPS into opposite exposure nobody
    asked for. Flat-with-a-stop-resting is the worst case — the trigger opens a naked short from nothing.

    `target_quantity` is what the order should be shrunk TO, not by. Zero means cancel: there is no
    position left to protect.
    """

    instrument_id: str
    side: str
    held: float
    covered: float
    order: dict
    target_quantity: float


@dataclass(frozen=True)
class WrongMode:
    """A resting stop of the WRONG KIND for its lane's policy, to be cancelled on this pass (#872).

    WHY A FOURTH OUTPUT EXISTS AT ALL, and it is the whole transition problem. A correctly-sized
    resting TRAIL counts as coverage (`PROTECTIVE_TYPES` holds both kinds, deliberately), so a lane
    that has just switched to `entry_floor` reads as covered and its floor is never planned. And
    place-then-cancel is impossible: the trail RESERVES the shares, so `free` is 0 and the submit comes
    back `available: 0`.

    So the cancel happens first and the floor follows IN THE SAME PASS, once the venue CONFIRMS the
    cancel (`engine_node._await_cancel_confirmed`, #898). The naked window is the cancel-confirm
    latency (~4 s measured on an Alpaca paper instance 2026-09-10), one leg across the whole book at a time — the engine
    flips them in client-order-id order (stable across passes, present on both venue paths) and
    names the rest `queued_behind`. Measured, not reasoned: `type` is not
    patchable and `stop_price` is refused on a trailing stop (422 42210000), so this shape is
    venue-forced. A live exit standoff DEFERS the cancel (`exit_in_flight`) rather than blocking the
    floor after it; a floor the planner would refuse is pre-flighted and the trail is kept
    (`floor_unplaceable`); fewer than three minutes to the close refuses (`too_late_in_session`).

    Scoped hard, because a cancel sweep that is too wide takes protection off live positions and
    nothing puts it back:
      * only lanes whose policy is NOT the default (`entry_floor`) — never a symmetric rule that would
        eat every fixed stop on every trail lane (bracket legs, PEAK stops, an operator's own sell);
      * only orders carrying `PROTECTION_COID_PREFIX`, i.e. ones THIS mechanism placed;
      * only where `lane_of` can NAME the owner. "" is not a match, it is "the cache cannot say".
    """

    instrument_id: str
    side: str
    strategy_id: str
    order: dict
    reason: str


@dataclass(frozen=True)
class ProtectionPlan:
    """What to place, what was refused, and what already had cover — the three are exhaustive.

    Every held position lands in exactly one of `intents`, `refusals`, or `covered_instrument_ids`. A
    position in none of them has left the audit, which is how "the book is covered" becomes true by
    omission rather than by fact.

    `wrong_mode` is ORTHOGONAL to those three and does not break the claim: a leg it names is covered
    RIGHT NOW (by the stop being cancelled) and appears in `covered_instrument_ids` for this pass. It
    becomes an intent on the next one.
    """

    intents: list[StopIntent]
    refusals: list[Refusal]
    covered_instrument_ids: set[str]
    oversize: list[Oversize] = field(default_factory=list)
    wrong_mode: list[WrongMode] = field(default_factory=list)

    @property
    def uncovered_notional(self) -> float:
        """Exposure that will STILL have no stop after this plan is executed.

        Deliberately excludes `intents` — those are about to be protected — and deliberately includes every
        refusal. This is the number any "is the book protected?" report must quote, and it means "during
        RTH" (see the module docstring: a trailing stop does not trigger outside regular hours).
        """
        return sum(r.uncovered_notional for r in self.refusals)


def plan_protection(
    *,
    positions: list,
    orders: list,
    atr_by_symbol: dict,
    price_by_symbol: dict,
    multiple: float = 1.5,
    min_pct: float = 1.0,
    max_pct: float = 15.0,
    lane_of=None,
    mode_of: Callable[[str], LaneProtection] | None = None,
    entry_by_lane: dict[tuple[str, str], float] | None = None,
) -> ProtectionPlan:
    """Decide which protective stops to rest, from BROKER state. Pure — places nothing.

    `positions` and `orders` must come from the BROKER, never the local cache (#285). On 2026-08-14 the
    engine held three orders as REJECTED that Alpaca reported as `new`; a cache-based coverage check
    answers "nothing resting" and the next pass would place a DUPLICATE stop. Two differently-sized stops
    on one position both stay independently valid at Alpaca, which does not auto-reconcile — that is the
    oversell that #245 produced.

    Sizes only the SHORTFALL, never the whole position, for the same reason.

    PER-LANE MODES (#872). `mode_of` maps a strategy id to its `LaneProtection`; `entry_by_lane` maps
    `(instrument_id, strategy_id)` to that lane's OWN `avg_px_open`, read from the NETTING position
    `{instrument}-{strategy_id}`. Both default to absent, and absent means exactly the pre-#872 plan
    for every lane — pinned by a class-level equality test rather than asserted here.

    THE ENTRY IS THE LANE'S, never the account's. The broker's `avg_entry_price` mixes every lane
    holding the name, so on a shared instrument it puts one lane's floor at the other's cost basis.
    Two lanes on one name give two legs, two entries and two orders, which is correct under NETTING.

    A SCALE-IN LADDERS, BY DESIGN. `avg_px_open` re-weights when a lane adds, and the resting floor is
    never re-priced — so the added shares get a SECOND floor at the new entry. Re-pricing the first
    would move a live stop; leaving the new shares bare would report a covered position with part of it
    uncovered. A flat->reopen resets the entry (Nautilus `position.pyx:568`) and starts a fresh floor.
    """
    intents: list[StopIntent] = []
    refusals: list[Refusal] = []
    covered: set[str] = set()
    entries = entry_by_lane or {}
    #: Instruments some lane carries a NON-DEFAULT policy on. A blank-lane leg on one of these cannot
    #: be given the default mode — we do not know whether those shares belong to the exempt lane — so
    #: it is refused by name. Scoped to these instruments so one exempt lane cannot stop the rest of
    #: the book being protected.
    overridden_instruments = {
        iid for (iid, lane) in entries
        if mode_of is not None and mode_of(lane) != DEFAULT_LANE_PROTECTION
    }

    def _policy(iid: str, lane: str) -> LaneProtection | None:
        """This leg's policy, or None when the leg cannot be attributed on an overridden instrument."""
        if mode_of is None:
            return DEFAULT_LANE_PROTECTION
        if lane:
            return mode_of(lane)
        return None if iid in overridden_instruments else DEFAULT_LANE_PROTECTION

    # Aggregated by (instrument, reducing side) — NOT by instrument alone, and NOT one intent per row.
    #
    # `unprotected_positions` returns a row per POSITION so the report can attribute exposure to a sleeve.
    # Placement must not follow that shape in either direction. Keying by instrument alone silently drops
    # a sleeve: AEM held 2 by MANUAL-001 and 54 by MOMENTUM-002 planned a stop for 54 of 56, and the
    # missing 2 appeared in neither `intents` nor `refusals` — reported protected while naked. Emitting one
    # intent per row instead rests two stops totalling 56 against a 56-share position, and Alpaca keeps
    # both independently valid; that is the oversell of #245.
    #
    # The broker does not know about our sleeves. One stop per instrument per reducing side, sized to the
    # combined shortfall, is what the venue can actually hold.
    legs: dict[tuple[str, str, str], dict] = {}
    for u in unprotected_positions(positions, orders, lane_of):
        key = (u.instrument_id, "SELL" if u.quantity > 0 else "BUY", u.strategy_id)
        leg = legs.setdefault(key, {"uncovered": 0.0, "notional": 0.0})
        leg["uncovered"] += u.uncovered
        leg["notional"] += u.uncovered_notional

    # Covered is judged per LEG, not per instrument (codex review, on the exhaustiveness claim). Collapsing
    # to the instrument meant a covered LONG on a name whose SHORT leg was naked vanished from the
    # accounting entirely: the short produced a BUY intent, and the long appeared in no bucket at all.
    for p in positions:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        if (str(p.get("instrument_id")), "SELL" if qty > 0 else "BUY") not in {k[:2] for k in legs}:
            covered.add(str(p.get("instrument_id")))

    for (iid, reducing_side, lane), leg in legs.items():
        uncovered, notional = leg["uncovered"], leg["notional"]
        policy = _policy(iid, lane)
        if policy is None:
            # THREE STATES. "the cache does not sum to the broker, so we cannot say whose shares these
            # are" is not "so trail them" — on an instrument some lane has opted out of, the default
            # mode would rest exactly the stop that lane was exempted from.
            refusals.append(Refusal(iid, "lane_unattributable", notional, lane=lane or None))
            continue
        if policy.mode == "none":
            # OPTED OUT is its own answer: not covered, not naked. Its notional stays in
            # `uncovered_notional` so the exposure is KNOWN rather than absorbed into "protected".
            # The lane and the policy's provenance travel with it (#1029): "SMHGLD-007, declared" and
            # "SMHGLD-007, settings" are the two states #965 says must stay distinguishable.
            refusals.append(Refusal(iid, "opted_out", notional, lane=lane or None, source=policy.source))
            continue
        atr_value = atr_by_symbol.get(iid)
        price = price_by_symbol.get(iid)
        if not price or price <= 0:
            refusals.append(Refusal(iid, "no_price", notional))
            continue
        if not atr_value or atr_value <= 0:
            # "Wait for real data, never fabricate" — the same rule the managers follow. A symbol we
            # cannot measure gets no stop rather than an arbitrary one.
            refusals.append(Refusal(iid, "no_atr", notional))
            continue
        # The lane's own multiple where it has one; the domain's otherwise. `None` means INHERIT and
        # can never be read as zero — see `LaneProtection`.
        k = policy.atr_multiple if policy.atr_multiple is not None else multiple
        width = None
        trigger_px = None
        if policy.mode == "entry_floor":
            entry = entries.get((iid, lane))
            if entry is None or entry <= 0:
                # NOT the account average, and not a distance from the last price either. A floor whose
                # entry we do not have is a floor we cannot compute, and naming that is the answer.
                refusals.append(Refusal(iid, "no_lane_entry", notional))
                continue
            # ONE computation, at placement. Nothing re-derives this on a later pass.
            trigger_px = entry - k * atr_value if reducing_side == "SELL" else entry + k * atr_value
            # THE SIBLING THAT WOULD HAVE KILLED THE FIRST DEPLOY. A SELL stop at or above the last
            # price is rejected pre-submit (`providers/alpaca/exec_client.py`), so submitting one burns
            # a client order id every tick for an order that cannot exist. The trail can never reach
            # this branch — it carries no absolute trigger at all.
            through_market = trigger_px >= price if reducing_side == "SELL" else trigger_px <= price
            if through_market:
                refusals.append(Refusal(iid, "floor_below_market", notional))
                continue
        else:
            width = trail_width(price, atr_value, multiple=k, min_pct=min_pct, max_pct=max_pct)
            if width is None:
                refusals.append(Refusal(iid, "no_atr", notional))
                continue
            if not width.placeable:
                # Ceiling clamp: the symbol's normal movement is wider than policy allows us to sit
                # through, so the order would protect less than one ATR. Refusing surfaces "this
                # position is too volatile for its size"; placing it anyway would report protection
                # that is not there.
                refusals.append(Refusal(iid, "ceiling", notional))
                continue
        # Size to what is PLACEABLE, not to what is uncovered (#287). Shares already reserved by another
        # resting reducing order cannot carry a second one — the venue rejects the whole request, so asking
        # for the full shortfall protects NOTHING rather than protecting what it could.
        held_total = sum(
            abs(_signed_qty(p)) for p in positions
            if str(p.get("instrument_id")) == iid
            and (("SELL" if _signed_qty(p) > 0 else "BUY") == reducing_side)
        )
        free = max(0.0, held_total - reserved_quantity(orders, iid, reducing_side))
        placeable = min(uncovered, free)
        if placeable <= _QTY_EPS:
            # Every uncovered share is locked behind another order. Reported, never silently skipped: it
            # genuinely cannot be protected while that order rests, and the audit must say so.
            refusals.append(Refusal(iid, "reserved_by_other_order", notional))
            continue
        if placeable < uncovered - _QTY_EPS:
            # Partly placeable. The remainder is a real, reportable gap.
            refusals.append(Refusal(
                iid, "reserved_by_other_order", notional * (uncovered - placeable) / uncovered,
            ))
        intents.append(StopIntent(
            instrument_id=iid,
            side=reducing_side,
            quantity=placeable,
            trail_bps=width.bps if width is not None else 0,
            # The symbol's own volatility, reported for BOTH kinds — it is what an operator reads to
            # judge whether the stop is one ATR away or a tenth of one, and a floor needs that as much
            # as a trail does.
            atr_pct=width.atr_pct if width is not None else atr_value / price * 100,
            clamped=width.clamped if width is not None else None,
            strategy_id=lane,
            kind=policy.mode,
            trigger_px=trigger_px,
        ))

    # Oversize is derived from the ORDERS, not from `positions` — a position that closed entirely is
    # absent from `positions`, and its orphan stop is exactly the case that matters most.
    held_by_leg: dict[tuple[str, str], float] = {}
    for p in positions:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        key = (str(p.get("instrument_id")), "SELL" if qty > 0 else "BUY")
        held_by_leg[key] = held_by_leg.get(key, 0.0) + abs(qty)

    oversize: list[Oversize] = []
    seen_legs: set[tuple[str, str]] = set()
    for o in orders:
        iid = str(o.get("instrument_id") or "") or None
        symbol = str(o.get("symbol") or "")
        side = "SELL" if str(o.get("side", "")).lower() == "sell" else "BUY"
        # Resolve the leg against a position when one exists, else against the order's own symbol.
        leg = next(
            (k for k in held_by_leg if k[1] == side and _same_instrument(symbol or iid or "", k[0])),
            (iid or symbol, side),
        )
        if leg in seen_legs:
            continue
        protective = protective_orders(orders, leg[0], side)
        if not protective:
            continue
        seen_legs.add(leg)
        covered_qty = sum(x["_remaining"] for x in protective)
        held = held_by_leg.get(leg, 0.0)
        if covered_qty <= held:
            continue
        # Correct as MANY orders as it takes, largest first, until what remains resting equals what is
        # held (codex review, Critical). Shrinking only the largest is not enough: held 50 against stops
        # of 40+40+40 has an excess of 70, so the largest clamps at 0 and 80 still covers 50 — still able
        # to over-liquidate and flip the position. Largest-first so the fewest orders are touched.
        #
        # With nothing held, every one of them is cancelled: each is independently able to open a naked
        # short out of nothing.
        excess = covered_qty - held
        for order in sorted(protective, key=lambda x: -x["_remaining"]):
            if excess <= _QTY_EPS:
                break
            take = min(order["_remaining"], excess)
            oversize.append(Oversize(
                instrument_id=leg[0],
                side=side,
                held=held,
                covered=covered_qty,
                order=order,
                target_quantity=max(0.0, order["_remaining"] - take),
            ))
            excess -= take

    # WRONG-MODE CANCELS (#872). Derived from the POSITIONS rather than from `legs`, and that is the
    # whole point: a lane whose trail rests is fully COVERED, so it never reaches `legs` at all and a
    # bucket built from them would be permanently empty. See `WrongMode` for why the cancel and the
    # placement cannot happen on the same pass.
    wrong_mode: list[WrongMode] = []
    if mode_of is not None:
        seen_coids: set[str] = set()
        for p in positions:
            qty = _signed_qty(p)
            if qty == 0:
                continue
            iid = str(p.get("instrument_id"))
            reducing_side = "SELL" if qty > 0 else "BUY"
            lane = str(p.get("strategy_id") or "")
            if not lane:
                continue  # cannot say whose shares these are, so cannot say whose stop rests on them
            policy = mode_of(lane)
            if policy.mode != "entry_floor":
                continue
            for o in protective_orders(orders, iid, reducing_side):
                coid = str(o.get("client_order_id") or "")
                if not coid or coid in seen_coids:
                    continue
                if not coid.startswith(PROTECTION_COID_PREFIX):
                    continue  # not ours: a bracket leg, a PEAK trail, an operator's own sell
                if str(o.get("type") or o.get("order_type") or "") not in _TRAILING_TYPES:
                    continue  # already a fixed stop — the kind this lane's policy asks for
                if lane_of is None or lane_of(coid) != lane:
                    continue  # "" is "the cache cannot name it", never a match
                seen_coids.add(coid)
                wrong_mode.append(WrongMode(
                    instrument_id=iid, side=reducing_side, strategy_id=lane, order=o,
                    reason=f"{lane} is on entry_floor and this is a trailing stop",
                ))

    return ProtectionPlan(
        intents=intents, refusals=refusals, covered_instrument_ids=covered, oversize=oversize,
        wrong_mode=wrong_mode,
    )


def broker_rows(positions: list, resolve_instrument_id) -> tuple[list[dict], list[str]]:
    """Alpaca REST position dicts → the row shape `plan_protection` reads. Pure.

    Returns `(rows, unresolved_symbols)`. The second element is NOT optional bookkeeping: a symbol whose
    instrument id cannot be resolved is a position holding real exposure, and dropping it silently removes
    that exposure from the audit — the caller must report it, exactly like a `Refusal`. Same reasoning as
    `Refusal` itself: a case classified as an exception lands in whichever bucket is left over.

    Alpaca reports `qty` SIGNED (negative for a short) and also carries an explicit `side`. Both are
    honoured, with `side` winning, because `_signed_qty` already does that and two derivations of one fact
    will disagree.
    """
    rows: list[dict] = []
    unresolved: list[str] = []
    for p in positions:
        symbol = str(p.get("symbol") or "")
        iid = resolve_instrument_id(symbol) if symbol else None
        if not iid:
            unresolved.append(symbol)
            continue
        try:
            qty = float(p.get("qty") or 0)
            mv = float(p.get("market_value") or 0)
        except (TypeError, ValueError):
            unresolved.append(symbol)
            continue
        side = str(p.get("side") or "").upper()
        rows.append({
            "instrument_id": iid,
            "quantity": abs(qty),
            # Alpaca says "long"/"short"; fall back to the sign when it is absent.
            "side": side if side in ("LONG", "SHORT") else ("SHORT" if qty < 0 else "LONG"),
            "market_value": mv,
            "strategy_id": "",   # the broker does not know about our sleeves
        })
    return rows, unresolved


def position_rows_from_reports(reports: list, price_of) -> list[dict]:
    """Nautilus `PositionStatusReport`s → the row shape `plan_protection` reads. Pure.

    The venue-neutral sibling of `broker_rows`, for the typed position source (#641): the shipped
    Interactive Brokers adapter and our Alpaca client both produce these reports, so this is what lets
    the protection reconciler run on a node with no Alpaca REST client at all. Instrument ids arrive
    typed — there is no symbol to resolve and nothing can be unresolved.

    `market_value` is DERIVED as signed quantity x `price_of(instrument_id)`, because the typed report
    does not carry the broker's own statement of it (`PositionStatusReport` has no such field —
    test_broker_read_gaps.py pins that). On the typed path this derivation is THE source, not a second
    one beside the broker's; `test_the_typed_and_rest_position_sources_produce_the_SAME_rows` pins the
    two paths equal where both exist. A missing price yields market_value 0.0 with the row KEPT: the
    same price source feeds `plan_protection`, whose `no_price` refusal then reports the leg — dropping
    the row instead would silently call the position protected.

    FLAT and zero-quantity rows are skipped: adapters may report them, and a zero-share "position"
    would key a leg and dilute the covered/naked accounting.
    """
    rows: list[dict] = []
    for r in reports:
        if r is None:
            continue
        qty = float(r.quantity)
        side = r.position_side.name
        if qty == 0 or side not in ("LONG", "SHORT"):
            continue
        iid = str(r.instrument_id)
        signed = -qty if side == "SHORT" else qty
        price = price_of(iid)
        rows.append({
            "instrument_id": iid,
            "quantity": abs(qty),
            "side": side,
            "market_value": 0.0 if price is None else signed * float(price),
            "strategy_id": "",   # the broker does not know about our sleeves
        })
    return rows


@dataclass(frozen=True)
class PeakWidths:
    """PEAK's two trail widths for one symbol, both scaled to its own volatility (#288)."""

    wide_bps: int
    tight_bps: int
    atr_pct: float


def peak_trail_bps(
    *,
    price: float,
    atr_value: float,
    wide_multiple: float,
    tight_multiple: float,
    min_pct: float = 1.0,
    max_pct: float = 15.0,
) -> PeakWidths | None:
    """PEAK's wide and tight trails, derived from the symbol's ATR rather than a fixed percentage.

    Found on the live book 2026-08-14: FIG carried a PEAK trail of a flat 2.5% against an ATR of 7.94% —
    **0.31x ATR**, which fires on an ordinary session rather than on anything going wrong. The same 2.5%
    is 1.52x ATR on VFLO. A single percentage cannot serve a book whose ATR spans 1.64% to 11.18%, which
    is the argument #239 already settled for backstop stops; PEAK was simply never revisited.

    Deliberately built on `trail_width`, so "how wide should a stop on this symbol be" has ONE derivation.
    Two disagree, and these two already did by a factor of five — #239 computed 11.91% for FIG while PEAK
    rested 2.5%, on the same instrument at the same moment.

    Returns None when the wide leg hits the CEILING, matching the backstop's refusal: past that width the
    order protects less than one ATR and gives false comfort. Also None when it cannot be computed at all —
    no guessing a width, the same "wait for real data, never fabricate" rule the managers follow.
    """
    wide = trail_width(price, atr_value, multiple=wide_multiple, min_pct=min_pct, max_pct=max_pct)
    if wide is None or not wide.placeable:
        return None
    tight = trail_width(price, atr_value, multiple=tight_multiple, min_pct=min_pct, max_pct=max_pct)
    if tight is None:
        return None
    # The blowoff branch exists to TIGHTEN. If the floor has lifted both legs to the same width the branch
    # would be a no-op, so the tight leg is stepped below it — never under Alpaca's 0.1% minimum.
    tight_bps = min(tight.bps, wide.bps - 1) if tight.bps >= wide.bps else tight.bps
    tight_bps = max(10, tight_bps)   # never under Alpaca's own 0.1% minimum
    if tight_bps >= wide.bps:
        # The floor has collapsed both legs onto the venue minimum, so the tighten would change nothing
        # (codex review, Medium — reachable because `minTrailPct` bottoms out at 0.1 in the schema).
        # Refuse rather than arm a manager whose blowoff branch is a silent no-op: a tighten that does
        # nothing is worse than a refusal, because only the refusal is reported.
        return None
    return PeakWidths(wide_bps=wide.bps, tight_bps=tight_bps, atr_pct=wide.atr_pct)
