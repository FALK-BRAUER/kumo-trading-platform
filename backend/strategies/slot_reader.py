"""A cheap, sync, never-raising settings read for the adapters' `_arm()` callbacks (#514).

WHY THIS EXISTS SEPARATELY. kumo-trading-strategies db25413 lets an adapter re-read its decision slot on every
re-arm, by calling a callable cockpit supplies. That callable runs INSIDE the Nautilus clock callback,
so it has three hard constraints and each has already been paid for once here:

  SYNC     an async reader is awaited nowhere; it returns a coroutine, the adapter rejects it as
           non-numeric, and the knob is dead again while looking fixed.
  CHEAP    blocking I/O on the event loop is the #377 shape — `on_start` reaching a socket took
           MANUAL, MOMENTUM and BCTROT down together on 2026-08-22. `settings.resolve` is a file
           read, measured at 1.22 ms on the engine, so the TTL below exists to collapse a burst of
           arms rather than to make a single read affordable.
  SAFE     never raise. A lane that stopped scheduling looks exactly like one that decided to hold,
           which is why the adapter re-arms BEFORE deciding. Returning None keeps the current
           schedule; raising would put that guarantee at the mercy of the settings store.

THE TTL IS DELIBERATELY SHORT. `_arm` runs a handful of times a day, so 5s is effectively "read fresh
every arm" — the cache is there for the case where several lanes arm on the same tick, not to avoid
reading. A long TTL would reintroduce the very staleness this fixes.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

_TTL_NS = 5_000_000_000
_cache: dict[str, tuple[int, dict]] = {}


def invalidate() -> None:
    """Drop the cache. For tests, and for anything that knows settings just changed."""
    _cache.clear()


def resolve_cached(domain: str) -> dict | None:
    """`settings.resolve(domain)`, memoised for `_TTL_NS`. None when it cannot be read at all."""
    now = time.monotonic_ns()
    hit = _cache.get(domain)
    if hit and now - hit[0] < _TTL_NS:
        return hit[1]
    try:
        from api.settings import resolve

        values = resolve(domain)
    except Exception as exc:  # noqa: BLE001 — see SAFE above
        _log.warning("slot re-read: settings domain %r unavailable (%r) — keeping the current "
                     "schedule", domain, exc)
        return None
    _cache[domain] = (now, values)
    return values


def _live_reread_kwargs(strategy_cls, **readers) -> dict:
    """Only the re-reader kwargs the INSTALLED adapter actually accepts (#514).

    THIS REPO PINS kumo-trading-strategies BY REVISION, and the seam arrived in db25413. Passing
    `read_open_offset=` to an adapter that predates it raises
    `TypeError: __init__() got an unexpected keyword argument` — at BUILD, which takes the node down
    before any lane registers. That is the #377 shape: one strategy's construction killing every
    other lane.

    Same guard `momentum._limits_for_session` already carries for `RiskLimits.allocated_equity`, and
    for the same reason: the local venv and the running container can disagree about which revision is
    installed, so a cross-repo attribute is checked before it is used, never assumed.

    Degrades to TODAY'S BEHAVIOUR — the slot stays captured at build, which is the pre-#514 state — and
    says so once, rather than failing to boot.
    """
    import inspect as _inspect

    try:
        accepted = _inspect.signature(strategy_cls.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover — a C-level __init__ has no signature
        return {}
    out: dict = {}
    missing: list[str] = []
    for name, fn in readers.items():
        if name in accepted:
            out[name] = fn
        else:
            missing.append(name)
    if missing:
        _log.warning(
            "%s: installed kumo-trading-strategies does not accept %s — the decision slot stays captured at "
            "build and the settings knob needs a redeploy (#514). Pin a revision with db25413.",
            getattr(strategy_cls, "__name__", strategy_cls), ", ".join(missing))
    return out
