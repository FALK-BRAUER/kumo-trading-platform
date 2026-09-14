"""Scheduled refresh of QC345's ranking universe (#324).

The derivation measured 373.8s against the live account — 33,431 assets, 1.5M bar rows over 5,499
symbols. That number is what puts this in a scheduled job instead of in `build_qc345_strategy`: six
minutes inside synchronous node startup would hold every other strategy AND the UI data feed behind
a refresh of a universe that changes MONTHLY.

So the universe is written to `strategies.QC345_UNIVERSE` ahead of time and the builder just reads
it. The operator-supplied setting was never a scaffold; it is the interface this writes to, which is
why nothing about the builder changes here.

    OFF BY DEFAULT, via `strategies.QC345_UNIVERSE_REFRESH` in settings (CLAUDE.md: all new
    automation gates default False). Off means the universe stays exactly what an operator typed.

WHY THE WRITE IS A MERGE, WHICH IS THE PART THAT WOULD HAVE BITTEN
------------------------------------------------------------------
`settings.save(domain, incoming)` writes the WHOLE domain: keys absent from `incoming` are dropped
from the values file and silently fall back to their schema defaults on the next resolve. The
`strategies` domain also holds every budget target and `TRANSFER_TO`.

So `save("strategies", {"QC345_UNIVERSE": [...]})` would not fail, would not warn, and would reset
MOMENTUM's and QC345's allocations to their defaults — a monthly job quietly undoing an operator's
capital decisions, discovered whenever someone next looked at the numbers. Read-merge-write, and a
test asserts the budgets survive.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

#: Calendar days of history to fetch. QC345 needs 254 sessions of warmup; 420 calendar days covers
#: it with slack for holidays. Measured at 373.8s end to end.
HISTORY_DAYS = 420

#: How far a refresh may fall behind before the universe is treated as suspect. QC345 rebalances
#: monthly, so a universe older than this has missed at least one rebalance's worth of listings,
#: delistings and liquidity drift.
STALE_AFTER_DAYS = 45


def _enabled() -> bool:
    """From SETTINGS, not the environment — see `qc345._enabled` for why. Same single-source rule."""
    from api import settings

    return bool(settings.resolve("strategies").get("QC345_UNIVERSE_REFRESH"))



def refresh_universe(*, write: bool = True) -> tuple[list[str], dict]:
    """Derive the universe and persist it. BLOCKING — ~6 minutes of REST. Never call this on the
    node's event loop; `schedule_refresh` hands it to a worker thread.

    Returns (symbols, diagnostics). Raises rather than returning an empty universe: a refresh that
    quietly wrote `[]` would make the next node start refuse to boot with a message about an
    operator-supplied setting the operator never touched.
    """
    import pandas as pd

    from strategies import qc345_universe as U
    from strategies.qc345 import _live_config

    cfg = _live_config()
    assets = U.fetch_assets()
    names = U.substrate(assets, cfg)
    start = (pd.Timestamp.now("UTC").normalize() - pd.Timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    bars = U.fetch_daily_bars(names, start=start)
    selected, diag = U.derive_universe(bars, assets, cfg)

    if not selected:
        raise RuntimeError(
            "qc345 universe refresh selected nothing — refusing to persist an empty universe, which "
            "would make the next node start refuse to boot and blame an operator setting")
    if write:
        persist_universe(selected)
    _log.info("qc345 universe refreshed: %d names, substrate %s, rankable %s",
              len(selected), diag.get("substrate"), diag.get("rankable_names"))
    return selected, diag


def persist_universe(symbols: list[str]) -> dict:
    """Write the universe into `strategies` WITHOUT disturbing anything else in the domain.

    `settings.save` writes the whole domain, so the current values are read first and merged. Passing
    only this key would drop every budget target from the values file and silently reset each sleeve
    to its schema default — a monthly job quietly undoing an operator's capital decisions.
    """
    from api import settings

    current = dict(settings.resolve("strategies"))
    current["QC345_UNIVERSE"] = sorted(symbols)
    return settings.save("strategies", current)


def schedule_refresh(strategy) -> None:
    """Arm a daily refresh on the node's own clock, off the event loop.

    NAUTILUS OWNS THE SCHEDULE. `Clock.set_timer` is the native mechanism and it works in backtest
    too; hand-rolling asyncio sleeps plus launchd is a documented past mistake in this repo.

    DAILY rather than monthly even though the rebalance is monthly, and that is deliberate: a monthly
    timer that misses its fire — restart, redeploy, an exception — leaves the universe stale for a
    whole cycle before anyone finds out. A daily refresh is idempotent, and the cost of the extra
    runs is one background thread doing REST while nothing waits on it.

    Off the loop via `run_in_executor`: the fetch is blocking `urllib` for ~6 minutes, and running it
    on the node's loop would stall market data, order events and the UI feed for that entire window.
    """
    import asyncio

    import pandas as pd

    if not _enabled():
        return

    loop = getattr(strategy, "_loop", None)
    if loop is None:
        _log.warning("qc345 universe refresh enabled but no event loop was captured — not scheduling")
        return

    def _tick(event=None) -> None:
        async def _run() -> None:
            try:
                # to_thread, not await: `refresh_universe` is blocking REST. Awaiting it directly
                # would freeze the loop for the whole six minutes.
                await asyncio.to_thread(refresh_universe)
            except Exception as exc:  # noqa: BLE001 — a universe refresh must not kill the node
                _log.error("qc345 universe refresh failed, keeping the last known universe: %r", exc)

        asyncio.run_coroutine_threadsafe(_run(), loop)

    strategy.clock.set_timer(name="qc345_universe_refresh",
                             interval=pd.Timedelta(days=1), callback=_tick)


def universe_age_days(now=None) -> float | None:
    """How old the persisted universe is, in days. None if it has never been written.

    Exists so a stale universe is VISIBLE. A refresh that silently stopped firing looks exactly like
    one that has nothing new to say — the universe simply stops moving, and a monthly strategy would
    take months to make that obvious.
    """
    import pandas as pd

    from api.settings import store

    path = store._values_path("strategies")
    if not path.exists():
        return None
    stamp = pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
    return float(((pd.Timestamp(now) if now is not None else pd.Timestamp.now("UTC")) - stamp).days)
