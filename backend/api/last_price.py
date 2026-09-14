"""ONE price for the budget gate — the LANES' own sequence, mirrored here until it can be imported (#990).

WHY A MIRROR EXISTS AT ALL. The lanes size their orders with
`kumo_strategies.runtime.nautilus.broker.NautilusBroker.last_price`, whose sequence is
`cache.price` → `cache.quote_tick` → `cache.trade_tick` → the freshest BAR. That sequence lives inline
in a METHOD that needs `self.strategy.cache` and `self._iid`, so the gate cannot call it. The ruling
(coordinator, 2026-09-11 11:20Z): a safety fix does not queue behind an elegance fix — ship the gate
pricing from the same sequence, MIRRORED, with the source-reading pin beside it so the two cannot
diverge unnoticed, and a named follow-up that deletes the mirror and the pin together the day
kumo-strategies exposes `last_price_from_cache(cache, instrument_id, …)` (asked of l21wvpmj; the
follow-up ticket number is recorded in `FOLLOW_UP`). Until then `test_budget_guard_prices_from_nothing`
reads the installed broker's source and refuses to pass if its sequence and `SEQUENCE` differ.

WHAT IS DELIBERATELY DIFFERENT, and said here rather than discovered:
  * the bar fallback. The lane reads ITS OWN subscription's bar (`self.strategy._bar_type(iid)`); the
    gate has no lane, so it reads the FRESHEST bar the cache holds for the instrument across every
    bar type the node subscribes. For a market order that is the better number; for the pin it is a
    known, named difference, not drift.
  * `cache.price(iid)` is called with ONE argument, exactly as the lane calls it; on Nautilus 1.229
    `Cache.price` requires a `price_type` and raises TypeError, which the lane's loop swallows — so
    the effective sequence on both sides is quote (bid) → trade → bar. Mirrored faithfully rather than
    corrected: correcting it here would make the gate price from a source the lane does not, which is
    the drift this module exists to prevent. Reported upstream as its own finding.
  * every answer carries its SOURCE and AGE (`ts_event`), so the journal row can say which derivation
    priced the order; a QuoteTick prices at the bid, as the lane's attribute loop does.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: The lane's sequence, verbatim — `test_the_mirror_matches_the_installed_lanes_sequence` pins it.
SEQUENCE: tuple[str, ...] = ("price", "quote_tick", "trade_tick")
#: The follow-up that deletes this module: kumo-strategies exposing `last_price_from_cache`.
FOLLOW_UP = "kumo-cockpit#990 (follow-up: import `last_price_from_cache` from kumo_strategies, delete this mirror and its pin)"


@dataclass(frozen=True)
class Priced:
    price: float | None
    source: str            # "quote_tick" | "trade_tick" | "bar:<bar_type>" | "missing" | the tried list on refusal
    age_ns: int | None     # now - ts_event where the source carries one; None for a bare Price


def _as_float(v) -> float | None:
    for attr in ("as_double", "value"):
        if hasattr(v, attr):
            got = getattr(v, attr)
            try:
                return float(got() if callable(got) else got)
            except Exception:                                      # noqa: BLE001
                return None
    for attr in ("last_px", "bid_price", "price"):
        if hasattr(v, attr):
            try:
                return float(getattr(v, attr).as_double())
            except Exception:                                      # noqa: BLE001
                try:
                    return float(getattr(v, attr))
                except Exception:                                  # noqa: BLE001
                    return None
    return None


def _usable(px: float | None) -> bool:
    """Zero and non-finite are the ABSENCE of a price, never a price (#990, #954's nan)."""
    return px is not None and math.isfinite(px) and px > 0.0


def last_price(cache, instrument_id, *, max_age_ns: int | None = None, now_ns: int | None = None) -> Priced:
    """The lane's sequence over `cache` for `instrument_id`, with the source and age of the answer.

    `max_age_ns` is opt-in, as upstream: with a bound, a source with no `ts_event` is skipped rather
    than treated as fresh, and an older-than-bound source is skipped rather than accepted.
    """
    tried: list[str] = []
    for get in SEQUENCE:
        if max_age_ns is not None and get == "price":
            continue                        # a bare Price carries no ts_event — cannot honour the bound
        fn = getattr(cache, get, None)
        if fn is None:
            continue
        try:
            v = fn(instrument_id)
        except Exception:                                          # noqa: BLE001
            tried.append(f"{get}:raised")
            continue
        if v is None:
            tried.append(f"{get}:none")
            continue
        age = None
        if now_ns is not None and hasattr(v, "ts_event"):
            age = int(now_ns) - int(v.ts_event)
        if max_age_ns is not None and age is not None and age > max_age_ns:
            tried.append(f"{get}:stale({age})")
            continue
        px = _as_float(v)
        if _usable(px):
            return Priced(px, get, age)
        tried.append(f"{get}:unusable({px})")
    # the freshest bar the cache holds for this instrument, across the node's bar types
    try:
        bar_types = cache.bar_types(instrument_id=instrument_id)
    except Exception:                                              # noqa: BLE001
        bar_types = []
    best = None
    for bt in bar_types or []:
        try:
            bar = cache.bar(bt)
        except Exception:                                          # noqa: BLE001
            continue
        if bar is None:
            continue
        if best is None or int(bar.ts_event) > int(best[1].ts_event):
            best = (bt, bar)
    if best is not None:
        bt, bar = best
        age = (int(now_ns) - int(bar.ts_event)) if now_ns is not None else None
        if max_age_ns is not None and age is not None and age > max_age_ns:
            tried.append(f"bar:{bt}:stale({age})")
        else:
            px = _as_float(bar.close) if hasattr(bar, "close") else None
            if _usable(px):
                return Priced(px, f"bar:{bt}", age)
            tried.append(f"bar:{bt}:unusable({px})")
    else:
        tried.append("bar:none")
    return Priced(None, "missing (tried " + ", ".join(tried) + ")", None)
