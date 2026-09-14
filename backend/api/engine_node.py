"""Engine process (issue #20) — the standalone Nautilus trading node.

Runs the live `TradingNode` (Databento data + IBKR exec) in its OWN OS process, so the UI/FastAPI
process can crash or restart without stopping trading. Blocking `node.run()` — its own loop and signal
handling, no uvicorn to share with.

The node holds NO display logic beyond a thin bridge: `UiFeedStrategy` loads the configured universe
(instrument definition → historical bars → live subscription, per granularity) and publishes what it sees
to a **Redis Stream** (`ui:stream`). Redis I/O runs on a background writer thread fed by a bounded queue,
so a stalled/slow Redis can NEVER block the trading loop — frames are dropped on backpressure instead.
The separate UI process reads that stream (see `api/consumer.py`). Native msgbus→Redis streaming is
unavailable on the standard wheel (#19), so this in-node bridge is the sanctioned path.

Run:  backend/scripts/run-engine.sh   (injects secrets, python -m api.engine_node)
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
import math
import os
import queue
import threading
import time
import time as _time
import uuid
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from functools import partial
from math import isfinite

import pandas as pd
import redis
from nautilus_trader.config import (
    CacheConfig,
    DatabaseConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderType, TimeInForce
from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderSubmitted, PositionClosed
from nautilus_trader.model.identifiers import (
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# VALIDATES THE DECLARED REGISTRY AT BOOT (#888) — see `api.app` for why the import is eager;
# this process imports `budget_store` lazily inside a try/except that would swallow the refusal.
import api.strategy_registry  # noqa: F401
from api.bar_spec import (
    DEFAULT_GRANULARITY,
    DEFN_LOOKBACK,
    GRANULARITIES,
    aggregation_plan,
    bar_type,
    granularity_of,
)
from api.boot_gate import BootGateState as _BootGateState
from api.build_info import build_stamp, build_stamp_line
from api.cancel_attribution import (
    OUR_STOP_PREFIXES,
    may_cancel_order,
    owner_from_prefix,
)
from api.command_ledger import CommandLedgerStore, Reserve, payload_hash
from api.cycle_store import CycleEnvelopeStore, envelope_from_dto, envelope_key
from api.data_types import FundamentalsData, fundamentals_data_type, nan_if_none
from api.db.engine import database_url
from api.external_activity import classify_external
from api.feed_config import BAR_KINDS, STATE_KEY_PREFIX, STATE_KINDS, FeedConfig, load_feed_config
from api.managers import register_manager
from api.order_actions import OrderBuildContext, get_action
from api.failed_requests import FailedRequests
from api.market_aware import (
    ASSESSMENT_RECOVERED, CRITICAL, EMERGENCY_CLEARED, EMERGENCY_TRIGGERED, EXIT_ONLY_ENTERED, EXIT_ONLY_LEFT,
    HOOK_FAULT, HOOK_RECOVERED, HOOK_UNKNOWN_TWICE, NOTIFY, STAND_DOWN_REQUESTED, as_payload, contract_state,
    notification, poll_lane,
)
from api.subscription_ledger import SubscriptionLedger
from api.book_truth import DisagreementStreaks, InferredFills, TerminalFills, position_truth, truth_summary
from api.contra_ledger import ContraLedger
from api.observation import Observations
from api.protective_reconcile import reconcile_protection
from api.protective_stamp import stamp_diagnosis, stamp_for
from api.protection import PROTECTION_COID_PREFIX as _PROT_PREFIX
from api.protection import atr as _atr
from api.protection import is_resting as _is_resting
from api.providers import build_data_client_spec, build_exec_client_spec
from api.bus_topics import (  # shared bus topics — one NEUTRAL definition (#41 / #26 / #608)
    ACCOUNT_TOPIC as _ACCOUNT_TOPIC,
    EQUITY_TOPIC as _EQUITY_TOPIC,
    RECONCILE_TOPIC as _RECONCILE_TOPIC,
    UNRECONCILED_TOPIC as _UNRECONCILED_TOPIC,
)

# ALLOWLISTED in test_import_boundary.py (#608): the today's-range and realized-periods sweeps are
# documented Alpaca-only and gated on `data_provider == "alpaca"`. The neutral shape — a
# provider-neutral broker-history port — is proposed on #608; until it lands these two imports are
# the named debt, not a precedent.
from api.providers.alpaca.config import AlpacaDataClientConfig
from api.providers.alpaca.http import AlpacaHttpClient
from api.providers.databento import dataset_for_venue, request_end
from api.providers.fmp.http import FmpHttpClient
from api.snapshot_reader import read_restored_legs
from api.strategy_ids import MANUAL_NAME, MANUAL_TAG
from api.realized import empty_windows, et_day_bounds_ns, et_window_start_ns, lane_flows_by_day, realized_windows
from api.trade_cycle import CycleLeg, TradeCycleProjection, newer_close_wins
from api.venue_calendar import VenueCalendar

_log = logging.getLogger("kumo.engine_node")

#: Every strategy's order events. Nautilus's exec engine publishes each order event to
#: `events.order.{strategy_id}` (nautilus_trader/execution/engine.pyx), and `Strategy.on_start`
#: subscribes only its OWN id — so a display strategy that wants the whole book has to ask the bus
#: for the wildcard. `MessageBus.subscribe` documents `*` and `?` as supported patterns.
_ORDER_EVENTS_TOPIC = "events.order.*"
#: Same hole, same fix, one plane over (#846): `Strategy.on_start` subscribes `events.position.{self.id}`
#: (trading/strategy.pyx:322-323), so a leg registry fed from `on_position_event` sees MANUAL-001's
#: closes and no other lane's. The exec engine publishes to `events.position.{strategy_id}` per position.
_POSITION_EVENTS_TOPIC = "events.position.*"
_SNAPSHOT_TIMER = "ui_position_snapshot"
_REFETCH_TIMER = "ui_bar_refetch"
_SESSION_STATE_TIMER = "kumo-session-state"
_WATCHLIST_TIMER = "ui_symbol_reconcile"
_WATCHLIST_SECS = 5  # reconcile the persistent watchlist (Postgres) for symbols to load + stream
#: Strategy-state cadence (#212). Its own timer, NOT the 5s watchlist tick it was first hung on: the
#: frame is three Postgres SELECTs and ~6 KB, and a session decides once a morning. 20s is already
#: far tighter than the data changes.
_SESSION_STATE_SECS = 20
#: Shorter than the interval, so a stalled query is abandoned before the next tick would fire.
_SESSION_STATE_TIMEOUT = 10.0
#: #873 phase 1 — ask every registered lane's market-aware hooks. Cadence from settings at start.
_MARKET_AWARE_TIMER = "kumo-market-aware"
_MANAGER_TIMER = "manager_dispatch"
#: Hourly log compaction + retention (#758). Nautilus rotates by SIZE and has no notion of
#: days, so age-based retention lives here.
_COMPACTION_TIMER = "log-compaction"
_COMPACTION_SECS = int(os.environ.get("KUMO_LOG_COMPACT_SECS", 3600))
_MANAGER_TICK_SECS = 30  # how promptly a queued manager fires after its trigger — not latency-critical
_FUNDAMENTALS_TIMER = "ui_fundamentals_refresh"
#: Backstop protective stops (#239). Slow on purpose: this rests orders at the VENUE, so a fast cadence
#: buys nothing and multiplies the chance of racing a fill. 60s is well inside the window that matters —
#: the failure it exists for is a thirteen-hour outage, not a thirteen-second one.
def derived_account_frame(*, equity: float, cash: float, cash_ccy, standing, ts: int) -> dict:
    """The derived account frame for a node with NO broker publisher (IBKR) — shared body for the
    msgbus snapshot and the UI frame, so the two cannot drift (#591).

    `long_market_value = equity - cash` is the DTO's OWN identity (`cash + lmv == equity`) evaluated
    from two broker-supplied fields — not a mark-derived estimate, and true by construction on a flat
    book (0.0). The literal 0.0 it replaces was written for the flat branch and survived the branch
    growing a held-book path: staging published lmv 0.00 beside a held book, off by 25,393.12 against
    its own invariant, and BookTile rendered a zero market value over real positions.

    `buying_power` stays cash — a conservative floor, NOT the venue's figure; its honest treatment is
    #590's subject and changing its meaning here would silently move a risk gate.
    """
    return {"equity": equity, "cash": cash, "buying_power": cash, "multiplier": 1.0,
            "long_market_value": round(equity - cash, 2), "last_equity": None,
            "unrealized_standing_total": standing,
            "unrealized_intraday_total": None,
            "currency": str(cash_ccy) if cash_ccy is not None else None,
            "ts": ts}


def _broker_qty_by_symbol(position_reports) -> dict[str, float] | None:
    """Venue position reports -> `{bare symbol: signed net qty}`, or None when the venue was unreadable.

    The same fold `_venue_net_position` performs for one instrument, done once for all of them. Reads
    the typed report's `position_side` / `quantity` — the shape the shipped IBKR and Alpaca clients
    both produce — and ignores FLAT. `None` in, `None` out (#649: a broker-read cache must be able to
    return to UNKNOWN); an empty list is a real answer and folds to `{}`.
    """
    if position_reports is None:
        return None
    out: dict[str, float] = {}
    for r in position_reports:
        side = getattr(getattr(r, "position_side", None), "name", None)
        if side not in ("LONG", "SHORT"):
            continue
        sym = str(r.instrument_id).rsplit(".", 1)[0]
        qty = float(r.quantity)
        out[sym] = out.get(sym, 0.0) + (-qty if side == "SHORT" else qty)
    return out


def market_data_type_name(value) -> str | None:
    """IB's market-data-type enum value -> its NAME, for the health frame (#834).

    `None` in, `None` out: a provider with no such notion (Alpaca, Databento) declares nothing, and
    nothing must not become "REALTIME" — that would be a claim nobody made, on the exact surface the
    UI uses to decide whether a 900-second-old print is dead or healthy.

    A value the adapter does not know RAISES rather than mapping to None: an unknown number quietly
    becoming "undeclared" is the silent-fallback shape, and it would render a mis-set gateway as a
    provider that simply never said.
    """
    if value is None:
        return None
    from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum

    names = {getattr(IBMarketDataTypeEnum, a): a for a in dir(IBMarketDataTypeEnum) if a.isupper()}
    try:
        return names[int(value)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            f"market_data_type={value!r} is not a value the installed IB adapter knows: {sorted(names.values())}"
        ) from None


def _protection_coid(instrument_id: str, side: str, attempt: int = 0, lane: str = "",
                     trader_id: str = "") -> str:
    """Client order id for a backstop stop. Nautilus caps identifiers at 36 characters.

    Side first, then the instrument, then a hash of the FULL `(instrument_id, side)` pair.

    Two truncation collisions have to be impossible here, and a collision means one position's stop is
    rejected as a duplicate or silently replaces another's — the loser going naked while the audit sees an
    order resting on the instrument:

      * side appended LAST is the first thing truncation eats, so a 30+ char instrument id erased it and
        the long leg collided with the short leg on one instrument;
      * side first still left two SELL instruments sharing a 26-char prefix colliding (codex, High).

    The hash closes both. The readable head is kept so an ordinary `AEM.XNYS` is still recognisable in the
    blotter as this feature's output.
    """
    import hashlib

    # The ATTEMPT is part of the identity (#295). A deterministic coid is right for idempotency WITHIN an
    # attempt and fatal ACROSS them: a retry that reuses the id of an order the venue already REJECTED
    # gets DENIED as a duplicate, and two terminal events on one order make `load_orders` throw
    # `InvalidStateTrigger: REJECTED -> DENIED` on every subsequent start. That bricked the engine.
    # THE LANE IS PART OF THE IDENTITY (#748). Two lanes now rest a stop each on one leg, and a
    # hash over (instrument, side, attempt) alone gives them the SAME client order id — the
    # second submit is then denied as a duplicate and that lane's shares are left naked.
    # THE TRADER ID IS THE NEXT TERM IN THIS SERIES (#762). Instrument, side, attempt and lane were
    # each added after a collision, and each collision was found the same way — something went naked.
    # The account was the term still missing, and it is the one that spans INSTANCES: measured on
    # 2026-08-31, paper and staging both minted PROT-SELL-AEM-XNYS-763905c0 against two different
    # brokers, and all NINE of staging's protective orders were denied locally as duplicates. None
    # rested. `22 of 23 unprotected` on that screen was accurate.
    #
    # HASHED, NEVER APPENDED: the head stays legible in the blotter and the id stays inside Nautilus's
    # 36-character cap, which is what the truncation collisions above were about.
    digest = hashlib.sha1(
        f"{instrument_id}|{side}|{attempt}|{lane}|{trader_id}".encode()
    ).hexdigest()[:8]
    # ONE derivation of the prefix, shared with `protection.PROTECTION_COID_PREFIX` — `wrong_mode`
    # cancels only ids from this family, and a private copy of the string on either side would be free
    # to drift from the one that mints them.
    head = f"{_PROT_PREFIX}{side}-{instrument_id.replace('.', '-')}"[:27]
    return f"{head}-{digest}"


def _row_keys(row: dict) -> set[str]:
    """The ids a BROKER order row can be recognised by — its venue id and its client order id.

    Both, because which one identifies our order depends on who created it. An order we submitted
    carries the client order id we chose; a bracket LEG carries one Alpaca generated, and only the venue
    id links it back to what we hold. Matching on one alone silently fails for the other kind.
    """
    return {str(row.get("id") or ""), str(row.get("client_order_id") or "")} - {""}


def _identity_keys(orders) -> set[str]:
    """Every id by which a cache order can be recognised in a broker payload.

    BOTH IDS, BECAUSE A BRACKET LEG DOES NOT CARRY OURS. We submit a bracket as ONE native Alpaca
    request (`order_class=bracket`) and the VENUE generates the stop and take-profit legs — with its
    own client order ids. `_submit_order_list` maps each leg's venue id back to our order, so the
    cache holds the leg under OUR coid while Alpaca reports it under a UUID of its own:

        AEM sell limit qty=20 class=bracket coid=379fcfb5-6e6d-4947-8666-55e061c20b02

    Joining on `client_order_id` alone therefore NEVER matches a bracket leg. On 2026-08-17 that made
    this system's own take-profit look both invisible to the cache and foreign to us, and PEAK refused
    to arm over the operator's AEM bracket: "the broker holds resting exit order(s) this system did not place
    — cancel them manually". That is #265 exactly, reintroduced by a join key.

    `venue_order_id` is the reliable side of the join because the venue assigns it and we record it.
    The coid stays in the set for orders we did name ourselves, and because an order can be resting at
    the venue before its acceptance has written a venue id back into the cache.
    """
    keys: set[str] = set()
    for o in orders:
        keys.add(str(o.client_order_id))
        venue_id = getattr(o, "venue_order_id", None)
        if venue_id is not None:
            keys.add(str(venue_id))
    return keys


def _side_name(side) -> str:
    """A Nautilus `OrderSide` (or anything side-shaped) as the venue's own word: "buy" / "sell".

    Broker payloads carry lowercase strings and our code carries a Cython enum whose `str()` is
    `"OrderSide.SELL"`. Comparing those directly is silently always-false, and an always-false filter on
    an exit path reads as "nothing is resting" — the same wrong answer this whole seam exists to stop.
    """
    name = getattr(side, "name", None) or str(side)
    return name.rsplit(".", 1)[-1].lower()


_PROTECTION_TIMER = "protection_backstop"
#: Realized P&L per period, from the broker's own fill record (#322). Five minutes, not the 2s display
#: cadence: this is a paginated REST sweep of every fill the account has ever had, and the answer to
#: "what did this month make" does not change between two bars.
#: The rotation read, computed IN THE ENGINE from our own bars (#384). Five minutes, matching what the
#: file-backed sidecar did — the grading is on DAILY bars, so it barely moves intraday, and the cost of a
#: recompute is arithmetic over already-fetched history.
#:
#: NO LONGER A FILE. The payload used to be written by a sidecar shelling out to `fintrack/tools/
#: rotation_read.py` on a host mount and read back off disk. Operator, 2026-08-21: "it should not feed from a
#: file". Two things were wrong with it beyond the plumbing: it was a SECOND data source (the cockpit
#: graded rotations off Yahoo while trading off Alpaca bars, and the two can disagree about the same
#: session), and it had no relationship to the engine's own liveness — on 2026-08-21 the sidecar's bind
#: mount went stale, every refresh failed with `FileNotFoundError`, and the Market tab served a payload
#: from four hours earlier until it was noticed by eye.
_ROTATION_TIMER = "rotation_read"

#: Cockpit's own preflight probes (#532). Slow on purpose — `lifecycle` and `budget` are database
#: reads, and they change when an operator acts, not tick by tick.
_PLATFORM_PROBE_TIMER = "platform_probes"
_PLATFORM_PROBE_SECS = 60.0
_ROTATION_SECS = 300
#: `read_pair` refuses a ratio series shorter than 400 bars. Three calendar years is ~750 trading days —
#: margin over the floor, so a holiday-heavy stretch cannot silently drop an axis below it.
_ROTATION_LOOKBACK_DAYS = 1100

#: HOW MANY DAILY BARS THE COMPASS GRADES OVER, fixed so two stacks agree.
#:
#: Measured 2026-08-26 with both instances on identical code and the identical source:
#:
#:     :8000  IWM/SPY  adx=15.8  px=0.3907        GLD/SPY  adx=32.9
#:     :8010  IWM/SPY  adx=43.5  px=0.3907        GLD/SPY  adx=73.2
#:
#: Same prices, same verdicts, different ADX on three of four pairs — because the two caches held
#: different amounts of history. Wilder's ADX is RECURSIVE FROM THE START of the series: seeded from
#: the first n bars and iterated forward, so a longer history yields a different number for the same
#: recent price action. The grade depended on how long a node had been up.
#:
#: IT MUST BE BELOW THE SHALLOWEST CACHE, and 800 was not. Measured after deploying it:
#:
#:     paper    axes=22  errors=3 "thin history"   depth 754..800, 20 tickers at the cap
#:     staging  axes=24  errors=1 "thin history"   depth 754..800,  6 tickers at the cap
#:
#: stable across six minutes — it does not converge. 754 is the FULL requested window: 1100 calendar
#: days is about 754 trading days. The tickers reading 800 are ones a strategy already subscribes, so
#: their caches are deeper. Capping at 800 therefore truncated the deep legs and left the shallow ones
#: whole — MISMATCHED WINDOWS. `ratio_bars` joins by timestamp, the intersection fell under the 400 a
#: ratio needs, and those axes returned "thin history" on one stack and not the other.
#:
#: 700 is below the floor every ticker can meet, so every leg is cut to exactly 700 and the windows
#: line up. It still leaves room for what the grading needs: 400+ AFTER alignment, and 52 weekly bars
#: (about 260 daily) before `ichi` can position anything.
#:
#: The truncation exists because Wilder's ADX is recursive from the START of the series, so two stacks
#: with different depths graded the same market differently — measured, IWM/SPY adx 15.8 vs 43.5 on
#: identical prices.
#: The floor a leg must reach before the compass will grade it. A ratio needs 400+ bars AFTER the two
#: legs are joined, and the weekly cloud needs 52 weekly bars (about 260 daily) before `ichi` can
#: position anything. Below this a ticker is still SEEDING, not thin.
_ROTATION_MIN_BARS = 500
#: 'NOT YET' AND 'NEVER' ARE DIFFERENT STATES (#606). A compass member that stays at zero new bars
#: for this many CONSECUTIVE refresh ticks — while at least one sibling is fully seeded — is
#: classified ABSENT for that tick and stops gating publication. Measured 2026-08-27 on staging-ibkr:
#: XLU/XLRE/XLP were not in the IBKR instrument provider, every request was refused with "instrument
#: not found", `short` never emptied, and the compass published no axis for the life of the process
#: while claiming "requested, not yet delivered".
#:
#: An EVENT COUNT, not a wall clock: the refresh runs off the engine's timer, so backtests and slow
#: feeds classify identically (at the live 5-minute timer, 3 ticks is ~15 minutes). The
#: sibling-seeded condition is what keeps a cold cache honest — after a restart NOTHING has bars, no
#: sibling is satisfied, and every member stays SEEDING indefinitely, which is exactly what the
#: seeding branch's own comment defends. Classification is re-derived from the cache every tick and
#: the member keeps being re-requested, so it is never a blacklist: bars that finally land put the
#: member straight back into the grade (absence is a timestamp, not a property).
_ROTATION_ABSENT_TICKS = 3
_REALIZED_TIMER = "realized_periods"

def _baseline_from_env(value: str | None) -> float:
    """The account's opening equity for the realized reconciliation (#345 item 1), or 0.0 = disabled.

    Alpaca exposes no such field — `/v2/account` carries `created_at` but no funding figure — so it
    has to be TOLD, and the instance is what knows it: kumo-cockpit-instances declares
    `ACCOUNT_BASELINE_EQUITY` per stack (paper's actual opening balance is 100,000; staging-ibkr's is
    an SGD number nothing here can guess).

    UNTOLD MEANS DISABLED, SAID OUT LOUD — not 100,000 (#651 item 7). The old default armed the check
    against the Alpaca paper number on every instance that never set the knob, so on any account that
    did not start at exactly $100,000 the two derivations disagreed by a constant forever: an alarm
    calibrated by default rather than by fact, which is the alarm nobody reads. Compose interpolates
    an unset variable to the EMPTY STRING (#581), so blank is "never told us" too. A malformed value
    REFUSES at boot naming the input, rather than guessing or silently disabling.
    """
    if value is None or not value.strip():
        _log.warning(
            "ACCOUNT_BASELINE_EQUITY is not set — the realized reconciliation (#345) is DISABLED. "
            "Declare the account's actual opening equity in the instance's env to arm it.")
        return 0.0
    try:
        return float(value)
    except ValueError:
        raise ValueError(
            f"ACCOUNT_BASELINE_EQUITY={value!r} is not a number — set the account's actual opening "
            f"equity, or leave it unset to disable the realized reconciliation") from None


#: See `_baseline_from_env`. 0.0 disables the reconciliation; the instance env is the only thing
#: that can arm it, because only the instance knows what the account actually started with.
_ACCOUNT_BASELINE = _baseline_from_env(os.environ.get("ACCOUNT_BASELINE_EQUITY"))
_REALIZED_SECS = 300
_PROTECTION_SECS = 60
#: How long a wrong-mode FLIP waits for the venue to CONFIRM the cancel before it places the floor
#: (#898). Its OWN constant, never `_SHARES_FREE_S` (30 s): this wait runs INSIDE the protection pass,
#: behind `_protection_running`, so a long stall makes the next 60 s tick a no-op for the whole book.
#: Measured cancel-confirm on paper 2026-09-10: ~4 s. On timeout the leg is bare until the next pass
#: places the floor through the normal path — named, never silent.
_FLIP_CONFIRM_S = 10.0
#: A flip needs the cancel, its confirmation and the floor submit inside the session, with one retry
#: tick to spare: two protection ticks plus six confirm timeouts = 180 s. A cancel at 15:59:30 left a
#: leg bare until the next session's first in-RTH tick, ~17.5 h on a weekday (#898 measured).
#: DERIVED FROM THE DEFAULT, not from the env override on the class: raising
#: `KUMO_FLIP_CONFIRM_TIMEOUT_S` on live does not widen this gate. Safe direction (a flip refused a
#: little late rather than started with too little session left to confirm), stated so nobody tunes
#: the timeout past 30 s and expects the gate to follow.
_FLIP_MIN_SESSION_S = 2 * _PROTECTION_SECS + 6 * _FLIP_CONFIRM_S
#: A cancelled order's terminal statuses, in BOTH vocabularies the two venue reads speak (Nautilus's
#: `OrderStatus` names on the typed path, Alpaca's on REST, upper-cased by the reader).
_VENUE_GONE_STATUSES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REPLACED", "REJECTED"})
_VENUE_FILLED_STATUSES = frozenset({"FILLED", "PARTIALLY_FILLED"})

#: Order types whose trigger the VENUE computes rather than one we submitted. DERIVED from Nautilus's
#: own enum so a version that adds a member is covered without an edit here.
#:
#: NAMES, NOT MEMBERS, and that is not laziness. `OrderType` is a Cython enum and does NOT compare
#: equal to its own name — `OrderType.TRAILING_STOP_MARKET == "TRAILING_STOP_MARKET"` is False — while
#: `models.py` declares `order_type: str` and the live DTO carries `'TRAILING_STOP_MARKET'`. Comparing
#: the members, which is what review suggested and is the more idiomatic-looking code, is False for
#: every order and would silently revert every trailing trigger to the stale value with nothing failing.
_TRAILING_ORDER_TYPES = frozenset(t.name for t in OrderType if "TRAILING" in t.name)
#: How long a submitted backstop stop is treated as in-flight before it is retried.
#:
#: `_submit` returning does NOT mean the venue accepted (codex, Critical). Alpaca can reject
#: asynchronously, and a PERMANENT marker then suppresses every future attempt while broker REST shows no
#: open stop — the position naked forever and silent about it. So the marker EXPIRES.
#:
#: It exists only to stop a double-submit in the window before the order appears in broker REST. Past that
#: window the broker is the guard: an order that really was accepted shows up as coverage and suppresses
#: the intent anyway, so expiring is safe and self-healing. Three ticks of slack.
_PROTECTION_PENDING_NS = 180 * 1_000_000_000
#: How many client order ids one leg may burn before the reconciler gives up for this tick. Bounds the
#: cache-collision search in `_reconcile_protection`; a leg that has genuinely failed this many times wants
#: a human, not another order.
_PROTECTION_MAX_ATTEMPTS = 50
_FUNDAMENTALS_SECS = 6 * 3600  # fundamentals don't move intraday — no reason to poll on the 5s watchlist
# cadence. Warmed once at startup too (on_start schedules an immediate first fetch), so a fresh boot isn't
# blank for up to 6h waiting on the first timer tick.
# Real-time subscription budget — a PLAN limit, so it follows the configured feed rather than being a fixed
# constant. Alpaca's free/IEX plan caps WS subscriptions at 30 channel-slots combined; a budgeted symbol
# takes 4 (trades, quotes, native `bars` 1m, native `dailyBars` 1d) — confirmed empirically (a
# trades+quotes-only budget of 15 still 405'd; 1m/1d bars share the same cap, see `_after_definition`).
# 30 / 4 = 7.5 → 7, leaving one slot of headroom rather than landing exactly on the documented ceiling.
#
# Algo Trader Plus (the `sip` feed) documents UNLIMITED WS symbol subscriptions, so the budget is lifted
# entirely there (upgraded 2026-08-01). The gate itself STAYS: it is what keeps a downgrade — or a stray
# `feed = "iex"` — from turning into a 405 that kills the whole subscribe for every symbol at once.
#
# Positions load before watchlist in `on_start`/`_on_watchlist` (config universe + open positions first,
# runtime watchlist second), so on a capped feed the budget naturally favors real capital over "nice to
# have" watchlist symbols. A symbol past the budget still charts from historical/REST bars — it just has
# no LIVE last-price/spread/candle-tick until a slot frees up (position closed).
# THE REALTIME BUDGET IS THE DATA PROVIDER'S, NOT THE ENGINE'S (#619). `_MAX_REALTIME_SYMBOLS_IEX` and
# `realtime_symbol_budget` used to live here and were applied to every provider, so staging-ibkr rationed
# itself to 7 live symbols against Alpaca's free-tier plan and logged `feed=iex` on a node that holds no
# Alpaca credential. They now live in `providers/alpaca/data_client.py`, reachable only through Alpaca's
# own `build_data`, and the number arrives on `DataClientSpec.realtime_symbol_budget`.
# Command layer (#32): the reverse of ui:stream — the api publishes typed commands here, the engine
# consumes them (consumer group + XACK, at-least-once). command_ack flows back over ui:stream.
#: How often the paced queue releases work. Short enough that a boot backfill is not glacial,
#: long enough that the timer itself is not the cost.
_BAR_DRAIN_SECS = 10.0
#: Queue tiers (#836), worst-starved first. Everything at or past _DISPLAY_PRIORITY is display work —
#: the only tiers KUMO_DISPLAY_BACKFILL_HOLD_SECS may park. Lane warmups sit ahead of every live
#: subscription but a held position's: measured 2026-09-09, ~300 live subscribes at 6/min put the
#: lanes' 180-day history ~50 minutes out, past BCTROT-004's slot.
_HELD_SUBSCRIBE_PRIORITY = 0
_HELD_HISTORY_PRIORITY = 1
_LANE_REQUEST_PRIORITY = 2
_LANE_SUBSCRIBE_PRIORITY = 3
_DISPLAY_PRIORITY = 4          # display live subscribe
_DISPLAY_HISTORY_PRIORITY = 5  # display backfill
#: How long the engine waits on IB's reqMatchingSymbols before refusing a search (#837). BELOW the
#: api's 4 s so 'the venue did not answer' reaches the api as a refusal, not as silence.
_SEARCH_VENUE_TIMEOUT_S = 3.5
_BAR_DRAIN_TIMER = "bar-request-drain"
_CMD_STREAM = "ui:commands"
_CMD_GROUP = "engine"
_CMD_CONSUMER = "engine-1"
# Latest-state planes (#56): republished every snapshot tick — only the LATEST matters, and their history
# would flood the bounded bar stream and evict the low-volume historical bars (the cause of the "loading…"
# breakage). These go to Redis KEYS (SET, overwrite); the api polls them. Event/live planes (bar/fill/price/
# quote/order) stay on the stream (need replay or are per-event). Taxonomy shared with the consumer.
# TTL on the state keys: a live engine re-SETs every snapshot tick (~2s) and keeps them alive; if the engine
# DIES the keys must expire, else the api (esp. on a restart) reads the frozen last value and shows a dead
# engine as alive / stale positions as current. Comfortably above the 2s cadence so a single missed tick
# doesn't blank the UI; aligned with the api's bridge-stale window.
_STATE_KEY_TTL_SECS = 6
#: Per-kind override. A key's TTL must outlive its own publish interval or it is absent more often
#: than present — `session` publishes every 20s, so the shared 6s TTL left it missing most of the
#: time and a cold WebSocket connect would simply not find it. Three intervals of slack.
# `equity_curve` refreshes every 120s (see `_EQUITY_REFRESH_SECS`); under the 6s default its Redis key
# would be EXPIRED for ~95% of the interval, so any cold API restart or new consumer would find nothing.
# 3x the publish interval, matching `session`'s reasoning. (codex review, High.)
_STATE_KEY_TTL_OVERRIDES = {"session": _SESSION_STATE_SECS * 3, "equity_curve": 360}
_LEDGER_TIMEOUT_SECS = 5.0  # bound the command-idempotency ledger call so a slow DB fails closed fast (#78)
_REFETCH_SECS = 30  # re-request historical bars: heals granularities that came back empty + picks up
#                     new data when the feed has it (a historical-only Databento plan lags intraday, so
#                     this does NOT produce live intraday movement — that needs a live feed).
_QUEUE_MAX = 10000  # bounded — drop UI frames on backpressure rather than block trading
# Bar-series heal (#peng-stale-bars): re-request a series that is empty, too thin for the indicators, or
# holed (stale history + a lone carried live bar). Bounded by a cooldown so a genuinely thin/new symbol isn't
# re-requested every tick.
_MIN_HEALTHY_BARS = 30  # fewer completed bars → too thin for ichimoku(52)/MA200 → heal
_HEAL_COOLDOWN_NS = 300 * 1_000_000_000  # 5 min between heal attempts per bar type
_DROP_REPORT_INTERVAL_S = 30.0  # summarise dropped UI frames at most this often
# Backfill frames WAIT for queue space instead of being dropped; live frames never wait. A 7-day 1m
# backfill is ~1700 frames per symbol, so across the pool it is ~127k against a 10k queue — 92% of the
# history was being discarded, which is why the UI held 6 minute bars for a symbol whose series the
# engine had received 1761 of. Bounded, so a wedged writer still cannot stall the engine.
_BACKFILL_PUT_TIMEOUT_S = 0.5
_BACKFILL_STALL_COOLDOWN_S = 30.0
# How long a refetch may be outstanding before we assume its callback is never coming. A failed
# request never calls back at all, so without this the guard is permanent rather than protective.
# Comfortably longer than a paginated multi-page fetch, short enough that a stuck series recovers
# within a couple of heal cycles rather than needing a restart.
_INFLIGHT_TIMEOUT_NS = 120 * 1_000_000_000
_DEFAULT_MAX_GAP_NS = 4 * 24 * 3600 * 1_000_000_000  # intraday: >4d between newest bars = a hole (weekend < 3d)
_MAX_BAR_GAP_NS = {  # per-granularity "hole" threshold — normal weekend/holiday gaps stay under
    "1d": 7 * 24 * 3600 * 1_000_000_000,
    "1w": 28 * 24 * 3600 * 1_000_000_000,
}


def _series_stale_bars(bars, granularity: str) -> bool:
    """True if a bar series should be re-fetched: EMPTY, too THIN for the indicators (< _MIN_HEALTHY_BARS),
    or HOLED — the two newest bars are farther apart than the granularity's gap threshold (an old series
    capped by a lone carried live bar, i.e. the PENG +313% failure). Pure so the heal path is testable."""
    if not bars:
        return True
    if len(bars) < _MIN_HEALTHY_BARS:
        return True
    ts = sorted(b.ts_event for b in bars)  # order-independent of the cache's return order
    return (ts[-1] - ts[-2]) > _MAX_BAR_GAP_NS.get(granularity, _DEFAULT_MAX_GAP_NS)


class UnstampableProtection(RuntimeError):
    """No lane could be resolved to own a protective stop, so none was placed (#748).

    A sibling of `ExitReleaseInFlight` and raised for the same reason: the callers submit a
    replacement BEFORE cancelling the order it replaces, so a silent None gives up the old stop and
    places nothing. An exception stops the sequence before anything is surrendered.
    """


class ExitReleaseInFlight(RuntimeError):
    """A protective stop was about to be placed while an exit is releasing that position's shares (#358).

    Its own type rather than a bare RuntimeError so a caller can tell "the standoff refused this" from
    "the venue rejected this" — the first is expected and self-clearing within the standoff's TTL, the
    second is not.
    """


#: The attribute a lane carries WHY it is not armed, when the installed kumo-strategies has one
#: (`{state, reason, at_ns, attempts}`). Optional by design: cockpit must report the same lanes
#: correctly on a pin that predates it, saying the reason is UNAVAILABLE rather than inventing one.
ARM_REASON_ATTR = "arm_state"


def armed_by_lane(siblings: dict) -> dict:
    """{strategy_id: {armed, state, session, slot, next_fire_ns, reason, reason_available, …}}.

    ARMED IS NOT RUNNING. A lane whose trading calendar never answered is alive, counted in
    `automated_lanes_running`, and will never fire its slot. QC345-003 was in exactly that state at
    01:39 on 2026-08-23; it recovered on the retry, and the only evidence was the ABSENCE of further
    `UNARMED` log lines — absence of an error is not evidence of success.

    THIS USED TO BE A BARE BOOL, AND THE BOOL COST TWO SESSIONS AN HOUR ON 2026-09-11 (#997). It read
    the RIGHT thing — `is_armed` is `_armed_session is not None`, the session arm — and then discarded
    everything that made the answer checkable:

      * WHICH SESSION. `_armed_session` holds the date and `_armed_slot` the slot; a lane armed for a
        STALE session read True identically to one armed for today. The value was present and dropped
        at the boundary — the same shape as a screened count without its denominator.
      * NOT ARMED vs MID-RE-ARM. `_on_session_alert` clears `_armed_session` BEFORE re-arming, so a
        healthy lane reads False at the exact instant it decides. Measured: the re-arm does not
        refetch (the calendar span is cached 10 days back / 45 forward; cold 1068.6 ms, warm
        0.023 ms), so the window is ~20 microseconds — rare enough that nobody was ever paged by it,
        and closed here BY CONSTRUCTION rather than by a tolerance: a live decision alert means the
        lane is scheduled, whatever `is_armed` says this instant.
      * THE REASON. The permanent refusal — "not a network problem and will not be retried … CANNOT
        DECIDE until the cause is fixed and it is restarted" — ended the arming task and set nothing
        a caller could read. It is the state an operator must act on and no surface could show it.

    `None` for a lane that cannot answer, never True: reporting armed when we cannot tell is the
    silencing direction, the one that made TECHIVOL look healthy while it formed nothing.

    THE FIELDS, and two whose meaning will mislead a reader who guesses:

      armed             the session arm as the lane reports it; None when the lane cannot be asked.
      state             armed | rearming | not_armed | unknown, or the lane's OWN account when the
                        installed package carries one (retrying | window_pending | refused_permanent).
                        The lane wins: it knows whether a retry is pending, cockpit can only see that
                        an alert is missing.
      session, slot     WHAT it armed FOR. A stale date here is the whole reason these are carried.
      next_fire_ns      this lane's own decision alert, from its clock.
      reason            the lane's words as TEXT, or None. Coerced here, not forwarded raw: upstream
                        owns `arm_state` and one unserialisable value in it would take the whole
                        frame down for EVERY lane (#998 review).

    `armed` AND `state` CAN DISAGREE, AND THAT IS THE POINT, not a contradiction to tidy away: a lane
    armed for a STALE session whose latest attempt was refused reads `armed: True, state:
    refused_permanent`. Two different facts — whether it armed, and what it says about arming now.
    Forcing them to agree would discard whichever one an operator needs.
      reason_available  FALSE means the installed package cannot report a reason — NOT that nothing
                        went wrong. A missing reason must not read as the absence of a failure.
      reason_at_ns      MAY BE None BY DESIGN. Upstream's first version read the clock unguarded
                        inside the failure handler and RAISED OVER THE ARMING FAILURE IT WAS
                        REPORTING; None is what a clock that cannot answer produces, never invented.
      attempts          COUNTS THE CURRENT CONDITION, NOT THE LANE'S HISTORY. A lane that retried
                        fifty times on a network error and then failed permanently reports
                        `refused_permanent, attempts: 1`. Total arming attempts is a different number
                        and nothing exposes it.
    """
    out: dict = {}
    for sid, lane in siblings.items():
        row = {"armed": None, "state": "unknown", "session": None, "slot": None, "next_fire_ns": None,
               "reason": None, "reason_available": False, "attempts": None, "reason_at_ns": None}
        v = getattr(lane, "is_armed", None)
        if isinstance(v, bool):
            row["armed"] = v
            row["session"] = _as_text(getattr(lane, "_armed_session", None))
            row["slot"] = _as_text(getattr(lane, "_armed_slot", None))
            row["next_fire_ns"] = _next_decision_alert_ns(lane)
            # THE LANE'S OWN ACCOUNT WINS over cockpit's inference: it knows whether a retry is
            # pending; cockpit can only see that an alert is missing.
            declared = getattr(lane, ARM_REASON_ATTR, None)
            if isinstance(declared, dict) and declared.get("state"):
                # `at_ns` IS OPTIONAL AND MAY BE None BY DESIGN (l21, 6dcdd27): the first upstream
                # version read the clock unguarded inside the failure handlers, so a host whose clock
                # could not answer RAISED OVER THE ARMING FAILURE IT WAS REPORTING — the diagnostic
                # destroying the thing it reports. None is never invented here either.
                # `attempts` counts the CURRENT condition, not the lane's history: a lane that retried
                # fifty times and then failed permanently reports attempts 1 against the new state.
                # EVERY FIELD IS COERCED AT THIS BOUNDARY, not just `state` (ffv73l93, #998 review).
                # `arm_state` is built in kumo-strategies and this repo does not own its types. A
                # Decimal from a DB-backed counter, a pandas Timestamp, or the exception object
                # itself in any one of these serialises to nothing — and because ONE frame carries
                # ALL lanes, that takes `armed_lanes` down for every lane, not just the one that
                # declared a reason. The failure mode of this change would then be strictly worse
                # than the bare bool it replaced, in precisely the case it exists to report.
                # `_as_text` already proves the rule for this boundary: it exists because
                # `_armed_session` is a pandas Timestamp on every adapter.
                row.update({"state": str(declared["state"]),
                            "reason": _as_reason(declared.get("reason")),
                            "reason_available": True,
                            "attempts": _as_int(declared.get("attempts")),
                            "reason_at_ns": _as_int(declared.get("at_ns"))})
            elif v:
                row["state"] = "armed"
            elif row["next_fire_ns"]:
                row["state"] = "rearming"
            else:
                row["state"] = "not_armed"
        out[str(sid)] = row
    return out


def _as_reason(value):
    """The declared reason as text, or None.

    `None` MEANS "the lane declared a state but no reason" and must stay None — `str(None)` would
    render the string "None" into an operator-facing field, which is the absence-as-a-value trap
    this row was built to end.
    """
    return None if value is None else str(value)


def _as_int(value):
    """A counter or a nanosecond stamp as a plain int, or None when it cannot be one.

    REFUSES rather than guesses: a value that is not an integer is reported as absent, because the
    row already distinguishes absent from present and a fabricated 0 would read as a real count.
    `bool` is excluded explicitly — it is an `int` subclass, so `True` would otherwise arrive as
    `attempts: 1`.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    # pandas Timestamp keeps its nanoseconds in `.value`; `int(Timestamp)` raises on modern pandas.
    inner = getattr(value, "value", None)
    try:
        return int(inner) if inner is not None else None
    except (TypeError, ValueError):
        return None


def _as_text(value):
    """A session or slot as a plain string — `_armed_session` is a pandas Timestamp on every adapter,
    and a frame that carries one cannot be serialised to the bridge."""
    if value is None:
        return None
    date = getattr(value, "date", None)
    return str(date() if callable(date) else value)


def _next_decision_alert_ns(lane):
    """This lane's own decision alert, or None — the same clock read `next_fire_by_lane` performs,
    called per lane so an arm row and a fire time cannot disagree about one lane."""
    try:
        clock = getattr(lane, "clock", None)
        names = [n for n in (getattr(clock, "timer_names", None) or []) if "session_decide" in str(n)]
        due = [int(d) for d in (clock.next_time_ns(n) for n in names) if d]
        return min(due) if due else None
    except Exception:  # noqa: BLE001 — health must never raise; unknown is an answer
        return None


def next_fire_by_lane(siblings: dict) -> dict:
    """{strategy_id: epoch_ns} — when each lane's OWN registered alert is next due, or None.

    READ FROM THE LANE'S NAUTILUS CLOCK, NOT RE-DERIVED. Each lane arms itself:
    `_arm()` computes `fire_at` from its calendar and its own slot offset, then calls
    `clock.set_time_alert(...)`. The clock is therefore the single place that knows when a decision is
    due, and asking it is the only reading that cannot drift from the lane's actual behaviour.

    THE ALTERNATIVE IS INFERRING A SCHEDULE FROM A PAST DECISION, AND I DID EXACTLY THAT.
    On 2026-08-25 I read `last_decision_slot` off `/strategies` — which truthfully answers "where did
    this lane LAST decide" — and reported it as QC345-003's live schedule. It said `open+100m` because
    that was an override removed the day before; the lane actually fires at the built-in `open+5m`.

    I then built an incident narrative on it: "a deploy landed at 11:10 ET, exactly its slot". Both
    kumo-strategies and codex, working independently, refuted it — QC345 is a MONTHLY rebalancer, and
    2026-08-25 was neither a month-start nor in `forced_rebalance_dates`
    (['2026-08-20','2026-08-21','2026-08-24']). It journalled nothing because it had nothing to do.
    Next natural rebalance: 2026-09-01.

    The mechanism this function exists for is still real — kumo-strategies' MOMENTUM-002 lost its
    2026-08-17 session to a restart at 09:35 ET, and there is no catch-up because firing late trades a
    stale ranking at moved prices. But it is NOT what happened to QC345, and a hardcoded slot table
    here would reproduce the reading error that produced the wrong story.

    Read the lane's own alert. `last_decision_slot` answers a NEARBY question.

    ONLY THE DECISION ALERT, AND THE FIRST VERSION OF THIS TOOK THE MINIMUM OF EVERY TIMER. A lane's
    clock also carries routine timers — bar aggregation and the like — so the earliest of ALL of them
    is almost always seconds away. Deployed, the gate refused with "MOMENTUM-002 decides in 1s" at
    12:58 ET, when that lane decides at 09:35. A gate that fires on the normal path gets switched off,
    which is the failure this whole mechanism exists to avoid.

    Matched on `session_decide`, which every adapter's own constant contains:

        momentum_rotation.SESSION_ALERT = "session_decide"
        qc27_rotation.SESSION_ALERT     = "qc27_session_decide"
        qc345_rotation.SESSION_ALERT    = "qc345_session_decide"

    A substring rather than a hardcoded list of three, so a new adapter following the same convention
    is covered without cockpit being edited — and if one ever does not, it reports None, which the
    gate names out loud rather than treating as safe.

    None where a lane has no alert or cannot be asked: unknown must be distinguishable from "not due".
    """
    out: dict = {}
    for sid, s in siblings.items():
        try:
            clock = getattr(s, "clock", None)
            names = [n for n in (getattr(clock, "timer_names", None) or [])
                     if "session_decide" in str(n)]
            due = [clock.next_time_ns(n) for n in names]
            due = [int(d) for d in due if d]
            out[str(sid)] = min(due) if due else None
        except Exception:  # noqa: BLE001 — health must never raise; unknown is an answer
            out[str(sid)] = None
    return out


def strategy_run_counts(siblings: dict, absent: dict | None = None) -> tuple[int, int]:
    """(registered, running) across the node's sibling strategies.

    FAILS CLOSED. An object that cannot answer `is_running` counts as NOT running: reporting a lane as
    trading when we cannot tell is the silencing direction, and it is the direction that made
    TECHIVOL-005 look healthy while it formed nothing.

    Returns both numbers because the ratio is what carries the meaning — `0 of 4` is an outage, `0 of 0`
    is a node with nothing registered, and one number cannot tell them apart.
    """
    # THE DENOMINATOR COUNTS LANES THAT SHOULD EXIST, NOT ONLY THOSE THAT DO (#539).
    #
    # `len(siblings)` alone reported 3/3 while QC345-003 was absent, because a lane that failed to
    # build was never added and so could not be counted as missing. #498's defect — a healthy-looking
    # ratio whose missing member is in neither number — and why the absence survived a whole session.
    #
    # `absent` IS A PARAMETER, NOT A MODULE READ, and that was a real mistake I made first. Reading
    # `_SKIPPED_BUILDS` in here turned a pure function of its argument into one with hidden global
    # state that ANY node build can write — four unrelated tests broke immediately, because a build
    # failure recorded in one test leaked into another's arithmetic. The caller holds the state; this
    # function stays a function.
    #
    # Same direction as the existing rule below: a lane we cannot vouch for is NOT running.
    missing = {n for n in (absent or {}) if n not in siblings}
    registered = len(siblings) + len(missing)
    running = sum(1 for s in siblings.values() if getattr(s, "is_running", False) is True)
    return registered, running


#: Adapters that put NET LIQUIDATION in `AccountBalance.total`, so it may be read as equity.
#:
#: An ALLOW-LIST rather than a default, because the same field means different things per adapter and
#: guessing wrong is silent: cockpit's own Alpaca client puts CASH in `total`, and publishing that as
#: equity is exactly the #382 defect — a 25% phantom loss on every deploy, on the channel the
#: daily-loss halt trusts. A venue absent from this set keeps the #382 refusal.
_TOTAL_IS_NET_LIQUIDATION = frozenset({"INTERACTIVE_BROKERS"})


#: Marks an account snapshot this node DERIVED rather than received from a broker adapter. It travels
#: on the same bus topic so strategies need no special case, and is refused by our own subscriber so the
#: derivation never becomes its own input.
_DERIVED = "_derived_by_cockpit"


def broker_calendar_or_none():
    """The broker's own trading calendar, or None — never the holiday-unaware fallback.

    `require_exchange=True` is the whole point. `build_calendar` otherwise hands back a weekday
    calendar that "will happily schedule a session on Thanksgiving, and nothing in the logs
    distinguishes it from the real thing" — its own words. For a job that STAMPS A SESSION DATE onto
    an append-only row, a plausible calendar is worse than none.

    So the refusal becomes `None`, which `schedule_capture` already reads as "do not arm" and states
    in the boot log. Three states: a real calendar, a considered refusal, and never the plausible one.

    Cockpit deliberately does not know WHICH vendor supplies it — that is `build_calendar`'s job, and
    the same separation as #622, where the layer that knows the rule stopped being the layer that
    knows the venue.
    """
    try:
        from kumo_strategies.runtime.calendar import build_calendar

        return build_calendar(require_exchange=True)
    except Exception as exc:  # noqa: BLE001 — no credential, or no calendar available at all
        _log.info("no broker trading calendar available (%s); anything scheduling off the venue's "
                  "close will decline to arm and say so", exc)
        return None


def _single_currency_amount_and_ccy(balances: dict, *, prefer):
    """The amount AND the currency it is in, or `(None, None)` when choosing would be a GUESS.

    THE UNIT WAS DESTROYED HERE (#518). This helper's entire job is CHOOSING a currency — `prefer`
    when present, otherwise the single unambiguous leg — and it returned the bare float, so the one
    place that knows which unit the number is in threw it away. On staging-ibkr that published
    999,215.19 SGD as an unlabelled float beside USD position values, with `base_currency=None`
    meaning Nautilus converts nothing.
    """
    if not balances:
        return None, None
    if prefer in balances:
        return float(balances[prefer]), prefer
    if len(balances) == 1:
        ccy, amount = next(iter(balances.items()))
        return float(amount), ccy
    return None, None


def _single_currency_amount(balances: dict, *, prefer) -> float | None:
    """One number out of a per-currency balance map, or None when that would be a GUESS.

    `prefer` (USD) wins when present. Otherwise a SINGLE leg is unambiguous and is used — the live IB
    paper account reports only SGD (`base_currency=None`), and a USD-keyed read finds nothing there, so
    requiring USD means publishing nothing at all on that stack.

    Two or more legs and no `prefer` is a guess, and a guess on the channel the daily-loss halt anchors
    on is the #382 defect wearing a different unit. None means "unknown", which callers already handle.
    """
    return _single_currency_amount_and_ccy(balances, prefer=prefer)[0]


def _standing_unrealized_total(cache, portfolio) -> float | None:
    """Σ unrealized P&L across open positions, or None when any of it is unknown (#596).

    THE Δ HALF OF NET ON A NODE WITH NO BROKER PUBLISHER. `NET(period) = realized + Δunrealized`,
    and the Δ term reaches the panel from the Alpaca exec client, which sums that venue's own
    per-position fields. An IBKR node has no broker-snapshot publisher at all, so staging served
    `unrealized_standing_total: null` and every NET period rendered an em dash — correct behaviour
    on missing input, and the input should not have been missing.

    `Portfolio.unrealized_pnl` is NAUTILUS's own figure and exists on every adapter, so this serves
    IBKR without reaching into the Alpaca client. The intraday term stays None on that path: IBKR
    reports no day figure, and substituting the standing number would report a position's whole life
    as today — the #573 mistake in another costume.

    A MODULE FUNCTION, NOT A METHOD, and that is load-bearing. Thirteen existing tests drive
    `_publish_account` UNBOUND against a `types.SimpleNamespace`; requiring a new attribute on
    `self` raised AttributeError in every one of them. The double cannot represent a production
    `self`, so the function must not demand anything from it.

    UNKNOWN PROPAGATES. An instrument with no mark returns None from `unrealized_pnl`, and summing
    the rest would report part of the book as all of it — the quiet wrong answer this panel keeps
    producing. One unpriced position makes the whole total unknown. An EMPTY book is 0.0, because a
    book holding nothing has an unrealized of exactly zero and that is a fact, not an absence.
    """
    if cache is None or portfolio is None:
        return None
    try:
        positions = cache.positions_open()
    except Exception:                                                   # noqa: BLE001
        return None
    if not positions:
        return 0.0
    total = 0.0
    try:
        instrument_ids = {p.instrument_id for p in positions}
    except Exception as exc:                                            # noqa: BLE001
        # A book we cannot enumerate is UNKNOWN, not zero — and it says so rather than passing as a
        # healthy reading. The panel renders an em dash, which is visible; a silent 0.0 would not be.
        _log.warning("standing unrealized: position book unreadable (%r) — publishing unknown", exc)
        return None
    for instrument_id in instrument_ids:
        try:
            pnl = portfolio.unrealized_pnl(instrument_id)
        except Exception:                                               # noqa: BLE001
            return None
        if pnl is None:
            return None
        try:
            value = float(pnl.as_double())
        except Exception:                                               # noqa: BLE001
            return None
        # NaN survives every comparison written for numbers — the family that disarmed a daily-loss
        # halt in kumo-strategies 43c6d3e and reached this repo again in #588.
        if not math.isfinite(value):
            return None
        total += value
    return total


def _new_envelope_lock():
    """REENTRANT, and that is load-bearing (#564).

    `_publish_trades` holds this lock while calling `add_done_callback`, and a future that has ALREADY
    completed runs its callback synchronously on the calling thread — straight back into
    `_on_envelope_write`, which takes the same lock. A plain `threading.Lock` deadlocks the fold thread
    there and the engine stops publishing entirely.

    A FACTORY, not an inline call, so tests construct the lock the way production does. The first version
    of the deadlock test built its own `RLock` in the double, so mutating production to `Lock()` failed
    the test's own isinstance check instead of deadlocking — the mutation never reached the code it was
    aimed at, which is indistinguishable from a test that does not work.
    """
    return threading.RLock()


#: How long the command reader waits on one handler before SAYING it is still holding the queue
#: (#652 item 6). Paces the logging only — the wait itself holds until the handler finishes or the
#: node stops, because the ordering invariant ("orders must apply in order, never concurrently")
#: has no 30-second escape hatch.
_CMD_SERIALIZE_WAIT_SECS = 30.0


class UiFeedStrategy(Strategy):
    """Loads the universe onto the bus and mirrors it to Redis for the UI process: every bar
    (historical + live, all configured granularities) plus a periodic position snapshot. Redis writes go
    through a bounded queue drained by a background thread — never on the trading loop."""

    def __init__(
        self,
        cfg: FeedConfig,
        data_client_id: ClientId,
        api_key: str,
        exec_client_id: ClientId | None = None,
        realtime_symbol_budget: float = float("inf"),
        historical_requests_per_minute: float = float("inf"),
        supplies_trading_calendar: bool = False,
        daily_bars_cover: str | None = None,
        streams_trade_ticks: bool | None = None,
        streams_quote_ticks: bool | None = None,
        market_data_type: str | None = None,
    ) -> None:
        # Stable MANUAL identity (#68 Step 0): this strategy owns the human's discretionary orders, so its
        # StrategyId IS the cycle-attribution key (NETTING position id = {instrument}-{strategy_id}). Pin it via
        # the native config path — name `MANUAL` + tag `001` → id `MANUAL-001` with the order-id tag bound at
        # registration, keeping client_order_id / position ids aligned. Not the class-name default `UiFeedStrategy-000`.
        super().__init__(config=StrategyConfig(strategy_id=MANUAL_NAME, order_id_tag=MANUAL_TAG))
        self._cfg = cfg
        self._data_client_id = data_client_id
        self._api_key = api_key
        self._bridge = cfg.ui_bridge
        self._stream_key: str = self._bridge.get("stream_key", "ui:stream")
        self._maxlen = int(self._bridge.get("stream_maxlen", 200000))
        # Bars get their OWN stream (#309). Sharing one with quotes meant a ~127k-frame backfill was
        # evicted inside 7.5 minutes by ~27k quote entries a minute, so the watchlist never got history.
        # Separate streams mean the two cannot compete for the same cap.
        self._bar_stream_key = f"{self._stream_key}:bars"
        # Sized for a full backfill with room to spare, not for a live rate: bars are written once per
        # symbol-granularity and must survive until a browser connects, which may be hours later.
        self._bar_maxlen = int(self._bridge.get("bar_stream_maxlen", 500000))
        # Granularities to stream: those the chart config declares, else just the default.
        self._granularities = list(cfg.chart_lookback_days) or [DEFAULT_GRANULARITY]
        self._redis: redis.Redis | None = None
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=_QUEUE_MAX)
        # NOT `_stop`: `Strategy._stop` is Nautilus's own FSM action, and assigning an Event over it
        # made the stop transition call the Event — TypeError('Event' object is not callable), which
        # HALTS the transition, so on_stop() never ran. Every restart left timers uncancelled, the
        # writer and command-reader threads unjoined, and redis and the HTTP session unclosed.
        # Visible in the logs on every shutdown as "UiFeedStrategy(MANUAL-001): Error on STOP".
        self._stopping = threading.Event()
        self._writer: threading.Thread | None = None
        self._cmd_reader: threading.Thread | None = None  # reads ui:commands (command layer #32)
        self._bar_types: list = []  # every (instrument, granularity) requested — for the refetch heal
        # bar types with a refetch in flight → the ns it started. NOT a bare set: the only thing that
        # cleared it was the request's success callback, so a failed request (Alpaca 429/5xx, timeout,
        # a raise anywhere in _request_bars) left the key in it FOREVER — _handle_bars never runs, the
        # callback never fires, and _on_refetch then skips that bar type for the life of the process.
        # One transient error and a thin series could never heal again, which is exactly the shape of
        # a chart stuck at a low bar count regardless of the configured lookback.
        #
        # The other two in-flight guards here clear in `finally`; this one had no failure path at all.
        self._inflight: dict[str, int] = {}
        self._healed: dict[str, int] = {}  # bar type → last heal-attempt ts (ns) — cooldown for stale-series refetch
        self._boot_gate = _BootGateState()
        #: {strategy_id: {lifecycle, budget, slot_size}} — cockpit's own preflight probes
        #: (#532), refreshed on the loop because they are DB reads and the gate runs on a
        #: msgbus callback. Empty until the first refresh; an unmeasured lane reports {}.
        self._platform_probes: dict[str, dict] = {}
        self._instrument_ids = [
            InstrumentId.from_str(f"{sym}.{cfg.venue}") for sym in cfg.symbols
        ]
        # Feed-freshness clock (#26): the newest `ts_init` seen by a LIVE data handler, MONOTONIC (#917) —
        # every write is `max(self._last_tick_ts, x.ts_init)`, never a bare assignment, so a datum whose
        # stamp is older than the last one cannot age the feed. Only `on_trade_tick`/`on_quote_tick`/`on_bar`
        # write it; `on_historical_data` never does (see the comment there).
        self._last_tick_ts: int = 0
        # WHAT KIND OF PRINT THE FEED CARRIES (#834): "REALTIME" / "DELAYED" for IB, None where the
        # provider declares nothing. Published on the health frame because the UI's feed-freshness
        # derivation cannot be right without it: a DELAYED feed is ~15 minutes behind BY DESIGN, so
        # "feed 15m" rendered as a fault is the healthy state, and an entry price shown as current is
        # a quarter of an hour old. Three states — a stale frame drops it to None, never to REALTIME.
        self._market_data_type: str | None = market_data_type
        self._loaded: set[str] = set()  # instrument ids already loaded/streaming (config + positions + watchlist)
        self._realtime_subscribed: set[str] = set()  # instrument ids with a live trade+quote subscription
        self._realtime_budget_warned: set[str] = set()  # already logged as budget-skipped — don't spam every retry
        self._loop: asyncio.AbstractEventLoop | None = None  # the node's event loop (for scheduling async work)
        # IB shortability (#857): registry + client attached by `attach_shortable` on IBKR nodes only.
        # `shortable_provider` is what the CRSISHORT builder reads; None means "this node has none".
        self._shortable_plane = None
        self._shortable_subscribed: set = set()
        #: iid -> the scheduled subscription (a concurrent.futures.Future-shaped object). A
        #: subscription that RAISED is discarded from `_shortable_subscribed` so the next pass asks
        #: again, and counted — a dropped future is a silent failure (codex, #857 review).
        self._shortable_pending: dict = {}
        self._shortable_subscribe_failures: int = 0
        self.shortable_provider = None
        # Trade receipts (#199 E). `_receipted` guards against reporting one order twice: FILLED is
        # terminal, but the reconciling snapshot re-publish can revisit an order that is already done.
        self._receipted: set[str] = set()
        #: Wall clock when THIS PROCESS started, so a restart cannot replay old receipts (#520).
        #:
        #: `_receipted` is in-memory and therefore empty after every restart, and the reconciling
        #: snapshot re-publishes historical FILLED orders through `_handle_order_event` — so each one
        #: produced a receipt for a trade that happened days ago. Observed after a redeploy on
        #: 2026-08-24: six alerts, including sells that were not from that session.
        #:
        #: A timestamp rather than seeding the set from the cache: reconciliation delivers orders
        #: over time, so a set built once at boot would miss whatever arrived after it, and the
        #: comparison is the fact we actually want ("did this happen while we were watching").
        self._started_ns = time.time_ns()
        # Which strategy the `session` frame describes (#212). One automated strategy exists today;
        # when a second lands this becomes a list and the frame carries them keyed by id, which is
        # why the payload already names its `strategy_id` rather than leaving it implicit.
        self._strategy_id_for_state = os.environ.get("KUMO_SESSION_STRATEGY_ID", "MOMENTUM-002")
        self._session_state_inflight = False
        self._notifier = None
        # Order commands (#32 increment 2): DISARMED by default — no order is ever submitted unless a human
        # explicitly arms it (KUMO_ORDERS_ARMED). Carries the no-autonomous-BUY discipline into the channel.
        self._orders_armed = os.environ.get("KUMO_ORDERS_ARMED", "").strip().lower() in ("1", "true", "yes")
        self._seen_orders: set[str] = set()  # client_order_ids already submitted — dedup (at-least-once)
        #: Sibling strategies in THIS node, by StrategyId (#372, kumo-strategies#49). Registering an id
        #: gives a strategy a cycle projection; registering the OBJECT is what lets an operator flatten
        #: it. Under NETTING a position may only be closed by the strategy that owns it, so a
        #: cross-strategy flatten has to be routed to that instance rather than placed on its behalf.
        self._sibling_strategies: dict[str, object] = {}
        self._deny_reasons: dict[str, str] = {}  # client_order_id → last deny/reject reason (surfaced to detail)
        self._broker_account: dict | None = None  # latest Alpaca account snapshot off the bus (#41)
        #: Warn once, not per tick: an account reporting a non-USD equity is a standing property of
        #: the account, so repeating it every 2s would bury the signal exactly as the
        #: 'Cannot calculate unrealized PnL' flood did (93% of the log).
        self._foreign_equity_warned = False
        #: One WARN per outage, not one per tick — the account plane republishes continuously (#382).
        self._equity_estimate_warned: bool = False
        #: Same discipline, per INSTRUMENT (#386). `_marking_basis` runs once per position per feed tick,
        #: so one contested basis emitted a WARN every two seconds indefinitely — WHD had been contested
        #: since 2026-08-19 — and drowned the log being used to diagnose #385.
        #:
        #: A set rather than a bool because two contested positions are two facts: silencing the second
        #: because the first spoke would hide a divergence entirely. Discarded when the bases agree
        #: again, so a position that diverges, heals and diverges again warns twice — the second episode
        #: is new information.
        self._basis_divergence_warned: set[str] = set()
        self._equity_curve: dict | None = None
        #: Last good rotation payload, and the deep daily bars it was built from. The bars are memoised
        #: per ET DATE: the read grades daily candles, so refetching 20 tickers of 3-year history every
        #: five minutes would buy nothing. One fetch per ticker per session; the timer then recomputes.
        self._rotation: dict | None = None
        self._rotation_bars: dict[str, list[tuple]] = {}
        self._rotation_bars_date: str | None = None
        self._reconcile_drift: list = []  # latest broker-vs-cache position drift off the bus (#26)
        #: The broker's per-symbol signed quantity, off the same snapshot (#807 item 3). None until the
        #: exec adapter has answered once — absence is "not told", never "holds none".
        self._broker_qty: dict[str, float] | None = None
        #: Venue lookups the exec adapter could not get answered (#354). None until told.
        self._venue_unanswered_lookups: int | None = None
        self._unreconciled_orders: list = []  # orders reconciliation had to SKIP, off the bus (#643)
        self._last_close: dict = {}  # InstrumentId → last bar close (Price) — the mark for Nautilus P&L (#26)
        self._venue_account_cache: dict = {}  # Venue → bool, does Portfolio hold an account for it
        self._dropped: dict[str, int] = {}    # frame kind → dropped since the last report
        self._dropped_at: float = time.monotonic()
        self._backfill_stalled_at: float = 0.0  # when backfill blocking was last suspended
        # VWAP (#182 follow-up, watchlist KPI Phase 2) — per-instrument session state. Keyed by instrument_id
        # string, not InstrumentId, to match every other per-symbol dict here. Hand-rolled rather than the
        # native `VolumeWeightedAveragePrice` indicator (codex review, Phase 2): that indicator's running-sum
        # accumulation has no revision/idempotency support, so a corrected live bar (Alpaca's `u` update) or
        # a refetch-heal replaying an already-seen minute would double-count. Keying contributions by
        # `ts_event` instead makes a repeat/revision a REPLACE, not an add.
        self._vwap_bars: dict[str, dict[int, tuple[float, float]]] = {}  # instrument_id → {ts_event: (typical_price, volume)}, this SESSION only
        self._vwap_session_date: dict[str, str] = {}  # instrument_id → ET session date (YYYY-MM-DD) last reset to
        # Today's Range + Prior Close (#182 follow-up, watchlist KPI Phase 3) — Alpaca-only (no Databento
        # equivalent batch snapshot), built lazily in `on_start` since credentials live in env vars, not
        # this constructor. None on any other provider — `_refresh_today_ranges` no-ops.
        self._http: AlpacaHttpClient | None = None
        #: (instrument_id, side) -> submit instant. In-flight backstop stops (#239); entries EXPIRE, see
        #: _PROTECTION_PENDING_NS. Keyed by the LEG, not by the coid: the coid now changes per attempt
        #: (#295), so a coid-keyed marker would never match its own retry and would double-submit.
        self._protection_pending: dict[tuple[str, str], int] = {}
        #: (instrument_id, side) -> attempts so far. A retry is a NEW order and needs a NEW id (#295).
        #: Per-lane `OrderFactory` cache (#748). Built lazily; see `_lane_order_factory`.
        #: What we ASKED the outside world for and did not get (#757). Standing state, coalesced by
        #: (kind, subject) with a count and the venue's own words. On 2026-08-31 the venue refused
        #: 275 market-data subscriptions and 136 permission requests, every one was logged, nothing
        #: added up, and `/health` said ok while half a book could not be priced.
        self._failed_requests = FailedRequests()
        # WHAT WE ASKED THE VENUE TO STREAM, AGAINST WHAT ARRIVED (#618). 392 subscriptions went out
        # on 2026-08-31 and a large subset was refused (275 x `10089`, 136 x `10189`); each refusal
        # was logged and nothing held both numbers, so half a book being dark was indistinguishable
        # from a quiet market. Recorded here rather than from the adapter's error codes because the
        # codes reach that adapter's logger, not a callback — and because "nothing came down this
        # stream" is the same measurement on every venue.
        self._subscriptions = SubscriptionLedger()
        # WHAT THE BOOK REPAIR HAS ALREADY DONE (#744). Consulted before any pair is booked: running
        # a repair twice books offsetting legs against a book the first run corrected, re-opening the
        # short it closed. Rehydrated from durable storage by the caller — process memory is not a
        # record when the engine is recreated on every deploy.
        self._contra_ledger = ContraLedger()
        # FILLS THE ENGINE FABRICATED (#758). `OrderFilled.reconciliation` is Nautilus's own flag for
        # a fill it generated to force the cache to match — not one the venue reported. Per
        # the operating notes that poll is the phantom's mint: a reduce-only fill lands on no position and the
        # <=10s poll "fabricates the difference as a synthetic sell at a price that never traded".
        # It is the highest-signal event the engine produces and it logged at INFO among thousands;
        # GMAB's history had to be reconstructed by counting grep matches.
        self._inferred_fills = InferredFills()
        self._terminal_fills = TerminalFills()  # fills on orders the cache holds terminal (#807 item 4)
        # Last computed cache-vs-venue truth, stamped. Recorded on the protection tick, which ALREADY
        # reads venue positions — a second poll would be a second derivation of the same fact, and
        # those drift (and cost pacing budget on IBKR).
        self._book_truth: dict = {}
        # Consecutive-disagreement streaks. Nautilus announces its own give-up as a LOG LINE and
        # nothing else, so the symptom is measured instead of the internal — which is also the fact
        # an operator needs: this has disagreed on N reads and nothing has fixed it.
        self._disagreements = DisagreementStreaks()
        # ONE PLACE WHERE AN OBSERVATION MAY FAIL (#758). Not a try/except per site: the site-local
        # version was written three times in this file and one of them had an EXCEPT BRANCH THAT
        # RAISED, aborting the protection pass it rode on. Declared up front so an observer that
        # never runs is visible as such — absence on a failures-only surface is indistinguishable
        # from health, which is where four defects hid on 2026-08-31.
        self._observations = Observations()
        # The IB client handle for instrument search (#837), attached by build_node once the data
        # engine has its client. None on a node without one — the search command then REFUSES.
        self._ib_client_ref = None
        self._search_timeout_s = _SEARCH_VENUE_TIMEOUT_S
        self._observations.declare("shortable", "book_truth", "feed_stale", "subscriptions", "inferred_fills",
                                   "paced_venue_call",
                                   "fills_on_terminal_orders")
        # #873 phase 1: the market-aware plane exists from construction (so the frame's container is a
        # dict, never None, on a build that carries it); `on_start` re-reads the knobs from settings.
        self._init_market_aware(dwell=3, poll_secs=60)
        self._log_compaction: dict | None = None
        self._lane_factories: dict = {}
        self._protection_attempts: dict[tuple[str, str], int] = {}
        #: Guards against two ticks overlapping. The reconciler does network I/O on a 60s timer, so a slow
        #: broker read lets a second tick in before the first has written its in-flight marker — and that
        #: marker is written AFTER the submit by design, so it cannot close the window. Both ticks would
        #: submit, resting two stops on shares that can carry one (codex review, High).
        self._protection_running: bool = False
        #: instrument_id -> ns deadline until which the protection reconciler must NOT re-arm.
        #: Set by `release_for_exit` for the window between cancelling a protective stop and the exit
        #: landing. TTL-bounded on purpose: every entry here is a position deliberately left naked, so a
        #: failed exit has to give the protection back on its own rather than waiting for an operator.
        self._exit_suppressed: dict[str, int] = {}
        #: A FLIP DEFERRED PASS AFTER PASS IS A NUMBER (#907). Per (instrument, lane) leg in
        #: `wrong_mode` that did not flip this pass: consecutive passes, when the CURRENT streak
        #: started (it resets with the count — #908's surviving `first_seen` is the other fact), and
        #: the named refusal that stopped it last. Rebuilt every pass from the previous pass's record, so an
        #: early return (outside RTH, settings unreadable, no broker) leaves it EMPTY — a count frozen
        #: across the 17.5 h between sessions would describe a book nobody evaluated.
        self._flip_pending: dict[tuple[str, str], dict] = {}
        #: Whether the LAST protection pass reached the flip block at all (#907 review). `[]` on the
        #: frame must mean "evaluated, nothing deferred" — never "outside RTH / disabled / unreadable
        #: settings / no broker", which publish None. Three states one level inside the DTO's None.
        self._flip_evaluated: bool = False
        #: Positions where the broker holds a protective stop this engine's cache cannot see (#269). The
        #: detector's output, kept so it can be surfaced rather than only logged — a divergence that is
        #: merely logged is one nobody reads until an operator cannot exit a position.
        self._protection_divergence: list[dict] = []
        #: Positions whose exit was REJECTED while their protection was already released (#546).
        #: Carried on the HEALTH frame beside `protection_divergence` because that is a plane
        #: /health serves and the banner renders — the first version published its own topic, which
        #: nothing consumed, reproducing the "trace nobody watches" the ticket was filed about.
        #: Bounded: the newest few, so a repeating venue refusal cannot grow this without limit.
        self._naked_positions: list[dict] = []
        #: instrument_id -> {"until": ns, "saw_exit": bool, "qty": n} for each release window (#546).
        #: THE OBSERVABLE THAT DOES NOT DEPEND ON AN EVENT. `NautilusBroker.exit` releases
        #: protection and then calls `submit()`, which has four LOCAL refusal paths that return
        #: ok=False before Nautilus creates an order at all — no rejection event is emitted, so the
        #: rejection hook cannot fire, and the window used to stand its full TTL in silence with the
        #: position bare. A window that CLOSES having never carried an exit is that case, visible.
        self._exit_windows: dict[str, dict] = {}
        #: Last successfully projected trades payload (#298). Republished, marked stale, when a projection
        #: tick fails — showing the last known book beats blanking it.
        self._last_good_trades: list = []
        #: client_order_id -> the venue's own computed stop price (#289). A trailing stop's trigger lives
        #: only at the broker; without this, SECURED is structurally $0.00.
        self._broker_stop_prices: dict[str, float] = {}
        #: Instrument ids the BROKER has a protective sell resting on (#285). None until the first poll
        #: — deliberately distinct from empty, because "not asked" must not read as "nothing protected".
        self._broker_protected: set[str] | None = None
        #: The BROKER's cost basis per instrument, or None while the broker has not been asked (#370).
        #: Three-state for the same reason `_broker_protected` is: "no divergence" and "never checked"
        #: are different claims, and collapsing them is how a stale basis passes for a verified one.
        self._broker_avg_entry: dict[str, float] | None = None
        # None until the first sweep lands — DISTINCT from "computed and it was zero". The UI must not
        # render a confident $0.00 for a window nobody has asked the broker about yet.
        self._realized_periods: dict | None = None
        #: Every closed leg this engine knows of (#846), keyed (position id, open time) — the identity the
        #: snapshot store dedups by. Filled by the restart seed (Nautilus's persisted snapshots) and by
        #: every `PositionClosed` seen live. `positions_closed()` loses a leg on a NETTING reopen; this
        #: does not. Grows for the process life — 716 states over five weeks on paper, trivial.
        self._closed_legs: dict[tuple[str, int], CycleLeg] = {}
        self._legs_restored: int = 0
        #: THREE STATES: None = not seeded yet; a key = the seed's Redis scan stopped there (its legs are
        #: incomplete and every window says so); "" = the scan finished.
        self._legs_seed_stopped_at: str | None = None
        self._legs_seeded: bool = False
        # The degraded-state message once multi-period realized is known to be unavailable on this
        # stack (#641: the activities read is Alpaca-REST-specific and deliberately unported). Set on
        # first sweep and WARNED once — a permanent, accepted property of the stack repeated every
        # sweep is the cry-wolf alarm operators learn to scroll past. None = not degraded (or not yet
        # asked), which is a different fact from "degraded" — three states beat two.
        self._realized_periods_degraded: str | None = None
        # NONE, NOT "iex" (#619). The default was the defect: the field was never unset, so no code path
        # could notice it had never been configured, and a non-Alpaca provider silently carried an Alpaca
        # plan string into the subscription gate. It is set only on the Alpaca path in `on_start`.
        self._alpaca_feed: str | None = None
        # Declared by the DATA PROVIDER through `DataClientSpec` (#619). A provider that declares no cap
        # is not capped; a venue's real limits belong to its own adapter.
        self._realtime_symbol_budget = realtime_symbol_budget
        # Declared by the DATA PROVIDER (#617). IB serves ~60 historical requests per 10 minutes and
        # enforces it by answering EMPTY rather than erroring, so an over-ask looks exactly like a
        # venue with no data — 744 requests, 2,232 empty responses, 0 bars, 0 errors.
        self._hist_rate = historical_requests_per_minute
        # ONE QUEUE FOR EVERY HISTORICAL CALL THIS NODE MAKES (#836): the feed's own history AND its
        # live subscriptions AND the lanes' warmups. A heap of (priority, seq, fn, args, kw, key) —
        # priority 0 = live subscribe for a HELD position, 1 = held-position history (its ATR is
        # its PROTECTION — real capital, not display), 2 = lane warmups, 3 = lane live subscribes,
        # 4 = display live, 5 = display history — so what a held position needs leaves in the
        # first minute and a lane is warm by its slot, not after ~300 display subscriptions. Measured 2026-09-09: ~507 reqHistoricalData in ONE
        # SECOND at boot, IB answered 39 and went silent for the session.
        self._bar_request_q: list = []
        self._bar_request_seq: int = 0
        self._bar_request_seen: set = set()
        #: DISPLAY BACKFILL YIELDS TO TRADING WARMUP (#618 step 2). The paced queue is display-only
        #: — every lane request leaves on its own path — but both fight the same venue budget
        #: (IB ~60/10min shared). A 12:54 restart left lane warmup at 78% at the 13:05 slot and the
        #: first IBKR decision refused on the coverage floor. Holding the display drain for N
        #: seconds after start gives the whole budget to the lanes. Default 0 = OFF (every new
        #: behaviour gate defaults off); an unmetered venue skips the queue entirely regardless.
        self._display_hold_secs = float(os.environ.get("KUMO_DISPLAY_BACKFILL_HOLD_SECS", "0") or 0)
        # Stamped LAZILY at the first drain tick: the base Strategy clock is abstract until the
        # node registers the actor, so reading it in __init__ raises NotImplementedError.
        self._feed_started_ns: int | None = None
        # A calendar from the ATTACHED adapter's own sessions (#628), or None when this provider
        # cannot supply one — `build_calendar` then behaves exactly as before and the trading tenant
        # is unchanged. LAZY: the cache is empty at construction.
        # A calendar from the ATTACHED adapter's own sessions (#628) — or, when this provider
        # cannot supply one, the BROKER's (#739). Only the IBKR provider declares the flag, so
        # before this the paper instance had no calendar at all and #734's capture could not arm on
        # the one stack that holds the trading history it exists to describe.
        #
        # THE FALLBACK IS THE ONE kumo-strategies ALREADY SHIPS. `build_calendar` selects
        # `AlpacaCalendar` when APCA credentials are present, and it is tz-aware ET, fetches with
        # sync urllib, and already keeps `session` as a `date`. A second implementation was written
        # here and DELETED: it got the awareness wrong, got the session type wrong, and its async
        # fetch raised in every loop configuration production actually presents.
        self._venue_calendar = (
            VenueCalendar(lambda: self.cache) if supplies_trading_calendar
            else broker_calendar_or_none()
        )
        # What session span this provider's 1-DAY bars cover (#616) — the spec's REQUIRED field,
        # forwarded by build_node. None here is NOT a fourth session value: it is "never told us"
        # (a test double built without the builder), and the compass stamps it as 'undeclared'
        # rather than letting absence read as either real answer.
        self._daily_bars_cover = daily_bars_cover
        # WHICH GRANULARITIES THIS VENUE CAN ACTUALLY BUILD (#612).
        #
        # An INTERNAL bar type is aggregated by Nautilus from TRADE TICKS — `_subscribe_bar_aggregator`
        # issues `SubscribeTradeTicks` — so on a venue that streams none the subscription succeeds,
        # the series stays empty forever, and nothing anywhere says why. Measured 2026-08-31: paper
        # carried 1-HOUR and 1-WEEK INTERNAL series, staging carried neither, four of six sparklines
        # were blank and 11 of 22 held positions had no price. Same code, one venue apart.
        #
        # None is "never told us", not "no" — and it refuses the dependent granularities exactly like
        # a declared False, because assuming ticks because Alpaca had them is what produced this.
        self._aggregation = aggregation_plan(GRANULARITIES, streams_trade_ticks=streams_trade_ticks)
        # RETAINED, because the SUBSCRIPTION path needs them and only the aggregation split had them
        # (#812). `streams_trade_ticks` reached `aggregation_plan` and nothing else, so IBKR declared
        # "no trade tape" and `_after_definition` subscribed one anyway — 224 unservable tick-by-tick
        # requests per boot, burning IB's shared concurrency limit.
        #
        # `None` is NOT False. It means a double built outside `build_node` never declared, and the
        # gate below treats it as "say nothing, subscribe" — the behaviour before this change — so a
        # test fixture that predates the field cannot silently lose its tick planes.
        self._streams_trade_ticks = streams_trade_ticks
        self._streams_quote_ticks = streams_quote_ticks
        self._today_range_inflight = False  # guards against overlapping batch fetches if one runs long
        # Fundamentals (#182 follow-up, KPI Phase 2) — Financial Modeling Prep, INDEPENDENT of
        # `data_provider`/`self._http` above (codex review: don't hang enrichment off the market-data
        # provider choice — a Databento/IBKR swap must not silently break fundamentals). Built lazily in
        # `on_start` from its own env var (FMP_API_KEY). None if unset — `_refresh_fundamentals` no-ops.
        self._fmp: FmpHttpClient | None = None
        self._fundamentals_inflight = False
        # Trade-cycle projection (#73): threads native positions/snapshots/orders into TradeDTOs. Keyed to the
        # EXECUTION client (exec_client_id), not the data client — the canonical identity is broker-side. None
        # when there's no exec client (data-only node) → no trades plane.
        # ONE PROJECTION PER NODE STRATEGY, keyed by strategy id. It used to be a single projection
        # pinned to this feed strategy (MANUAL), which meant every other cockpit strategy in the same
        # node — MOMENTUM-002 and its eight positions — projected no cycles at all and fell through to
        # the #79 quarantine plane, rendering as "UNCLAIMED · foreign strategy". They are ours; the
        # Orders tab labelled them MOMENTUM correctly the whole time, because only this plane consulted
        # the owned set. `register_strategy` adds the peers once the node has built them.
        self._trade_cycles: dict[str, TradeCycleProjection] = (
            {str(self.id): TradeCycleProjection(exec_client_id, self.id)}
            if exec_client_id is not None
            else {}
        )
        # Durable cycle envelope (#74b.2): the engine authors it. On startup it seeds the projection from the
        # envelope (cycle boundary) + Nautilus's persisted position snapshots (leg P&L); on each transition it
        # upserts. Only when durable persistence is on — otherwise the projection is in-memory-only as before.
        self._exec_client_id = exec_client_id  # the projection's client_id — scopes the envelope seed load
        # Durable command idempotency ledger (#78): reserve-before-act so a restart + redelivered ui:commands
        # entry can't double-send an order. Only when there's an exec client (order commands are possible).
        self._cmd_ledger: CommandLedgerStore | None = (
            CommandLedgerStore() if exec_client_id is not None else None
        )
        self._cycle_store: CycleEnvelopeStore | None = (
            CycleEnvelopeStore() if (durable_cache_enabled() and self._trade_cycles) else None
        )
        # Gate publishing until the restart seed completes, so events don't mint fresh cycles over durable ones.
        self._cycles_seeded: bool = self._cycle_store is None
        #: cycle_id -> `envelope_key` of the last write that DURABLY LANDED, and of the one in flight, so
        #: the fold writes only changes and never more than one write per cycle at a time (#564). Touched
        #: from the fold thread and the event-loop thread both, hence the lock.
        self._envelope_written: dict[str, tuple] = {}
        #: cycle_id -> (attempt, key) of the ONE write in flight for it. The attempt counter makes two
        #: writes carrying the same key distinguishable, which a key alone cannot do.
        self._envelope_pending: dict[str, tuple[int, tuple]] = {}
        self._attempt_seq: int = 0
        self._envelope_lock = _new_envelope_lock()
        #: Guards EVERY read and write of `_trade_cycles` projections (#568). `project()` MUTATES, so
        #: `current_cycle_for` is a writer too — that is why the seed could not simply run on another
        #: thread. Reentrant: `_publish_trades` and `current_cycle_for` can nest.
        self._projection_lock = threading.RLock()
        self._seed_thread: threading.Thread | None = None

    def current_cycle_for(self, instrument_id: str, strategy_id: str):
        """The cycle DTO for ONE (instrument, strategy) right now, or None.

        `_trade_cycles` is a dict keyed by strategy id (one projection per strategy, added by
        `register_strategy`). Calling `.project()` on the dict itself raises `'dict' object has no
        attribute 'project'` — which is exactly what every manager's cycle-drift guard did once this
        became multi-strategy: the guard threw, `apply()` failed, and PEAK / STOP-AND-REENTER /
        PYRAMID / deferred-flatten stopped working in the live engine while the UI showed the
        AttributeError as the manager's own error.

        SCOPED to one strategy on purpose, rather than folding every projection:

        1. `project()` MUTATES — it advances the projection and can pop CLOSED cycles. A read-only
           guard must not drive OTHER strategies' projections forward outside `_publish_trades()`,
           which is what persists them.
        2. Selecting across strategies by instrument alone picks whichever cycle comes first for a
           name two strategies both hold (FIG has been held by MANUAL and MOMENTUM), so a guard could
           compare its row against a cycle belonging to someone else and fail as "stale".

        Both were raised by codex review of the first version of this fix, which folded over all
        projections.
        """
        proj = self._trade_cycles.get(str(strategy_id))
        if proj is None:
            return None
        # UNDER THE LOCK: `project()` mutates the fold, so this is a WRITER, not a reader. Manager drift
        # guards call it on the message-bus thread while the seed applies on its own — codex caught the
        # claim that nothing else touched the projection, which was false.
        with self._projection_lock:
            dtos = proj.project(self.cache, self.clock.timestamp_ns())
        return next(
            (d for d in dtos if d.instrument_id == instrument_id and d.strategy_id == strategy_id),
            None,
        )

    def register_strategy(self, strategy_id, strategy=None) -> None:
        """Give another strategy in THIS node its own cycle projection, and thereby cockpit ownership.

        Ownership and the trades plane are the same fact: a strategy the cockpit runs must project
        cycles (so it renders as a managed position) AND be absent from the quarantine plane (so it is
        not also reported as someone else's). Registering here does both, which is why they cannot
        drift apart — the previous split let MOMENTUM be owned by the order stream and foreign to the
        portfolio at the same time.
        """
        # The OBJECT is recorded first and unconditionally. The projection below is skipped when there
        # is no exec client or the id is already known, and a flatten that could not find its owner
        # because of an early return would refuse a position it is perfectly able to close.
        if strategy is not None:
            self._sibling_strategies[str(strategy_id)] = strategy
            self._pace_lane(strategy)
            # #873: declared per lane, so a lane the poller never asks reads `never ran` on /health.
            self._observations.declare(f"market_aware:{strategy_id}")
            readings = getattr(self, "_market_aware_readings", None)
            if readings is not None:
                readings.setdefault(str(strategy_id), None)
        if not self._trade_cycles or str(strategy_id) in self._trade_cycles:
            return  # no exec client (nothing to project), or already registered
        self._trade_cycles[str(strategy_id)] = TradeCycleProjection(self._exec_client_id, strategy_id)

    def _pace_lane(self, strategy) -> None:
        """Route a sibling lane's outbound venue calls through this node's ONE paced queue (#836).

        The lanes are kumo-strategies runners built by cockpit. Their warmup — `momentum_rotation.py:297`,
        `self.request_bars(bt, start)` for every symbol in the pool at on_start — is outside cockpit's
        pacer, and on 2026-09-09 MOMENTUM and BCTROT each fired 105 daily requests for the SAME 105
        symbols in the same second the feed fired 297 subscriptions. Wrapping here, at the one place
        every lane already passes through, paces them without touching the pinned package.

        ISSUED THROUGH THE LANE'S OWN BOUND METHOD, so the response routes back to the lane. Two
        lanes asking for the same series are two venue calls, each in its own slot — nothing here
        collapses them (see `_paced_request` on why `join_request` must NOT be set).
        Warmups at _LANE_REQUEST_PRIORITY, a lane's own live subscriptions one tier behind — both
        ahead of every display tier, behind only a HELD position's own data.

        A provider declaring no rate wraps nothing — the lane's calls go out exactly as before.
        """
        if self._hist_rate == float("inf"):
            return
        orig_request = getattr(strategy, "request_bars", None)
        orig_subscribe = getattr(strategy, "subscribe_bars", None)
        sid = str(getattr(strategy, "id", id(strategy)))
        if callable(orig_request):
            def _paced_request(bar_type, *args, _orig=orig_request, **kw):
                # `*args, **kw` VERBATIM: Actor.request_bars takes limit/client_id/callback/... positionally
                # too, and a wrapper naming only (bar_type, start, end) put a lane's `limit` into the
                # slot after `end` — codex on 5f15fdd. The pinned lanes pass only `start` today.
                # Forwarded EXACTLY as the lane made it. `join_request=True` was set here from
                # 3d3572e to 920bf48 on the belief that Nautilus joins identical in-flight requests;
                # in 1.229 it marks a RequestJoin LEG and the DataEngine PARKS it until a join arrives
                # (engine.pyx _handle_request). Measured 2026-09-09 15:45Z: 173 warmups parked, 0 issued.
                self._enqueue_paced(_LANE_REQUEST_PRIORITY, _orig, (bar_type, *args), kw,
                                    key=("request", sid, str(bar_type)))
            try:
                strategy.request_bars = _paced_request
            except (AttributeError, TypeError):
                _log.warning("lane %s does not accept a request_bars override — NOT paced", sid)
        if callable(orig_subscribe):
            def _paced_subscribe(bar_type, *args, _orig=orig_subscribe, **kw):
                self._enqueue_paced(_LANE_SUBSCRIBE_PRIORITY, _orig, (bar_type, *args), kw,
                                    key=("subscribe", sid, str(bar_type)))
            try:
                strategy.subscribe_bars = _paced_subscribe
            except (AttributeError, TypeError):
                _log.warning("lane %s does not accept a subscribe_bars override — NOT paced", sid)

    async def _recover_transfers(self) -> None:
        """Finish or fail any transfer interrupted by a crash, BEFORE trading resumes.

        A transfer stuck at SOURCE_APPLIED has taken quantity out of one strategy without giving it to the
        other: the internal books don't sum to the broker net until it completes. Left alone, continuous
        reconciliation would see the gap and invent a compensating EXTERNAL position on top of it.

        Recovery is driven by the deterministic leg order ids — if the cache already has a leg, it landed
        before the crash and is not reapplied. Fail-CLOSED: if recovery itself fails, the transfer is marked
        FAILED and surfaced rather than left ambiguous.
        """
        from api import transfers as tr
        from api.db.engine import session_factory

        try:
            async with session_factory() as session:
                pending = tr.incomplete(await tr.all_transfers(session))
        except Exception as exc:  # noqa: BLE001 — store unreachable
            _log.error("transfer recovery could not read the log: %r", exc)
            return

        for p in pending:
            req = p.request
            source_done = self.cache.order(ClientOrderId(req.source_order_id)) is not None
            dest_done = self.cache.order(ClientOrderId(req.dest_order_id)) is not None
            _log.warning(
                "recovering transfer %s (state=%s, source_applied=%s, dest_applied=%s)",
                req.transfer_id, p.state, source_done, dest_done,
            )
            if p.state == "FAILED" and not source_done and not dest_done:
                # Failed before anything landed — the books are consistent and the failure had a reason.
                # Completing it now would resurrect a transfer nobody confirmed. Leave it.
                _log.warning("transfer %s failed with no legs applied — leaving it failed", req.transfer_id)
                continue
            try:
                source_side, dest_side = tr.leg_sides(req.side)
                if not source_done:
                    self._apply_leg(req, req.source_strategy_id, source_side, req.source_order_id, "SRC")
                if not dest_done:
                    self._apply_leg(req, req.target_strategy_id, dest_side, req.dest_order_id, "DST")
                async with session_factory() as session:
                    await tr.record(session, req, "COMPLETED")
                _log.warning("transfer %s recovered", req.transfer_id)
            except Exception as exc:  # noqa: BLE001
                async with session_factory() as session:
                    await tr.record(session, req, "FAILED", error=f"recovery: {exc}"[:250])
                _log.error("transfer %s could not be recovered: %r", req.transfer_id, exc)

    def on_start(self) -> None:
        # Capture the node's event loop — Nautilus timer callbacks don't run on it, so the async watchlist
        # reconcile must be scheduled onto it via run_coroutine_threadsafe (not ensure_future).
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()
        # Finish any transfer interrupted by a crash before anything else touches the books (#80 spin-off).
        asyncio.run_coroutine_threadsafe(self._recover_transfers(), self._loop)
        # Short timeouts so a Redis stall fails fast on the writer thread (never the trading loop).
        self._redis = redis.Redis(
            host=os.environ.get("KUMO_REDIS_HOST", self._bridge.get("redis_host", "127.0.0.1")),
            port=int(os.environ.get("KUMO_REDIS_PORT", self._bridge.get("redis_port", 6379))),
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        self._writer = threading.Thread(target=self._drain, name="ui-bridge-writer", daemon=True)
        self._writer.start()
        # Today's Range + Prior Close (#182 follow-up, KPI Phase 3) — Alpaca-only, credentials from env
        # (same convention as `AlpacaDataClientConfig`/`build_data`). Missing/unset creds just leave
        # `self._http` None — `_refresh_today_ranges` no-ops, same as any other non-Alpaca provider.
        if self._cfg.data_provider == "alpaca":
            provider_config = self._cfg.provider_config
            api_key = os.environ.get(provider_config.get("key_env", "APCA_API_KEY_ID"))
            api_secret = os.environ.get(provider_config.get("secret_env", "APCA_API_SECRET_KEY"))
            if api_key and api_secret:
                defaults = AlpacaDataClientConfig()
                self._alpaca_feed = provider_config.get("feed", defaults.feed)
                self._http = AlpacaHttpClient(
                    key=api_key,
                    secret=api_secret,
                    trading_base=provider_config.get("trading_base_url", defaults.trading_base_url),
                    data_base=provider_config.get("data_base_url", defaults.data_base_url),
                )
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(self._http.connect(), self._loop)
        # Fundamentals (#182 follow-up, KPI Phase 2) — INDEPENDENT of data_provider/the block above (codex
        # review: enrichment must not be hung off the market-data provider choice). Its own env credential,
        # its own client. Missing FMP_API_KEY just leaves `self._fmp` None — `_refresh_fundamentals` no-ops.
        fmp_key = os.environ.get("FMP_API_KEY")
        if fmp_key:
            self._fmp = FmpHttpClient(fmp_key)
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._fmp.connect(), self._loop)
        # #74b.2 restart seed: rebuild the projection's active cycles from the durable envelope + the persisted
        # position snapshots BEFORE folding live events. On its OWN THREAD (#568) — queued on the node
        # loop it waited 4m47s for a slot that never came, because `on_start` also starts the backfill
        # for ~500 instruments. `_publish_trades` no-ops until it completes. Only when durable
        # persistence is on.
        if self._cycle_store is not None:
            self._start_seed_thread()
        # Command layer (#32): consumer group on ui:commands, then a reader thread. MKSTREAM so it exists
        # before the api ever publishes; BUSYGROUP (group already exists) is fine on restart.
        try:
            # id="0" so the group sees commands published BEFORE it existed (engine-down gap) — not just
            # the tail. BUSYGROUP (exists already) is fine on restart, keeping the group's delivery cursor.
            self._redis.xgroup_create(_CMD_STREAM, _CMD_GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                _log.warning("command group create failed: %s", exc)
        self._cmd_reader = threading.Thread(target=self._drain_commands, name="ui-command-reader", daemon=True)
        self._cmd_reader.start()
        # Account plane (#41): the exec adapter publishes Alpaca's own equity/buying_power/multiplier on the
        # in-process bus (Nautilus AccountState only carries cash). Subscribe + cache the latest snapshot.
        self.msgbus.subscribe(_ACCOUNT_TOPIC, self._on_broker_account)
        self.msgbus.subscribe(_EQUITY_TOPIC, self._on_broker_equity_curve)
        # THE WAIT STARTS WHEN WE START WAITING, not at the first tick that happens to notice. Stamping
        # it inside `_state_equity_curve_unavailable` meant the first tick only recorded the time and
        # the second made the claim — so the real delay was _ROTATION_SECS + the grace, ~8 minutes,
        # while the constant said 180s. A grace that does not mean what it says is a grace nobody can
        # reason about.
        self.msgbus.subscribe(_RECONCILE_TOPIC, self._on_broker_reconcile)
        self.msgbus.subscribe(_UNRECONCILED_TOPIC, self._on_broker_unreconciled)
        # EVERY strategy's order events, not just this one's (#207). Nautilus delivers `on_order_event`
        # only for the subscribing strategy's own orders, and this strategy is MANUAL — so the trade
        # receipt, whose entire job is reporting AUTOMATED orders, was wired to the one source that can
        # never carry them. Eight orders filled on 2026-08-10 and not one receipt was sent. The Orders
        # blotter had the same hole and did not show it, because the periodic snapshot kept healing it.
        self.msgbus.subscribe(_ORDER_EVENTS_TOPIC, self._on_any_order_event)
        # EVERY strategy's position events (#846): a closed leg is a fact about the account's realized
        # P&L whichever lane closed it, and `on_position_event` below only ever sees MANUAL's.
        self._subscribe_position_events()
        # Universe = the configured symbols + any held positions, so watched AND held symbols stream.
        # Positions are reconciled into the cache by the exec client before on_start.
        for instrument_id in [*self._instrument_ids, *(p.instrument_id for p in self.cache.positions())]:
            key = str(instrument_id)
            if key in self._loaded:
                continue
            self._loaded.add(key)
            try:
                self._load(instrument_id)
            except (ValueError, KeyError):
                continue  # unroutable venue (no dataset) — skip, don't abort startup
        interval = pd.Timedelta(seconds=int(self._bridge.get("snapshot_interval_secs", 2)))
        self.clock.set_timer(name=_SNAPSHOT_TIMER, interval=interval, callback=self._on_snapshot)
        self._schedule_bar_drain()
        # Periodic re-request: heals any granularity whose initial load came back empty (flaky per-request
        # historical), and refreshes bars when the feed has newer data.
        self.clock.set_timer(
            name=_REFETCH_TIMER, interval=pd.Timedelta(seconds=_REFETCH_SECS), callback=self._on_refetch
        )
        # Runtime watchlist (Postgres): pick up symbols added at runtime and stream them like the config
        # universe — else a just-added watched symbol never gets bars/price (stuck "Loading").
        self.clock.set_timer(
            name=_SESSION_STATE_TIMER, interval=pd.Timedelta(seconds=_SESSION_STATE_SECS),
            callback=self._on_session_state,
        )
        # #873 phase 1: the market-aware poll. Knobs read here, once — the schema says "applies on the
        # next engine restart"; a per-tick settings read inside the observed poll would make a
        # settings hiccup read as a lane's hook failing.
        dwell, poll_secs = self._market_aware_settings()
        self._init_market_aware(dwell=dwell, poll_secs=poll_secs)
        self.clock.set_timer(name=_MARKET_AWARE_TIMER, interval=pd.Timedelta(seconds=poll_secs),
                             callback=self._on_market_aware)
        self.clock.set_timer(
            name=_WATCHLIST_TIMER, interval=pd.Timedelta(seconds=_WATCHLIST_SECS), callback=self._on_watchlist
        )
        # Dispatch every registered manager kind (#55) — a deferred_flatten fires the moment its session
        # trigger is met. Reconcile + one immediate dispatch pass run ONCE here, ahead of the steady-state
        # timer, so a restart mid-RTH doesn't wait 30s AND doesn't reconcile on every subsequent tick
        # (see _on_manager_tick).
        self.clock.set_timer(
            name=_MANAGER_TIMER,
            interval=pd.Timedelta(seconds=_MANAGER_TICK_SECS),
            callback=self._on_manager_tick,
        )
        # LOG COMPACTION AND AGE-BASED RETENTION (#758). Hourly: it touches only files the logger has
        # finished with, so there is nothing to gain from running it often, and a compaction pass
        # competing with a busy session is exactly the wrong trade.
        #
        # Only when a directory is configured — an instance with no log volume has nothing to compact
        # and must not be given a timer that does nothing on every tick.
        if os.environ.get("KUMO_LOG_DIR"):
            self._observations.declare("log_compaction")
            self.clock.set_timer(
                name=_COMPACTION_TIMER,
                interval=pd.Timedelta(seconds=_COMPACTION_SECS),
                callback=self._on_compaction,
            )
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._startup_manager_check(), self._loop)
        # Fundamentals (#182 follow-up, KPI Phase 2): its own low-frequency timer, not the 5s watchlist
        # cadence — fundamentals don't move intraday. Fires once immediately too (below), so a fresh boot
        # isn't blank for up to 6h waiting on the first timer tick.
        self.clock.set_timer(
            name=_FUNDAMENTALS_TIMER, interval=pd.Timedelta(seconds=_FUNDAMENTALS_SECS), callback=self._on_fundamentals
        )
        # Backstop protective stops (#239). Gated OFF by default in settings — this places orders.
        self.clock.set_timer(
            name=_PROTECTION_TIMER, interval=pd.Timedelta(seconds=_PROTECTION_SECS),
            callback=self._on_protection_tick,
        )
        # Period-scoped realized (#322). Fires once immediately as well as on the timer, so a restart
        # does not show five empty periods for the first five minutes.
        self.clock.set_timer(
            name=_REALIZED_TIMER, interval=pd.Timedelta(seconds=_REALIZED_SECS),
            callback=self._on_realized_tick,
        )
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_realized_periods(), self._loop)
        # The rotation read (#384), on the same shape as the sweep above and for the same reason: fires
        # once immediately so a restart does not serve an empty Market tab for five minutes.
        self.clock.set_timer(
            name=_ROTATION_TIMER, interval=pd.Timedelta(seconds=_ROTATION_SECS),
            callback=self._on_rotation_tick,
        )
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_rotation(), self._loop)
        # COCKPIT'S OWN PREFLIGHT PROBES (#532), on the same shape and for the same reason: fired once
        # IMMEDIATELY so the boot gate — which runs on the first account snapshot, seconds from here —
        # has something to read. Without this the gate reads an empty cache and reports all three
        # missing, which is the state it was in for its entire life.
        #
        # Refreshed on a timer as well because `lifecycle` and `budget` change while the node runs: an
        # operator disables a lane or moves a sleeve, and a probe cached at boot would answer for a
        # world that no longer exists.
        self.clock.set_timer(
            name=_PLATFORM_PROBE_TIMER, interval=pd.Timedelta(seconds=_PLATFORM_PROBE_SECS),
            callback=self._on_platform_probe_tick,
        )
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_platform_probes(), self._loop)
        # QC345's universe refresh (#324), armed on the node's own clock. Scheduled from HERE rather
        # than from `build_qc345_strategy` for a mechanical reason: the strategy captures its event
        # loop in `on_start`, which has not run at build time, and the refresh is ~373.8s of blocking
        # REST that must go to a worker thread rather than be awaited on the loop. This actor is
        # already started, already owns `_loop`, and already owns every other periodic job.
        #
        # Gated off by default — with the gate unset nothing is armed and the universe stays whatever
        # an operator typed.
        try:
            from strategies.qc345_refresh import schedule_refresh

            schedule_refresh(self)
        except Exception as exc:  # noqa: BLE001 — a display actor must not fail on an optional job
            self.log.warning(f"qc345 universe refresh could not be scheduled: {exc}")
        # END-OF-DAY POSITION OBSERVATION (#734), on the same shape and for the same reasons: an
        # optional job, armed from here because this actor already owns the clock and every other
        # periodic task, and wrapped because a display actor must not die on one.
        #
        # Gated OFF by default (`KUMO_EOD_CAPTURE`). With the gate unset NO TIMER IS REGISTERED at
        # all — not a timer that returns early — because the tick is where a refusal gets logged, and
        # a disarmed-but-registered job would warn about work nobody turned on.
        #
        # It also declines to arm when the provider supplies no trading calendar. The capture
        # schedules off the VENUE's own close: a fixed 16:00 ET timer is wrong on every half day, and
        # the operator's clock is SGT, so a locally-stamped date files an ET session under the
        # following day. Not arming is the correct answer there, not a fallback.
        try:
            from api.eod_hook import schedule_capture

            schedule_capture(self)
        except Exception as exc:  # noqa: BLE001 — a display actor must not fail on an optional job
            self.log.warning(f"eod capture could not be scheduled: {exc}")
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_fundamentals(self.clock.timestamp_ns()), self._loop)

    def on_stop(self) -> None:
        self.clock.cancel_timer(_SNAPSHOT_TIMER)
        self.clock.cancel_timer(_REFETCH_TIMER)
        self.clock.cancel_timer(_MANAGER_TIMER)
        self.clock.cancel_timer(_WATCHLIST_TIMER)
        self.clock.cancel_timer(_SESSION_STATE_TIMER)
        self.clock.cancel_timer(_MARKET_AWARE_TIMER)
        self.clock.cancel_timer(_FUNDAMENTALS_TIMER)
        # Cancelled by NAME and tolerantly: it is only registered when armed, and cancelling a timer
        # that was never set must not take down a shutdown path.
        try:
            from api.eod_hook import EOD_TIMER

            self.clock.cancel_timer(EOD_TIMER)
        except Exception:  # noqa: BLE001 — never registered, or already gone
            pass
        self._stopping.set()
        if self._writer is not None:
            self._writer.join(timeout=2.0)
        if self._cmd_reader is not None:
            self._cmd_reader.join(timeout=3.0)
        if self._redis is not None:
            self._redis.close()
        if self._http is not None and self._loop is not None:
            # Bounded wait (matches the writer/cmd_reader joins above) — an unawaited close leaves the
            # aiohttp session's connector open until GC, logging a warning at best (codex review, Phase 3).
            try:
                asyncio.run_coroutine_threadsafe(self._http.close(), self._loop).result(timeout=2.0)
            except Exception as exc:  # noqa: BLE001 — shutdown must not hang or raise on a slow/dead loop
                _log.warning("http client close failed: %s", exc)
        if self._fmp is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._fmp.close(), self._loop).result(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                _log.warning("fmp client close failed: %s", exc)

    # --- universe load (bars flow to on_bar / on_historical_data, then to Redis) ------------------
    def _window(self, instrument_id: InstrumentId, granularity: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        days = self._cfg.chart_lookback_days.get(granularity, self._cfg.backfill_days)
        if self._cfg.data_provider == "databento":
            # Databento historical-only plans lag the live edge → back off to each feed's availability end.
            schema = GRANULARITIES[granularity][0]
            dataset = dataset_for_venue(instrument_id.venue.value)
            end = request_end(self._api_key, dataset, schema)
        else:
            # IBKR (and any real-time provider): the window ends now; live subscriptions carry it forward.
            end = pd.Timestamp.utcnow()
        return end - pd.Timedelta(days=days), end

    def _load(self, instrument_id: InstrumentId) -> None:
        # Definition once per instrument, then bars per granularity in the callback.
        _start, end = self._window(instrument_id, DEFAULT_GRANULARITY)
        self.request_instrument(
            instrument_id,
            start=end - DEFN_LOOKBACK,
            end=end,
            client_id=self._data_client_id,
            callback=lambda _r, iid=instrument_id: self._after_definition(iid),
        )

    def _schedule_bar_drain(self) -> None:
        """Register the paced drain on the NAUTILUS clock (#617).

        Its own method so it can be DRIVEN in a test. Asserting that `set_timer` appears somewhere in
        `on_start` did not bite when the registration was disabled — the text survives inside a dead
        branch, and an unscheduled drain means the queue fills and never empties: no bars at all,
        which is worse than the burst it replaces and looks deliberate.

        Only when the provider declared a rate. An unpaced provider passes straight through, so a
        timer firing on a permanently empty queue every 10 seconds is noise on the tenant that is
        actually trading.
        """
        if self._hist_rate == float("inf"):
            return
        self.clock.set_timer(
            name=_BAR_DRAIN_TIMER,
            interval=pd.Timedelta(seconds=_BAR_DRAIN_SECS),
            callback=self._drain_bar_requests,
        )

    def _request_bars_paced(self, bar_type, **kw) -> None:
        """Enqueue a historical bar request, to leave at the rate the PROVIDER declared (#617).

        Every historical bar request in this class goes through here. Three call sites issue them —
        the initial load, the refetch heal loop, and the compass — and ONE unpaced site reproduces
        the whole burst, which is why a tripwire pins that `self.request_bars(` appears nowhere else.

        A provider declaring no rate passes straight through, so Alpaca is byte-for-byte unchanged.

        DE-DUPLICATED ON THE WAY IN. A bar type already waiting must not be queued twice: the refetch
        loop reissues on a timer, so without this the queue grows faster than it drains and pacing
        becomes a slower version of the same starvation.
        """
        held = bool(kw.pop("held", False))
        if self._hist_rate == float("inf"):
            self.request_bars(bar_type, **kw)
            return
        self._enqueue_paced(_HELD_HISTORY_PRIORITY if held else _DISPLAY_HISTORY_PRIORITY,
                            self.request_bars, (bar_type,), kw,
                            key=("request", str(self.id), str(bar_type)))

    def _enqueue_paced(self, priority: int, fn, args: tuple, kw: dict, *, key) -> None:
        """Queue one outbound venue call to leave at the declared rate, ordered by `priority` (#836).

        `fn` is the BOUND method that must issue the call — the feed's own, or a LANE's — because
        Nautilus routes a request's response to the actor that issued it. The feed issuing a lane's
        request would deliver the bars to the feed and leave the lane in warmup forever.

        De-duplicated on `key`, never on the bar type alone: two lanes and the feed all want the same
        daily series, and each needs its own delivery — and each is its own venue call.
        """
        import heapq

        if key in self._bar_request_seen:
            return
        self._bar_request_seen.add(key)
        self._bar_request_seq += 1
        heapq.heappush(self._bar_request_q, (priority, self._bar_request_seq, fn, args, kw, key))

    def _subscribe_bars_paced(self, bt, *, held: bool) -> None:
        """A live bar subscription, through the queue on a paced provider — ahead of all history.

        ON IB THIS IS A HISTORICAL REQUEST. The shipped adapter's `subscribe_historical_bars` issues
        `reqHistoricalData(..., keepUpToDate=True)` for every bar size but 5s, and it counts against
        the same ~60-per-10-minutes allowance as the backfill. 297 of them in one second on
        2026-09-09 was most of the boot burst. The old comment here said the opposite and a test
        pinned it; the measurement is in `test_boot_does_not_burst_the_venue.py`.

        The ledger is stamped WHEN THE CALL LEAVES, not when it is queued: `state_of` turns a
        subscription SILENT 30 trading minutes after `requested_ns`, and stamping at enqueue would
        have the pacer manufacture the alarm it exists to prevent.

        A provider declaring no rate (Alpaca — real WebSocket streams that cost nothing) subscribes
        synchronously, byte-for-byte as before.

        Behind a HELD position's history (priority 1): that history is the ATR its protective stop
        is sized from, and at 6/min the 297 display subscriptions of a boot would otherwise hold it
        back ~50 minutes — seven positions sat `no_atr` for a session on 2026-09-09.
        """
        def _issue(bt=bt):
            self.subscribe_bars(bt)
            self._subscriptions.requested("bars", str(bt), self.clock.timestamp_ns())

        if self._hist_rate == float("inf"):
            _issue()
            return
        self._enqueue_paced(_HELD_SUBSCRIBE_PRIORITY if held else _DISPLAY_PRIORITY, _issue, (), {},
                            key=("subscribe", str(self.id), str(bt)))

    def _drain_bar_requests(self, _event=None) -> None:
        """Release one interval's worth of queued requests. Driven by the NAUTILUS CLOCK.

        Not a sleep and not a thread: #598 was a synchronous fetch on the Nautilus thread that parked
        `node.run()` — py-spy showed MainThread in `ssl.read`, the log frozen, nothing published on
        BOTH tenants. `clock.set_timer` is native, already used here, and works in backtest, which a
        wall-clock sleep would not.
        """
        hold = getattr(self, "_display_hold_secs", 0.0)
        if getattr(self, "_feed_started_ns", None) is None:
            self._feed_started_ns = self.clock.timestamp_ns()
        holding = hold > 0 and (self.clock.timestamp_ns() - self._feed_started_ns) < hold * 1e9
        import heapq

        allowance = max(1, int(self._hist_rate * _BAR_DRAIN_SECS / 60.0))
        for _ in range(allowance):
            if not self._bar_request_q:
                return
            if holding and self._bar_request_q[0][0] >= _DISPLAY_PRIORITY:
                # THE HOLD PARKS DISPLAY ONLY. It was written for a display-only queue (#618 step
                # 2); since #836 the same heap carries a held position's live plane, its ATR history
                # and the lanes' warmups, and on 2026-09-09 at 14:47Z a 900 s hold parked ALL of it —
                # 348 instruments loaded, 0 requests out, 15 positions no_price. The heap is
                # priority-ordered, so everything ahead of the first display entry has already left.
                # Held, not dropped: the rest keeps its order and drains the moment the hold lifts.
                return
            _prio, _seq, fn, args, kw, key = heapq.heappop(self._bar_request_q)
            self._bar_request_seen.discard(key)
            # A raise here must neither stop the drain nor vanish into a log line: recorded on the
            # registry /health reads (CLAUDE.md 2026-08-31 — observation is a mechanism, not a
            # try/except). The failed call is NOT re-queued; the refetch loop reissues on its own.
            self._observations.run("paced_venue_call", fn, *args,
                                   ts_ns=self.clock.timestamp_ns(), **kw)

    def _realtime_budget(self) -> float:
        """How many symbols may hold live subscriptions here — asked of the PROVIDER (#619).

        This used to be `realtime_symbol_budget(self._alpaca_feed)`, an Alpaca free-plan constant
        evaluated on every provider. staging-ibkr therefore rationed itself to 7 live symbols and
        logged `feed=iex` on a node that holds no Alpaca credential, denying 87 symbols live data for
        a reason with nothing to do with IBKR.

        The number now arrives on `DataClientSpec`, so a provider that declares no cap is not capped
        and there is no Alpaca constant another provider can reach.
        """
        return self._realtime_symbol_budget

    def _after_definition(self, instrument_id: InstrumentId) -> None:
        # Real-time last-price + NBBO planes are budget-gated by the feed's plan limit (Alpaca's free/IEX
        # plan 405s the whole subscribe once trades+quotes combined cross 30 channel-slots; SIP/Algo Trader
        # Plus is unlimited → `inf`). A HELD POSITION always gets live data regardless of budget — real
        # capital, not "nice to have"; `on_start` already loads positions before the runtime watchlist, so
        # on a capped feed positions claim their slots first in the common case too.
        budget = self._realtime_budget()
        key = str(instrument_id)
        is_position = any(str(p.instrument_id) == key for p in self.cache.positions_open())
        allow_realtime = is_position or len(self._realtime_subscribed) < budget
        if allow_realtime:
            self._realtime_subscribed.add(key)
            # A VENUE THAT DECLARES IT SERVES NO TAPE IS NOT ASKED FOR ONE (#812).
            #
            # These two calls used to be unconditional, on the belief written here that "providers
            # without a live trade stream simply emit nothing (the client no-ops)". THE IBKR ADAPTER
            # DOES NOT NO-OP — it issues `reqTickByTickData` and IB rejects it. staging2's boot of
            # 2026-09-09: 112 instruments x 2 planes = 224 requests, 135 x 10189 (no market data
            # permissions), 199 x 10190 (max tick-by-tick requests reached), 0 ticks delivered. IB's
            # tick-by-tick concurrency limit is small and SHARED, so those requests consume it and a
            # symbol that could have been served is denied a slot.
            #
            # THE LEDGER ENTRY IS GATED TOO, and that is not tidiness. `requested: 224, bound: 0`
            # reads as a venue failing to serve us; we never asked for something askable. `state_of`
            # already returns None for "never asked" — three states, and this is the third.
            #
            # `is not False` rather than truthiness: `None` means a double built outside `build_node`
            # never declared, and an undeclared provider keeps the old behaviour rather than silently
            # losing its live planes.
            #
            # Real-time last-price plane: trade ticks (every execution, sub-second). This is what the
            # cockpit shows as the live price — bars are too coarse to trade against. Charts still
            # come from the bars below either way, which is why refusing here does not blank them.
            if self._streams_trade_ticks is not False:
                self.subscribe_trade_ticks(instrument_id, client_id=self._data_client_id)
                self._subscriptions.requested("trades", key, self.clock.timestamp_ns())
            # NBBO quote plane (#40): bid/ask for the order ticket's spread/mid/marketable-limit prefill and
            # the auto-select rules. Gated on its OWN declaration — trades and quotes are two facts about a
            # venue, and one flag standing for both cannot be right about a venue that serves only one.
            if self._streams_quote_ticks is not False:
                self.subscribe_quote_ticks(instrument_id, client_id=self._data_client_id)
                # SEPARATE FROM TRADES ON THE SAME SYMBOL. `10089` on quotes while bars flow is exactly
                # the half-served state; keying on the symbol alone would let one vouch for the other.
                self._subscriptions.requested("quotes", key, self.clock.timestamp_ns())
        elif key not in self._realtime_budget_warned:
            self._realtime_budget_warned.add(key)
            # No promise of a slot "freeing up": `_realtime_subscribed` is add-only — nothing anywhere
            # releases a slot, so a denied symbol stays bars/history-only for the LIFE of the session
            # (#652 item 4). If slot release is ever implemented, update this message with it
            # (test_realtime_budget_warning_is_honest.py pins the pairing).
            _log.warning(
                "realtime symbol budget (%s, provider=%s) full — %s gets bars/history only for this "
                "session (slots are never released; held positions always get one). Upgrade the "
                "provider's plan to lift the cap",
                budget,
                self._cfg.data_provider,
                key,
            )
        for granularity in self._granularities:
            bt = bar_type(instrument_id, granularity)
            self._bar_types.append(bt)
            start, end = self._window(instrument_id, granularity)
            # Historical bars load regardless of budget (a static chart still renders). The LIVE follow-on
            # subscribe is gated for 1m/1d — those are Alpaca's two NATIVE `bars`/`dailyBars` WS channels and
            # each consumes its own channel-slot toward the same free-plan cap as trades/quotes (code review:
            # the trades/quotes-only gate above still 405'd — 1m/1d bars share the same 30-slot budget).
            # 5m/15m/30m/1h/1w are INTERNALLY aggregated by Nautilus's DataEngine from trade ticks
            # FOR THE LIVE STREAM ONLY. This comment used to claim they "never reach the Alpaca client
            # at all", which is wrong and cost a debugging session: DataEngine._handle_request_bars
            # goes straight to _handle_date_range_request(client, request) with NO aggregation-source
            # check — the INTERNAL/EXTERNAL split is tested on SUBSCRIBE (engine.pyx:1234, 1827), never
            # on request. Alpaca's _request_bars then maps step+unit via _alpaca_timeframe, ignoring the
            # source, and REST serves 5Min/15Min/30Min/1Hour/1Week natively. So history is real for all
            # of them, exactly as bar_spec.py says.
            #
            # Gating the subscribe here would still be a no-op: an over-budget symbol has no live trade
            # ticks to aggregate from, so it gets no internal bars either way.
            self._request_bars_paced(
                bt,
                start=start,
                end=end,
                client_id=self._data_client_id,
                callback=lambda _r, b=bt: None,   # history only — the LIVE subscribe happens below
                held=is_position,                 # a held position's ATR ranks ahead of chart depth
            )
            # THE LIVE SUBSCRIBE IS NOT CHAINED BEHIND THE HISTORY (#617 follow-up) — it is QUEUED
            # AHEAD OF IT (#836).
            #
            # It used to hang off `request_bars`'s callback, which put every live subscription
            # behind the whole backfill: staging measured 0 live bars for 25 minutes after boot,
            # roughly five hours before a lane could see a price. The follow-up made it synchronous,
            # on the belief — written here — that a subscription "does not consume IB's historical
            # allowance". IT DOES: on IB `subscribe_bars` is `reqHistoricalData(keepUpToDate=True)`,
            # and 297 of them in one second on 2026-09-09 was most of the burst that left IB silent
            # for the session. So it goes through the same queue as everything else, ahead of every
            # history request but a HELD position's — which keeps the follow-up's intent without
            # the exemption, and puts protection before display.
            if granularity in self._aggregation.refused:
                # DEGRADE LOUDLY. The subscription would be accepted and then sit silent, which is
                # indistinguishable from a quiet market — so it is not made, and the refusal is
                # recorded with its reason instead. History above is untouched: it is a REST request
                # that the INTERNAL/EXTERNAL split never reaches (the source is checked on subscribe,
                # engine.pyx:1234/1827, never on request), so refusing the live stream does not take
                # a working chart dark, it only stops claiming a live one exists.
                self._failed_requests.record(
                    "aggregation",
                    granularity,
                    self._aggregation.reason_for(granularity),
                )
                continue
            if allow_realtime or granularity not in ("1m", "1d"):
                self._subscribe_bars_paced(bt, held=is_position)

    # -- IB shortability (#857) ------------------------------------------------------------------
    def attach_shortable(self, plane) -> None:
        """Wire the venue's shortability plane (declared by the connector on its data spec, #857).
        On a venue with none nothing is attached and `shortable_provider` stays None, which the
        CRSISHORT builder reads as "no data" (three states, never a silent 0.0)."""
        self._shortable_plane = plane
        self.shortable_provider = plane.borrow_rates(now_ns=lambda: self.clock.timestamp_ns())

    def _short_lanes(self) -> list:
        """Registered sibling strategies that hold the SHORT side — by their own declaration
        (`POSITION_SIDE`, kumo-strategies sides.py: "short"), never by name."""
        return [s for s in self._sibling_strategies.values()
                if str(getattr(s, "POSITION_SIDE", "")).lower() == "short"]

    def _shortable_catchup(self) -> int:
        """Subscribe tick 236 for every instrument a SHORT lane has resolved and we have not asked
        for yet. Returns how many were scheduled this pass. RAISES when a short lane exists and no
        registry/client is attached — that is the armed-and-inert shape, and `_observations`
        records it rather than letting it pass as a quiet zero."""
        lanes = self._short_lanes()
        if not lanes:
            return 0
        if self._shortable_plane is None:
            raise RuntimeError(
                f"{[str(s.id) for s in lanes]} hold the SHORT side but no shortable plane is "
                f"attached — the borrow gate is armed and inert (#857)")
        # Settle last pass's futures first: a subscription that raised is retried, and counted.
        for iid, fut in list(self._shortable_pending.items()):
            if not fut.done():
                continue
            del self._shortable_pending[iid]
            exc = fut.exception()
            if exc is not None:
                self._shortable_subscribed.discard(iid)
                self._shortable_subscribe_failures += 1
                _log.error("shortable subscription for %s failed (%r) — will retry next pass", iid, exc)
        scheduled = 0
        for lane in lanes:
            for iid in list(getattr(lane, "_iids", None) or ()):
                if iid in self._shortable_subscribed:
                    continue
                instrument = self.cache.instrument(iid)
                if instrument is None:
                    continue                     # not defined yet; the next pass asks again
                self._shortable_subscribed.add(iid)
                self._shortable_pending[iid] = self._schedule_shortable(
                    self._shortable_plane.subscribe(instrument))
                scheduled += 1
        return scheduled

    def _schedule_shortable(self, coro):
        """Timer callbacks arrive off the loop; the subscription is a coroutine on it. Returns the
        future so the next pass can read its outcome — dropping it is how a failure disappears."""
        loop = self._loop or getattr(getattr(self._shortable_plane, "client", None), "_loop", None)
        if loop is None:
            coro.close()
            raise RuntimeError("no event loop to schedule the shortable subscription on")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _on_refetch(self, _event) -> None:
        """Heal bar types whose series is EMPTY *or* STALE/incomplete — re-request them (in-flight guard so
        slow requests never overlap). The old guard skipped any non-empty series, so a bad/partial first load
        (e.g. old bars + a lone carried live bar = a hole) was frozen forever while the live price drifted —
        producing a nonsense chg% (the PENG +313% bug). A per-bar-type cooldown bounds re-fetching so a
        genuinely thin (newly-listed) series isn't re-requested every tick."""
        now = self.clock.timestamp_ns()
        # Shortability subscriptions for the SHORT lanes' instruments (#857) ride this timer because
        # a lane resolves its symbols in ITS OWN on_start, after this strategy's — so the catch-up
        # is periodic, idempotent, and observed (a failure here must not stop the bar heal below).
        self._observations.run("shortable", self._shortable_catchup, ts_ns=now)
        for bt in self._bar_types:
            key = str(bt)
            started = self._inflight.get(key)
            if started is not None and now - started < _INFLIGHT_TIMEOUT_NS:
                continue                      # genuinely in flight
            if started is not None:
                # Timed out: the callback never came, so the request failed silently. Drop the guard
                # and let the cooldown below decide when to try again.
                _log.warning("bar refetch for %s never completed — releasing the in-flight guard", key)
                self._inflight.pop(key, None)
            if not self._series_stale(bt):
                continue
            if now - self._healed.get(key, 0) < _HEAL_COOLDOWN_NS:
                continue  # recently attempted — don't spam a series the feed genuinely can't complete
            try:
                start, end = self._window(bt.instrument_id, granularity_of(key))
            except (ValueError, KeyError) as exc:
                _log.warning("refetch window failed for %s: %s", bt, exc)
                continue
            self._inflight[key] = now
            self._healed[key] = now
            _log.info("bar heal — re-requesting stale/empty series %s", key)
            self._request_bars_paced(
                bt, start=start, end=end, client_id=self._data_client_id,
                callback=lambda _r, k=key: self._inflight.pop(k, None),
            )

    def _series_stale(self, bt) -> bool:
        """A bar series needs healing when it's EMPTY, too THIN for the indicator windows, or HOLED (an old
        series capped by a lone carried live bar — the observable PENG failure)."""
        return _series_stale_bars(self.cache.bars(bt), granularity_of(str(bt)))

    # --- symbol reconcile: stream watchlist (Postgres) + on-demand views (Redis) ------------------
    def _on_watchlist(self, _event) -> None:
        """Timer → reconcile the persistent watchlist (Postgres) onto the node's loop. On-demand views are
        no longer polled here — they arrive as `stream_request` commands (#32) handled by the command
        consumer directly. Also the cadence for the Today's Range/Prior Close batch refresh (#182
        follow-up, KPI Phase 3) — that data doesn't need sub-minute freshness, so it piggybacks here
        rather than getting its own timer."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._reconcile_symbols(), self._loop)
            asyncio.run_coroutine_threadsafe(self._refresh_today_ranges(self.clock.timestamp_ns()), self._loop)

    def _on_session_state(self, _event) -> None:
        """Timer → publish the strategy's own state (#212). Scheduled onto the loop, never awaited
        here, so a slow database can never delay the market-data path."""
        if self._loop is None or self._session_state_inflight:
            return
        # Skip while a previous build runs. Without this a stalled database lets one task accumulate
        # per tick, each holding a connection from a pool of 5 (+10 overflow) shared with everything
        # else this process does — the failure would surface as the ENGINE degrading, over a panel
        # nobody was looking at.
        self._session_state_inflight = True
        asyncio.run_coroutine_threadsafe(self._publish_session_state(), self._loop)

    async def _publish_session_state(self) -> None:
        """Publish the `session` frame — the strategy's lifecycle, decision, journal and exit trail.

        Never raises out. A database hiccup must cost one frame, not the market-data bridge: the UI
        keeps the last frame (the key carries a TTL) and the next tick recovers. Exceptions are logged
        at warning rather than swallowed silently, because a frame that stops arriving with no trace is
        how a dead panel gets mistaken for a quiet strategy.
        """
        try:
            from api.db.engine import session_factory
            from api.session_state import build

            # Bounded: a query that never returns must not hold a pooled connection forever.
            payload = await asyncio.wait_for(
                build(session_factory, self._strategy_id_for_state), timeout=_SESSION_STATE_TIMEOUT)
        except Exception as exc:                                    # noqa: BLE001
            _log.warning("session state unavailable (%r) — no frame this tick", exc)
            return
        finally:
            self._session_state_inflight = False
        payload["ts"] = self.clock.timestamp_ns()
        self._publish("session", payload)

    async def _refresh_today_ranges(self, now_ns: int) -> None:
        """One Alpaca snapshot call for every loaded symbol → today's high/low + prior close (#182
        follow-up, KPI Phase 3). Unlike VWAP this needs no LOCAL accumulation to reset, but it still needs
        a staleness check: Alpaca's `dailyBar` can legitimately lag "today" (a thin symbol before its
        first tick, a holiday, an outage) and — without checking its own date — that would let a value
        from a PRIOR day keep rendering as current indefinitely. So each fetch validates `dailyBar.t`'s own
        ET date against today's before treating it as fresh, and UNCONDITIONALLY tombstones (publishes
        `high: null`) any symbol that isn't fresh this batch — not gated on "did this process remember
        publishing a value before" (codex review round 3: that gate isn't restart-safe — an engine restart
        wipes any such in-memory bookkeeping, but the consumer/Redis can still be replaying an old frame
        from before the restart; only an unconditional tombstone reaches that case too). A whole-request
        failure tombstones every currently-loaded symbol the same way (codex review round 4: silently
        preserving old state on a failed fetch reopens the identical stale-value hole — better to show
        "no data" than possibly-days-old data) and heals on the next successful tick.

        Tickers are looked up via `InstrumentId.from_str` (not a config-wide default venue) — Alpaca is
        multi-venue (NASDAQ→XNAS, NYSE→XNYS, `providers.py`'s `EXCHANGE_TO_MIC`), so reconstructing
        `f"{ticker}.{cfg.venue}"` would mislabel any symbol not on that one default venue, and naive
        dot-splitting would mangle a dotted ticker like "BRK.B" — `InstrumentId.from_str`'s `.symbol.value`
        handles both correctly (it splits on the LAST dot).
        """
        if self._http is None or not self._loaded:
            return
        if self._today_range_inflight:
            return  # previous batch still in flight — this tick's data will arrive slightly late, not lost
        self._today_range_inflight = True
        try:
            ticker_to_iid: dict[str, str] = {}
            for iid_str in self._loaded:
                try:
                    iid = InstrumentId.from_str(iid_str)
                except ValueError:
                    continue
                # Alpaca's snapshot request is ticker-keyed (no MIC) — a same-ticker cross-venue
                # collision is last-write-wins, an inherent limit of the Alpaca API itself (its own
                # response can't distinguish the two venues either), not something fixable here.
                ticker_to_iid[iid.symbol.value] = iid_str
            if not ticker_to_iid:
                return
            try:
                snapshots = await self._http.get_stock_snapshots(list(ticker_to_iid), feed=self._alpaca_feed)
            except Exception as exc:  # noqa: BLE001 — one bad batch must not break the timer
                _log.warning("today-range snapshot fetch failed: %s", exc)
                snapshots = {}  # fall through to the tombstone path below for every loaded symbol —
                # NOT a silent return: preserving old state on a failed fetch is the same stale-value
                # hole as a per-symbol miss, just for the whole batch at once (codex review round 4).
            today_date = _et_date(now_ns)
            for ticker, iid_str in ticker_to_iid.items():
                snap = snapshots.get(ticker) or {}
                daily = snap.get("dailyBar")
                prev = snap.get("prevDailyBar")
                daily_date = _et_date_from_iso(daily["t"]) if daily and daily.get("t") else None
                if not daily or not prev or daily_date != today_date:
                    # No data, or Alpaca's dailyBar is still stale from a prior day — ALWAYS tombstone,
                    # unconditionally (codex review round 3: gating this on "did THIS process previously
                    # publish a value" isn't restart-safe — an engine restart wipes any in-memory record
                    # of what it published before, but the consumer/Redis can still be replaying an old
                    # `today_range` frame from before the restart; only an unconditional tombstone reaches
                    # that case). Never an error for the batch either way.
                    self._publish(
                        "today_range",
                        {"instrument_id": iid_str, "high": None, "low": None, "prev_close": None, "ts_event": now_ns},
                    )
                    continue
                self._publish(
                    "today_range",
                    {
                        "instrument_id": iid_str,
                        "high": float(daily["h"]),
                        "low": float(daily["l"]),
                        "prev_close": float(prev["c"]),
                        "ts_event": now_ns,
                    },
                )
        finally:
            self._today_range_inflight = False

    def _on_fundamentals(self, _event) -> None:
        """Timer → refresh fundamentals on the node loop (#182 follow-up, KPI Phase 2). Separate from
        `_on_watchlist`'s cadence — fundamentals don't need the 5s watchlist tick."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_fundamentals(self.clock.timestamp_ns()), self._loop)

    def _on_protection_tick(self, _event) -> None:
        """Timer callback. Nautilus timer callbacks do not run on the node's event loop, so the async work
        is handed to it — same shape as every other async timer here."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._reconcile_protection(), self._loop)

    def _resolve_instrument_id(self, symbol: str) -> str | None:
        """Broker ticker -> our instrument id, using ONLY instruments the engine actually has loaded.

        Returns None rather than guessing a venue. A fabricated id would resolve to an instrument we cannot
        price or size, and the caller reports the miss instead of dropping the position.

        TWO LISTINGS ARE THE NORMAL CASE ON IB (#862, #625): SMART contract details return every venue,
        so the cache holds `CGAU.XNAS` AND `CGAU.XNYS`. The first version returned whichever id the cache
        iterated first, and on staging2 that keyed the venue's CGAU stop `(CGAU.XNAS, SELL)` while the
        position asked for `(CGAU.XNYS, SELL)` — `broker_protected: False` on two covered names, and the
        same hole in the drift and stop-price planes, which share this resolver. Order of preference:
          1. the listing that IS its own primary exchange (`prefer_primary_exchange`, the lanes' rule);
          2. the listing an OPEN POSITION is on — the venue is describing an order on the held instrument;
          3. the sorted first, deterministically, and never iteration order.
        """
        candidates = [iid for iid in self.cache.instrument_ids()
                      if str(iid).rpartition(".")[0] == symbol]
        if not candidates:
            return None
        if len(candidates) == 1:
            return str(candidates[0])
        from api.venue_preference import prefer_primary_exchange

        primary = prefer_primary_exchange(self.cache, symbol, candidates)
        if primary is not None:
            return str(primary)
        # No fallback here: a cache that cannot list positions is a broken cache, and resolving to the
        # sorted-first listing in that state would be the #862 hole again, this time silent.
        held = {str(p.instrument_id) for p in self.cache.positions_open()}
        for iid in sorted(candidates, key=str):
            if str(iid) in held:
                return str(iid)
        return str(sorted(candidates, key=str)[0])

    def _peak_scaled_widths(self, instrument_id: str, params: dict):
        """PEAK's trail widths for this symbol, from its own ATR (#288). None when it cannot be measured.

        Reads the multiples from the `peak` settings domain so an operator still controls how many ATRs
        wide the stop sits — what changes is that the number means "1.5 x this symbol's range" rather than
        "2.5% of every symbol".
        """
        from api.protection import peak_trail_bps
        from api.settings import resolve

        try:
            cfg = resolve("peak")
        except Exception:
            return None
        try:
            atr_value = self._protection_atr(instrument_id, int(cfg.get("atrLookback", 14)))
            price = self._last_price_for(instrument_id)
        except Exception:
            # Scaling is an IMPROVEMENT on the caller's width, never a precondition for arming. A missing
            # cache, an unloaded instrument or a bad bar must leave PEAK working exactly as before.
            return None
        if atr_value is None or price is None:
            return None
        return peak_trail_bps(
            price=float(price),
            atr_value=atr_value,
            wide_multiple=float(cfg.get("trailWideAtrMultiple", 1.5)),
            tight_multiple=float(cfg.get("trailTightAtrMultiple", 0.75)),
        )

    def _protection_atr(self, instrument_id: str, lookback: int) -> float | None:
        """Daily ATR for one symbol, from completed sessions only.

        `_daily_bars` already excludes today's still-forming bar, and `atr()` needs `lookback + 1` bars
        because the first true range has no previous close to gap from — so ask for two spare.
        """
        bars = _daily_bars(self, instrument_id, lookback + 2)
        rows = [{"h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in bars]
        return _atr(rows, lookback=lookback)

    async def _reconcile_protection(self) -> None:
        """Rest a trailing stop at the broker for every position without protective coverage (#239).

        Three gates before anything is placed, in order:

        1. **Settings `enabled`, default False.** This places orders; new automation is opt-in.
        2. **Regular trading hours.** An Alpaca trailing stop does NOT trigger outside RTH, so resting one
           after the close creates an order that protects nothing while reporting that it does.
        3. **A broker connection.** No HTTP client means no broker truth, and cache truth is not a
           substitute (#285).

        State comes from the BROKER, never the cache. On 2026-08-14 the engine held three orders as
        REJECTED that Alpaca reported `new`; a cache-based coverage check answers "nothing resting" and the
        next tick would rest a DUPLICATE stop. Alpaca does not auto-reconcile two overlapping sells — that
        is the oversell of #245.
        """
        from api.protection import broker_rows, plan_protection
        from api.settings import declared, resolve

        if self._protection_running:
            return
        self._protection_running = True
        try:
            await self._reconcile_protection_inner(broker_rows, plan_protection, resolve, declared)
        finally:
            self._protection_running = False

    def _broker_state_unknown(self) -> None:
        """Return every broker-read cache to UNKNOWN (#649). A cache with no path back to unknown
        renders the last snapshot as CURRENT: after one good tick, a permanent broker outage left
        SECURED badges and stop prices painting a dead read as live truth. Downstream already speaks
        three states (`broker_protected=None` renders as unknown, never NAKED) — the caches just
        never said it again after their first success."""
        self._broker_stop_prices = {}
        self._broker_avg_entry = None
        self._broker_protected = None

    async def _reconcile_protection_inner(self, broker_rows, plan_protection, resolve, declared) -> None:
        # BEFORE the settings gate, deliberately: the sweep REPORTS, it never places an order, and a
        # release window that closed with no exit is exactly as true when protection is switched off
        # (#546 part 2). Gating the report behind `enabled` is the #298 shape — a subsystem that is
        # off reading identically to one with nothing to say.
        self._sweep_exit_windows()
        # THE PREVIOUS PASS'S FLIP RECORD, taken and cleared before any gate (#907): a pass that does
        # not reach the flip block — outside RTH, settings unreadable, disabled, no broker — must leave
        # NO count behind, and the block below rebuilds consecutiveness from `pending_before`.
        pending_before, self._flip_pending = self._flip_pending, {}
        self._flip_evaluated = False
        try:
            cfg = resolve("protection")
            # The RAW file beside the resolved one (#1029): which `<lane>_protection` keys the operator
            # WROTE. `resolve` fills every schema default, so `cfg` alone cannot tell a written `none`
            # from a defaulted one, and `lane_modes` needs to (#965). Same try: an unreadable file is
            # one condition, not two.
            declared_cfg = declared("protection")
        except Exception as exc:
            self.log.exception("protection: settings unreadable — not placing anything", exc)
            return
        now_ns = self.clock.timestamp_ns()
        # DISABLED still means inert — no broker call at all. Opt-in is opt-in, and a test pinned it.
        if not cfg.get("enabled", False):
            return

        try:
            # THE POSITION READ, TYPED FIRST (#641), same shape as the order read below. This used to
            # be `if self._http is None: return` followed by `self._http.list_positions()` — which made
            # the ENTIRE reconciler dead on staging-ibkr (no AlpacaHttpClient exists there) while every
            # surface said RUNNING, however well the typed order path underneath it worked. The typed
            # read resolves on the registered execution client; the shipped IB adapter implements it
            # (execution.py:830). Proven row-identical to the REST parse in
            # `test_the_typed_and_rest_position_sources_produce_the_SAME_rows`.
            #
            # The REST read remains as the fallback for a node with no execution client — the synthetic
            # engine has none — and says so rather than degrading quietly.
            position_reports = await self._venue_position_reports()
            # THE TRUTH SURFACE (#758), taken from the read that already happened. `None` here is
            # carried through as None, never coerced to an empty list: "the venue could not be read"
            # and "the venue holds nothing" are different answers, and collapsing them is the exact
            # mistake that produced four wrong claims on 2026-08-31.
            self._record_book_truth(position_reports)
            positions = None
            if position_reports is None:
                if self._http is None:
                    # Enabled and completely blind. Placing nothing is right; placing nothing SILENTLY
                    # is #298 — a subsystem that is off reads exactly like one with nothing to do.
                    self.log.warning(
                        "protection: no execution client and no broker REST client — broker state is "
                        "UNREADABLE, so nothing is placed and nothing is reported protected"
                    )
                    self._broker_state_unknown()
                    return
                self.log.warning(
                    "protection: no execution client — falling back to the broker REST position read. "
                    "This path is Alpaca-specific and will not work for another venue."
                )
                positions = await self._http.list_positions()
            # "open" OMITS `held`, AND A HELD STOP IS A STOP (#387).
            #
            # APA's bracket stop was accepted at the venue and sat there as `status=held`. This read did
            # not return it, so the check concluded there was no protection and logged
            # "left unprotected, $10,320 exposed" every 60 seconds about a position that had a stop.
            #
            # Measured on the real account (`scripts/probe_held_orders.py`): 269 orders, 9 returned by
            # the open filter, and TWO protective stops invisible to it — CRAK 180 @ 57.06 and
            # APA 231 @ 43.99.
            #
            # This is #285 in reverse. That issue was the tile reporting 8 of 8 unprotected while 8 GTC
            # stops rested, and its fix taught this check to trust the broker over the cache. It simply
            # never asked for the states the broker uses.
            #
            # `all` rather than a wider enum: Alpaca's filter takes open/closed/all, and closed orders
            # are harmless here because the classifier below already filters on type and side, and a
            # filled or cancelled stop is not a resting one. Fetching them costs one larger response.
            #
            # WHAT THIS DOES NOT SETTLE: whether a held stop FIRES. That is an Alpaca semantic the probe
            # explicitly leaves open. This fix only lets the check tell "no stop" from "a stop the broker
            # is holding" — today it cannot, and it reports the second as the first.
            # TYPED FIRST. `generate_order_status_reports` is Nautilus's own venue read and the shipped
            # Interactive Brokers adapter implements it exactly as our Alpaca client does, so this is the
            # line that makes the reconciler broker-agnostic. Proven to produce an IDENTICAL
            # `ProtectionPlan` to the raw read in `test_protection_typed_source.py`.
            #
            # The REST read remains as the fallback for a node with no execution client — the synthetic
            # engine has none — and says so rather than degrading quietly. It is the same payload either
            # way; only the parse differs.
            reports = await self._venue_order_reports()
            if reports is not None:
                from api.protection import orders_from_reports

                orders = orders_from_reports(reports)
            else:
                if self._http is None:
                    # Positions were readable but orders were not (the typed order read failed), and
                    # there is no REST fallback. Half a broker read is worse than none: a plan built on
                    # positions with an empty order book would arm DUPLICATE stops on covered legs.
                    self.log.warning(
                        "protection: venue orders unreadable and no broker REST fallback exists — "
                        "not placing anything"
                    )
                    self._broker_state_unknown()
                    return
                self.log.warning(
                    "protection: no execution client — falling back to the broker REST read. This path "
                    "is Alpaca-specific and will not work for another venue."
                )
                orders = await self._http.list_orders(status="all", paginate=True)
        except Exception as exc:
            self.log.exception("protection: could not read broker state — not placing anything", exc)
            self._broker_state_unknown()
            return

        # A trailing stop's ACTUAL stop price exists only at the venue: we submit an offset, Alpaca
        # computes the trigger and ratchets it as its high-water mark rises, and none of that flows back
        # into the order we hold. So SECURED read $0.00 with eleven stops resting (#289). Cached here
        # because this is the only place that reads broker orders.
        #
        # Deliberately ABOVE the enabled/RTH gates: reading broker state is not acting on it. Gating the
        # READ meant the display went dark outside regular hours and whenever the feature was switched
        # off, which is the "silence looks like nothing is wrong" failure #298 is about.
        # FILTERED ON STATUS (#387 review, both reviewers independently). Widening the fetch to `all`
        # let every terminal stop's trigger into this map. Stale keys are mostly inert — lookups are by
        # client order id — but NOT on collision: an Alpaca replace leaves the original as `replaced` and
        # creates a new order, and `exec_client.py:817` documents that the client order id is what
        # survives that handoff. Two rows, one key, dict comprehension, newest-first response: the older
        # `replaced` row wins and SECURED displays the PRE-shrink trigger. That is every stop the
        # oversize path has ever modified.
        self._broker_stop_prices = {
            str(o.get("client_order_id") or ""): float(o["stop_price"])
            for o in orders
            if o.get("stop_price") not in (None, "")
            and o.get("client_order_id")
            and _is_resting(o.get("status"))
        }

        # WHICH INSTRUMENTS THE BROKER IS ACTUALLY PROTECTING (#285). The cache cannot answer it: the
        # engine holds orders as REJECTED that Alpaca reports OPEN — a submit whose HTTP call failed
        # after the venue accepted it — and Nautilus refuses REJECTED -> ACCEPTED as an invalid
        # transition, so reconciliation can never repair them. They drop out of `orders_open()` and the
        # book reported 8 of 8 positions unprotected while 8 GTC stops rested at the broker.
        #
        # A false "unprotected" is worse than a missing feature: it is a safety claim that is wrong, and
        # an alarm that cries wolf is one an operator learns to scroll past.
        # THE BROKER'S COST BASIS (#370). Cached from the read already happening here, above the
        # enabled/RTH gates for the same reason the two below are: reading broker state is not acting on
        # it, and a display that goes dark when a feature is switched off is the "silence looks like
        # nothing is wrong" failure again.
        #
        # This is the only hard anchor there is. CLAUDE.md: broker net is the only hard reconciliation
        # anchor. When the cache and the broker disagree about a basis, the broker wins.
        #
        # Typed and REST state the same fact under different names: `avg_px_open` on the report IS
        # Alpaca's `avg_entry_price` — one number, parsed once from whichever source answered above.
        basis: dict[str, float] = {}
        if position_reports is not None:
            for r in position_reports:
                px = r.avg_px_open
                if px is None:
                    continue
                basis[str(r.instrument_id)] = float(px)
        else:
            for p in positions:
                raw = p.get("avg_entry_price")
                if raw in (None, ""):
                    continue
                iid = str(self._resolve_instrument_id(str(p.get("symbol") or "")) or "")
                if not iid:
                    continue
                try:
                    basis[iid] = float(raw)
                except (TypeError, ValueError):
                    continue
        self._broker_avg_entry = basis

        # THE SHARED SET, not a private copy. `protection.PROTECTIVE_TYPES` holds BOTH vocabularies —
        # Alpaca's `stop`/`trailing_stop` and Nautilus's `STOP_MARKET`/`TRAILING_STOP_MARKET` — so it
        # survives a typed venue read unchanged. The copy that used to live here held only Alpaca's, and
        # fed typed orders it would match nothing: every protective stop invisible, the book read as
        # naked, a duplicate stop armed on every position that already had one.
        from api.protection import PROTECTIVE_TYPES as _PROTECTIVE
        # STATUS IS LOAD-BEARING, because the fetch above widened from "open" to "all" (#387).
        #
        # This classifier filtered on TYPE and SIDE only. That was safe while the fetch returned open
        # orders and nothing else; against `all` it counts this account's 86 CANCELLED protective sells
        # as live protection and renders every position covered — the dangerous direction, and a far
        # worse bug than the one being fixed.
        #
        # `is_resting` rather than a set written here. The first version of this fix DID write one, and
        # review found it disagreed with `protection._OPEN_STATUSES` in both directions on the same
        # payload: `pending_cancel` missing (badge NAKED while the placer saw it covered) and
        # `done_for_day`/`calculated` wrongly present (badge SECURED *and* a duplicate stop placed).
        # Both predicates answer "is this order live at the venue"; there is now one of them.
        # KEYED BY (INSTRUMENT, REDUCING SIDE), because a position is protected by an order on ITS
        # OWN reducing side and an instrument-level answer cannot say which. Measured on the live
        # paper book: CGAU, HALO and VCTR each carried a real LONG in one lane and a phantom SHORT in
        # another, with a single SELL stop resting — and all three SHORTS reported protected. A SELL
        # does not protect a short; on trigger it ADDS to it.
        #
        # BUY STOPS COUNT TOO, which the sell-only set could never express. Staging's RDN.XNYS is
        # SHORT 72 with protection armed, and a correct BUY stop there would have reported the
        # position NAKED — the same defect in the opposite direction, from the same cause.
        self._broker_protected = {
            (str(self._resolve_instrument_id(str(o.get("symbol") or "")) or ""),
             "SELL" if str(o.get("side") or "").lower() == "sell" else "BUY")
            for o in orders
            if str(o.get("type") or "") in _PROTECTIVE
            and str(o.get("side") or "").lower() in ("sell", "buy")
            and _is_resting(o.get("status"))
        }
        self._broker_protected = {k for k in self._broker_protected if k[0]}

        # THE DETECTOR (#269). Two derivations of one fact, compared every tick.
        #
        # Everything above reads the broker; every order path reads `cache.orders_open()`. When those two
        # disagree the engine acts on a book that does not exist, and NOTHING previously noticed: eight
        # protective stops rested at Alpaca for a full day while the cache held all eight as REJECTED, and
        # the only visible symptom was an operator unable to exit a position.
        #
        # Assert the RESIDUE, not the process — the same shape as the leftover-lot reconciliation that
        # found the `sell_short` bug in `realized_broker`. Neither derivation is trusted; the disagreement
        # is the finding. Above the RTH gate deliberately: divergence does not keep market hours, and a
        # detector that sleeps overnight would have reported nothing about the state we woke up to.
        # Compared per ORDER, not per instrument. An instrument-level check ("does the broker protect it,
        # does the cache see any stop") misses the case that actually bites: one stop visible and a SECOND
        # stuck one resting beside it, both holding shares. That position looks fine from every angle and
        # still rejects every exit.
        self._protection_divergence = []
        # IDENTITY, not classification. The question is "does the cache know this venue order exists at
        # all", so every open sell counts — including one this system would not call protective. Filtering
        # by `_is_identifiable_protective_stop` here instead raised a false alarm on a perfectly healthy
        # position: an operator's own stop, visible to both sides, was reported as invisible purely
        # because it lacked one of our client-order-id prefixes. An alarm that fires on the healthy case
        # is one an operator learns to scroll past, and this runs every 60 seconds on every position.
        cache_coids = {
            str(o.client_order_id) for o in self.cache.orders_open() if o.side == OrderSide.SELL
        }
        unseen: dict[str, list[str]] = {}
        for o in orders:
            if str(o.get("type") or "") not in _PROTECTIVE or str(o.get("side") or "") != "sell":
                continue
            # THE SAME `is_resting` AS ABOVE (#387 review, Critical). This loop compares broker orders
            # against `cache.orders_open()` and reports the residue. It filtered on type and side only,
            # which was correct while the fetch was `status="open"` and became a klaxon the moment it
            # widened: every protective sell ever FILLED or CANCELLED became "a resting order this
            # engine cannot see", one ERROR line per symbol ever traded, every 60 seconds — including
            # for symbols with no position at all. The message says "resting"; this makes it true.
            #
            # The detector exists to catch the REJECTED-in-cache / open-at-venue case (#269). Drowning
            # that in ninety rows of history disables the only thing that has ever caught it, which is
            # precisely the cry-wolf failure the comment above warns about.
            if not _is_resting(o.get("status")):
                continue
            coid = str(o.get("client_order_id") or o.get("id") or "")
            if coid in cache_coids:
                continue
            iid = str(self._resolve_instrument_id(str(o.get("symbol") or "")) or "")
            if iid:
                unseen.setdefault(iid, []).append(coid)

        for iid, coids in sorted(unseen.items()):
            held = [
                f"{o.client_order_id}={o.status.name}"
                for o in self.cache.orders()
                if str(o.instrument_id) == iid and str(o.client_order_id) in coids
            ]
            self._protection_divergence.append({"instrument_id": iid, "coids": coids, "cache_holds": held})
            self.log.error(
                f"PROTECTION DIVERGENCE on {iid}: the broker holds {len(coids)} resting protective "
                f"order(s) this engine cannot see ({', '.join(coids)}). The cache holds them "
                f"{held or 'not at all'}. Exits on this position will be rejected on `available: 0` "
                "unless they cancel at the venue."
            )

        # Only the ACTING is gated on regular hours. Reading broker state is not acting on it, and gating
        # the read meant SECURED went dark outside the session — silence that looks like "nothing is
        # protected" rather than "the market is shut" (#298's failure, one plane over).
        if not _us_market_open(now_ns):
            return

        # THE PASS BOUNDARY FOR PROTECTION ROWS (#757, moved here by #908). Every protection row this
        # pass records — `_cancel_unprotectable_stops` first, then the planner's refusals, then the
        # flip's — describes THIS evaluation, so the clear precedes all of them. It used to sit below
        # the cancel pass, which (a) wiped that pass's own rows a hundred lines later, every pass, so
        # they never reached the frame, and (b) put the recurrence record's pass boundary in the
        # middle of a pass: a key recorded before the clear and again after it read a permanent +1.
        # Below the RTH gate on purpose: outside the session nothing is evaluated, and the last
        # evaluation's rows stay on the surface rather than vanishing into "nothing refused".
        self._failed_requests.clear_kind("protection")

        # The typed rows derive `market_value` as signed qty x `_last_price_for` because the report
        # carries no broker statement of it (test_broker_read_gaps.py); the SAME price source feeds
        # `plan_protection` below, so a symbol with no price yields a `no_price` refusal rather than a
        # silently dropped row. Instrument ids arrive typed, so nothing can be unresolved on that path.
        if position_reports is not None:
            from api.protection import position_rows_from_reports

            rows, unresolved = position_rows_from_reports(position_reports, self._last_price_for), []
        else:
            rows, unresolved = broker_rows(positions, self._resolve_instrument_id)

        # PER-LANE ATTRIBUTION (#748 wiring, #801). Broker rows carry `strategy_id: ""` because the
        # venue does not know about sleeves — and that blank collapsed two lanes into one leg, left
        # `intent.strategy_id` empty, and sent arming to `stamp_for`, which refuses a multi-holder
        # instrument outright. Measured on paper 2026-09-08: CRAK/LAND/PAGP/GMAB each had one lane's
        # slice covered and the other's naked, 433 shares with no stop, for as long as both lanes held.
        #
        # The cache split is used ONLY where it sums to the broker's own quantity. Where it disagrees
        # the aggregate row survives untouched and behaviour is exactly what it was.
        from api.protection import attribute_rows_to_lanes, lane_entries, lane_modes, lane_quantities

        # NOT wrapped in `self._observations`. That mechanism is for code that WATCHES the system and
        # must not be able to break it; this is the protection path itself. An absorbed failure here
        # would silently revert to aggregate rows and leave a lane naked while reporting nothing
        # actionable — a fallback that degrades protection is worse than a crash that stops the pass.
        rows = attribute_rows_to_lanes(rows, lane_quantities(self.cache.positions_open() or []))

        # client_order_id -> owning lane, for PER-LANE COVERAGE. Venue reports cannot carry it: a
        # resting stop's lane lives only in our cache. Where a resting stop cannot be named,
        # `unprotected_positions` falls back to pro-rata for that leg rather than guessing.
        def _lane_of(coid: str) -> str:
            order = self._lookup_order(coid) if coid else None
            return str(getattr(order, "strategy_id", "") or "") if order is not None else ""

        # An exit is mid-flight on these, so its shares are deliberately unreserved (#358). See the
        # placement loop below, which is the ONLY thing this suppresses.
        #
        # Expired marks are dropped here rather than on a timer. This is the only pruner, so pruning at
        # the point of use cannot drift from the check, and an entry that outlives its deadline can never
        # keep a position naked past it.
        suppressed = self._active_exit_suppressions(now_ns)
        self._cancel_unprotectable_stops(now_ns)
        if unresolved:
            # Real exposure we cannot map. Reported, never silently dropped — a position missing from the
            # audit is a position the audit implicitly calls protected.
            self.log.warning(f"protection: {len(unresolved)} position(s) unmappable, left UNPROTECTED: {unresolved}")

        lookback = int(cfg.get("atrLookback", 14))
        atr_by_symbol, price_by_symbol = {}, {}
        for r in rows:
            iid = r["instrument_id"]
            value = self._protection_atr(iid, lookback)
            if value is not None:
                atr_by_symbol[iid] = value
            price = self._last_price_for(iid)
            if price is not None:
                price_by_symbol[iid] = float(price)

        # ONE CALL SITE for every plan this pass makes — the first plan, a flip's pre-flight with the
        # trail hypothetically gone, and the re-plan after the venue confirms the cancel (#898). Three
        # copies of these arguments would be three chances to plan the floor off different inputs.
        # The lane entries are read ONCE, here, where this pass's other per-lane reads live — the
        # ownership guard (`test_ownership_reads`) names the functions allowed to read the position
        # book without a lane, and a closure is not one of them.
        entries_by_lane = lane_entries(self.cache.positions_open() or [])

        def _plan(order_rows):
            return plan_protection(
            positions=rows,
            orders=order_rows,
            atr_by_symbol=atr_by_symbol,
            price_by_symbol=price_by_symbol,
            multiple=float(cfg.get("atrMultiple", 1.5)),
            min_pct=float(cfg.get("minTrailPct", 1.0)),
            max_pct=float(cfg.get("maxTrailPct", 15.0)),
            lane_of=_lane_of,
            # PER-LANE MODES (#872). `lane_modes` is the ONE reading of the `<LANE>_protection` keys —
            # the settings tests drive the same callable, so the schema and the planner cannot drift.
            # A lane the file does not name gets ITS OWN declared stance from the registry (#1029) —
            # SMHGLD-007 and CRSISHORT-006 declare `none`, every other lane `trail`. `declared_cfg` is
            # the RAW file: `cfg` is resolved and carries every schema default filled, so it cannot say
            # whether a value was written or defaulted, and the readback needs to (#965).
            mode_of=lane_modes(cfg, declared=declared_cfg),
            # The lane's OWN entry, from the NETTING position row. Never the broker's account average,
            # which mixes lanes and is identical to the lane's on any single-holder name — the exact
            # condition under which reading the wrong one is invisible.
            entry_by_lane=entries_by_lane,
        )

        plan = _plan(orders)

        # Over-coverage FIRST, before adding anything. An oversized stop is not a lesser problem than a
        # missing one: when it triggers it sells shares that are not held, so the position over-liquidates
        # and FLIPS into opposite exposure. Flat-with-a-stop-resting opens a naked short out of nothing.
        for over in plan.oversize:
            coid = str(over.order.get("client_order_id") or "")
            resting = self._lookup_order(coid) if coid else None
            if resting is None:
                # #285: our order book and the broker's disagree, so a resting order may have no Nautilus
                # Order to act on. Say so — silence here leaves an over-liquidating stop in place while the
                # reconciler reports success.
                self.log.warning(
                    f"protection: OVERSIZE stop on {over.instrument_id} ({over.covered} covering "
                    f"{over.held} held) but coid {coid!r} is not in the cache — cannot correct it"
                )
                continue
            try:
                if over.target_quantity <= 0:
                    # Nothing left to protect. Shrinking to zero is not a modification, it is a cancel.
                    self._cancel(resting)
                    self.log.info(f"protection: cancelled orphan stop on {over.instrument_id} (position closed)")
                else:
                    # PATCH in place, never cancel-and-replace: the trailing stop's high-water mark SURVIVES
                    # a replace (measured 2026-08-12) and `qty` is replaceable, while cancel-and-replace
                    # would surrender the hwm and open a naked window. Submit-then-cancel would be worse
                    # still here — it briefly rests EVEN MORE oversize coverage.
                    #
                    # `Strategy.modify_order` is the NATIVE Nautilus path and bottoms out in the adapter's
                    # Alpaca PATCH. Checked before hand-rolling, per CLAUDE.md.
                    # A real Quantity at the instrument's own precision — `modify_order` is Cython-typed
                    # (`Quantity quantity=None`) and a plain float raises at the boundary.
                    instrument = self._instrument_or_raise(InstrumentId.from_str(over.instrument_id))
                    self.modify_order(resting, quantity=self._make_qty(instrument, over.target_quantity))
                    self.log.info(
                        f"protection: shrank {over.instrument_id} stop to {over.target_quantity} "
                        f"(was covering {over.covered} against {over.held} held)"
                    )
            except Exception as exc:
                self.log.exception(f"protection: could not correct the oversize stop on {over.instrument_id}", exc)

        # A leg that is now covered starts clean if it ever needs protecting again, rather than inheriting
        # a stale attempt number that walks the hash further on every future arm (codex review, Medium).
        for iid in plan.covered_instrument_ids:
            # EVERY LANE'S counter for this leg, not a fixed pair of keys: the key now carries the lane
            # (#748), so popping ("AEM.XNYS", "SELL") would miss ("AEM.XNYS", "SELL", "MOMENTUM-002")
            # and leave a stale attempt number walking the hash on every future arm.
            for key in [k for k in self._protection_attempts if k[0] == iid]:
                self._protection_attempts.pop(key, None)

        # A REFUSAL IS A REQUEST THAT FAILED (#757). `plan_protection` correctly declines to size a
        # stop it cannot price or measure — and that decision was a log line, so 11 unpriceable
        # positions on staging could never be protected and no surface said so. Recorded as standing
        # state, cleared at the top of the pass (below the RTH gate — see there) when the instrument
        # becomes protectable again, so this reports what is true NOW rather than a history of
        # everything that ever failed.
        for refusal in plan.refusals:
            self._failed_requests.record(
                "protection", refusal.instrument_id,
                f"cannot rest a stop: {refusal.reason} (${refusal.uncovered_notional:,.0f} exposed)"
                + _refusal_provenance(refusal),
                ts=now_ns,
            )
        for refusal in plan.refusals:
            self.log.warning(
                f"protection: {refusal.instrument_id} left unprotected ({refusal.reason}), "
                f"${refusal.uncovered_notional:,.0f} exposed" + _refusal_provenance(refusal)
            )

        # THE WRONG-MODE FLIP, trail -> floor, ONE LEG PER PASS, INSIDE THIS PASS (#872, #898).
        #
        # Measured on paper 2026-09-10 (`scripts/probe_trailing_replace.py --m1`): `type` is not
        # patchable in either direction (422 42210000 "order type cannot be changed") and `stop_price`
        # is refused on a trailing stop, so the switch is a VENUE-FORCED cancel-then-place. What is not
        # forced is a 60 s tick between the two. The floor is placed in this same pass, once the venue
        # CONFIRMS the cancel; the naked window is the cancel-confirm latency (~4 s measured).
        #
        # Four gates before the cancel, each a NAMED refusal in `failed_requests`, because a trail that
        # is cancelled for a floor that never lands is a strip, not a transition:
        #   exit_in_flight      — a live exit standoff on the leg; the cancel waits, the trail keeps covering
        #   too_late_in_session — fewer than `_FLIP_MIN_SESSION_S` to the close; the floor could not follow
        #   floor_unplaceable   — the planner, with the trail hypothetically gone, would not plan the floor
        #   queued_behind       — another leg is flipping this pass; one naked leg across the book at a time
        # And three after it: `cancel_unconfirmed` / `trail_filled_during_flip` / `venue_unreadable` —
        # nothing is placed on any; the next pass's normal path sizes the floor off the venue's truth.
        #
        # THE RACE the coordinator named — the reconciler re-covering the leg the instant the cancel
        # frees it, ahead of the floor — cannot happen from this process: the whole sequence runs inside
        # ONE pass behind `_protection_running`, and a lane on `entry_floor` is planned a floor, never a
        # trail, by every pass. Pinned by test_THE_RACE_... firing a nested pass from inside the wait.
        # STABLE ACROSS PASSES, BY A KEY BOTH VENUE PATHS CARRY. "Oldest first" was the first version
        # and it was inert in production: the typed path (`orders_from_reports`) emits no submission
        # time and the parser stamps `ts_accepted` with NOW, so every row sorted equal and the order
        # was whatever the report listed. The client order id exists on every row from either path;
        # sorting on it makes the sequence deterministic and PROGRESSING — each pass flips one leg and
        # that leg leaves `wrong_mode`, so no leg can starve.
        flipped_iid: str | None = None
        self._flip_evaluated = True
        flipping = sorted(plan.wrong_mode, key=lambda w: str(w.order.get("client_order_id") or ""))

        def _still_pending(w, reason: str) -> None:
            # Consecutive from the previous pass's record; the streak start kept; reason is the CURRENT one.
            key = (str(w.instrument_id), str(w.strategy_id))
            prev = pending_before.get(key)
            self._flip_pending[key] = {
                "passes": (int(prev["passes"]) + 1) if prev else 1,
                "streak_started_ns": int(prev["streak_started_ns"]) if prev else now_ns,
                "last_reason": reason,
            }

        for wrong in flipping[1:]:
            _still_pending(wrong, "queued_behind")
            self._failed_requests.record(
                "protection", str(wrong.instrument_id),
                f"queued_behind:{flipping[0].order.get('client_order_id') or ''} — "
                f"{wrong.order.get('client_order_id') or ''} flips on a later pass ({wrong.reason})",
                ts=now_ns,
            )
        if flipping:
            wrong = flipping[0]
            iid = str(wrong.instrument_id)
            coid = str(wrong.order.get("client_order_id") or "")
            venue_id = str(wrong.order.get("id") or "")
            remaining = _rth_seconds_remaining(now_ns)
            # BY IDENTITY KEYS, not object identity: the planner may hand back a copy of the row.
            gone = _row_keys(wrong.order)
            without_trail = [o for o in orders if not (_row_keys(o) & gone)]
            dry = _plan(without_trail)
            # THE LANE MATCHES OR IS BLANK. An intent carries a lane only where the cache split sums to
            # the broker's quantity (`attribute_rows_to_lanes`); a single-holder aggregate row plans a
            # floor for the whole instrument with `strategy_id == ""`, and that IS this lane's floor.
            would_place = any(
                i.instrument_id == iid and i.kind == "entry_floor"
                and (not i.strategy_id or i.strategy_id == wrong.strategy_id)
                for i in dry.intents
            )
            verdict: str | None = None
            if iid in suppressed:
                verdict = "exit_in_flight"
                self._failed_requests.record(
                    "protection", iid,
                    f"exit_in_flight: not cancelling {coid} — an exit is mid-flight on {iid}; the trail "
                    "keeps covering the leg until the standoff clears",
                    ts=now_ns,
                )
            elif remaining is None or remaining < _FLIP_MIN_SESSION_S:
                verdict = "too_late_in_session"
                # `None` is unreachable behind the pass's RTH gate; kept as belt-and-braces and NAMED
                # as what it would mean rather than printed as "0s".
                left = "outside the session" if remaining is None else f"{remaining:.0f}s to the close"
                self._failed_requests.record(
                    "protection", iid,
                    f"too_late_in_session: not cancelling {coid} — {left}, the flip needs "
                    f"{_FLIP_MIN_SESSION_S:.0f}s; next session",
                    ts=now_ns,
                )
            elif not would_place:
                why = next((r.reason for r in dry.refusals if r.instrument_id == iid), "not_planned")
                verdict = f"floor_unplaceable:{why}"
                self._failed_requests.record(
                    "protection", iid,
                    f"floor_unplaceable:{why} — not cancelling {coid}; the trail keeps covering the leg",
                    ts=now_ns,
                )
            else:
                resting = self._lookup_order(coid) if coid else None
                self._failed_requests.record(
                    "protection", iid,
                    f"cancelling {coid or venue_id}: {wrong.reason} — the floor is placed this pass once "
                    "the venue confirms",
                    ts=now_ns,
                )
                cancelled = False
                try:
                    # OPEN IN THE CACHE, not merely PRESENT in it. `Strategy.cancel_order` refuses a
                    # closed order (strategy.pyx:1651), so a stop the cache holds terminal while the venue
                    # still rests it — the #807 corpse, measured — would be "cancelled" by a warning and
                    # keep reserving its shares forever. That is the case the venue route exists for.
                    if resting is not None and resting.is_open:
                        # THROUGH NAUTILUS whenever the cache is tracking it, so its state machine is
                        # never bypassed — the same boundary `_cancel_reducing_leg` keeps.
                        self._cancel(resting)
                    else:
                        await self._cancel_at_venue(iid, coid, venue_id)
                    cancelled = True
                    self.log.warning(
                        f"protection: cancelled {coid or venue_id} on {iid} — {wrong.reason}. "
                        f"{wrong.strategy_id} is unprotected until the venue confirms and the floor rests."
                    )
                except Exception as exc:
                    verdict = "cancel_failed"
                    self.log.exception(
                        f"protection: could not cancel the wrong-mode stop {coid or venue_id} on "
                        f"{iid} — its shares stay reserved and no floor can be placed", exc)
                if cancelled:
                    state = await self._await_cancel_confirmed(iid, coid, venue_id)
                    if state == "confirmed":
                        flipped_iid = iid
                        # THE RE-PLAN, off the same inputs minus the cancelled row only — never every
                        # order on the instrument, which would plan a second lane's floor while its
                        # trail still rests. Its refusals for THIS leg are recorded explicitly: the
                        # pass-wide `clear_kind` and refusal loop ran before this, and a second
                        # `clear_kind` here would erase every other instrument's rows.
                        # A FRESH MARK for the flipped leg. The pass's price map is seconds old by now;
                        # the exec client's own wrong-side check reads the live cache at submit, and a
                        # floor planned off the stale mark would be refused there and burn a coid. The
                        # planner's refusal is the one that gets NAMED, so it reads the same freshness.
                        fresh = self._last_price_for(iid)
                        if fresh is not None:
                            price_by_symbol[iid] = float(fresh)
                        orders = without_trail
                        plan = _plan(orders)
                        for refusal in plan.refusals:
                            if refusal.instrument_id == iid:
                                self._failed_requests.record(
                                    "protection", iid,
                                    f"after the flip: cannot rest the floor: {refusal.reason} "
                                    f"(${refusal.uncovered_notional:,.0f} exposed)",
                                    ts=now_ns,
                                )
                        # RE-SAMPLE THE STANDOFF. `suppressed` was read at the top of the pass; an exit
                        # that started during the wait would otherwise be invisible to the placement
                        # loop — #358 restored through the flip.
                        suppressed = self._active_exit_suppressions(self.clock.timestamp_ns())
                    else:
                        label, why = {
                            "timeout": ("cancel_unconfirmed", "still resting after the confirm wait"),
                            "filled": ("trail_filled_during_flip",
                                       "the trail FILLED while being cancelled — the shares are gone or fewer"),
                        }.get(state, ("venue_unreadable", "the venue could not be read after the cancel"))
                        verdict = label
                        self._failed_requests.record(
                            "protection", iid,
                            f"{label}: {coid or venue_id} on {iid} — {why}; nothing placed, the next pass "
                            "sizes the floor off the venue's truth once the trail is gone",
                            ts=now_ns,
                        )
                        self.log.error(
                            f"protection: {label} for {coid or venue_id} on {iid} — floor not placed this pass"
                        )

            if verdict is not None:
                # NOT FLIPPED THIS PASS — whichever gate or post-cancel state said so. A confirmed flip
                # leaves no entry; the key simply is not written and the previous count is gone.
                _still_pending(wrong, verdict)

        for intent in plan.intents:
            # THE ONLY THING THE STANDOFF SUPPRESSES, and the first version got this badly wrong.
            #
            # It filtered the POSITION ROWS instead. `plan_protection` derives oversize from the ORDERS
            # rather than from `positions` — deliberately, so an orphan stop on a closed position is
            # caught (protection.py:546) — so deleting the row made `held` 0, turned a correctly-sized
            # resting stop into an orphan with `target_quantity` 0, and the reconciler CANCELLED it while
            # logging "position closed" over a fully-held position. Two reviewers reproduced it
            # independently: same fixture, cancelled=0 without the mark and cancelled=1 with it. The fix
            # for #358 would have stripped protection off live positions, on exactly the failure branches
            # where the release correctly gives up and the caller sends nothing.
            #
            # Placement is the only action that can hurt an exit in flight, because a NEW stop re-reserves
            # the shares the release just freed. Shrinking an oversize stop REDUCES the reservation and
            # cancelling a true orphan FREES it — neither can defeat an exit, so neither is suppressed.
            if intent.instrument_id in suppressed:
                self.log.info(
                    f"protection: not arming {intent.instrument_id} — an exit is in flight and a new stop "
                    "would reserve the shares the release just freed"
                )
                # STANDING STATE, not only a log line — for the leg whose trail THIS PASS cancelled
                # (#898 review): that is a bare leg with a reason, and a reason that scrolled past is
                # the silence this surface exists to end. An ordinary standoff on a leg that still has
                # its stop stays an info line, so a trail-only book keeps its empty protection rows
                # (the readback criterion for #872 and this PR).
                if intent.instrument_id == flipped_iid:
                    self._failed_requests.record(
                        "protection", str(intent.instrument_id),
                        f"exit_in_flight: not arming {intent.instrument_id} after its trail was cancelled "
                        "— an exit is mid-flight; a new stop would reserve the shares the release just freed",
                        ts=now_ns,
                    )
                continue
            # KEYED BY LANE TOO. `_protection_attempts` and `_protection_pending` both hang off
            # this; sharing one counter between two lanes on a leg made them collide on coids
            # and made one lane's in-flight mark suppress the other lane's stop entirely.
            # RESOLVE THE OWNER FIRST, AND REFUSE IF THERE IS NONE (#748).
            #
            # This used to read `intent.strategy_id or stamp_for(...)` at BUILD time and submit anyway
            # when the stamp came back None — the order then carried the SUBMITTING strategy,
            # MANUAL-001. Under NETTING the position id derives from the ORDER's strategy_id, so on an
            # instrument MANUAL-001 does not hold the fill resolves to a position that has never
            # existed, the ExecEngine REJECTS it, and the <=10s poll fabricates the difference as a
            # synthetic sell. That is the mint.
            #
            # AND THE REJECTED FILL IS WHY THE BUDGET NEVER COMES BACK. The shares left the broker;
            # the cache never recorded the sale; `deployed` stays high; the lane starves. Measured
            # 2026-08-31: MOMENTUM, BCTROT and TECHIVOL all refusing entries on 0 budget while holding
            # positions the broker had already sold. The loop closes on itself here.
            #
            # 17 of 23 live protective orders carried a lane holding nothing of the instrument. Some
            # refusals are explicable (HALO 3 holders, RGEN 2); at least one was not (CRM, a single
            # holder, still stamped MANUAL-001). So the diagnosis is emitted on EVERY refusal — the
            # cause is not established and this is what will establish it.
            positions_now = self.cache.positions_open() or []
            owner = intent.strategy_id or stamp_for(intent.instrument_id, positions_now)
            if not owner:
                why = stamp_diagnosis(intent.instrument_id, positions_now)
                self.log.warning(
                    f"protection: REFUSING to arm {intent.instrument_id} — no owner could be resolved "
                    f"and an unstamped stop mints a phantom when it fills. {why['reason']}"
                )
                # Standing state, not only a log line: a refusal that scrolls past is exactly the
                # silence this class of defect lives in. Keyed on the INSTRUMENT so rows coalesce per
                # symbol rather than growing per tick.
                self._failed_requests.record(
                    "protection", str(intent.instrument_id), why["reason"], ts=now_ns,
                )
                continue

            # DELIBERATELY THE RAW INTENT, NOT THE RESOLVED OWNER — and this is a decision, not an
            # oversight. Keying the coid on `owner` looks more correct (the lane axis is otherwise
            # INERT, because `intent.strategy_id` is empty in production, so two lanes on one leg mint
            # the same id and the second is denied as a duplicate). But CHANGING THE DERIVATION
            # CHANGES EVERY ID: a stop armed under the old one becomes invisible to the
            # already-in-the-cache check below, and the next tick rests a SECOND stop on a position
            # that already has one.
            #
            # That is not hypothetical. On 2026-08-31 HALO and GMAB were each sold by two distinct
            # protective orders — 118 GMAB against a 59 holding — which is why GMAB now reads 0 at
            # the venue and 59 in the cache. `test_a_coid_already_in_the_cache_is_SKIPPED...` caught
            # this the moment the derivation moved.
            #
            # The inert lane axis is a real defect and needs its own change WITH a migration for
            # in-flight ids. It is not a free rider on this one.
            leg = (intent.instrument_id, intent.side, intent.strategy_id)
            attempt = self._protection_attempts.get(leg, 0)
            coid = _protection_coid(intent.instrument_id, intent.side, attempt, intent.strategy_id,
                                        self._trader_id_str())
            # Advance past any id the CACHE already knows (#295, second half). `_protection_attempts` is
            # process memory and resets on restart, so the first post-restart retry would otherwise
            # regenerate attempt-0's coid — the very id that may already be REJECTED in the durable cache,
            # reproducing the REJECTED -> DENIED pair that made the engine unbootable. The cache is the
            # durable record; process state is not. Bounded so a pathological cache cannot spin.
            for _ in range(_PROTECTION_MAX_ATTEMPTS):
                if self._lookup_order(coid) is None:
                    break
                attempt += 1
                coid = _protection_coid(intent.instrument_id, intent.side, attempt, intent.strategy_id,
                                        self._trader_id_str())
            else:
                self.log.warning(
                    f"protection: {intent.instrument_id} has {_PROTECTION_MAX_ATTEMPTS} used client order "
                    f"ids in the cache — refusing to submit rather than guessing another"
                )
                continue
            pending_at = self._protection_pending.get(leg)
            if pending_at is not None and (now_ns - pending_at) < _PROTECTION_PENDING_NS:
                continue  # submitted moments ago and not yet visible in broker REST — do not double-send
            try:
                # THE TWO KINDS DIVERGE HERE AND NOWHERE ELSE (#872). A `trail` intent becomes a native
                # trailing stop whose trigger the VENUE recomputes; an `entry_floor` intent becomes a
                # fixed STOP_MARKET whose trigger was computed once, in the planner, from this lane's
                # own entry. `StopIntent.__post_init__` has already refused every other combination, so
                # this dispatch cannot silently build a 0-bps trailing stop out of a floor.
                if intent.kind == "entry_floor":
                    instrument = self._instrument_or_raise(
                        InstrumentId.from_str(intent.instrument_id))
                    kind_fields = {
                        "order_type": "stop_market",
                        # ON THE VENUE'S TICK, and ROUNDED AWAY FROM THE MARKET. `_canon_price` is
                        # fail-closed for an operator-supplied price — a typo must never be silently
                        # rounded into a different order — but this price is COMPUTED (entry - k x ATR)
                        # and lands between ticks routinely, so it is quantised deliberately here and
                        # arrives already exact. Down for a SELL, up for a BUY: the direction that can
                        # only ever make the stop less likely to fire early.
                        "trigger_price": str(self._tick_away_from_market(
                            instrument, intent.trigger_px, intent.side)),
                        # THE MODE, ON THE ORDER. The UI reads this to label the stop "floor" rather
                        # than deriving the kind from the order type — a STOP_MARKET alone could be a
                        # bracket leg or a manual stop, and a second derivation of "is this a floor"
                        # would drift from the one that placed it.
                        "extra_tags": ["mode:entry_floor"],
                    }
                else:
                    kind_fields = {"order_type": "trailing_stop", "trail_bps": intent.trail_bps}
                order = self._build_order({
                    "instrument_id": intent.instrument_id,
                    "side": intent.side,
                    "quantity": intent.quantity,
                    # THE OWNER, and this is what stops the phantom mint. Without a lane the order
                    # is stamped with the SUBMITTING strategy (MANUAL-001), and on an instrument
                    # MANUAL-001 does not hold, the reduce-only fill resolves to a position that has
                    # never existed — applied to nothing, then fabricated by the position poll.
                    #
                    # `intent.strategy_id` is blank in production today, because #748's per-lane row
                    # sources are built and deliberately not wired. So the lane is resolved HERE from
                    # the cache, for the case that needs no split at all: exactly one holder, whose
                    # quantity the aggregate stop already equals. Ambiguous instruments fall back to
                    # today's behaviour rather than being stamped on a guess — see `protective_stamp`.
                    "strategy_id": owner,
                    **kind_fields,
                    "time_in_force": "gtc",
                    "client_order_id": coid,
                    # A PROTECTIVE STOP MUST NEVER OPEN A POSITION. If this rests and the position closes
                    # by another route — a manual flatten, a PEAK exit, a reconciliation — firing does not
                    # close anything, it opens the opposite side. That is FIG in #252, the oversell in
                    # #245, and the phantom HSBC short. Enforced only when `_submit` also names the
                    # position (`risk/engine.pyx:425`); the flag alone is decoration.
                    "reduce_only": True,
                })
            except (ValueError, KeyError) as exc:
                self.log.exception(f"protection: could not build a stop for {intent.instrument_id}", exc)
                continue
            # Marked in-flight only AFTER the submit lands, and the mark EXPIRES. Adding it first meant a
            # throw here burned the coid for the life of the process; making it permanent meant an async
            # venue rejection did the same, more quietly. Failure to place NOW is never a decision not to
            # place. (Same distinction as #255's terminal FAILED.)
            try:
                self._submit(order)
            except Exception as exc:
                self.log.exception(f"protection: submit failed for {intent.instrument_id} — will retry", exc)
                continue
            self._protection_pending[leg] = now_ns
            # Next attempt on this leg gets a fresh identity, whatever becomes of this one.
            self._protection_attempts[leg] = attempt + 1
            width = (f"floor at {intent.trigger_px:.2f}" if intent.kind == "entry_floor"
                     else f"trailing {intent.trail_bps}bps")
            self.log.info(
                f"protection: rested {intent.side} {intent.quantity} {intent.instrument_id} "
                f"{width} for {intent.strategy_id or owner} (ATR {intent.atr_pct:.2f}%)"
            )

    async def _refresh_fundamentals(self, now_ns: int) -> None:
        """Per-symbol FMP fetch (quote + profile + annual income statement) for every loaded symbol (#182
        follow-up, KPI Phase 2). No batch endpoint — confirmed empirically that FMP's multi-symbol quote
        is a paid-tier-only feature (403 on the free tier) — so this loops sequentially, one symbol at a
        time, awaiting each before starting the next (simple backpressure; at daily-ish cadence for a
        watchlist-sized universe this is well within the free tier's request budget).

        Unlike VWAP/Today's Range, a fetch failure for one symbol does NOT tombstone it — fundamentals have
        no daily-boundary reset (yesterday's EPS is still today's EPS), so keeping the prior value on a
        transient failure is correct, not stale-masquerading-as-fresh. A missing field within an otherwise
        successful response (e.g. no `beta` yet for a very new listing) publishes as null for that field
        specifically, not skipped as if the whole symbol failed.
        """
        if self._fmp is None or not self._loaded:
            return
        if self._fundamentals_inflight:
            return  # previous refresh still in flight — this cycle's data arrives slightly late, not lost
        self._fundamentals_inflight = True
        try:
            for iid_str in list(self._loaded):
                try:
                    instrument_id = InstrumentId.from_str(iid_str)
                except ValueError:
                    continue  # unparseable instrument id — skip, nothing FMP-able to request anyway
                ticker = instrument_id.symbol.value  # not iid_str.split(".") — mangles a dotted ticker
                # like "BRK.B" (codex review: the exact bug already fixed for Today's Range)
                try:
                    quote = await self._fmp.get_quote(ticker)
                except Exception as exc:  # noqa: BLE001 — one bad symbol must not break the rest
                    _log.warning("fundamentals quote fetch failed for %s: %s", iid_str, exc)
                    quote = None
                try:
                    profile = await self._fmp.get_profile(ticker)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("fundamentals profile fetch failed for %s: %s", iid_str, exc)
                    profile = None
                try:
                    eps = await self._fmp.get_annual_eps(ticker)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("fundamentals EPS fetch failed for %s: %s", iid_str, exc)
                    eps = None
                if quote is None and profile is None and eps is None:
                    continue  # nothing at all this cycle for this symbol — try again next cycle
                price = quote.get("price") if quote else None
                pe = (price / eps) if (price is not None and eps not in (None, 0)) else None
                market_cap = (quote or profile or {}).get("marketCap")
                beta = (profile or {}).get("beta")
                dividend_amount = (profile or {}).get("lastDividend")
                as_of = (quote or {}).get("timestamp")
                as_of_str = (
                    _et_date(int(as_of) * 1_000_000_000) if isinstance(as_of, (int, float)) else "unknown"
                )
                self._publish(
                    "fundamentals",
                    {
                        "instrument_id": iid_str,
                        "market_cap": market_cap,
                        "beta": beta,
                        "eps": eps,
                        "pe": pe,
                        "dividend_amount": dividend_amount,
                        "as_of": as_of_str,
                        "ts_event": now_ns,
                    },
                )
                # ALSO publish onto Nautilus's own in-process message bus (Operator: "shouldn't algorithms be
                # able to look at the KPIs too?") — the engine→Redis→WS path above is UI-only; this is the
                # proper Nautilus-native path so a live Strategy running on the same node can
                # `subscribe_data(fundamentals_data_type(iid), instrument_id=...)` and read these values
                # itself, without going through the UI at all. `nan_if_none` because `@customdataclass`'s
                # Arrow-schema generator doesn't support `float | None` fields — see data_types.py.
                try:
                    self.publish_data(
                        fundamentals_data_type(iid_str),
                        FundamentalsData(
                            ts_event=now_ns,
                            ts_init=now_ns,
                            instrument_id=instrument_id,
                            market_cap=nan_if_none(market_cap),
                            beta=nan_if_none(beta),
                            eps=nan_if_none(eps),
                            pe=nan_if_none(pe),
                            dividend_amount=nan_if_none(dividend_amount),
                            as_of=as_of_str,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — the Redis/UI publish above already succeeded;
                    # a msgbus publish failure (bad instrument id, strategy not yet registered) must not
                    # undo that or break the rest of the loop.
                    _log.warning("fundamentals msgbus publish failed for %s: %s", iid_str, exc)
        finally:
            self._fundamentals_inflight = False

    async def _reconcile_symbols(self) -> None:
        """Load + stream any persistent-watchlist symbol, and any OPEN POSITION, not already loaded.

        The data provider holds every instrument definition, so a valid TICKER.MIC loads on demand.

        Positions are included because the startup universe is a SNAPSHOT (#206): `on_start` loads the
        configured symbols plus whatever was held at boot, and nothing subscribed a symbol at the moment
        it was bought. On 2026-08-10 the strategy entered six names and every one of them streamed zero
        frames until the node was restarted — the UI showed `LAST —` on the whole automated book.

        That is worse than a display gap. `_trail_exits` prices positions from the live quote and falls
        back to the previous daily CLOSE when there is none, so the morning after any entry those
        positions would be evaluated against yesterday — and a position that gapped through its exit
        level overnight is exactly the one that must be cut. It would read as "not triggered" rather
        than "could not see it", and the two are indistinguishable in the book.

        Cheap to include: `_loaded` already dedupes, so a held symbol that is also on the watchlist
        loads once, and this loop is the existing periodic sweep rather than a new mechanism.
        """
        wanted: set[str] = set()
        try:
            from api.db.engine import session_factory
            from api.watchlist import list_symbols

            async with session_factory() as session:
                wanted.update(await list_symbols(session))
        except Exception as exc:  # noqa: BLE001 — a DB hiccup must never break the trading loop
            _log.warning("symbol reconcile — watchlist read failed: %s", exc)
        try:
            # Real capital first: a held name streams even if the watchlist read above failed.
            wanted.update(str(p.instrument_id) for p in self.cache.positions_open())
        except Exception as exc:  # noqa: BLE001
            _log.warning("symbol reconcile — open-position read failed: %s", exc)
        for iid_str in wanted:
            if iid_str in self._loaded:
                continue
            try:
                instrument_id = InstrumentId.from_str(iid_str)
            except ValueError:
                _log.warning("symbol reconcile — bad instrument id %r", iid_str)
                continue
            try:
                self._load(instrument_id)
            except (ValueError, KeyError) as exc:
                # NOT marked loaded: `_loaded` is monotonic, so recording a failed load would retire the
                # symbol permanently and the next sweep would skip it forever. That was survivable while
                # this only carried the watchlist; it is not now that a HELD POSITION comes through here
                # and a transient failure would leave real capital unpriced with no second attempt.
                _log.warning("symbol reconcile — load %s failed, will retry: %s", iid_str, exc)
                continue
            self._loaded.add(iid_str)
            _log.info("symbol reconcile — loading %s", iid_str)

    # --- command layer: consume ui:commands, dispatch to Nautilus actions (#32) -------------------
    def _drain_commands(self) -> None:
        """Reader thread: XREADGROUP ui:commands, schedule each command onto the trading loop. First pass
        reclaims THIS consumer's un-acked pending entries (PEL, id '0' — a crash before ack), then switches
        to new messages ('>'). The XACK happens in `_handle_command` AFTER the effect runs (at-least-once,
        not at-most-once). Redis-py is pool-backed so sharing the client with the writer thread is safe;
        block < socket_timeout(1s) so idle reads return empty instead of raising."""
        if self._redis is None:
            return
        cursor = "0"  # start by draining our pending list, then move to new entries
        while not self._stopping.is_set():
            try:
                resp = self._redis.xreadgroup(
                    _CMD_GROUP, _CMD_CONSUMER, {_CMD_STREAM: cursor}, count=10, block=900
                )
            except redis.RedisError as exc:
                _log.warning("command read failed: %s", exc)
                time.sleep(0.5)
                continue
            entries = [e for _stream, es in (resp or []) for e in es]
            if cursor == "0" and not entries:
                cursor = ">"  # pending drained → consume new commands
                continue
            for entry_id, fields in entries:
                if self._loop is None:
                    continue
                # SERIALIZE: wait for each command to finish before the next (orders must apply in order,
                # never concurrently). The handler runs on the loop; this reader thread just gates ordering.
                fut = asyncio.run_coroutine_threadsafe(self._handle_command(entry_id, fields), self._loop)
                self._await_command_serialized(fut, entry_id)

    def _await_command_serialized(self, fut, entry_id: str) -> None:
        """Wait for one command's handler to FINISH before the reader takes the next entry.

        The old `fut.result(timeout=30)` swallowed its own timeout: the reader proceeded while the
        slow handler kept running on the loop, breaking the ordering invariant the comment above
        states — a flatten legitimately exceeds 30s waiting on venue confirms, and the next command
        for the same instrument could interleave mid-cancel (#652 item 6). Now the interval only
        paces the LOGGING: past it we keep holding the queue and say so, out loud, every interval.
        Two exits only: the handler finishes (success or error), or the node is stopping (there is
        no next command to mis-order, and blocking shutdown behind a wedged handler helps nobody).
        A TimeoutError raised BY the handler (fut.done()) is that handler's error, not our wait
        expiring — treating it as "still running" would spin on a completed future forever."""
        waited = 0.0
        while True:
            try:
                fut.result(timeout=_CMD_SERIALIZE_WAIT_SECS)
                return
            except TimeoutError as exc:
                if fut.done():  # the handler itself raised TimeoutError — its failure, not our wait
                    _log.warning("command %s handler error: %s", entry_id, exc)
                    return
                if self._stopping.is_set():
                    _log.warning(
                        "command %s handler still running at shutdown after %.0fs — abandoning the "
                        "wait (node stopping; its outcome is unrecorded)", entry_id, waited,
                    )
                    return
                waited += _CMD_SERIALIZE_WAIT_SECS
                _log.warning(
                    "command %s handler still running after %.0fs — holding the queue (orders must "
                    "apply in order, never concurrently)", entry_id, waited,
                )
            except Exception as exc:  # noqa: BLE001 — a failed handler must not wedge the reader
                _log.warning("command %s handler error: %s", entry_id, exc)
                return

    async def _handle_command(self, entry_id: str, fields: dict) -> None:
        """Dispatch one command on the trading loop, ack back over ui:stream, then XACK Redis (after the
        effect, so a failed handler leaves the entry pending for reclaim). Malformed/unknown/failed commands
        ack with an error — never crash the loop."""
        cid = fields.get("id", entry_id)
        ctype = fields.get("type")
        status, error = "ok", ""
        results = None  # only the search command answers with rows (#837); every other ack is unchanged
        try:
            payload = json.loads(fields.get("payload", "{}"))
            if ctype == "stream_request":
                iid_str = payload["instrument_id"]
                if iid_str not in self._loaded:
                    self._load(InstrumentId.from_str(iid_str))  # provider holds every def → loads on demand
                    self._loaded.add(iid_str)  # only after a successful load (a failure stays retryable)
            elif ctype in ("submit_order", "cancel_order", "modify_order", "submit_bracket"):
                status, error = await self._handle_order_command_idempotent(cid, ctype, payload, entry_id)
            elif ctype == "flatten_position":
                status, error = await self._handle_flatten_command_idempotent(cid, payload, entry_id)
            elif ctype == "liquidate_lane":
                # #922: the ONE command that can flatten a lane deliberately. Three states on the ack:
                # ok (submitted / held nothing / deferred to the next open) carries the report in
                # `results`; partial and refused are ERRORS with the same report — an operator who reads
                # "ok" must be able to believe the book is on its way to flat.
                lq_status, detail = await self._handle_liquidate_lane_command(cid, payload, entry_id)
                results = detail
                if lq_status in ("ok", "deferred_to_next_open"):
                    status, error = "ok", "" if lq_status == "ok" else "deferred_to_next_open: LIQUIDATING written, nothing submitted outside regular hours"
                else:
                    status, error = "error", f"{lq_status}: {detail.get('why') or json.dumps({k: detail.get(k) for k in ('submitted', 'held', 'failed', 'remainder')}, default=str)}"
            elif ctype == "attach_manager":
                status, error = await self._handle_attach_manager_command(cid, payload, entry_id)
            elif ctype == "cancel_manager":
                status, error = await self._handle_cancel_manager_command(cid, payload, entry_id)
            elif ctype == "eod_backfill":
                # Applied HERE for the same reason as `transfer_position` below: the acceptance gate
                # compares against the engine's LIVE cache, and the api process holds only stale
                # snapshots. No order is ever routed to a venue by this — it writes observation rows.
                status, error = await self._handle_eod_backfill_command(cid, payload)
            elif ctype == "book_repair":
                # Applied HERE for the same reason as `eod_backfill`: planning reads the engine's
                # LIVE cache and its venue, and the api process holds only stale snapshots. NO ORDER
                # IS EVER ROUTED TO A VENUE by this — every leg is internal and the broker's net does
                # not move. `arm` must be sent explicitly; the default is report-only.
                status, error = await self._handle_book_repair_command(cid, payload)
            elif ctype == "search_instruments":
                # Answered by the engine because it holds the venue session (#837) — but OFF this
                # lane: the reader waits for each handler before taking the next command, and IB's
                # matching request can take the adapter's full 60 s timeout. One slow search must not
                # hold a cancel behind it (codex on 920bf48). Its own task publishes its own ack.
                asyncio.ensure_future(self._answer_search(cid, ctype, payload))
                self._xack_command(entry_id)
                return
            elif ctype == "transfer_position":
                # Applied HERE, not in the api process: validation needs the engine's live cache, and the api
                # holds only stale snapshots (#20 split). No order is ever routed to a venue by this.
                status, error = await self._handle_transfer_command(cid, payload)
            else:
                status, error = "error", f"unknown command type {ctype!r}"
        except Exception as exc:  # noqa: BLE001 — a bad command must never break trading
            status, error = "error", str(exc)
            _log.warning("command %s (%s) failed: %s", cid, ctype, exc)
        self._publish_ack(cid, ctype, status, error, results)
        self._xack_command(entry_id)

    def _publish_ack(self, cid: str, ctype: str, status: str, error: str, results=None) -> None:
        ack = {"id": cid, "type": ctype, "status": status, "error": error}
        if results is not None:  # search only (#837); every other ack keeps its exact shape
            ack["results"] = results
        self._publish("command_ack", ack)

    def _xack_command(self, entry_id: str) -> None:
        if self._redis is not None:
            try:
                self._redis.xack(_CMD_STREAM, _CMD_GROUP, entry_id)
            except redis.RedisError:
                pass

    async def _answer_search(self, cid: str, ctype: str, payload: dict) -> None:
        """The search's own task: ask the venue, bounded, and ack — never on the command lane."""
        try:
            status, error, results = await asyncio.wait_for(
                self._handle_search_command(payload), timeout=self._search_timeout_s)
        except asyncio.TimeoutError:
            q = str(payload.get("q", ""))[:40]
            status, error, results = "error", (
                f"venue did not answer search for {q!r} within {self._search_timeout_s:.1f}s"), None
        except Exception as exc:  # noqa: BLE001 — a bad search must never take the engine down
            status, error, results = "error", str(exc), None
            _log.warning("search %s failed: %s", cid, exc)
        self._publish_ack(cid, ctype, status, error, results)

    async def _handle_search_command(self, payload: dict) -> tuple[str, str, list | None]:
        """Ask the broker for instruments matching a prefix (#837): `reqMatchingSymbols` through the
        IB client the data engine already holds. US equities only, named the way this book already
        names IB instruments (the adapter's own id builder, so `IBKR.XNAS` and `TRT.AMEX` come out
        exactly as the positions plane shows them). Ranked with the same tiers the Alpaca index uses.

        REFUSES, with a reason, when there is no client or the client will not answer — rows, no rows
        and never-answered are three facts and only the first two are a list.
        """
        q = str(payload.get("q", "")).strip()
        limit = max(1, min(int(payload.get("limit", 20) or 20), 50))
        client = self._ib_client_ref() if callable(self._ib_client_ref) else None
        if client is None or not hasattr(client, "get_matching_contracts"):
            return "error", "no instrument provider: this node holds no IB client", None
        if not q:
            return "ok", "", []
        contracts = await client.get_matching_contracts(q)
        if contracts is None:
            return "error", f"search for {q!r} already in flight at the venue", None
        from api.instrument_search import rank_matches
        return "ok", "", rank_matches(q, _ib_contracts_to_matches(contracts))[:limit]

    async def _handle_book_repair_command(self, cid: str, payload: dict) -> tuple[str, str]:
        """Plan and (only when explicitly armed) book the ownership repair (#771).

        ARM IS OPT-IN AND MUST BE THE BOOLEAN TRUE. `bool(payload.get("arm"))` would arm on the
        STRING "false", which is what a hand-written JSON payload most easily produces — every gate
        in this repo defaults off, and a gate that a typo can open is not a gate.
        """
        arm = payload.get("arm") is True
        # An operator-named pairing, for a book the planner refuses to guess (#784). Absent means
        # the ordinary automatic sweep — the two must not be confusable in the reply.
        pair = payload.get("pair")
        result = await self.build_book_repair(arm=arm, pair=pair)
        if result.get("error"):
            return "error", str(result["error"])
        self.log.info(
            f"book_repair {cid}: armed={arm} planned={result.get('planned')} "
            f"evictions={result.get('evictions')} surplus={result.get('surplus')} "
            f"not_planned={result.get('not_planned')} "
            f"booked={result.get('booked')} refused={result.get('refused')}"
        )
        return "ok", ""

    async def _handle_order_command_idempotent(
        self, cid: str, ctype: str, payload: dict, entry_id: str
    ) -> tuple[str, str]:
        """Durable idempotency (#78) in FRONT of every order command: RESERVE the command in the ledger BEFORE
        the disarmed check + submission, so a redelivered command_id (transport) or a reused client_order_id
        (economic) is skipped even across restart (the in-memory `_seen_orders` set is lost on restart).
        Fail-CLOSED: if the ledger is unreachable, do NOT submit — reject and let the human retry deliberately."""
        if self._cmd_ledger is None:
            return self._handle_order_command(ctype, payload)  # no exec client → no durable ledger
        # Economic key applies to order-CREATING commands; cancel/modify dedup by command_id only (they target a
        # client_order_id but must not block future commands on it).
        coid = payload.get("client_order_id") if ctype in ("submit_order", "submit_bracket") else None
        # BOUND the ledger call: a slow/hung Postgres must fail-closed FAST, not let the serialization gate
        # time out and this coroutine resume later out of order (codex-flagged).
        try:
            res = await asyncio.wait_for(
                self._cmd_ledger.reserve(cid, ctype, coid, payload_hash(payload), entry_id),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — unreachable/slow → FAIL CLOSED, never submit
            _log.error("command idempotency ledger unavailable — rejecting %s: %r", cid, exc)
            return "error", "idempotency ledger unavailable — command rejected; retry with a new command id"

        if res.outcome is Reserve.DUPLICATE_CLIENT_ORDER_ID:
            return "error", "an order with this client_order_id already exists — rejected; use a new order id"
        if res.outcome is Reserve.DUPLICATE_COMMAND:
            # Mirror the PRIOR attempt's real outcome — never blanket-accept (a RESERVED-but-never-DONE
            # redelivery would be a silent lost order if shown as accepted).
            if res.hash_mismatch:
                return "error", "command_id already used with a different payload — rejected; use a new id"
            if res.existing_status == "DONE":
                return "ok", "already processed (idempotent) — not resubmitted"
            if res.existing_status == "REJECTED":
                return "error", "command was already rejected on the prior attempt"
            return "error", "prior attempt outcome unknown (interrupted mid-submit) — retry with a new command id"

        # RESERVED — first sight. Now the disarmed check + (armed) submission.
        status, error = self._handle_order_command(ctype, payload)
        try:
            await asyncio.wait_for(
                self._cmd_ledger.mark(cid, "DONE" if status == "ok" else "REJECTED", error),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
            _log.warning("command ledger mark failed for %s: %r", cid, exc)
        return status, error

    # --- liquidate a LANE, now (#922) ------------------------------------------------------------------
    #
    # MEASURED 2026-09-11: nothing in cockpit wrote LIQUIDATING (operator_only upstream, no route, no
    # command); `daily_loss -> HALT` sells nothing and a HALTED lane cannot exit; pool-exclude sells at the
    # next session; the one lane-attributed close was `owner.close_position` — per position, no sweep. This
    # is the ACT behind the #873 LIQUIDATE condition and the target of kumo-strategies' `on_emergency_exit`
    # callback (a8504dc). The notice (Telegram, label) is a separate path and never gates it.
    #
    # THE ORDER OF EFFECTS IS THE DESIGN (coordinator's review, 03:01Z):
    #   1. refuse by name, before anything: not armed, unregistered lane, missing provenance, a leash that
    #      disagrees with ONE snapshot of the lane's book;
    #   2. write LIQUIDATING and read it back BEFORE the first close — `target = 0` plus do-not-add is what
    #      stops the lane re-entering what is about to be sold; a slot can fire inside a 60 s gap;
    #   3. outside regular hours: submit NOTHING (a DAY close would expire and, per #165, write no row) but
    #      keep step 2 — the lane wakes up unable to add; the command says `deferred_to_next_open`;
    #   4. cancel the lane's WORKING orders (an entry already at the venue is not stopped by a state);
    #   5. sweep the snapshot: release the resting stop, `owner.close_position(..., DAY)` — the owner's own
    #      factory, so order and position id belong to it under NETTING — then drop the lane's claim;
    #   6. re-read the lane's book: a fill that landed mid-sweep is `remainder`, reported, never discarded;
    #   7. partial failure is a REPORT, not a rollback; the response vocabulary is SUBMITTED, never closed.
    # Idempotency keys on the command id: a replay is a duplicate; a retry after a partial is a NEW command
    # whose leash describes the remainder (#164's retry-vs-replay distinction).

    async def _handle_liquidate_lane_command(self, cid: str, payload: dict, entry_id: str) -> tuple[str, dict]:
        if self._cmd_ledger is None:
            return "refused", {"why": "idempotency ledger unavailable — command refused; retry with a new command id"}
        try:
            res = await asyncio.wait_for(
                self._cmd_ledger.reserve(cid, "liquidate_lane", None, payload_hash(payload), entry_id),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — unreachable/slow → FAIL CLOSED, never act twice
            _log.error("liquidate_lane idempotency ledger unavailable — refusing %s: %r", cid, exc)
            return "refused", {"why": "idempotency ledger unavailable — command refused; retry with a new command id"}
        if res.outcome is Reserve.DUPLICATE_COMMAND:
            return "duplicate", {"why": f"duplicate command id — prior outcome {res.existing_status or 'unknown'}; "
                                        f"a retry after a partial is a NEW command whose leash is the remainder"}
        status, detail = await self._liquidate_lane(cid, payload)
        try:
            await asyncio.wait_for(
                self._cmd_ledger.mark(cid, "DONE" if status in ("ok", "partial", "deferred_to_next_open") else "REJECTED",
                                      json.dumps(detail, default=str)),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
            _log.warning("command ledger mark failed for %s: %r", cid, exc)
        return status, detail

    async def _liquidate_lane(self, cid: str, payload: dict) -> tuple[str, dict]:
        from decimal import Decimal as _D

        def _sym(p) -> str:
            return str(p.instrument_id).rpartition(".")[0]

        if not self._orders_armed:
            return "refused", {"why": "orders disarmed (KUMO_ORDERS_ARMED off) — re-checked inside the handler; nothing submitted"}
        sid = str(payload.get("strategy_id") or "")
        invoked_by = str(payload.get("invoked_by") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not invoked_by:
            return "refused", {"why": "invoked_by is required — a liquidation without provenance is archaeology later"}
        if not reason:
            return "refused", {"why": "reason is required — the reason a lane was flattened is the first thing asked afterwards"}
        owner = self._sibling_strategies.get(sid)
        if owner is None:
            return "refused", {"why": f"{sid} is not registered on this node — nothing to route a close to; nothing submitted"}
        if payload.get("expected_positions") is None or payload.get("expected_total_qty") is None:
            return "refused", {"why": "expected_positions and expected_total_qty are required — the leash; nothing submitted"}
        # ONE snapshot serves the leash AND the sweep. A second read could satisfy the leash while the
        # sweep acted on a book that had moved — agreement-is-not-connection on the safety check.
        snapshot = list(self.cache.positions_open(strategy_id=sid))
        held = len(snapshot)
        total = sum((abs(_D(str(p.quantity))) for p in snapshot), _D(0))
        exp_n, exp_q = int(payload["expected_positions"]), _D(str(payload["expected_total_qty"]))
        if exp_n != held:
            return "refused", {"why": f"expected {exp_n} positions, the lane holds {held} — the book moved; re-read and resubmit"}
        if exp_q != total:
            return "refused", {"why": f"expected total {exp_q} shares, the lane holds {total} — the book moved; re-read and resubmit"}
        listed = [{"symbol": _sym(p), "qty": int(abs(_D(str(p.quantity)))), "side": p.side.name} for p in snapshot]
        provenance = {"invoked_by": invoked_by, "reason": reason, "command_id": cid}

        # 2. STATE FIRST, and read back — LIQUIDATING must be true before any close.
        current = await self._read_lifecycle(sid)
        outcome = await self._write_lifecycle(sid, "LIQUIDATING", reason=f"liquidate_lane by {invoked_by}: {reason}", by=invoked_by)
        if outcome != "SAVED":
            return "refused", {"why": f"could not write LIQUIDATING for {sid} (was {current}, write returned {outcome}) — nothing submitted",
                               **provenance}
        readback = await self._read_lifecycle(sid)
        if readback != "LIQUIDATING":
            return "refused", {"why": f"LIQUIDATING did not read back for {sid} (reads {readback}) — nothing submitted", **provenance}

        # 3. Outside regular hours: the lane is stopped from adding; nothing is sent to the venue.
        if not _us_market_open(self.clock.timestamp_ns()):
            detail = {"state": "deferred_to_next_open", "held": held, "submitted": 0, "cancelled_orders": 0,
                      "failed": [], "remainder": [], "positions": listed, "previous_state": current,
                      "basis": "accepted-by-nautilus-locally, not venue-confirmed", **provenance,
                      "note": "outside regular hours a DAY close would expire unfilled and write no row; LIQUIDATING is written, "
                              "the book keeps its resting stops, nothing has been sold — invoke again in hours"}
            await self._journal_liquidation(sid, detail)
            return "deferred_to_next_open", detail

        failed: list[dict] = []
        # 4. WORKING orders first — a state does not cancel an entry already at the venue.
        cancelled = 0
        for order in list(self.cache.orders_open(strategy_id=sid)):
            try:
                ok = await self._cancel_working_order(order)
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                ok, err = False, f"{type(exc).__name__}: {exc}"
            else:
                err = "cancel not confirmed"
            if ok:
                cancelled += 1
            else:
                failed.append({"order": str(order.client_order_id), "error": err})

        # 5. The sweep, per position, owner-routed, DAY, claim dropped after.
        submitted = 0
        tag = f"liquidate:{cid[:20]}"
        for p in snapshot:
            iid, sym = str(p.instrument_id), _sym(p)
            if str(p.strategy_id) != sid:  # cannot happen off a lane-scoped snapshot; refuse rather than route to the wrong owner
                failed.append({"symbol": sym, "qty": int(abs(_D(str(p.quantity)))), "error": f"position belongs to {p.strategy_id}, not {sid} — not closed"})
                continue
            reducing_side = "SELL" if p.side.name == "LONG" else "BUY"
            try:
                await self._cancel_reducing_leg(iid, sid, reducing_side)
                await self._await_reducing_orders_clear(iid, sid, reducing_side)
                owner.close_position(p, tags=[tag], time_in_force=TimeInForce.DAY)
                submitted += 1
            except Exception as exc:  # noqa: BLE001 — a REPORT, not a rollback: the rest of the book still closes
                failed.append({"symbol": sym, "qty": int(abs(_D(str(p.quantity)))), "error": f"{type(exc).__name__}: {exc}"})
                continue
            try:
                await self._drop_claim(sid, sym)
            except Exception as exc:  # noqa: BLE001 — the close went out; a claim that would not drop is named
                failed.append({"symbol": sym, "qty": 0, "error": f"claim not dropped: {type(exc).__name__}: {exc}"})

        # 6. Re-read: what the lane holds NOW that was not in the snapshot.
        seen = {str(p.instrument_id) for p in snapshot}
        remainder = [{"symbol": _sym(p), "qty": int(abs(_D(str(p.quantity))))}
                     for p in self.cache.positions_open(strategy_id=sid) if str(p.instrument_id) not in seen]

        status = "ok" if not failed and not remainder else "partial"
        detail = {"state": "held_nothing" if held == 0 else status, "held": held, "submitted": submitted,
                  "cancelled_orders": cancelled, "failed": failed, "remainder": remainder, "positions": listed,
                  "previous_state": current, "basis": "accepted-by-nautilus-locally, not venue-confirmed", **provenance}
        await self._journal_liquidation(sid, detail)
        return status, detail

    # The seams below reach the LANE's own durable state through the runner the adapter carries
    # (`session_runner=`, momentum.py / qc345.py / qc27.py / crsi_short.py). Each refuses by name when the
    # lane exposes nothing — a liquidation whose state cannot be written must not proceed to a close.

    def _lane_runner(self, sid: str):
        owner = self._sibling_strategies.get(sid)
        runner = getattr(owner, "_runner", None)
        if runner is None:
            raise RuntimeError(f"{sid} exposes no session runner — cannot reach its lifecycle/journal")
        return runner

    # THE FOUR LANE FAMILIES NAME THESE DIFFERENTLY (review, 03:35Z): the rotation gateways keep
    # `self._sm` / `self._journal`, the QC27 runner is a dataclass with a public `journal`, the CRSISHORT
    # gateway keeps `self.journal`, and every PgJournal carries its `sessionmaker`. A resolver that knew
    # one spelling would have liquidated MOMENTUM and refused TECHIVOL by name — the lane the emergency
    # is most likely about. `test_liquidate_lane.py` walks the real gateway modules to keep this list
    # honest.
    _JOURNAL_ATTRS = ("_journal", "journal")
    _SESSIONMAKER_ATTRS = ("_sm", "sm", "sessionmaker")

    def _lane_journal(self, sid: str):
        runner = self._lane_runner(sid)
        for name in self._JOURNAL_ATTRS:
            journal = getattr(runner, name, None)
            if journal is not None:
                return journal
        raise RuntimeError(f"{sid}'s runner exposes no journal under {self._JOURNAL_ATTRS}")

    def _lane_sessionmaker(self, sid: str):
        runner = self._lane_runner(sid)
        for name in self._SESSIONMAKER_ATTRS:
            sm = getattr(runner, name, None)
            if sm is not None:
                return sm
        try:
            sm = getattr(self._lane_journal(sid), "sessionmaker", None)
        except RuntimeError:
            sm = None
        if sm is None:
            raise RuntimeError(f"{sid}'s runner exposes no sessionmaker under {self._SESSIONMAKER_ATTRS} and its journal carries none")
        return sm

    async def _read_lifecycle(self, sid: str) -> str | None:
        from sqlalchemy import select
        from kumo_strategies.runtime.executor.store import StrategyState
        sm = self._lane_sessionmaker(sid)
        async with sm() as s:
            return (await s.execute(select(StrategyState.state).where(StrategyState.strategy_id == sid))).scalars().first()

    async def _write_lifecycle(self, sid: str, state: str, *, reason: str, by: str) -> str:
        from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
        from strategies.lifecycle_state import save_if_unchanged
        sm = self._lane_sessionmaker(sid)
        current = await self._read_lifecycle(sid)
        expected = State(current) if current is not None else State.TRADING  # an absent row reads TRADING (#absent-row rule)
        return await save_if_unchanged(sm, sid, Lifecycle(State(state), reason), expected)

    async def _drop_claim(self, sid: str, symbol: str) -> None:
        from kumo_strategies.runtime.executor.store import drop_claim
        await drop_claim(self._lane_journal(sid), sid, symbol)

    async def _journal_liquidation(self, sid: str, row: dict) -> None:
        try:
            journal = self._lane_journal(sid)
        except RuntimeError as exc:
            _log.error("%s: liquidate_lane outcome NOT journaled — %s: %s", sid, exc, row)
            return
        session = str(unix_nanos_to_dt(self.clock.timestamp_ns()).date())
        await journal.write("state", f"liquidate_lane {row.get('state')}: submitted {row.get('submitted')} of {row.get('held')} "
                            f"by {row.get('invoked_by')} — {row.get('reason')}", session=session, detail=row, slot="liquidate")

    async def _cancel_working_order(self, order) -> bool:
        """Cancel one of the lane's working orders through its OWNER and wait for the venue to confirm."""
        owner = self._sibling_strategies.get(str(order.strategy_id))
        if owner is None:
            return False
        owner.cancel_order(order)
        coid = order.client_order_id
        for _ in range(50):
            cached = self.cache.order(coid)
            if cached is not None and cached.is_closed:
                return True          # the venue confirmed: the order is terminal in the cache
            await asyncio.sleep(0.1)
        return False                 # unknown to the cache, or still open after 5 s — NOT confirmed

    # --- flatten (#170 first slice) + the generic manager framework it now runs on (#55) -----------------

    async def _handle_flatten_command_idempotent(self, cid: str, payload: dict, entry_id: str) -> tuple[str, str]:
        """Durable idempotency in FRONT of `_handle_flatten_command`, mirroring `_handle_order_command_idempotent`
        exactly — RESERVE `cid` under THIS command's own identity before doing anything.

        Why this exists (code review, deploy blocker): the in-memory `coid in self._seen_orders` check inside
        `_handle_flatten_command` only guards a redelivery that took the IMMEDIATE-submit branch before. A
        command accepted OFF-HOURS takes the QUEUE branch instead — it never touches `_seen_orders` — so a
        later redelivery of that SAME `cid` (crash-before-XACK, at-least-once stream replay) after the market
        has since opened would re-run `decide()` fresh, see `market_open=True`, and submit a SECOND real order
        under `FL-{cid[:20]}` — a different client_order_id than the manager's own `FL-{manager_id[:20]}`, so
        neither the DB unique index nor `_seen_orders` catches the duplicate. Reserving `cid` here, once, before
        `decide()` is ever called, closes that: the redelivery mirrors the first attempt's real outcome instead
        of re-deciding against live state a second time.
        """
        if self._cmd_ledger is None:
            return "error", "idempotency ledger unavailable — command rejected; retry with a new command id"
        try:
            res = await asyncio.wait_for(
                self._cmd_ledger.reserve(cid, "flatten_position", None, payload_hash(payload), entry_id),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — unreachable/slow → FAIL CLOSED, never act twice
            _log.error("flatten idempotency ledger unavailable — rejecting %s: %r", cid, exc)
            return "error", "idempotency ledger unavailable — command rejected; retry with a new command id"

        if res.outcome is Reserve.DUPLICATE_COMMAND:
            if res.hash_mismatch:
                return "error", "command_id already used with a different payload — rejected; use a new id"
            if res.existing_status == "DONE":
                return "ok", "already processed (idempotent) — not resubmitted"
            if res.existing_status == "REJECTED":
                return "error", "command was already rejected on the prior attempt"
            return "error", "prior attempt outcome unknown (interrupted mid-submit) — retry with a new command id"

        # RESERVED — first sight. Now the real decide()-and-act.
        status, error = await self._handle_flatten_command(cid, payload, entry_id)
        try:
            await asyncio.wait_for(
                self._cmd_ledger.mark(cid, "DONE" if status == "ok" else "REJECTED", error),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
            _log.warning("command ledger mark failed for %s: %r", cid, exc)
        return status, error

    async def _handle_flatten_command(self, cid: str, payload: dict, entry_id: str) -> tuple[str, str]:
        """Close a position, long or short (#170 first slice).

        The SIZE is decided here against the live cache, never taken from the request: an order sized when
        the screen rendered can be wrong by the time the human finishes the confirm gesture, and sending a
        stale quantity doesn't flatten — it reverses. The request carries what the human SAW so a moved
        position is rejected rather than silently turned into a different trade.

        Outside regular hours the SAME decision ATTACHES a `deferred_flatten` MANAGER instead of executing
        (`api.flatten.decide`) — see that module for why pricing into an off-hours spread was the wrong
        problem to solve. The manager framework (`api.managers`, #55) re-runs this exact decision at the next
        open through its own dispatch loop, not by calling back into this method — see `_DeferredFlatten`.
        """
        from api import flatten as fl

        if not self._orders_armed:
            return "error", "orders disarmed (KUMO_ORDERS_ARMED off) — no order submitted"
        try:
            instrument_id = payload["instrument_id"]
            strategy_id = payload.get("strategy_id", str(self.id))
            expected_side = str(payload["expected_side"]).upper()
            coid = f"FL-{cid[:20]}"  # deterministic — a redelivery collides, never doubles
            if coid in self._seen_orders:
                return "ok", ""

            # REFUSE BEFORE TOUCHING ANYTHING when the position belongs to another strategy.
            #
            # Nautilus is explicit and it is not negotiable under our OMS:
            #
            #   `position_id` PositionId('WHD.XNYS-MOMENTUM-002') is not valid for NETTING OMS;
            #   expected 'WHD.XNYS-MANUAL-001' (use HEDGING for custom position IDs)
            #
            # A strategy may only submit against its OWN position. This feed strategy is MANUAL-001, so
            # it cannot close MOMENTUM-002's position at all — and both ways of trying are worse than
            # refusing. WITHOUT a position_id the sell is attributed to MANUAL-001 and OPENS a short
            # beside the position it was meant to close: that is what happened to WHD on 2026-08-19,
            # costing $16.32 while MOMENTUM-002 kept all 136 shares. WITH one it is DENIED — after the
            # resting protection has already been cancelled, which leaves the position bare for as long
            # as the reconciler takes to notice (measured: 28s).
            #
            # So this refuses FIRST, before any cancel goes out. An operator who cannot flatten is worse
            # off than one who can, but not as badly off as one whose flatten silently opened a short or
            # stripped the stop off a live position. Closing a strategy's position has to be done BY that
            # strategy; that route does not exist yet (kumo-strategies#49) and pretending otherwise here
            # is what produced both failures.
            # ROUTE IT TO THE OWNER RATHER THAN REFUSING (kumo-strategies#49).
            #
            # Everything above is still true: this strategy cannot close another's position, and both
            # ways of trying are worse than refusing. What changed is that there is now somewhere to send
            # it. `register_strategy` records the sibling INSTANCE, and `Strategy.close_position` submits
            # from that instance — so the position id is its own and Nautilus accepts it.
            #
            # NOT `Strategy.market_exit()`, which #49 proposed: it closes ALL of that strategy's
            # positions and cancels ALL its orders. An operator flattening AEM would also dump the five
            # other MOMENTUM holdings. `close_position` is the per-position primitive and is what the
            # operator actually asked for.
            #
            # The refusal survives for the case it was written for — an owner this node cannot reach
            # (a position reconciled in from a strategy that is not registered here, EXTERNAL, or a
            # config where the strategy failed to build). Refusing is right THERE, and was only ever
            # wrong as a blanket answer.
            owner = None
            if strategy_id != str(self.id):
                owner = self._sibling_strategies.get(strategy_id)
                if owner is None:
                    return "error", (
                        f"{instrument_id} belongs to {strategy_id}, which this node does not run — "
                        f"nothing to route the close to. Nothing was cancelled and nothing was sent; "
                        f"the position keeps its protection."
                    )

            pos = self._position_for(instrument_id, strategy_id, expected_side)
            if pos is None:
                # Look again without the side filter: a flipped position must say so, not "not found".
                pos = next(
                    (
                        q
                        for q in self.cache.positions_open()
                        if str(q.instrument_id) == instrument_id and str(q.strategy_id) == strategy_id
                    ),
                    None,
                )
            if pos is None:
                return "error", "nothing to flatten — the position is already closed"

            expected_qty = payload.get("expected_qty")
            decision = fl.decide(
                position_side=pos.side.name,
                live_qty=Decimal(str(pos.quantity)),
                expected_side=expected_side,
                expected_qty=Decimal(str(expected_qty)) if expected_qty is not None else None,
                resting_reducing_qty=await self._reducing_qty_for_exit(
                    instrument_id, strategy_id, pos.side.name
                ),
                market_open=_us_market_open(self.clock.timestamp_ns()),
            )
            if decision.action == "REJECT":
                return "error", decision.reason

            if decision.cancel_resting_first and decision.action == "EXECUTE":
                # Cancel the resting exits and WAIT for the venue to confirm before closing.
                #
                # The wait is not optional. A cancel is asynchronous — Alpaca releases the reserved shares
                # only when it CONFIRMS — so a close sent immediately after the request is rejected on
                # `available: 0`, which is how five protective stops were lost on 2026-08-12. And leaving
                # them resting is the hazard the old REJECT branch named: an exit that outlives the
                # position fires against nothing and opens the opposite side.
                #
                # On timeout nothing is sent. That is the safe failure: the position keeps whatever was
                # protecting it, and the operator is told to retry rather than left holding an unprotected
                # position with a close that may or may not have gone out.
                reducing_side = OrderSide.SELL if pos.side.name == "LONG" else OrderSide.BUY
                # Cancel through Nautilus where the cache can, at the VENUE where it cannot — an order the
                # cache holds as REJECTED still reserves the shares, and `Strategy.cancel_order` refuses to
                # act on it (strategy.pyx:1649). Then wait on the RESERVATION rather than on order status:
                # Alpaca frees shares on the confirmed cancel, and those are not the same event.
                await self._cancel_reducing_leg(instrument_id, strategy_id, reducing_side)
                cleared = await self._await_reducing_orders_clear(instrument_id, strategy_id, reducing_side)
                if cleared:
                    cleared = await self._await_shares_available(
                        instrument_id, Decimal(str(decision.order.quantity))
                    )
                if not cleared:
                    # The cancels have ALREADY been requested and cannot be un-requested (codex review,
                    # Critical). If they land after this point the position is left with no protection —
                    # and returning a plain error would leave it that way with no close pending either,
                    # which is the worst of both.
                    #
                    # So the close is QUEUED instead of abandoned. The operator asked to exit; the exit
                    # stays pending and replays until it completes, rather than the request evaporating
                    # while its side effects survive.
                    status, detail = await self._handle_attach_manager(
                        cid,
                        entry_id,
                        kind="deferred_flatten",
                        account_id=str(pos.account_id),
                        instrument_id=instrument_id,
                        strategy_id=strategy_id,
                        cycle_id=payload.get("cycle_id"),
                        leash="AUTO",
                        params={"expected_side": expected_side, "expected_qty": expected_qty},
                    )
                    if status != "ok":
                        # LOUD, as its own condition (#646). By this point the cancels are in flight and
                        # cannot be recalled — an attach error returned bare ("cannot attach a manager
                        # for strategy 'MOMENTUM-002'", "ledger unavailable") reads as a routing or
                        # infrastructure problem, while the actual state on the book is a position whose
                        # protection is gone with NO close pending. That fact, not the attach detail, is
                        # what decides whether the operator must act right now.
                        return "error", (
                            f"DEGRADED — the resting protective orders for {instrument_id} were already "
                            f"cancelled (or are still cancelling) and the close could NOT be queued: "
                            f"{detail}. The position may be UNPROTECTED with no close pending — "
                            f"re-attach protection or retry the flatten NOW."
                        )
                    return status, (
                        f"cancel-confirm timed out — close queued to retry while the resting exits "
                        f"finish cancelling ({detail})"
                    )

            if decision.action == "QUEUE":
                return await self._handle_attach_manager(
                    cid,
                    entry_id,
                    kind="deferred_flatten",
                    account_id=str(pos.account_id),
                    instrument_id=instrument_id,
                    strategy_id=strategy_id,
                    cycle_id=payload.get("cycle_id"),
                    leash="AUTO",
                    params={"expected_side": expected_side, "expected_qty": expected_qty},
                )

            order = self._build_order(
                {
                    "instrument_id": instrument_id,
                    "side": decision.order.side,
                    "quantity": float(decision.order.quantity),
                    "order_type": "market",
                    "time_in_force": "day",
                    "client_order_id": coid,
                    "extended_hours": False,
                }
            )
            # THE POSITION, NOT JUST THE INSTRUMENT.
            #
            # Under NETTING the position id is `{instrument}-{strategy_id}`, and an order submitted
            # WITHOUT one is attributed to the strategy that submits it — this feed strategy, MANUAL-001.
            # So flattening a MOMENTUM-002 position opened a NEW MANUAL SHORT beside it instead of
            # closing it: on 2026-08-19 an emergency flatten of WHD sold 136 shares MANUAL-001 had never
            # held, then bought them back, leaving MOMENTUM-002's 136 untouched and -16.32 realised. The
            # operator could not emergency-exit a strategy position at all, and the UI reported the
            # flatten as still in progress twenty minutes later.
            #
            # It is also what makes `reduce_only` real: `risk/engine.pyx:425` gates the whole reduce-only
            # check behind `command.position_id is not None`, so without this the flag is decoration and
            # an oversized close can flip the position instead of closing it.
            if owner is None:
                self._submit(order, position_id=pos.id)
            else:
                # The OWNER submits. `close_position` builds the closing market order from that
                # strategy's own factory, so both the order and the position id belong to it — which is
                # the whole reason the cross-strategy attempt was rejected before.
                #
                # The release above has already run: the resting protection is cancelled and the venue
                # has confirmed the shares are free. `close_position` cancels nothing itself, so doing it
                # in the other order would be denied on `available: 0`.
                owner.close_position(pos, tags=[f"flatten:{coid}"], time_in_force=TimeInForce.DAY)
                self._seen_orders.add(coid)  # the close is OUT — record it before anything below can return early (review)
                # DROP THE LANE'S CLAIM (#923). The adapter releases claims only on fills it recognises
                # by coid PREFIX (`PROT-`/`FL-`/`TR-`) or a `session:` tag; `close_position` mints the
                # coid from the owner's own factory, so nothing upstream sees this exit and the
                # `exec_position_state` row survives at its pre-flatten qty — narrowing every neighbour
                # lane's `own_ceiling` on the symbol (#910). Same seam as `liquidate_lane`.
                try:
                    await self._drop_claim(strategy_id, instrument_id.rpartition(".")[0])
                except Exception as exc:  # noqa: BLE001 — the close went out; a claim that would not drop is REPORTED
                    return "error", f"close submitted for {instrument_id} but {strategy_id}'s claim was NOT dropped: {type(exc).__name__}: {exc}"
            self._seen_orders.add(coid)
            _log.warning(
                "flatten %s %s %s (%s) submitted against %s by %s",
                strategy_id, instrument_id, decision.order.quantity, decision.order.side, pos.id,
                "owner" if owner is not None else "self",
            )
            return "ok", ""
        except Exception as exc:  # noqa: BLE001 — a bad flatten must never break trading
            _log.error("flatten command %s failed: %r", cid, exc)
            return "error", str(exc)

    async def _reserve_attach_command(
        self,
        cid: str,
        entry_id: str,
        *,
        kind: str,
        account_id: str,
        instrument_id: str,
        strategy_id: str,
        cycle_id: str | None,
        leash: str,
        params: dict,
    ) -> tuple[str, str, str | None]:
        """The idempotency-gate HALF of `_handle_attach_manager` (codex review, High — split out so a kind
        that must place a real order as part of arming, e.g. PEAK #46, can reserve FIRST and only place that
        order once it's confirmed this isn't a redelivered/duplicate/unavailable-ledger command. Before this
        split, PEAK's arm step did its resting-order check and initial order submit BEFORE this reservation,
        so a redelivered command_id could hit its own just-placed order via `_reducing_orders_open` and
        wrongly report "a resting exit order exists", or a ledger outage could still leave a live order
        behind despite the whole point of `_cmd_ledger` being fail-closed idempotency.

        Returns `("proceed", "", ledger_cid)` when the caller should continue (do any side effects, then call
        `_commit_attach_command` with the same `ledger_cid`); `("ok", detail, None)` when this is a confirmed-
        idempotent redelivery — the caller must skip ALL side effects and return this tuple's first two
        elements as-is; `("error", detail, None)` when the caller must not proceed at all."""
        from api import managers as mg

        handler = mg.handler_for(kind)
        if handler is None:
            return "error", f"unknown manager kind {kind!r}", None, params
        if strategy_id != str(self.id):
            # A manager row is dispatched by THIS feed strategy, so by default it may only act on this
            # strategy's own positions (#178) — every order it builds goes out through the MANUAL order
            # factory and would be misattributed under NETTING.
            #
            # The one exception (#646) is a kind that submits through the OWNING strategy instance
            # (`SUBMITS_VIA_OWNER`): deferred_flatten routes its close through `owner.close_position`,
            # exactly like the immediate flatten path — and then only when that owner is actually
            # registered on this node. Before this, a LANE flatten whose cancel-confirm timed out was
            # refused HERE, after its cancels had already flown: stops gone, no queued close, and the
            # operator shown a message that read as a routing problem. The off-hours QUEUE branch failed
            # identically, so deferred flatten for lane positions structurally did not exist.
            if not getattr(handler, "SUBMITS_VIA_OWNER", False):
                return "error", f"this engine process cannot attach a manager for strategy {strategy_id!r}", None, params
            if strategy_id not in self._sibling_strategies:
                return "error", (
                    f"this node does not run strategy {strategy_id!r} — nothing to route a {kind} to"
                ), None, params
        reason = handler.validate_params(params)
        if reason:
            return "error", reason, None, params
        # PRICE PARAMS MUST BE ON THE INSTRUMENT'S TICK, CHECKED NOW (#375).
        #
        # `_canon_price` refuses rather than rounds — silently moving an operator's price is worse than
        # refusing it — but nothing applied that to MANAGER params, only to order payloads
        # (`_PRICE_FIELDS`). So a stop-and-reenter armed on MNDY with `floor_price 81.155` attached
        # cleanly, sat ARMED through the stop-out, and only failed when the rearm tried to build the
        # re-entry's protective stop from it: "price 81.155 is not a valid tick for MNDY.XNAS", terminal,
        # `rearm_count` still 0. The operator was waiting for a re-entry that had already been abandoned.
        #
        # Same shape as the QC345 empty-universe outage: accepted at write, detonates later, and the
        # failure names something unrelated to the mistake.
        #
        # ROUNDED, NOT REFUSED (Operator, 2026-08-19: "if it is subdecimal you can round. no problem").
        #
        # `_canon_price` refuses because silently moving an ORDER's price changes what the operator
        # asked the venue to do. A manager LEVEL is a different thing: 81.155 on a penny-ticked
        # instrument is not a different intention from 81.16, it is the same intention written at a
        # precision the venue has no way to express. Refusing it would trade a re-entry that dies
        # silently for a re-entry that never arms, which is not obviously better.
        #
        # `make_price` is the instrument's OWN canonicalisation, the same one every order goes through,
        # so the level stored here is exactly the level the rearm will later build its stop from — the
        # two cannot round differently. The adjustment is logged rather than applied invisibly: the
        # operator typed a number and a different one is now armed, and that is worth one line.
        try:
            params = self._canon_manager_params(handler, instrument_id, params)
        except Exception as exc:  # noqa: BLE001 — an unknown instrument is the caller's error
            return "error", f"cannot resolve {instrument_id} to canonicalise manager prices: {exc}", None, params
        if self._cmd_ledger is None:
            return "error", "idempotency ledger unavailable — command rejected; retry with a new command id", None, params

        # Own ledger key, DERIVED from `cid` but distinct from it (code review, deploy blocker): the
        # `flatten_position` command that calls this reserves `cid` itself FIRST, under its own command_type,
        # before ever reaching here — reserving the bare `cid` a second time under "attach_manager" would
        # collide with that row (CommandLedgerEntry.command_id is the sole unique key, command_type isn't part
        # of it) and wrongly hash-mismatch a legitimate first attach. `mg.attach()` still records the ORIGINAL
        # `cid` on the manager_event row below (audit trail, human-readable join) — only the ledger row's key
        # differs. `command_id` is String(64); `cid` is a uuid4().hex (32 chars) in practice, but sliced here
        # defensively so this can never violate the column length.
        ledger_cid = f"{cid}#attach"[:64]

        # Hash the FULL attach intent, not just `params` — a redelivered command_id with a different
        # instrument/strategy/cycle/leash/kind/account/client is a genuine anomaly and must hash-mismatch,
        # not silently look idempotent because only `params` happened to match (code review). Includes every
        # field `mg.attach()` persists below, not a hand-picked subset. Deliberately does NOT include any
        # kind-specific fields a caller might add to `params` AFTER this reserve (e.g. PEAK's
        # `current_trail_coid`/`tightened`/`trim_count`) — those are derived deterministically from `cid`
        # or fixed initial values, never from anything that could legitimately differ between the first
        # attempt and a redelivery of the SAME command_id.
        intent = {
            "kind": kind,
            "account_id": account_id,
            "client_id": str(self._exec_client_id),
            "instrument_id": instrument_id,
            "strategy_id": strategy_id,
            "cycle_id": cycle_id,
            "leash": leash,
            "params": params,
        }
        try:
            res = await asyncio.wait_for(
                self._cmd_ledger.reserve(ledger_cid, "attach_manager", None, payload_hash(intent), entry_id),
                timeout=_LEDGER_TIMEOUT_SECS,
            )
        except Exception as exc:  # noqa: BLE001 — unreachable/slow → FAIL CLOSED, never attach
            _log.error("manager attach ledger unavailable — rejecting %s: %r", cid, exc)
            return "error", "idempotency ledger unavailable — command rejected; retry with a new command id", None, params

        if res.outcome is Reserve.DUPLICATE_COMMAND:
            if res.hash_mismatch:
                return "error", "command_id already used with a different payload — rejected; use a new id", None, params
            if res.existing_status == "DONE":
                return "ok", f"{kind} already queued (idempotent)", None, params
            if res.existing_status == "REJECTED":
                return "error", "this attach was already rejected on the prior attempt", None, params
            return "error", "prior attach outcome unknown (interrupted mid-attach) — retry with a new command id", None, params

        # THE CANONICALISED PARAMS TRAVEL BACK (#401). They used to be assigned to a local here and
        # discarded when this method returned — `make_price` ran, its result was hashed, and the
        # CALLER then persisted its own off-tick dict. A mechanism computed and thrown away, which is
        # the dead-mechanism half of CLAUDE.md's verification-by-disagreement rule: identical when it
        # should differ. Returned rather than mutated in place because the caller's dict is also what
        # the idempotency hash was taken over, and mutating it under them would change that fact.
        return "proceed", "", ledger_cid, params

    async def _commit_attach_command(
        self,
        cid: str,
        ledger_cid: str,
        *,
        kind: str,
        account_id: str,
        instrument_id: str,
        strategy_id: str,
        cycle_id: str | None,
        leash: str,
        params: dict,
    ) -> tuple[str, str]:
        """The DB-attach HALF of `_handle_attach_manager` (codex review, High) — only ever called after
        `_reserve_attach_command` returned `"proceed"` for this exact `ledger_cid`. `params` here may be the
        MUTATED version (e.g. PEAK's `current_trail_coid` filled in after placing its initial order) — that's
        fine, this only persists it, it never re-checks it against the reserve-time hash."""
        from api import managers as mg

        try:
            from api.db.engine import session_factory

            manager_id = str(uuid.uuid4())
            async with session_factory() as session:
                await mg.attach(
                    session,
                    manager_id=manager_id,
                    kind=kind,
                    account_id=account_id,
                    client_id=str(self._exec_client_id),
                    instrument_id=instrument_id,
                    strategy_id=strategy_id,
                    cycle_id=cycle_id,
                    leash=leash,
                    params=params,
                    command_id=cid,
                )
        except Exception as exc:  # noqa: BLE001
            try:
                await asyncio.wait_for(
                    self._cmd_ledger.mark(ledger_cid, "REJECTED", str(exc)[:250]), timeout=_LEDGER_TIMEOUT_SECS
                )
            except Exception as mark_exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
                _log.warning("command ledger mark failed for %s: %r", cid, mark_exc)
            _log.error("manager attach %s failed: %r", cid, exc)
            return "error", str(exc)

        # attach() committed — the manager is durably live. mark(DONE) below is audit-only from here on;
        # its failure must NOT flip this into a reported rejection (code review: that would misreport a
        # successful attach as an error, same class of bug the order-command path already guards against).
        # Timeout-wrapped like the order path (code review) — a hung ledger must not hang this ack too.
        try:
            await asyncio.wait_for(self._cmd_ledger.mark(ledger_cid, "DONE"), timeout=_LEDGER_TIMEOUT_SECS)
        except Exception as exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
            _log.warning("command ledger mark failed for %s: %r", cid, exc)
        _log.warning("manager %s (%s) attached for %s %s", manager_id, kind, strategy_id, instrument_id)
        return "ok", f"{kind} queued for the next regular-hours open"

    async def _reject_reserved_ledger_entry(self, ledger_cid: str, reason: str) -> None:
        """Mark a reserved-but-not-yet-committed ledger entry REJECTED (codex review, Medium) — for a
        caller that reserved via `_reserve_attach_command` (outcome `"proceed"`) but then failed a kind-
        specific pre-commit check BEFORE ever calling `_commit_attach_command` (PEAK's resting-order refusal
        and initial-order-submit failure, specifically). Without this, the ledger row is left stuck at
        RESERVED — a same-`command_id` redelivery would then see `existing_status` not in `("DONE",
        "REJECTED")` and report "prior attach outcome unknown (interrupted mid-attach)" instead of correctly
        mirroring the original rejection. Audit-only, like every other `mark()` call in this file — its own
        failure must never change the outcome already being returned to the caller."""
        try:
            await asyncio.wait_for(
                self._cmd_ledger.mark(ledger_cid, "REJECTED", reason[:250]), timeout=_LEDGER_TIMEOUT_SECS
            )
        except Exception as exc:  # noqa: BLE001 — mark is audit-only; reserve already prevented double-send
            _log.warning("command ledger mark failed for %s: %r", ledger_cid, exc)

    async def _handle_attach_manager(
        self,
        cid: str,
        entry_id: str,
        *,
        kind: str,
        account_id: str,
        instrument_id: str,
        strategy_id: str,
        cycle_id: str | None,
        leash: str,
        params: dict,
    ) -> tuple[str, str]:
        """Attach a manager instance (#55) — generic across every kind. Gated through the SAME
        `CommandLedgerStore` (#78) order commands use, not a second idempotency mechanism: a redelivered
        `command_id` short-circuits to the prior outcome instead of attaching a second manager.

        Refuses any `strategy_id` that isn't THIS running Strategy's own id, here — once, centrally — rather
        than trusting every future manager kind to remember the guard (code review; #178: the order factory
        this dispatch ultimately submits through is bound to `self.id`, and real cross-strategy dispatch is
        the coordinator's job, #72, not built).

        Thin wrapper over `_reserve_attach_command` + `_commit_attach_command` (codex review, High) — kept as
        a single call for callers (flatten's QUEUE branch, every non-PEAK manager kind) that have no side
        effect to sequence between reserve and commit. PEAK's arm step calls the two halves directly instead,
        so it can place its initial order strictly between them.
        """
        # `params` is REBOUND to the canonicalised copy (#401) — see `_reserve_attach_command`.
        outcome, detail, ledger_cid, params = await self._reserve_attach_command(
            cid, entry_id, kind=kind, account_id=account_id, instrument_id=instrument_id,
            strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
        )
        if outcome != "proceed":
            return outcome, detail
        return await self._commit_attach_command(
            cid, ledger_cid, kind=kind, account_id=account_id, instrument_id=instrument_id,
            strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
        )

    async def _handle_attach_manager_command(self, cid: str, payload: dict, entry_id: str) -> tuple[str, str]:
        """`attach_manager` command (#47) — the first public, UI-reachable caller of `_handle_attach_manager`
        (previously only reachable internally from the flatten command's QUEUE branch). Resolves `account_id`
        from the LIVE position (the UI has no way to know Nautilus's internal account id) and gates on
        `_orders_armed`, same as every other command that can eventually place a real order — arming IS the
        human gate for automation (Operator, 2026-08-02: no per-action confirm step once armed)."""
        if not self._orders_armed:
            return "error", "orders disarmed (KUMO_ORDERS_ARMED off) — no manager attached"
        try:
            kind = payload["kind"]
            instrument_id = payload["instrument_id"]
            strategy_id = payload.get("strategy_id", str(self.id))
            params = payload["params"]
            expected_side = str(params["expected_side"]).upper()
        except KeyError as exc:
            return "error", f"missing required field {exc}"

        pos = self._position_for(instrument_id, strategy_id, expected_side)
        if pos is None:
            return "error", "no matching HELD position to attach this manager to"

        # Bind `qty` to the LIVE position, never whatever the UI sent (codex review, #47): a stale render or
        # a crafted payload arming e.g. qty=100 against an actual 10-share position would otherwise size a
        # FUTURE re-entry order off that untrusted value. Any kind whose params carry a `qty` meaning "this
        # many shares of the position being attached to" gets it overridden here — not a per-kind special
        # case, a blanket rule for this generic, UI-reachable entrypoint.
        if "qty" in params:
            params = {**params, "qty": float(pos.quantity)}

        account_id = str(pos.account_id)
        cycle_id = payload.get("cycle_id")
        leash = payload.get("leash", "AUTO")

        # Refuse a leash this engine does not honour, rather than accepting it and ignoring it (codex
        # review, #255). `mg.claim` is an unconditional ARMED -> APPLYING update with no leash predicate,
        # so a row armed CONFIRM or ALERT is claimed and applied exactly like AUTO — the reducer's
        # PROPOSED branch is unreachable from the dispatch loop. AUTO-only is the deliberate model (arming
        # IS the gate, there is no per-action confirmation), so the defect is not the missing gate: it is
        # that the API advertises two settings that silently do nothing, which is the most dangerous kind
        # of safety control. Say no instead.
        leash_error = _validate_leash(leash)
        if leash_error:
            return "error", leash_error

        if kind == "pyramid_watch":
            # PYRAMID (#38) arm step — lighter than PEAK's: Pyramid works WITH the position's EXISTING
            # bracket stop rather than placing its own, so there's no order to roll back. But codex review
            # (Medium): still reserve the idempotency ledger FIRST, before doing any live reads — a
            # redelivered command_id must be recognized as a duplicate/rejected against the ORIGINAL
            # snapshot, not re-derive a possibly DIFFERENT one (e.g. the bracket stop this call would find
            # may no longer be the one the first successful attempt found, since a prior add can have
            # replaced it) and return a confusingly different outcome.
            outcome, detail, ledger_cid, params = await self._reserve_attach_command(  # rebound (#401)
                cid, entry_id, kind=kind, account_id=account_id, instrument_id=instrument_id,
                strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
            )
            if outcome != "proceed":
                return outcome, detail

            driver_id = str(params["driver_instrument_id"])
            if self._last_price_for(driver_id) is None:
                detail = (
                    f"driver instrument {driver_id!r} has no live price yet — add it to the watchlist "
                    "before arming Pyramid"
                )
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            reducing_side = OrderSide.SELL if expected_side == "LONG" else OrderSide.BUY
            bracket_stop = self._bracket_protective_stop_open(instrument_id, strategy_id, reducing_side)
            # Widened for the GUARD only (#303) — R below still needs a real trigger price, which a
            # trailing stop does not carry, so PYRAMID still refuses with its own honest reason.
            identified = self._identified_protective_stop_open(instrument_id, strategy_id, reducing_side)
            resting = self._reducing_orders_open(instrument_id, strategy_id, reducing_side)
            # codex review (High) — same guard PEAK's arm step already has: require EXACTLY ONE resting
            # reducing order and that it IS the identified bracket stop. Without this, a second, unidentified
            # resting order could survive arm untouched while Pyramid tracks only the bracket stop's coid —
            # a stale exit order left live beside whatever the trail later becomes.
            if not (len(resting) == 0 or (len(resting) == 1 and identified is not None and resting[0] is identified)):
                detail = (
                    "a resting exit order exists that isn't identifiable as this position's sole bracket "
                    "stop — cancel it manually before arming Pyramid"
                )
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            if bracket_stop is None:
                detail = (
                    "no identifiable bracket-tagged protective stop on this position — Pyramid needs one "
                    "to compute R (risk per share); arm a stop first"
                )
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            if not bracket_stop.has_trigger_price:
                detail = "the identified protective stop has no trigger price — cannot compute R"
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            initial_risk_per_share = float(pos.avg_px_open) - float(bracket_stop.trigger_price)
            if initial_risk_per_share <= 0:
                detail = "computed non-positive R from the protective stop — refusing to arm"
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            params = {
                **params,
                "initial_qty": float(pos.quantity),
                "rung_count": 0,
                "current_trail_coid": str(bracket_stop.client_order_id),
                "initial_risk_per_share": initial_risk_per_share,
            }
            return await self._commit_attach_command(
                cid, ledger_cid, kind=kind, account_id=account_id, instrument_id=instrument_id,
                strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
            )

        if kind != "peak_watch":
            return await self._handle_attach_manager(
                cid, entry_id, kind=kind, account_id=account_id, instrument_id=instrument_id,
                strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
            )

        # PEAK (#46) arm step — kind-specific, not a generic case: unlike #47 (watch-only, needs a stop to
        # ALREADY exist), PEAK actively places its OWN adaptive trailing stop and must not let it race a
        # pre-existing one (two live protective stops on one position is a real hazard — whichever fires
        # first can leave the other resting on a now-smaller/flat position, or fire afterward and open an
        # unintended opposite position). Auto-cancels the existing stop ONLY when it's reliably identifiable
        # as this position's own bracket-tagged protective stop (same discrimination #47's
        # `_closing_order_for` already uses, applied to OPEN orders via `_bracket_protective_stop_open`);
        # refuses to arm if a resting reducing-side order exists that ISN'T identifiable that way, rather
        # than guess which one to cancel.
        #
        # codex review (High): reserve the idempotency ledger FIRST, before checking resting orders or
        # placing anything — a redelivered command_id must be recognized as a duplicate/rejected BEFORE any
        # order side effect, not after. `handler.validate_params` (called inside the reserve) covers full
        # param validation too, so no separate pre-check is needed here.
        outcome, detail, ledger_cid, params = await self._reserve_attach_command(  # rebound (#401)
            cid, entry_id, kind=kind, account_id=account_id, instrument_id=instrument_id,
            strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
        )
        if outcome != "proceed":
            return outcome, detail

        reducing_side = OrderSide.SELL if expected_side == "LONG" else OrderSide.BUY
        # Any protective stop WE placed counts as identifiable (#303) — a bracket leg, the #239
        # backstop's trailing stop, or a previous manager trail. PEAK cancels it and rests its own, so a
        # backstop stop is a perfectly good thing to replace; refusing to arm over one told the operator to
        # "cancel it manually", which would leave the position naked to satisfy a guard.
        bracket_stop = self._identified_protective_stop_open(instrument_id, strategy_id, reducing_side)
        resting = self._reducing_orders_open(instrument_id, strategy_id, reducing_side)
        # codex review (Critical): must require EXACTLY ONE resting reducing order and that it IS the
        # identified bracket stop — not just "a bracket stop is findable somewhere". The old check let a
        # bracket stop plus a SECOND, unidentified resting order both slip through as long as the bracket
        # stop was found; only the bracket stop got canceled, leaving the second one live alongside PEAK's
        # own fresh trail — the exact two-live-protective-stops race this guard exists to prevent.
        # #265 — arming over a bracket must WORK, not refuse (the operator's call, and his "I want PEAK to work").
        #
        # What this replaced demanded EXACTLY ONE resting order and that it BE the identified stop. A
        # bracket presents two reducing legs — the protective stop and the take-profit — so every
        # bracketed position refused, telling the operator to "cancel it manually". NBIS on 2026-08-15 was
        # the live case: 29 shares, stop at 260 and target at 310.61, PEAK unusable.
        #
        # Now: clear everything we can ACCOUNT for (this position's own protection, and legs of its own
        # bracket), and still refuse on anything else. An unrelated resting sell is exactly the case where
        # guessing strips protection.
        if resting and not self._clearable_for_arm(resting, bracket_stop):
            detail = (
                "a resting exit order exists that isn't this position's own protective stop or a leg of "
                "its own bracket — cancel it manually before arming PEAK"
            )
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail

        # THE SAME GUARD, ASKED OF THE BROKER — because the cache is not the only thing holding shares.
        #
        # `resting` above is `cache.orders_open()`, which silently omits any order stuck in a terminal
        # state while it still rests at the venue (see `_venue_reducing_orders`). On 2026-08-17 that made
        # NBIS look bare to this guard: the arm was permitted, and PEAK's own trail was then rejected with
        # `insufficient qty available (requested: 16, available: 0)` by the very stop PEAK had placed
        # earlier. The manager was fighting itself, and no guard here could see it.
        #
        # Unreadable REFUSES. An arm cancels the position's existing protection, and doing that without
        # knowing what is actually resting is the move that strips a live book.
        hidden = await self._venue_only_reducing_rows(instrument_id, strategy_id, reducing_side)
        if hidden is None:
            detail = "could not read the broker's open orders — refusing to arm rather than cancel blind"
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail
        unaccountable = [
            str(r.get("client_order_id") or r.get("id") or "?")
            for r in hidden
            if not str(r.get("client_order_id") or "").startswith(_OURS)
        ]
        if unaccountable:
            detail = (
                f"the broker holds resting exit order(s) this system did not place ({', '.join(unaccountable)}) "
                "— cancel them manually before arming PEAK"
            )
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail

        # Read the cycle's spent trim budget BEFORE anything is placed (#266, codex review Critical).
        #
        # `trim_max` bounds the POSITION — the reasoning was transaction cost, "not tiny repeated
        # nibbles" — but it was only compared against a chain-local `trim_count` that restarted at 0 on
        # every fresh arm. OKTA spent both trims (58 -> 6), was re-armed, and spent two more within 34
        # seconds (6 -> 2) at a HIGHER price. A re-arm inherits what the cycle already used.
        #
        # Placement matters as much as the value: done after the trail was submitted and the bracket stop
        # cancelled, a throw here would leave a live unmanaged trail, no bracket, and a stuck ledger row —
        # because this sits outside the rollback that `_commit_attach_command`'s error path performs.
        # Nothing has been placed yet at this point, so a failure is a clean refusal.
        from api import managers as mg
        from api.db.engine import session_factory

        try:
            async with session_factory() as session:
                spent = await mg.trims_spent_on_cycle(session, kind, instrument_id, strategy_id, cycle_id)
        except Exception as exc:  # noqa: BLE001 — refuse rather than arm with an unknown budget
            detail = f"could not read this cycle's trim history: {exc}"
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail

        # Widths scaled to THIS symbol's volatility, not the flat percentage the caller sent (#288).
        # FIG carried a PEAK trail of 2.5% against a 7.94% ATR — 0.31x ATR, firing on an ordinary session
        # rather than on anything going wrong, while the same 2.5% is 1.52x ATR on VFLO. Derived through
        # `peak_trail_bps`, which is built on the same `trail_width` the #239 backstop uses, so there is
        # ONE answer to "how wide should a stop on this symbol be" — these two already disagreed by 5x.
        #
        # If the ATR cannot be measured, or the width would hit the ceiling, the caller's own value stands
        # rather than blocking the arm: PEAK still protects better than nothing, and a refusal here would
        # be a silent behaviour change from what the operator asked for.
        scaled = self._peak_scaled_widths(instrument_id, params)
        if scaled is not None:
            # Captured BEFORE the reassignment below — reading `params` after it would report the new
            # widths as what the caller sent, which is the log lying about its own change.
            sent_wide, sent_tight = params.get("trail_wide_bps"), params.get("trail_tight_bps")
            params = {**params, "trail_wide_bps": scaled.wide_bps, "trail_tight_bps": scaled.tight_bps}
            _log.info(
                "peak %s: trail scaled to ATR %.2f%% — wide %dbps, tight %dbps (caller sent %s/%s)",
                instrument_id, scaled.atr_pct, scaled.wide_bps, scaled.tight_bps, sent_wide, sent_tight,
            )

        # ORDERING, and why it is not the safer-looking place-then-cancel below.
        #
        # Alpaca reserves shares against ANY resting sell and frees them only when the cancel is CONFIRMED
        # — measured, #245/#252, and the reason `_await_reducing_orders_clear` exists. So with 29 shares
        # held against a resting stop, submitting PEAK's 29-share trail FIRST is rejected on `available: 0`
        # before it can cancel anything. Place-then-cancel only ever worked because every PEAK arm in
        # production so far had ZERO resting orders (checked against the account's order history: the
        # 2026-08-13 NBIS arm was a bare position, nothing resting).
        #
        # So when anything rests, it must go first. That opens a window with no protection at the venue,
        # which is a real cost and is why this is bounded: the wait is confirmed rather than assumed, and a
        # timeout ABORTS instead of placing into an unknown reservation state. Arming is a deliberate
        # operator action in regular hours, not an automatic path — nothing here fires on its own.
        # Every failure AFTER this point leaves the position with nothing resting, so each one must say so.
        # One phrasing, used by all three exits — the alternative is three messages that drift, and the two
        # that drifted first were the ones that did not mention it at all.
        cleared_count = len(resting) + len(hidden)

        async def _naked(reason: str) -> str:
            return await self._protection_status_note(
                # `expected_side`, not `pos.side` — it is already in scope, already validated against the
                # live position above, and is what every other side-derivation in this handler uses.
                reason, cleared_count, instrument_id, strategy_id, expected_side
            )

        if resting or hidden:
            # Wrapped because ANY throw between the first cancel and the replacement leaves the position
            # bare, and an unwrapped one reaches the generic handler which reports `str(exc)` with no hint
            # that protection was removed (codex review, Critical).
            try:
                # Cache-open orders go out through Nautilus; cache-terminal ones through the venue. Then
                # wait on the RESERVATION, not on order status — PEAK's replacement trail claims the whole
                # position, so the shares must actually be free before it is sent.
                await self._cancel_reducing_leg(instrument_id, strategy_id, reducing_side)
                cleared = await self._await_reducing_orders_clear(instrument_id, strategy_id, reducing_side)
                if cleared:
                    cleared = await self._await_shares_available(
                        instrument_id, Decimal(str(pos.quantity))
                    )
            except Exception as exc:  # noqa: BLE001
                detail = await _naked(f"failed while clearing the resting exit orders: {exc!r}")
                _log.error("peak %s: %s", instrument_id, detail)
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail
            if not cleared:
                detail = await _naked("the venue did not confirm the cancel within the timeout, so nothing was placed")
                _log.error("peak %s: %s", instrument_id, detail)
                await self._reject_reserved_ledger_entry(ledger_cid, detail)
                return "error", detail

        initial_coid = f"PKW-{cid[:20]}"
        try:
            placed_order = self._submit_trailing_stop(
                instrument_id=instrument_id,
                side="SELL" if expected_side == "LONG" else "BUY",
                quantity=float(pos.quantity),
                trail_bps=float(params["trail_wide_bps"]),
                coid=initial_coid,
                manager_id=None,  # not known yet — this order predates the manager row it'll be tracked by
            )
        except Exception as exc:  # noqa: BLE001
            # This used to read "a bad initial submit must not proceed to cancel anything" — true while the
            # cancel came AFTER the submit. Reordering for the share reservation (#265) falsified it: the
            # old stop is already gone by the time we get here, so a failed submit leaves the position bare.
            detail = await _naked(f"could not place the initial trailing stop: {exc}")
            _log.error("peak %s: %s", instrument_id, detail)
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail
        # The old stop is already gone — cancelled above, and the cancel CONFIRMED before we placed. The
        # cancel that used to sit here would now target an order that no longer exists.
        #
        # CONFIRM the replacement is actually resting before writing a manager row that claims it is
        # (codex review, Critical). `_submit_trailing_stop` is fire-and-forget; under place-then-cancel a
        # rejected trail was harmless because the old stop stayed, and reordering removed that. Without
        # this the arm reports success over a naked position.
        if not await self._await_protection_resting(initial_coid):
            detail = await _naked(
                f"the trailing stop {initial_coid} was submitted but the venue never reported it working"
            )
            _log.error("peak %s: %s", instrument_id, detail)
            await self._reject_reserved_ledger_entry(ledger_cid, detail)
            return "error", detail
        params = {
            **params, "current_trail_coid": initial_coid, "tightened": False, "trim_count": spent,
        }

        outcome, detail = await self._commit_attach_command(
            cid, ledger_cid, kind=kind, account_id=account_id, instrument_id=instrument_id,
            strategy_id=strategy_id, cycle_id=cycle_id, leash=leash, params=params,
        )
        # codex review (High #4): the DB-attach can still fail (a DB error) AFTER PEAK's initial trailing
        # stop already went out above — cancel it (using the OBJECT `_submit_trailing_stop` returned, not a
        # fresh `_lookup_order` — codex review: Nautilus's `submit_order` doesn't guarantee the cache is
        # populated synchronously, so re-deriving the order via cache lookup right after submit isn't safe)
        # rather than leave a live, unmanaged order with no manager row tracking it.
        if outcome == "error":
            # Wrapped for the same reason the clearing loop is (codex review): a throw here escapes to the
            # generic handler, which reports `str(exc)` with no hint that both stops are gone. The rollback
            # is best-effort — failing to cancel the replacement is far less bad than failing to SAY the
            # position may be bare.
            try:
                self._cancel(placed_order)
            except Exception as exc:  # noqa: BLE001
                _log.error("peak %s: rollback cancel of %s failed: %r", instrument_id, initial_coid, exc)
            # Both stops are now gone: the pre-existing one was cancelled before the submit (#265), and
            # this rollback removes the replacement. That is the same bare position as the two exits above,
            # so it carries the same warning rather than reading as a tidy rollback.
            detail = await _naked(f"{detail} (trailing stop {initial_coid} placed then canceled — attach failed after submit)")
            _log.error("peak %s: %s", instrument_id, detail)
        return outcome, detail

    async def _handle_cancel_manager_command(self, cid: str, payload: dict, entry_id: str) -> tuple[str, str]:
        """`cancel_manager` — the toggle's OFF path (#47). Uses `mg.cancel_if_cancelable` — a single atomic
        UPDATE...WHERE (codex review: a read-then-write here races a concurrent dispatch tick's `claim()`;
        a stale read could cancel a row that's already APPLYING, overwriting a real outcome with a
        misleading CANCELLED). No order side effect from cancellation itself, so this skips the full
        CommandLedgerStore machinery every order-creating command goes through — a duplicate cancel is a
        harmless no-op once guarded atomically, not something that needs ledger dedup."""
        from api import managers as mg
        from api.db.engine import session_factory

        manager_id = payload.get("manager_id")
        if not manager_id:
            return "error", "manager_id required"

        async with session_factory() as session:
            # Cancel the WHOLE chain for this kind+position, not just the id the UI named. A chaining
            # kind (`_PeakWatch` hands off to a fresh successor after every trim/tighten) is one logical
            # "PEAK is on" spread across many manager_ids, and the UI can only ever name whichever row it
            # considered active when it rendered. Cancelling that one left the successor armed and the
            # toggle sprang back to ON — the reported "on works, off doesn't".
            #
            # The row is read FIRST so the cancel is scoped to its position: cancelling by kind alone
            # would reach every position holding that manager kind.
            target = await mg.get(session, manager_id)
            if target is None:
                return "ok", "no such manager — nothing to cancel"
            n = await mg.cancel_chain(session, target.kind, target.instrument_id, target.strategy_id, target.cycle_id)

        if n == 0:
            # Everything was already APPLYING or terminal. The chain-cancel MARK still matters: an apply
            # in flight consults `chain_cancelled` before handing off, so OFF takes effect at the handoff
            # even when no row was cancelable at this instant.
            return "ok", "already applying or terminal — chain will stop at the next handoff"
        return "ok", f"cancelled {n} {target.kind} manager(s) on {target.instrument_id}"

    def _on_compaction(self, _event=None) -> None:
        """Compact finished log files and drop compacted ones past the window.

        THROUGH `Observations`, not a bare try/except. A compaction that quietly stops is a disk that
        fills and a retention policy that silently lapses — the same silence #758 is about — so a
        failure has to become standing state rather than a skipped tick nobody sees.

        The work is offloaded to the executor: it is filesystem-bound and this callback runs on the
        engine's clock thread, where a slow disk would stall the timers that place orders.
        """
        from api.log_compaction import run_compaction

        directory = os.environ.get("KUMO_LOG_DIR")
        if not directory:
            return
        keep_days = float(os.environ.get("KUMO_LOG_KEEP_DAYS", 10))

        def _work() -> None:
            self._log_compaction = self._observations.run(
                "log_compaction",
                run_compaction,
                directory,
                keep_days=keep_days,
                ts_ns=self._safe_now(),
            )

        if self._loop is not None:
            self._loop.run_in_executor(None, _work)
        else:
            _work()

    def _on_manager_tick(self, _event=None) -> None:
        """Timer hook (steady state, every 30s) → dispatch every registered manager kind.

        Does NOT reconcile APPLYING rows — that is a startup-only concern (`_startup_manager_check`).
        Reconciling on every tick was tried for the flatten-specific predecessor of this framework and was
        itself a bug (code review, 2026-07-29): a row legitimately still being processed by an earlier,
        slower-running dispatch call is ALSO sitting in APPLYING, and a later tick's reconcile would revert it
        out from under that still-live call — recreating the exact double-submit race the atomic claim exists
        to prevent.
        """
        if self._loop is not None:
            self._spawn(self._dispatch_all_managers(), "manager dispatch (timer tick)")

    # --- #873 phase 1: the market-aware poll (observe, surface, notify; nothing acts) -----------------

    def _init_market_aware(self, *, dwell: int, poll_secs: int) -> None:
        if int(dwell) < 1 or int(poll_secs) < 1:
            raise ValueError(f"market-aware knobs must be >= 1: dwell={dwell!r} poll_secs={poll_secs!r}")
        self._market_aware_dwell = int(dwell)
        self._market_aware_poll_secs = int(poll_secs)
        self._market_aware_readings: dict[str, object] = {
            sid: None for sid in getattr(self, "_sibling_strategies", {})}
        self._market_aware_polled_ns: int | None = None
        self._market_aware_emit_failures: int = 0
        self._market_aware_journal_absent: set[str] = set()
        #: THREE STATES in the detector (#955): a lane is CHECKED-AND-ABSENT, CHECKED-AND-PRESENT, or
        #: NOT YET CHECKED. Absence from `journal_absent` must mean "has a journal", never "was not
        #: asked" — which is what it meant while the set was populated only inside the emit path, for
        #: lanes that happened to have events. Evaluated for every registered lane on every poll.
        self._market_aware_journal_checked: set[str] = set()
        #: FOURTH condition, disjoint from `journal_absent` (#958 cross-review): the resolver RAISED.
        #: A lane whose `session_journal()` raises is BROKEN — a wiring defect in the lane — not a
        #: lane without a journal (a pin without the contract, or a resolver answering None). Different
        #: facts, different fixes; a warning line alone is the shape that ran inert for two deploys
        #: on the notifier. {sid: "ExcType: message"}, re-evaluated every poll.
        self._market_aware_journal_broken: dict[str, str] = {}
        self._market_aware_contract: dict = contract_state()
        self._market_aware_notifier_obj = None

    def _market_aware_settings(self) -> tuple[int, int]:
        """The two knobs from the strategies domain; the schema enforces the minimums. A read that
        fails leaves the schema defaults AND is logged — it must not stop the poll from existing."""
        try:
            from api.settings.store import resolve
            values = resolve("strategies")
            return int(values.get("MARKET_AWARE_DWELL_POLLS", 3)), int(values.get("MARKET_AWARE_POLL_SECS", 60))
        except Exception as exc:  # noqa: BLE001 — logged; the defaults are the schema's own
            _log.warning("market-aware settings unreadable (%r) — using dwell 3 / 60 s", exc)
            return 3, 60

    def _market_aware_notifier(self):
        """The engine-side pager, built once, or None when alerting is unavailable — then every emit
        COUNTS as a failure on the frame rather than vanishing."""
        if self._market_aware_notifier_obj is None:
            try:
                from api.notify import Notifier
                self._market_aware_notifier_obj = Notifier()
            except Exception as exc:  # noqa: BLE001
                _log.warning("market-aware notifier unavailable (%r)", exc)
                return None
        return self._market_aware_notifier_obj

    def _market_aware_journal_of(self, lane):
        """The lane's journal, resolved by the LANE — `session_journal()` from kumo-strategies'
        contract (`runtime/nautilus/contract.py`), written for exactly this defect (kumo-cockpit#587):
        a Nautilus lane holds its journal on its runner, under names that differ per adapter, and it
        reports a wiring defect once by name. Cockpit hand-rolled a second derivation here with two
        hardcoded attribute names, found nothing on any production lane, and every market-aware event
        PAGED BUT DID NOT JOURNAL (#955). NO local name chain and no third name: a third name IS the
        drift. A lane without the resolver, or whose resolver raises, has no journal to write to —
        `_market_aware_journal_resolve` names WHICH on the frame, never guessed."""
        journal, _broken = self._market_aware_journal_resolve(lane)
        return journal

    def _market_aware_journal_resolve(self, lane) -> tuple[object | None, str | None]:
        """(journal, None) when the lane answers; (None, None) when it has no resolver or the resolver
        answers None — ABSENT; (None, "ExcType: msg") when the resolver RAISES — BROKEN. The two None
        cases are different conditions with different fixes and are kept apart on the frame
        (`journal_absent` vs `journal_broken`, #958). The warning here is the DIAGNOSIS; the frame
        field is the ALARM — a log line alone is what left two notifier detectors inert for two
        deploys, so neither may be removed as redundant with the other."""
        resolve = getattr(lane, "session_journal", None)
        if not callable(resolve):
            return None, None
        try:
            return resolve(), None
        except Exception as exc:  # noqa: BLE001 — recorded on the frame as `journal_broken`, per poll
            _log.warning("%s: session_journal() raised (%r) — market-aware rows will not be written",
                         getattr(lane, "id", lane), exc)
            return None, f"{type(exc).__name__}: {exc}"

    def _on_market_aware(self, _event) -> None:
        """Timer → ask every registered lane on THIS thread (the hooks read the lane's own state, as
        `next_fire_by_lane` does), through the observation registry PER LANE so a lane whose poll
        raises is recorded and the others still run. Only the EMITS (journal, pager) hop to the loop."""
        ts = self.clock.timestamp_ns()
        self._market_aware_contract = contract_state()
        for sid, lane in list(self._sibling_strategies.items()):
            # The journal detector runs for EVERY lane on EVERY poll (#955), events or not. Three
            # outcomes per checked lane — present, absent, BROKEN — each exclusive of the other two.
            journal, broken = self._market_aware_journal_resolve(lane)
            if broken is not None:
                self._market_aware_journal_broken[sid] = broken
                self._market_aware_journal_absent.discard(sid)
            elif journal is None:
                self._market_aware_journal_absent.add(sid)
                self._market_aware_journal_broken.pop(sid, None)
            else:
                self._market_aware_journal_absent.discard(sid)
                self._market_aware_journal_broken.pop(sid, None)
            self._market_aware_journal_checked.add(sid)
            out = self._observations.run(f"market_aware:{sid}", self._poll_market_aware, sid, lane, ts, ts_ns=ts)
            if out is None:
                continue
            readings, events = out
            self._market_aware_readings[sid] = readings
            if events:
                if self._loop is None:
                    self._market_aware_emit_failures += 1
                    _log.warning("%s: %d market-aware event(s) with no loop to emit on", sid, len(events))
                    continue
                self._spawn(self._emit_market_aware(sid, lane, events, ts), f"market-aware emit {sid}")
        self._market_aware_polled_ns = ts

    def _poll_market_aware(self, sid: str, lane, ts_ns: int):
        return poll_lane(lane, self._market_aware_readings.get(sid), dwell=self._market_aware_dwell, ts_ns=ts_ns)

    async def _emit_market_aware(self, sid: str, lane, events, ts_ns: int) -> None:
        """One journal row and one page PER EVENT — never per poll. Each failure is COUNTED on the
        frame and logged; none stops the next event or the next lane."""
        journal = self._market_aware_journal_of(lane)
        notifier = self._market_aware_notifier()
        for ev in events:
            body = notification(sid, ev, detail={"dwell": self._market_aware_dwell, "polled_at_ns": ts_ns})
            first = ev.reasons[0] if ev.reasons else "no reason given"
            summary = f"{ev.hook}: {body['label']} — {first}"
            if journal is not None:
                # `PgJournal.write` RETURNS None on failure (it catches `Exception` itself and never
                # raises) — so a raise-only counter would read a dead Postgres as `emit_failures: 0`.
                # None IS the failure; a raise is counted too, for a journal that does not follow that
                # contract. `test_market_aware_poller.py` drives the real PgJournal to pin the None.
                try:
                    row_id = await journal.write("error" if ev.kind == HOOK_FAULT else "state", summary,
                                                 session=_et_date(ts_ns), detail=body, slot="poll")
                except Exception as exc:  # noqa: BLE001
                    row_id = None
                    _log.warning("%s: market-aware journal row raised (%r): %s", sid, exc, summary)
                if row_id is None:
                    self._market_aware_emit_failures += 1
                    _log.warning("%s: market-aware journal row NOT written: %s", sid, summary)
            if notifier is None:
                self._market_aware_emit_failures += 1
                continue
            key = f"market_aware:{sid}:{ev.hook}:{body['kind']}"
            leaving = {EXIT_ONLY_LEFT: EXIT_ONLY_ENTERED, EMERGENCY_CLEARED: EMERGENCY_TRIGGERED,
                       ASSESSMENT_RECOVERED: STAND_DOWN_REQUESTED}.get(ev.kind)
            try:
                if leaving is not None:
                    # The recovery pages on its own key; clearing the entering key makes a re-entry news.
                    notifier.clear(f"market_aware:{sid}:{ev.hook}:{NOTIFY[leaving][0]}")
                if ev.kind != HOOK_FAULT and ev.kind != HOOK_UNKNOWN_TWICE:
                    # Any computable transition on this hook — a recovery, or an episode opening straight
                    # out of a gap — ends the fault / unknown condition: clear both keys so a recurrence
                    # within the pager's repeat window is news, as the fold already says it is.
                    for k in (NOTIFY[HOOK_FAULT][0], NOTIFY[HOOK_UNKNOWN_TWICE][0]):
                        notifier.clear(f"market_aware:{sid}:{ev.hook}:{k}")
                reasons = "\n".join(f"• {r}" for r in ev.reasons) or "• no reason given"
                dwelt = f" after {ev.polls} consecutive polls" if ev.kind == EMERGENCY_TRIGGERED else ""
                faults = f" ({ev.faults} consecutive faults)" if ev.kind == HOOK_FAULT else ""
                text = (f"*{sid}* {body['label']}.\n\n"
                        f"`{ev.hook}`: {ev.previous} → {ev.current} (answered {ev.reading}){dwelt}{faults}.\n"
                        f"{reasons}\n\nPhase 1: reported, nothing acted.")
                from api.notify import Alert
                await notifier.send(key, Alert(title=f"{sid} {body['label']}", body=text,
                                               critical=body["severity"] == CRITICAL))
            except Exception as exc:  # noqa: BLE001
                self._market_aware_emit_failures += 1
                _log.warning("%s: market-aware page failed (%r): %s", sid, exc, summary)

    def _market_aware_frame(self) -> dict:
        return {
            "contract": dict(self._market_aware_contract),
            "dwell": self._market_aware_dwell,
            "poll_secs": self._market_aware_poll_secs,
            "polled_at_ns": self._market_aware_polled_ns,
            # SNAPSHOT COPIES: this runs on a timer thread while `register_strategy` (another thread)
            # adds lanes and the loop thread adds to `journal_absent`; iterating the live containers
            # would raise "changed size during iteration" inside the whole health publish.
            "lanes": {sid: as_payload(r, dwell=self._market_aware_dwell)
                      for sid, r in list(self._market_aware_readings.items())},
            "emit_failures": self._market_aware_emit_failures,
            "journal_absent": sorted(list(self._market_aware_journal_absent)),
            # Lanes whose resolver RAISED, with the exception — disjoint from `journal_absent` (#958).
            "journal_broken": dict(self._market_aware_journal_broken),
            # Registered lanes the detector has not evaluated yet — in NEITHER list above (#955).
            "journal_unchecked": sorted(set(self._market_aware_readings) - set(self._market_aware_journal_checked)),
        }

    def _spawn(self, coro, what: str):
        """`run_coroutine_threadsafe` with the future's exception RETRIEVED and logged (#651 item 4).

        A dropped future is where exceptions go to die: `_maybe_receipt`'s NameError lived inside one
        for fourteen sessions, and a raising dispatch tick — a DB error after `handler.apply`, with a
        real order at the venue — vanished the same way. The done-callback costs nothing and turns
        "silently never ran" into a log line naming what failed.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _report(f) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                _log.error("%s failed: %r", what, exc)

        fut.add_done_callback(_report)
        return fut

    async def _startup_manager_check(self) -> None:
        """Run once, at engine start, BEFORE the steady-state timer takes over.

        Resolves anything a prior process crash left in APPLYING, then does one immediate dispatch pass so a
        restart mid-RTH doesn't sit idle for a full timer interval before picking up something already
        ARMED. Sequenced with `await`, not two separate `run_coroutine_threadsafe` calls, so reconciliation
        is guaranteed to finish before the first claim.
        """
        from api import managers as mg

        for kind in mg.registered_kinds():
            await self._reconcile_stuck_managers(kind)
        await self._dispatch_all_managers()

    async def _dispatch_all_managers(self) -> None:
        """Dispatch every registered manager kind — but NOT before the restart seed has landed (#568).

        Manager guards call `current_cycle_for`, which calls `project()`, which MUTATES the fold and
        mints a cycle_id for anything it does not already know. Run before the seed applies the durable
        envelope and it mints FRESH ids for instruments that already have live envelope rows — so every
        later write collides on `uq_trade_cycle_active_key`. That storm is what starved MOMENTUM-002's
        source refresh on 2026-08-26 and cost the session.

        THE GATE IS HERE, not on `current_cycle_for`. Codex proposed either; only this one is safe.
        `engine_node.py:6823` does `if current is None or current.cycle_id != row.cycle_id: return
        "FAILED"`, so an unseeded None would mark managers FAILED at every boot, while the other two
        call sites tolerate None. Deferring a dispatch costs a tick; failing a manager costs an
        operator's trust in the row.

        Both entry points — the 30s timer and the startup pass — funnel through here, so one gate
        closes both doors. `_startup_manager_check`'s reconcile of stuck APPLYING rows is deliberately
        NOT gated: it touches no projection.

        The wait is short. The seed's own work is 60ms and it now runs off the node loop.
        """
        if not self._cycles_seeded:
            self.log.info(
                "manager dispatch deferred — the restart seed has not landed yet; dispatching now "
                "would project and mint fresh cycle ids over the durable envelope (#568)")
            return

        from api import managers as mg

        for kind in mg.registered_kinds():
            await self._dispatch_managers_of_kind(kind)

    async def _dispatch_managers_of_kind(self, kind: str) -> None:
        """Evaluate every ARMED manager of this kind belonging to THIS strategy; claim + apply the ones whose
        trigger fired. Generalizes the flatten-specific replay loop this framework superseded — same shape
        (atomic per-row claim, intent recorded before the effect), parameterized by `kind` so #46/#47 reuse
        it without touching this method."""
        from api import managers as mg
        from api.db.engine import session_factory

        handler = mg.handler_for(kind)
        if handler is None:
            return
        try:
            async with session_factory() as session:
                candidates = await mg.armed_of_kind(session, kind, str(self.id))
        except Exception as exc:  # noqa: BLE001 — the store being briefly unreachable must not break trading
            _log.warning("could not read %s managers: %r", kind, exc)
            return

        for row in candidates:
            # Refusing a non-AUTO leash at attach is not enough (codex review, #255): a row that predates
            # that check, or one attached through an internal path that carries `row.leash` forward, would
            # still be claimed and applied here — `mg.claim` has no leash predicate. The dispatch loop is
            # where the leash would have to be honoured, so it is where a leash we do NOT honour has to
            # stop. Skipped loudly rather than silently: a manager the operator believes is gated, that
            # this engine will never act on, should be visible in the log rather than merely inert.
            # (Every row in the live book is AUTO today, so this is a guard, not a migration.)
            # The SAME predicate the attach path uses, not a second one that can drift from it (codex
            # review). Case-folding here while attach demands an exact "AUTO" meant a stored `"auto"`
            # passed dispatch and was applied — the guard failing open in exactly the case it exists for.
            if _validate_leash(row.leash) is not None:
                _log.warning(
                    "manager %s (%s) has leash %s which this engine does not honour — failing it",
                    row.manager_id, kind, row.leash,
                )
                # FAILED, not merely skipped. Skipping leaves the row ARMED, and `GET /managers` reports
                # ARMED as healthy — so the operator would see a manager that looks live and can never act,
                # which is the same "silently does nothing" failure this whole change is removing.
                #
                # It has to be CLAIMED first. The reducer only accepts FAILED out of APPLYING
                # (`next_state("ARMED", "FAILED", …) == "ARMED"`), so recording FAILED against an ARMED row
                # is a no-op — verified, not assumed. Claiming also makes this idempotent across ticks and
                # across processes: whoever wins the atomic ARMED -> APPLYING is the one that fails it.
                try:
                    async with session_factory() as session:
                        claimed = await mg.claim(session, row.manager_id)
                    if claimed:
                        async with session_factory() as session:
                            await mg.record_event(
                                session, row.manager_id, "FAILED", row.leash,
                                detail={"detail": f"leash {row.leash} is not honoured by this engine"},
                            )
                except Exception as exc:  # noqa: BLE001 — a DB blip must not end the kind's loop (#651 item 4)
                    _log.error("manager %s (%s): could not record the unhonoured-leash FAILED (%r) — "
                               "the row stays visible; the next tick retries", row.manager_id, kind, exc)
                continue
            # SKIP BEFORE CLAIMING while an exit is releasing this position's shares (#358).
            #
            # `_submit_trailing_stop` raises `ExitReleaseInFlight` as a backstop, but reaching it here
            # would mean the row is already CLAIMED and APPLYING, so `apply`'s handler marks it FAILED —
            # an operator seeing a dead manager for a condition that clears itself in under a minute.
            # Skipping before the claim changes no state at all: the row stays ARMED and the next tick
            # picks it up, by which time the standoff has expired.
            if row.instrument_id in self._active_exit_suppressions(self.clock.timestamp_ns()):
                _log.info(
                    "manager %s (%s) skipped this tick — an exit is releasing %s's shares and a new "
                    "trailing stop would reserve them again",
                    row.manager_id, kind, row.instrument_id,
                )
                continue
            try:
                if not await handler.trigger_met(self, row):
                    continue
            except Exception as exc:  # noqa: BLE001 — a bad trigger check must never break trading
                _log.error("trigger_met failed for manager %s: %r", row.manager_id, exc)
                continue

            # THE THREE WRITES BELOW ARE EACH GUARDED (#651 item 4). They used to be bare, and this
            # loop runs inside a fire-and-forget future — so a transient DB error after the claim
            # propagated into a future nobody inspected: never logged, the rest of the kind's rows
            # never dispatched that tick, and (worst) after `handler.apply` a REAL ORDER sat at the
            # venue with its row stuck APPLYING until restart. Each write failure is its own ERROR
            # naming the row and what state the world is actually in.
            try:
                async with session_factory() as session:
                    claimed = await mg.claim(session, row.manager_id)
            except Exception as exc:  # noqa: BLE001
                _log.error("manager %s (%s): claim failed (%r) — row untouched, no order placed; "
                           "the next tick retries", row.manager_id, kind, exc)
                continue
            if not claimed:
                continue  # another tick already took it — not an error, just lost the race

            coid = handler.client_order_id_for(row.manager_id)
            try:
                async with session_factory() as session:
                    await mg.record_event(
                        session, row.manager_id, "INTENT_RECORDED", row.leash,
                        detail={"client_order_id": coid}
                    )
            except Exception as exc:  # noqa: BLE001
                # INTENT-BEFORE-EFFECT: if the intent cannot be recorded, the effect must not
                # happen. The row is claimed (APPLYING) with NO order at the venue — the honest
                # stuck state `_startup_manager_check` already knows how to surface.
                _log.error("manager %s (%s): intent could not be recorded (%r) — NOT applying; the "
                           "row sits APPLYING with no order at the venue until reconciled",
                           row.manager_id, kind, exc)
                continue
            try:
                new_state, detail = await handler.apply(self, row)
            except Exception as exc:  # noqa: BLE001 — apply must never break trading
                new_state, detail = "FAILED", str(exc)[:250]
                _log.error("apply failed for manager %s: %r", row.manager_id, exc)

            try:
                async with session_factory() as session:
                    await mg.record_event(
                        session,
                        row.manager_id,
                        "APPLIED" if new_state == "APPLIED" else "FAILED",
                        row.leash,
                        detail={"detail": detail} if detail else None,
                    )
            except Exception as exc:  # noqa: BLE001
                _log.error("manager %s (%s): outcome %s could not be recorded (%r) — a REAL ORDER "
                           "(%s) may be live at the venue while the row sits stuck APPLYING until "
                           "an operator or restart resolves it", row.manager_id, kind, new_state,
                           exc, coid)
                continue
            _log.warning("manager %s (%s) %s: %s", row.manager_id, kind, new_state, detail or "")

    async def _reconcile_stuck_managers(self, kind: str) -> None:
        """Resolve any row left in APPLYING by a crash between claiming it and recording the outcome.

        Depends on the durable cache (`KUMO_DURABLE_CACHE=true`, on for this deployment — the same assumption
        `_recover_transfers` already makes): without it, "not in cache" after a restart means nothing, since
        the whole cache reset, not that the order never reached the venue.

        The deterministic client_order_id is read back from the row's own INTENT_RECORDED event (not
        reconstructed) — the SOLE source of truth for what order this manager was about to place, per
        review (a fixed naming convention would drift the moment a second manager kind picks a different
        one)."""
        from api import managers as mg
        from api.db.engine import session_factory

        # The docstring's precondition, ENFORCED (#652 item 5): without the durable cache, "not in
        # cache" after a restart is "the cache reset", not "the order never reached the venue" —
        # reverting to ARMED on that evidence re-fires an order that may already be live. Refuse
        # loudly rather than guess; the stuck rows stay in APPLYING, which is at least honest.
        if not durable_cache_enabled():
            _log.warning(
                "stuck %s managers NOT reconciled — KUMO_DURABLE_CACHE is off, so an order missing "
                "from the cache after restart proves nothing; refusing to revert APPLYING rows to "
                "ARMED (they remain APPLYING until an operator resolves them)",
                kind,
            )
            return

        try:
            async with session_factory() as session:
                stuck = await mg.applying_of_kind(session, kind, str(self.id))
                for row in stuck:
                    intent = await mg.last_event_detail(session, row.manager_id, "INTENT_RECORDED")
                    coid = (intent or {}).get("client_order_id")
                    if coid and self._lookup_order(coid) is not None:
                        self._seen_orders.add(coid)
                        await mg.record_event(session, row.manager_id, "APPLIED", row.leash, detail={"recovered": True})
                        _log.warning("manager %s found already applied on restart", row.manager_id)
                    else:
                        await mg.revert_to_armed(session, row.manager_id, detail={"reason": "not found in cache on restart"})
                        _log.warning("manager %s was never applied — reverted to ARMED", row.manager_id)
        except Exception as exc:  # noqa: BLE001 — the store being briefly unreachable must not break trading
            _log.warning("could not reconcile stuck %s managers: %r", kind, exc)

    async def _handle_transfer_command(self, cid: str, payload: dict) -> tuple[str, str]:
        """Move quantity between two strategies. Applies NO order to any venue — the broker net is identical
        before and after; only which strategy owns the position changes.

        Deliberately not gated on KUMO_ORDERS_ARMED: that gate guards live order flow, and refusing a pure
        bookkeeping move when disarmed would be safety theatre.
        """
        from api import transfers as tr
        from api.db.engine import session_factory

        try:
            async with session_factory() as session:
                if await tr.already_applied(session, cid):
                    return "ok", ""  # redelivered command — already booked

                instrument_id = payload["instrument_id"]
                side = str(payload["side"]).upper()
                source_sid = payload.get("source_strategy_id", "EXTERNAL")
                target_sid = payload["target_strategy_id"]
                qty = Decimal(str(payload["quantity"]))

                pos = self._position_for(instrument_id, source_sid, side)
                if pos is None:
                    return "error", "no source position for that instrument/strategy/side"

                source_avg = Decimal(str(pos.avg_px_open))
                mark = self._mark_px(instrument_id)
                pricing_mode = payload.get("pricing_mode", "CARRY_OVER")
                px = tr.resolve_transfer_px(pricing_mode, source_avg, mark)

                reason = tr.validate(
                    quantity=qty,
                    source_qty=Decimal(str(pos.quantity)),
                    source_ts_last=int(payload["source_ts_last"]),
                    current_ts_last=int(pos.ts_last or 0),
                    source_strategy_id=source_sid,
                    target_strategy_id=target_sid,
                    # DERIVED from the registry, never a literal (#781). `self.id` alone is
                    # MANUAL-001, so every other lane was refused as "not a registered strategy" —
                    # while `register_strategy` had registered them all along. The refusal's own
                    # reason named a fact this node can answer, and was handed a constant instead.
                    # Invisible because at the only value ever exercised the two AGREED.
                    # A second hand-maintained list would drift from `register_strategy`; this
                    # cannot, so registering a lane widens the set with no other edit.
                    manageable_strategies={str(self.id)} | {
                        str(sid) for sid in (self._sibling_strategies or {})},
                    source_reducing_qty=self._reducing_order_qty(instrument_id, source_sid, side),
                    target_opposite_qty=self._opposite_side_qty(instrument_id, target_sid, side),
                    transfer_px=px,
                )
                if reason:
                    return "error", reason

                req = tr.TransferRequest(
                    transfer_id=cid,  # the command id IS the transfer id — one transfer per command
                    command_id=cid,
                    account_id=str(pos.account_id),
                    client_id=str(self._exec_client_id),
                    instrument_id=instrument_id,
                    source_strategy_id=source_sid,
                    target_strategy_id=target_sid,
                    side=side,
                    quantity=qty,
                    pricing_mode=pricing_mode,
                    transfer_px=px,
                    source_avg_px=source_avg,
                    source_ts_last=int(payload["source_ts_last"]),
                    reason_code=payload.get("reason_code", "unspecified"),
                )
                # PREPARED lands BEFORE any native effect, so a crash mid-transfer is always recoverable:
                # the log knows a transfer was in flight even if neither leg made it.
                await tr.record(session, req, "PREPARED")

            return await self._apply_transfer(req)
        except Exception as exc:  # noqa: BLE001 — a bad transfer must never break trading
            _log.warning("transfer command %s failed: %r", cid, exc)
            return "error", str(exc)

    async def _apply_transfer(self, req) -> tuple[str, str]:
        """Apply both legs, recording progress between them so recovery can finish an interrupted transfer."""
        from api import transfers as tr
        from api.db.engine import session_factory

        source_side, dest_side = tr.leg_sides(req.side)
        try:
            self._apply_leg(req, req.source_strategy_id, source_side, req.source_order_id, "SRC")
            async with session_factory() as session:
                await tr.record(session, req, "SOURCE_APPLIED")

            self._apply_leg(req, req.target_strategy_id, dest_side, req.dest_order_id, "DST")
            async with session_factory() as session:
                await tr.record(session, req, "DEST_APPLIED")
                await tr.record(session, req, "COMPLETED")
            return "ok", ""
        except Exception as exc:  # noqa: BLE001
            async with session_factory() as session:
                await tr.record(session, req, "FAILED", error=str(exc)[:250])
            _log.error("transfer %s failed mid-apply: %r", req.transfer_id, exc)
            return "error", f"transfer failed mid-apply: {exc}"

    def _apply_leg(self, req, strategy_id: str, side: str, order_id: str, leg: str) -> None:
        """One internal leg: build the order, cache it, accept it, fill it.

        Never submitted through an exec client, so nothing reaches the venue. Nautilus refuses a fill for an
        order it doesn't know (execution/engine.pyx), which is why this is a real order rather than a bare
        fill. Zero commission — a book transfer costs nothing and must not accrue fees.
        """
        from api.transfers import INTERNAL_TAG

        sid = StrategyId(strategy_id)
        iid = InstrumentId.from_str(req.instrument_id)
        coid = ClientOrderId(order_id)
        voi = VenueOrderId(f"{order_id}-V")
        now = self.clock.timestamp_ns()
        instrument = self.cache.instrument(iid)

        order = MarketOrder(
            trader_id=self.trader_id,
            strategy_id=sid,
            instrument_id=iid,
            client_order_id=coid,
            order_side=OrderSide.SELL if side == "SELL" else OrderSide.BUY,
            quantity=Quantity.from_str(str(req.quantity)),
            init_id=UUID4(),
            ts_init=now,
            tags=[INTERNAL_TAG, req.transfer_id, leg],
        )
        self.cache.add_order(order, position_id=PositionId(f"{req.instrument_id}-{strategy_id}"))

        for event in (
            OrderSubmitted(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                account_id=self._account_id(), event_id=UUID4(), ts_event=now, ts_init=now,
            ),
            OrderAccepted(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                venue_order_id=voi, account_id=self._account_id(), event_id=UUID4(), ts_event=now, ts_init=now,
            ),
            OrderFilled(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                venue_order_id=voi, account_id=self._account_id(), trade_id=TradeId(f"{order_id}-T"),
                position_id=PositionId(f"{req.instrument_id}-{strategy_id}"),
                order_side=order.side, order_type=OrderType.MARKET,
                last_qty=order.quantity, last_px=Price.from_str(f"{req.transfer_px:.2f}"),
                currency=instrument.quote_currency, commission=Money(0, instrument.quote_currency),
                liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
                event_id=UUID4(), ts_event=now, ts_init=now,
            ),
        ):
            self._send_to_exec_engine(event)

    async def build_book_repair(self, *, arm: bool = False, pair: dict | None = None) -> dict:
        """Read this node's own book and venue, PLAN the repair, and hand it to `run_book_repair` (#771).

        THIS IS THE MISSING CALLER. `plan_contra_closes` had none — the whole chain existed and
        nothing on a running node ever built a plan, so the repair could only be exercised from an
        ad-hoc script. A capability nothing calls is indistinguishable from one that does not work.

        COMPLETENESS IS ASSERTED HERE BECAUSE THIS IS WHERE IT IS KNOWN. `_venue_position_reports`
        returns a LIST when the venue was read in full and `None` when it could not be read — the
        two answers this repo keeps having to separate. A list is therefore a complete snapshot, and
        an instrument absent from it is genuinely FLAT rather than unmentioned. That distinction is
        the entire subject of #771: a fully-offset minted pair NETS TO ZERO, venues report only
        non-zero positions, so every clean pair was refused by the one property that identifies it.

        `broker_complete=True` is passed ONLY on this path, and only after `reports is None` has been
        refused above. Any caller that cannot make that guarantee must leave the flag alone.
        """
        from api.contra_close import plan_contra_closes, plan_operator_pair
        from api.contra_execute import prepare_execution

        reports = await self._venue_position_reports()
        if reports is None:
            # REFUSE, never assume flat. Reading the venue as empty here would plan a repair against
            # a book we could not see, and every failure this repair can cause is a failure of aim.
            self.log.warning(
                "repair: REFUSING to plan — the venue position read failed, and an unreadable venue "
                "is not an empty one; no pair can be anchored without it"
            )
            return {"planned": 0, "booked": 0, "refused": 0,
                    "error": "venue unreadable — repair not planned"}

        broker: dict[str, float] = {}
        for r in reports:
            iid = str(r.instrument_id)
            broker[iid] = broker.get(iid, 0.0) + float(getattr(r, "signed_decimal_qty", 0) or 0)

        positions = list(self.cache.positions_open() or [])
        # THE SAME VOCABULARY THE POSITION BOOK SPEAKS. `_validated_working` RAISES on a bare symbol
        # rather than silently never matching, so this must hand over instrument ids.
        working = [str(o.instrument_id) for o in (self.cache.orders_open() or [])]
        known = [str(i) for i in (self.cache.instrument_ids() or [])] or None

        if pair is not None:
            # THE OPERATOR NAMED THE PAIRING (#784). The automatic planner refuses an instrument
            # whose residual it cannot attribute, and that refusal stays — this path does not relax
            # it, it supplies the one input the planner will not invent. Everything downstream is
            # unchanged: same aim checks, same fingerprint, same all-or-nothing booking.
            plan = plan_operator_pair(
                positions, broker,
                instrument_id=str(pair["instrument_id"]),
                long_strategy=str(pair["long_strategy"]),
                short_strategy=str(pair["short_strategy"]),
                quantity=float(pair["quantity"]),
                working_orders=working, known_instrument_ids=known, broker_complete=True,
            )
            self.log.warning(
                f"repair: OPERATOR-DIRECTED pair on {pair['instrument_id']} — "
                f"{pair['long_strategy']} gives back {pair['quantity']} against "
                f"{pair['short_strategy']}; the planner refused to choose this and a human did"
            )
        else:
            plan = plan_contra_closes(positions, broker, working_orders=working,
                                      known_instrument_ids=known, broker_complete=True)
        for refusal in plan.refused:
            self.log.info(f"repair: not planned for {refusal.instrument_id} — {refusal.reason}")

        execution_plan = prepare_execution(plan, positions, broker, working_orders=working,
                                           known_instrument_ids=known, broker_complete=True)
        self.log.info(
            f"repair: planned {len(plan.pairs)} pair(s), {len(plan.evictions)} eviction(s), "
            f"{len(plan.surplus_evictions)} surplus eviction(s), "
            f"{len(plan.refused)} not planned; execution says: {execution_plan.summary}"
        )
        for se in plan.surplus_evictions:
            self.log.warning(
                f"repair: EXTERNAL surplus on {se.instrument_id} — {se.leg.quantity:g} at {se.leg.price} "
                f"sits on real lanes holding {se.real_lanes_qty:g} against a venue quantity of "
                f"{se.broker_qty:g}; the surplus leg closes at its own basis (#1038)"
            )
        result = self.run_book_repair(execution_plan, arm=arm)
        # PAIRS, EVICTIONS AND SURPLUS EVICTIONS COUNTED SEPARATELY, deliberately. Merging them would
        # hide WHICH mechanism ran, and this dict is what the operator-visible command reply formats
        # (#779, #1038).
        result["planned"] = len(plan.pairs)
        result["evictions"] = len(plan.evictions)
        result["surplus"] = len(plan.surplus_evictions)
        result["not_planned"] = len(plan.refused)
        return result

    def run_book_repair(self, execution_plan, *, arm: bool = False) -> dict:
        """Verify an approved repair against the live book and, when ARMED, book it (#744).

        DEFAULTS TO REPORT-ONLY, and that is the point rather than caution theatre. Every failure
        this repair can cause is a failure of aim; the plan it produces can be read against a real
        damaged book with nothing at stake, and only then armed. Same rule as every other automation
        gate in this repo: opt-in.

        THE LEDGER IS CONSULTED BEFORE ANYTHING AND WRITTEN AFTER EACH PAIR. A repair that runs twice
        books offsetting legs against a book the first run already corrected, re-opening the short it
        closed. If booking a pair raises halfway, the pair is recorded as REFUSED with the error so
        the next tick does not treat a half-applied pair as untouched.
        """
        from api.contra_book import verify_and_book
        from api.contra_ledger import BOOKED, REFUSED

        now = self._safe_now()
        # OPEN AND CLOSED, deliberately: a leg aimed at a position that has since closed must be
        # caught by the aim check rather than silently opening a new one at that id.
        positions = list(self.cache.positions_open() or []) + list(self.cache.positions_closed() or [])
        plan = verify_and_book(execution_plan, positions, self._contra_ledger)

        for refusal in plan.refused:
            self.log.warning(
                f"repair: REFUSING {refusal.instrument_id} — {refusal.reason}: {refusal.detail}"
            )
            self._contra_ledger.record(refusal.fingerprint, refusal.instrument_id, REFUSED,
                                       detail=f"{refusal.reason}: {refusal.detail}", ts_ns=now)

        booked = 0
        if arm:
            for b in plan.book:
                try:
                    for leg in b.legs:
                        self._book_repair_leg(leg, tag=b.fingerprint[:8])
                except Exception as exc:  # noqa: BLE001
                    # LOUD, and recorded as refused rather than left silent: a pair that raised
                    # part-way through is NOT untouched, and the next tick must not treat it as such.
                    self.log.exception(f"repair: booking FAILED for {b.instrument_id}", exc)
                    self._contra_ledger.record(b.fingerprint, b.instrument_id, REFUSED,
                                               detail=f"booking raised: {exc}", ts_ns=now)
                    continue
                self._contra_ledger.record(b.fingerprint, b.instrument_id, BOOKED,
                                           legs=b.legs, ts_ns=now)
                booked += 1
                self.log.warning(
                    f"repair: BOOKED {b.instrument_id} ({len(b.legs)} legs) — the per-lane split is "
                    f"corrected; the broker net does not move"
                )
        elif plan.book:
            self.log.warning(
                f"repair: {len(plan.book)} pair(s) VERIFIED and ready, not armed — "
                + "; ".join(f"{b.instrument_id}:{len(b.legs)} legs" for b in plan.book)
            )

        return {
            "armed": bool(arm),
            "ready": len(plan.book),
            "booked": booked,
            "refused": len(plan.refused),
            "already_done": len(plan.already_done),
            "ledger": self._contra_ledger.summary(),
        }

    def _book_repair_leg(self, leg, *, tag: str) -> None:
        """Book ONE verified repair leg into the position book (#744). Never reaches a venue.

        Modelled on `_apply_leg`, with the one difference that matters: the position id comes from
        `leg.position_id`, which `contra_book.verify_and_book` READ from the live cache and checked —
        lane, direction and size — before this is ever called.

        `_apply_leg` RECONSTRUCTS `PositionId(f"{instrument}-{strategy}")`. For transfers that is
        sound because both sides are known-good; for a repair it is the defect's re-entry route, and
        the module docstrings say so in three places. A fill aimed at an id that is not the real one
        OPENS a position rather than closing one, in a lane that may hold nothing — re-minting exactly
        what is being repaired.

        A REAL ORDER, not a bare fill: Nautilus refuses a fill for an order it does not know
        (execution/engine.pyx). Zero commission — a book correction costs nothing and must not accrue
        fees. Each leg fills at ITS OWN basis, which is what makes the repair realize nothing.
        """
        sid = StrategyId(str(leg.strategy_id))
        iid = InstrumentId.from_str(str(leg.instrument_id))
        coid = ClientOrderId(f"RPR-{tag}-{leg.strategy_id}"[:36])
        voi = VenueOrderId(f"{coid.value}-V")
        now = self.clock.timestamp_ns()
        instrument = self.cache.instrument(iid)
        side = OrderSide.SELL if str(leg.side) == "SELL" else OrderSide.BUY

        order = MarketOrder(
            trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
            order_side=side, quantity=instrument.make_qty(float(leg.quantity)),
            init_id=UUID4(), ts_init=now, tags=["REPAIR", tag],
        )
        # THE VERIFIED ID, on the cache entry and on the fill alike. If these two ever disagree the
        # order is booked against one position and the fill against another, which is the split
        # defect wearing the repair's clothes.
        pid = PositionId(str(leg.position_id))
        self.cache.add_order(order, position_id=pid)

        for event in (
            OrderSubmitted(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                account_id=self._account_id(), event_id=UUID4(), ts_event=now, ts_init=now,
            ),
            OrderAccepted(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                venue_order_id=voi, account_id=self._account_id(), event_id=UUID4(),
                ts_event=now, ts_init=now,
            ),
            OrderFilled(
                trader_id=self.trader_id, strategy_id=sid, instrument_id=iid, client_order_id=coid,
                venue_order_id=voi, account_id=self._account_id(),
                trade_id=TradeId(f"{coid.value}-T"), position_id=pid,
                order_side=side, order_type=OrderType.MARKET,
                last_qty=order.quantity, last_px=instrument.make_price(float(leg.price)),
                currency=instrument.quote_currency, commission=Money(0, instrument.quote_currency),
                liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
                event_id=UUID4(), ts_event=now, ts_init=now,
            ),
        ):
            self._send_to_exec_engine(event)

    def _send_to_exec_engine(self, event) -> None:
        """Hand an event to the ExecutionEngine over the message bus.

        A Strategy has no direct handle on the engine; the engine registers `ExecEngine.process` as a bus
        endpoint (execution/engine.pyx:196) and that is the supported way in.
        """
        self._msgbus.send(endpoint="ExecEngine.process", msg=event)

    def _account_id(self):
        """The account these positions sit under — taken from the source position rather than a venue lookup,
        so it is right even before the account cache is warm."""
        for pos in self.cache.positions_open():
            return pos.account_id
        raise RuntimeError("no account visible — cannot apply an internal transfer")

    def _position_for(self, instrument_id: str, strategy_id: str, side: str):
        for pos in self.cache.positions_open():
            if (
                str(pos.instrument_id) == instrument_id
                and str(pos.strategy_id) == strategy_id
                and pos.side.name == side
            ):
                return pos
        return None

    def _opposite_side_qty(self, instrument_id: str, strategy_id: str, side: str) -> Decimal:
        """How much the TARGET already holds on the opposite side of the position being moved.

        Under NETTING the destination leg nets against an opposing position instead of adding to it, so
        moving a long into a strategy that is short the same instrument would quietly reduce that short
        rather than hand it the position — a change in exposure wearing bookkeeping's clothes.
        """
        opposite = "SHORT" if side.upper() == "LONG" else "LONG"
        total = Decimal(0)
        for pos in self.cache.positions_open():
            if (
                str(pos.instrument_id) == instrument_id
                and str(pos.strategy_id) == strategy_id
                and pos.side.name == opposite
            ):
                total += Decimal(str(pos.quantity))
        return total

    def _reducing_order_qty(self, instrument_id: str, strategy_id: str, side: str) -> Decimal:
        """Quantity resting in orders that would REDUCE this strategy's position.

        A LONG is reduced by SELLs, a SHORT by BUYs. Only these matter to a transfer: the source loses
        quantity, so an exit sized against the old position could sell more than remains. Opening orders on
        either side, and anything on the target, are harmless.
        """
        reducing = OrderSide.SELL if side.upper() == "LONG" else OrderSide.BUY
        total = Decimal(0)
        for o in self.cache.orders_open():
            if (
                str(o.instrument_id) == instrument_id
                and str(o.strategy_id) == strategy_id
                and o.side == reducing
            ):
                total += Decimal(str(o.leaves_qty))
        return total

    def _identified_protective_stop_open(self, instrument_id: str, strategy_id: str, reducing_side):
        """This position's protective stop, whoever placed it (#303).

        `_bracket_protective_stop_open` only matches a bracket LEG — `STOP_MARKET`/`STOP_LIMIT` carrying a
        `bracket:` tag. That was the only kind of resting exit when the arm guard was written. The #239
        backstop now rests a `PROT-` TRAILING stop on essentially every position, which that check can
        never match, so arming was refused everywhere with "cancel it manually" — advice that would leave
        the position naked to satisfy a guard.

        The guard itself stays: exactly one resting exit, and it must be IDENTIFIABLE. What widens is what
        counts as identifiable, because a resting protective stop is now normal rather than suspicious.
        """
        for o in self.cache.orders_open():
            if (
                str(o.instrument_id) == instrument_id
                and str(o.strategy_id) == strategy_id
                and o.side == reducing_side
                and _is_identifiable_protective_stop(o)
            ):
                return o
        return None

    def _bracket_protective_stop_open(self, instrument_id: str, strategy_id: str, reducing_side):
        """The resting, OPEN protective stop for this instrument+strategy, IF reliably identifiable (#46,
        PEAK's arm step) — a STOP_MARKET/STOP_LIMIT order on the reducing side, tagged `bracket:...`. Same
        discrimination `_closing_order_for` already uses on CLOSED orders (#47), applied here to the
        currently-resting one. Returns `None` if there's no resting reducing-side order at all, OR if one
        exists but isn't identifiable this way — callers must NOT conflate those two cases (see
        `_any_reducing_order_open`): "nothing to cancel" and "something's resting but I can't safely
        identify it as THE stop" call for different responses."""
        for o in self.cache.orders_open():
            if (
                str(o.instrument_id) == instrument_id
                and str(o.strategy_id) == strategy_id
                and o.side == reducing_side
                and o.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
                and any(str(t).startswith("bracket:") for t in (o.tags or []))
            ):
                return o
        return None

    def _clearable_for_arm(self, resting: list, identified) -> bool:
        """May a manager clear ALL of these resting orders to rest its own stop instead (#265)?

        True when every resting reducing order is either something this system placed as protection
        (`_is_identifiable_protective_stop`) or a leg of the SAME bracket as the identified stop. Both
        legs of a bracket carry one `bracket:<entry_coid>` tag, so the take-profit is recognisable as
        belonging to the position's own bracket rather than as an unrelated order someone left resting.

        The take-profit is deliberately NOT protection (`_is_identifiable_protective_stop` excludes it,
        and rightly — cancelling it discards an operator's target). But arming PEAK is itself the operator
        saying "trail this instead of working my exits", so within the arm step the target is in scope. What
        must never be cleared is an order we cannot account for: an unrelated resting sell, or a leg of some
        other bracket. Those still refuse, because guessing which one to cancel is how protection gets
        stripped.
        """
        if identified is None:
            return False
        groups = {str(t) for t in (identified.tags or []) if str(t).startswith("bracket:")}
        for o in resting:
            if _is_identifiable_protective_stop(o):
                continue
            tags = {str(t) for t in (o.tags or []) if str(t).startswith("bracket:")}
            if groups and tags & groups:
                continue
            return False
        return True

    def _reducing_orders_open(self, instrument_id: str, strategy_id: "str | set[str]", reducing_side) -> list:
        """ALL resting orders that would reduce this position (#46's arm-time guard, codex review Critical
        finding) — not just whether ANY exists. The arm step must confirm there is EXACTLY ONE resting
        reducing order and that it IS the identified bracket stop; a boolean "any" can't distinguish
        "only the bracket stop rests" from "the bracket stop rests ALONGSIDE something else unidentified",
        and conflating those left a second, uncanceled order live next to PEAK's own fresh trail."""
        # ONE STAMP OR SEVERAL (#840). Since #748 a protective stop carries the LANE whose shares it
        # covers, while older aggregate stops carry MANUAL-001; the exit path must watch both, so it
        # passes a set. A single str keeps every other caller (the arm-time guard) exactly as it was.
        owners = {strategy_id} if isinstance(strategy_id, str) else set(map(str, strategy_id))
        assert isinstance(strategy_id, (str, set, frozenset)), type(strategy_id)  # a list/tuple is a caller bug
        return [
            o
            for o in self.cache.orders_open()
            if str(o.instrument_id) == instrument_id and str(o.strategy_id) in owners and o.side == reducing_side
        ]

    # -- BROKER TRUTH for the reducing leg (#269) ------------------------------------------------------
    #
    # THE CACHE CANNOT ANSWER "WHAT IS HOLDING MY SHARES", AND EVERY EXIT PATH USED TO ASK IT.
    #
    # A submit whose HTTP call fails after Alpaca accepted the order lands in Nautilus as REJECTED, which
    # is TERMINAL. Reconciliation then reads the live order from the venue every few seconds and the state
    # machine throws each one away — `InvalidStateTrigger: REJECTED -> ACCEPTED`, 81,128 times between
    # 2026-08-16 20:02 and 2026-08-17 14:41. The order rests at the broker reserving shares for as long as
    # it lives; it is absent from `cache.orders_open()` forever.
    #
    # So on 2026-08-17 all eight live protective stops were invisible to the engine. `flatten.decide()`
    # was handed `resting_reducing_qty=0`, never set `cancel_resting_first`, and sent a market sell into a
    # fully reserved position:
    #
    #     FL-79c244f8cd8e4251aac0  403  insufficient qty available (requested: 29, available: 0)
    #
    # The cancel-then-confirm sequence directly above was correct and simply never ran. #285 already found
    # this and fixed only the DISPLAY (`_broker_protected`, the SECURED badge); the order paths kept
    # asking the cache. That split — one fact derived two ways, one of them wrong — is why this class of
    # defect keeps returning after each caller-specific fix.
    #
    # Every helper below fails CLOSED by returning None when the broker cannot be read. None means "no
    # answer", never "nothing resting": treating an unreadable broker as an empty book is exactly the
    # inference that sent the order above.

    #: Set after the node is built, from `node.kernel.exec_engine.default_client`. The strategy has no
    #: standard handle to the execution client, and this is the one thing that makes the venue readable
    #: through Nautilus's own report API rather than through Alpaca's REST client.
    _exec_client = None

    #: Whether the EXECUTION venue refuses a reducing order while another resting order reserves the
    #: shares (Alpaca: `available: 0` — #358; IB: no such concept — #430, api/venues/facts.py).
    #: DECLARED by the provider's `ExecClientSpec` and wired by the node builder, never inferred from a
    #: provider name (#619). The class default True covers only a node with no exec provider at all,
    #: and it is the conservative direction: a venue wrongly treated as reserving WAITS and refuses
    #: (recoverable — the position keeps its stop), while the reverse submits into a live reservation
    #: and is rejected at the venue.
    _exec_reserves_shares: bool = True

    async def _venue_order_reports(self) -> list | None:
        """Every order the VENUE knows about, as typed `OrderStatusReport`s — or None if unreadable.

        THE SAME BYTES, PARSED ONCE INSTEAD OF TWICE. `providers/alpaca/exec_client.py:1042` already turns
        this payload into `OrderStatusReport`, which is Nautilus's own form and the one the shipped
        Interactive Brokers adapter produces too. Reading it here is what lets a second broker work
        without porting the protection and exit paths, and it removes a second parse of Alpaca's field
        names that could disagree with the first.

        `open_only=False` deliberately, matching `status="all"` on the raw path: Alpaca's open filter does
        not return HELD orders, and a HELD bracket leg reserves its shares. That blindness is #387, and
        the 2026-08-20 probe found two protective stops invisible to the narrow read.
        """
        client = self._exec_client
        if client is None:
            return None
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import GenerateOrderStatusReports

        try:
            return await client.generate_order_status_reports(
                GenerateOrderStatusReports(
                    instrument_id=None,
                    start=None,
                    end=None,
                    open_only=False,
                    command_id=UUID4(),
                    ts_init=self.clock.timestamp_ns(),
                )
            )
        except Exception as exc:
            self.log.exception("could not read venue order reports", exc)
            return None


    async def _venue_position_reports(self) -> list | None:
        """Every position the VENUE knows about, as typed `PositionStatusReport`s — or None if unreadable.

        The position-side twin of `_venue_order_reports` (#641). `generate_position_status_reports` is
        Nautilus's own venue read; the shipped Interactive Brokers adapter implements it
        (execution.py:830) exactly as our Alpaca client does, so the protection reconciler and the
        share-availability derivation stop needing the Alpaca REST client at all.

        None means UNREADABLE, never "flat": an empty LIST is a real answer (the venue was read and
        holds nothing), and confusing the two is how a naked book reads as a protected one.
        """
        client = self._exec_client
        if client is None:
            return None
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import GeneratePositionStatusReports

        try:
            return await client.generate_position_status_reports(
                GeneratePositionStatusReports(
                    instrument_id=None,
                    start=None,
                    end=None,
                    command_id=UUID4(),
                    ts_init=self.clock.timestamp_ns(),
                )
            )
        except Exception as exc:
            self.log.exception("could not read venue position reports", exc)
            return None

    async def _venue_net_position(self, instrument_id: str) -> Decimal | None:
        """Signed net quantity for one instrument: typed report, then REST, then cache. None = unreadable.

        Broker sources first, because broker net is the only hard reconciliation anchor (CLAUDE.md);
        the cache is last because it is the local book — it can miss an external fill — but it is the
        ONLY book a node with no execution client has, and refusing there would just re-create the
        dead exit path this ordering exists to end. None means callers must refuse, never assume flat:
        flat is the empty-but-readable answer, and it is returned as Decimal(0) by the caller's own
        arithmetic, not by this function guessing.
        """
        from api.protection import _same_instrument

        reports = await self._venue_position_reports()
        if reports is not None:
            total = Decimal(0)
            for r in reports:
                if str(r.instrument_id) != instrument_id:
                    continue
                side = r.position_side.name
                if side not in ("LONG", "SHORT"):
                    continue
                qty = Decimal(str(r.quantity))
                total += -qty if side == "SHORT" else qty
            return total
        if self._http is not None:
            # The Alpaca REST fallback for a node with no execution client — the same role it plays in
            # `_reconcile_protection_inner`, and labelled the same way. Reads `qty` (the NET position),
            # NOT `qty_available`: the reservation arithmetic lives in `_venue_shares_available`.
            try:
                rows = await self._http.list_positions()
            except Exception as exc:
                self.log.exception(f"could not read broker positions for {instrument_id}", exc)
                rows = None
            if rows is not None:
                total = Decimal(0)
                for p in rows:
                    if not _same_instrument(str(p.get("symbol") or ""), instrument_id):
                        continue
                    try:
                        qty = Decimal(str(p.get("qty")))
                    except (TypeError, ValueError, ArithmeticError):
                        # An unparseable row is an UNREADABLE book, not a zero-share position: skipping
                        # it would understate the net and a wrong net authorises the wrong exit.
                        self.log.warning(
                            f"{instrument_id}: broker position row has unparseable qty {p.get('qty')!r} "
                            f"— net position is UNKNOWN"
                        )
                        return None
                    total += -abs(qty) if str(p.get("side") or "").lower() == "short" else qty
                return total
        try:
            positions = self.cache.positions_open()
        except Exception as exc:
            self.log.exception(f"could not read cached positions for {instrument_id}", exc)
            return None
        total = Decimal(0)
        for p in positions:
            if str(p.instrument_id) != instrument_id:
                continue
            total += Decimal(str(p.signed_decimal_qty()))
        return total

    async def _venue_reducing_orders(self, instrument_id: str, reducing_side: str) -> list[dict] | None:
        """Open orders AT THE VENUE reserving shares on this leg, or None if the broker can't be read.

        Not filtered by strategy: Alpaca reserves per ACCOUNT and per symbol, and knows nothing about our
        `StrategyId`. A stop another strategy rests on the same instrument holds the same shares, so a
        strategy-scoped question cannot answer an account-scoped constraint.
        """
        from api.protection import reserving_orders

        try:
            # `all`, NOT `open` (#387 review, Critical — the same blindness as the protection check, on
            # the path where it costs more). Alpaca's `open` filter does not return `held` orders: the
            # 2026-08-20 probe read 269 orders, 9 of them via `open`, and found two protective stops
            # invisible to it (CRAK 180 @ 57.06, APA 231 @ 43.99).
            #
            # A `held` bracket leg RESERVES its shares — that is why the log said `reserved_by_other_order`
            # in the first place — so the cancel-resting-first sweep below could not see the very order it
            # needed to cancel, and the exit that followed got `403 insufficient qty available
            # (available: 0)`. That is the 2026-08-17 failure documented in the comment block above,
            # verbatim, and the fix for it was sitting one keyword away.
            #
            # Widening is safe here because `reserving_orders` filters on `_OPEN_STATUSES` internally and
            # always has — the filter was ready, only the fetch was narrow. `paginate=True` for the same
            # reason as the protection read: a truncated list would answer "nothing is resting" for an
            # order it simply never received.
            # TYPED FIRST, same as the protection reconciler. Proven order-for-order identical to the
            # raw read in `test_reserving_orders_typed.py` — over a HELD bracket leg, a partial fill, a
            # closed order, the wrong side and another instrument.
            #
            # The REST read stays as the fallback for a node with no execution client and says so. It is
            # the same payload; only the parse differs.
            reports = await self._venue_order_reports()
            if reports is not None:
                from api.protection import reserving_orders_typed

                return reserving_orders_typed(reports, instrument_id, reducing_side)
            if self._http is None:
                # No execution client and no REST client: the venue genuinely cannot be read. The gate
                # used to sit ABOVE the typed read and keyed on `_http` alone (#641), which turned a
                # working IBKR execution client into a permanent silent UNKNOWN — and it returned
                # BARE, so a node that could never answer looked identical to a venue between polls.
                self.log.warning(
                    f"{instrument_id}: no execution client and no broker REST client — venue orders "
                    f"are UNREADABLE (not empty); callers must assume nothing is safe"
                )
                return None
            self.log.warning(
                "no execution client — reading reserving orders over the broker REST API. This path is "
                "Alpaca-specific and will not work for another venue."
            )
            rows = await self._http.list_orders(status="all", paginate=True)
        except Exception as exc:
            self.log.exception(f"could not read broker orders for {instrument_id} — assuming nothing is UNSAFE", exc)
            return None
        return reserving_orders(rows, instrument_id, reducing_side)

    async def _venue_shares_available(self, instrument_id: str) -> Decimal | None:
        """The shares an exit may actually claim — or None if unreadable.

        DERIVED, VENUE-NEUTRALLY (#641): |net position| − Σ resting reducing-order quantity. This used
        to read Alpaca's `qty_available` verbatim, which is a reservation concept nautilus_trader does
        not model anywhere (zero hits in the installed package — test_broker_read_gaps.py pins it), so
        the read was broker-specific forever and answered permanent-None on staging-ibkr. Alpaca
        reserves per account per symbol against any resting reducing order, which is exactly this
        arithmetic; the derivation is the same number stated from reads every adapter can answer.

        THE RESERVATION IS THE RESOURCE, and it is a different fact from order status. Alpaca frees
        shares when it CONFIRMS a cancel, not when it accepts the request — and the typed order read
        reflects that: `_OPEN_STATUSES` keeps `pending_cancel` reserving until the venue confirms, so a
        submit fired on `status: canceled` still cannot race the release here. #245 as a number.

        BOTH SIDES WORK, which the raw read did not: Alpaca reports `qty_available` NEGATIVE for a
        short (measured -88), so a wait on the reducing-BUY side could never be satisfied. Here a
        short's reducing side is BUY and the answer is absolute claimable shares on that side.

        None is UNKNOWN and callers must send nothing. In particular an unreadable ORDER book with a
        readable position is None, never "zero reserved" — assuming zero reserved authorises an exit
        straight into a protective stop's shares.
        """
        net = await self._venue_net_position(instrument_id)
        if net is None:
            return None
        if net == 0:
            return Decimal(0)  # flat at the broker — nothing held, nothing available
        rows = await self._venue_reducing_orders(
            instrument_id, "sell" if net > 0 else "buy"
        )
        if rows is None:
            return None
        reserved = Decimal(str(sum(r["_remaining"] for r in rows)))
        return max(Decimal(0), abs(net) - reserved)

    async def _reducing_qty_for_exit(self, instrument_id: str, strategy_id: str, side: str) -> Decimal:
        """How much is resting against this position, as an EXIT path must see it: the greater of what the
        cache knows and what the venue holds.

        The MAXIMUM, not the venue alone, and not the cache alone. Each source sees something the other
        misses: the cache misses any order stuck in a terminal state (the defect above), and the venue read
        can fail or lag a submit that Nautilus has already accepted. Taking the larger keeps the answer
        conservative in the only direction that matters here — over-reporting means a cancel step runs
        needlessly, under-reporting means an exit is fired into reserved shares and rejected.
        """
        cached = self._reducing_order_qty(instrument_id, strategy_id, side)
        rows = await self._venue_reducing_orders(
            instrument_id, "sell" if side.upper() == "LONG" else "buy"
        )
        if rows is None:
            return cached
        at_venue = Decimal(str(sum(r["_remaining"] for r in rows)))
        if at_venue != cached:
            self.log.warning(
                f"{instrument_id}: cache says {cached} resting on the reducing leg, broker says "
                f"{at_venue} — using {max(cached, at_venue)}"
            )
        return max(cached, at_venue)

    async def _venue_only_reducing_rows(
        self, instrument_id: str, strategy_id: str, reducing_side
    ) -> list[dict] | None:
        """Orders reserving this leg at the VENUE that the cache cannot see at all. None = unreadable.

        These are the dangerous ones. A guard that inspects only `cache.orders_open()` concludes the
        position is bare, so it permits an arm or an exit — and then the submit is rejected on `available:
        0` by an order the guard never knew existed. Every PEAK arm on NBIS on 2026-08-17 failed this way.
        """
        rows = await self._venue_reducing_orders(instrument_id, _side_name(reducing_side))
        if rows is None:
            return None
        known = _identity_keys(
            self._reducing_orders_open(instrument_id, strategy_id, reducing_side)
        )
        return [r for r in rows if not (_row_keys(r) & known)]


    def _owner_of(self, coid: str, venue_id: str) -> str | None:
        """The strategy owning a resting order, or None when it cannot be attributed at all.

        BY VENUE ID FIRST, for the reason `_client_order_id_for` does the same: a bracket leg carries a
        client id ALPACA minted, so a coid-only lookup misses exactly the orders reserving the most
        shares (#242).

        THEN BY PREFIX, and that fallback is not decoration. This sweep runs on orders the cache has
        WRITTEN OFF, so a cache miss is the CENTRAL case here, not an edge one — refusing on it turns
        the sweep off precisely where it is needed, which is how PEAK lost the ability to re-arm over
        its own stuck stop. `_OURS` is already this codebase's stated predicate for "a stop we placed",
        and every prefix in it is minted by this strategy, which IS MANUAL-001.

        Never raises: an unattributable order is REFUSED, and a raise would abort the sweep and leave
        every later row uncancelled.
        """
        try:
            if venue_id:
                mine = self.cache.client_order_id(VenueOrderId(venue_id))
                if mine is not None:
                    order = self.cache.order(mine)
                    if order is not None:
                        return str(order.strategy_id)
            if coid:
                order = self.cache.order(ClientOrderId(coid))
                if order is not None:
                    return str(order.strategy_id)
        except Exception as exc:  # noqa: BLE001 — unattributable, not fatal
            self.log.warning(f"could not attribute {coid or venue_id}: {exc!r}")
        return owner_from_prefix(coid)

    async def _cancel_reducing_leg(self, instrument_id: str, strategy_id: "str | set[str]", reducing_side,
                                   proxy: bool = False, canceller: str | None = None) -> None:
        """Cancel everything holding this leg's shares, through Nautilus where it can and the venue where
        it cannot.

        NATIVE FIRST, always. An order the cache holds as open is cancelled with `Strategy.cancel_order` so
        Nautilus stays authoritative and the fill/cancel events flow normally.

        THE VENUE FALLBACK IS NOT AN OPTIMISATION — IT IS THE ONLY PATH THAT WORKS for a cache-terminal
        order, and without it those shares can never be released by this engine. Verified against the
        installed package rather than assumed (nautilus_trader/trading/strategy.pyx:1649):

            if order.is_closed_c() or order.is_pending_cancel_c():
                self.log.warning(f"Cannot cancel order: state is {order.status_string_c()}, {order}")
                return None  # Cannot send command

        REJECTED is closed, so `cancel_order` logs and drops the command. The cockpit's own Orders-tab
        cancel bottoms out here, which is why cancelling NBIS from the UI did nothing at all.

        NAUTILUS CHECKED FIRST, AND IT CANNOT DO THIS — `Strategy.cancel_all_orders` looks like the native
        answer and is not (strategy.pyx, same file):

            cdef list[Order] open_orders = self.cache.orders_open(..., strategy_id=self.id, side=...)
            ...
            if not open_orders and not emulated_orders and not inflight_orders:
                self.log.info(f"No ... open, emulated, or inflight ... orders to cancel")
                return

        It is CACHE-GATED by construction. With NBIS's stop stuck terminal the cache held zero open
        orders, so it would log "nothing to cancel" and send nothing — the defect answering the question
        asked about the defect. Every native cancel path bottoms out in cache state, which is the thing
        that is wrong here, so this is not a hand-rolled duplicate of a Nautilus mechanism; it is the one
        operation Nautilus has no way to express.

        The boundary is kept tight: an order Nautilus IS tracking always goes through Nautilus (its coid
        is in `native_coids` and the venue loop skips it), so its state machine is never bypassed. Only
        orders Nautilus has already finished with are touched directly, and there is no state left to
        corrupt.
        """
        native = self._reducing_orders_open(instrument_id, strategy_id, reducing_side)
        # `strategy_id` is the FILTER (a str, or since #840 a set of stamps); the AUTHORISATION
        # identity is `canceller`, and when none was named it is THIS strategy — never the filter,
        # which as a set would compare unequal to every owner and silently kill the proxy concession.
        exiting = canceller or (strategy_id if isinstance(strategy_id, str) else str(self.id))
        for o in native:
            self._cancel(o)
        # Recognised by venue id AS WELL AS client order id. A bracket leg carries a client order id
        # ALPACA generated, so a coid-only skip-set does not recognise the order Nautilus is already
        # cancelling — and this loop would then cancel it a second time, directly at the venue.
        native_keys = _identity_keys(native)

        rows = await self._venue_reducing_orders(instrument_id, _side_name(reducing_side))
        if rows is None:
            return
        for row in rows:
            coid, venue_id = str(row.get("client_order_id") or ""), str(row.get("id") or "")
            if _row_keys(row) & native_keys:
                continue  # Nautilus is already cancelling this one
            # WHOSE ORDER IS THIS (#462). The native pass above is scoped by strategy; this venue sweep
            # was not, so an exit on a shared symbol cancelled whatever else rested there.
            #
            # OWNER *AND* TYPE. Owner alone would let a lane cancel a discretionary sell placed by hand
            # under MANUAL-001 — not protection, and nothing re-arms it. The prefix is not a sole key
            # either: a bracket leg carries an id ALPACA minted (#242), so keying on it alone would make
            # every bracketed position unexitable. It is the fallback inside `_owner_of`, and the type
            # check still applies on top.
            owner = self._owner_of(coid, venue_id)
            # `canceller`, NOT `strategy_id`. `strategy_id` is the cancel-confirm FILTER — which orders
            # to wait on — and protective stops are MANUAL-001's, so it must stay the feed whichever
            # lane is exiting. Using one value for both is the defect `NautilusBroker.exit`'s docstring
            # records: it "silently excluded the real order from the cancel-confirm filter and returned
            # True having waited for nothing", which is a naked position.
            if not may_cancel_order(owner=owner, canceller=canceller or exiting, row=row,
                                    canceller_is_proxy=proxy):
                self.log.error(
                    f"REFUSING to cancel {coid or venue_id} on {instrument_id}: owner="
                    f"{owner or '<unattributable>'} type={row.get('order_type') or row.get('type')!r} "
                    f"and {exiting} is exiting. Its shares stay reserved, so this exit may be "
                    f"rejected on `available: 0` — recoverable, unlike cancelling protection or a "
                    f"manual order that nothing puts back (#462)"
                )
                continue
            if not venue_id:
                # Nothing can be cancelled without the venue's own id, and this row is still holding
                # shares. Reported rather than skipped: a silent `continue` here would look exactly like
                # a clean sweep while the next submit is rejected on `available: 0`.
                self.log.error(
                    f"cannot cancel a resting order on {instrument_id} — the broker returned no order id "
                    f"for {coid or '<no client order id>'}; its shares stay reserved"
                )
                continue
            try:
                await self._cancel_at_venue(instrument_id, coid, venue_id)
            except Exception as exc:
                self.log.exception(f"venue cancel failed for {coid or venue_id} on {instrument_id}", exc)

    async def _cancel_at_venue(self, instrument_id: str, coid: str, venue_id: str) -> None:
        """Cancel a resting order the CACHE cannot act on. ONE derivation, two callers (#872).

        THROUGH NAUTILUS, for an order the cache has given up on.

        `_cancel_reducing_leg`'s docstring concludes that "every native cancel path bottoms out in cache
        state". That is true of `Strategy.cancel_order` (strategy.pyx:1651 refuses a closed order) and of
        `cancel_all_orders` — and NOT of the layer beneath them. `CancelOrder` is a plain command taking
        a venue id, and `ExecutionEngine._handle_cancel_order` does exactly one thing with it:
        `client.cancel_order(command)`. No cache lookup, no bookkeeping. The guard is on Strategy
        BUILDING the command, not on sending it.

        Worth taking for two reasons. It works on any adapter — the shipped Interactive Brokers client
        implements `cancel_order` as ours does (#430). And it stops the cancel happening behind
        Nautilus's back: a REST cancel leaves the cache holding an order the venue no longer has, which
        is the setup for the `InvalidStateTrigger: REJECTED -> ACCEPTED` churn — 81,128 occurrences on
        2026-08-17.

        Extracted rather than copied for #872's wrong-mode cancels: two derivations of "how do we cancel
        something the cache cannot see" would drift, and the direction that drifts silently is the one
        where the REST fallback survives in one copy and not the other.
        """
        if self._exec_client is not None:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.execution.messages import CancelOrder
            from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

            self._exec_client.cancel_order(CancelOrder(
                trader_id=self.trader_id,
                strategy_id=self.id,
                instrument_id=InstrumentId.from_str(instrument_id),
                # Alpaca generates its own id for a bracket leg, so a row may carry one we never
                # sent. The venue id is what identifies it either way, and the command needs a
                # client order id — so fall back to the venue id rather than refusing.
                client_order_id=ClientOrderId(coid or venue_id),
                venue_order_id=VenueOrderId(venue_id),
                command_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
            ))
        elif self._http is not None:
            self.log.warning(
                "no execution client — cancelling over the broker REST API. This path is "
                "Alpaca-specific and leaves the cache holding an order the venue has dropped."
            )
            await self._http.cancel_order(venue_id)
        else:
            # REFUSE, LOUDLY. Neither route exists, so the order stays resting and its shares stay
            # reserved. Returning quietly here would read exactly like a successful cancel.
            raise RuntimeError(
                f"no execution client and no REST client — {coid or venue_id} on {instrument_id} "
                "cannot be cancelled and its shares stay reserved"
            )
        self.log.warning(
            f"cancelled {coid or venue_id} at the VENUE — the cache holds it "
            f"{self._cache_status_of(coid)}, so Nautilus could not"
        )

    def _cache_status_of(self, client_order_id: str) -> str:
        """The cache's opinion of an order, for logging a disagreement in the same line as the fact."""
        if not client_order_id:
            return "not at all"
        order = self._lookup_order(client_order_id)
        return "not at all" if order is None else f"as {order.status.name}"

    #: How long a venue round trip may take before an exit path gives up. Both were 6s/10s, chosen when
    #: the Alpaca client had no retry: a single slow connect now costs up to three attempts with backoff
    #: (#367), so the old windows expire while the client is still legitimately working. PEAK's arm hit
    #: exactly this on 2026-08-19 — "the venue did not confirm the cancel within the timeout" — after the
    #: cancels had already gone out, which is the worst moment to stop waiting.
    #:
    #: Env-overridable, because the right value depends on the venue's mood rather than on us, and the
    #: alternative to a knob is a rebuild.
    _CANCEL_CONFIRM_S: float = float(os.environ.get("KUMO_CANCEL_CONFIRM_TIMEOUT_S", "20"))
    _SHARES_FREE_S: float = float(os.environ.get("KUMO_SHARES_FREE_TIMEOUT_S", "30"))
    #: The flip's cancel-confirm wait (#898), tunable on live like the two waits beside it; the module
    #: constant `_FLIP_CONFIRM_S` is the DEFAULT from which `_FLIP_MIN_SESSION_S` is derived.
    _FLIP_CONFIRM_S: float = float(os.environ.get("KUMO_FLIP_CONFIRM_TIMEOUT_S", str(_FLIP_CONFIRM_S)))

    async def _venue_order_status(self, instrument_id: str, coid: str, venue_id: str) -> tuple[str, float] | None:
        """ONE order's (status, filled_qty) at the venue — typed single report first, REST by venue id
        second — or None when the venue could not be asked. Statuses are Nautilus's names upper-cased
        on the typed path and Alpaca's on the REST path; the caller upper-cases both."""
        client = self._exec_client
        if client is not None:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.execution.messages import GenerateOrderStatusReport
            from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

            try:
                report = await client.generate_order_status_report(GenerateOrderStatusReport(
                    instrument_id=InstrumentId.from_str(instrument_id),
                    client_order_id=ClientOrderId(coid) if coid else None,
                    venue_order_id=VenueOrderId(venue_id) if venue_id else None,
                    command_id=UUID4(), ts_init=self.clock.timestamp_ns(),
                ))
            except Exception as exc:  # noqa: BLE001 — unreadable is its own answer, never a guess
                self.log.warning(f"protection: could not read {coid or venue_id} at the venue: {exc!r}")
                return None
            if report is None:
                return None
            return report.order_status.name.upper(), float(report.filled_qty)
        if self._http is None or not venue_id:
            return None
        try:
            row = await self._http.get_order(venue_id)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"protection: could not read {venue_id} at the venue: {exc!r}")
            return None
        if not row:
            return None
        return str(row.get("status") or "").upper(), float(row.get("filled_qty") or 0)

    async def _await_cancel_confirmed(
        self, instrument_id: str, coid: str, venue_id: str, timeout_s: float | None = None
    ) -> str:
        """Wait until the venue has CONFIRMED a cancel — by the order's TERMINAL STATUS, never by its
        absence from a list (#898).

        Four states, never two: `"confirmed"` (CANCELED / EXPIRED / REPLACED / REJECTED with nothing
        filled), `"filled"` (any fill during the window — the shares are gone or fewer, and a floor
        sized off the pass's position read would rest over shares just sold; the next pass sizes the
        floor off the venue's truth), `"timeout"` (still resting when the wait ran out), `"unreadable"`
        (the venue could not be asked — which says NOTHING about the cancel and must not be reported
        as "the venue did not confirm"). Callers place nothing on the last three.

        Absence from the open set was the first predicate, and the review killed it: a row leaves the
        open set on a FILL too, and a trail is most likely to fill exactly while it is being cancelled.
        One order is read per poll, on the side that exists, so a 10 s worst case is 20 single-order
        reads, not 80 paginated sweeps of the account.

        AN UNREADABLE POLL IS RETRIED, not returned: by the time this runs the trail is already
        cancelled, so one HTTP hiccup returning "unreadable" would leave the leg bare for a whole tick
        over a blip (delta review). "unreadable" means NO poll in the whole window could be read. A
        terminal status neither set names (`done_for_day`, Nautilus `DENIED`) polls to "timeout" —
        the safe direction, nothing placed — rather than being guessed either way.

        `time.monotonic`, not `self.clock`: the wait is real time spent in `asyncio.sleep`.
        """
        import asyncio
        import time

        timeout_s = self._FLIP_CONFIRM_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout_s
        readable = False
        while True:
            answer = await self._venue_order_status(instrument_id, coid, venue_id)
            if answer is not None:
                readable = True
                status, filled = answer
                if filled > 0 or status in _VENUE_FILLED_STATUSES:
                    return "filled"
                if status in _VENUE_GONE_STATUSES:
                    return "confirmed"
            if time.monotonic() >= deadline:
                return "timeout" if readable else "unreadable"
            await asyncio.sleep(0.5)

    async def _await_shares_available(
        self, instrument_id: str, needed: Decimal, timeout_s: float | None = None
    ) -> bool:
        """Wait until the broker will actually let `needed` shares be sold. False on timeout or no answer.

        Callers must treat False as SEND NOTHING. The position keeps whatever is protecting it, which is
        the safe end state; firing an exit on an unconfirmed release is how #245 happened.

        Longer than the 6s cache wait because this measures a venue round trip per poll, not a local
        dictionary lookup.
        """
        import asyncio
        import time

        # A VENUE THAT NEVER RESERVES HAS NOTHING TO WAIT FOR (#641). IB does not hold shares against
        # a resting order (api/venues/facts.py, #430) — the constraint this wait polls for does not
        # exist there. Before this gate the answer on such a venue was None, and None polled the FULL
        # timeout and returned False: every staging-ibkr exit died on a reservation the venue does not
        # have. "No reservation concept" means SKIP THE WAIT, not "wait then fail".
        if not self._exec_reserves_shares:
            self.log.info(
                f"{instrument_id}: this venue does not reserve shares against resting orders — "
                f"skipping the availability wait"
            )
            return True

        timeout_s = self._SHARES_FREE_S if timeout_s is None else timeout_s
        # `time.monotonic`, not `self.clock` — the wait is spent in `asyncio.sleep`, real time. A
        # simulated clock would never reach the deadline.
        deadline = time.monotonic() + timeout_s
        while True:
            available = await self._venue_shares_available(instrument_id)
            if available is not None and available >= needed:
                return True
            if time.monotonic() >= deadline:
                self.log.error(
                    f"{instrument_id}: {needed} shares still not available after {timeout_s}s "
                    f"(broker says {available}) — sending nothing"
                )
                return False
            await asyncio.sleep(0.25)

    #: How long the protection reconciler is told to stand off while an exit is in flight. Covers the
    #: cancel confirm (6s) plus the share wait (10s) plus the submit, with margin — and NOTHING beyond
    #: that, because every second here is a position knowingly left without a stop.
    _EXIT_SUPPRESSION_S: float = 45.0

    #: Absolute ceiling on how long one instrument can be held off, however slow the venue is. The
    #: standoff is EXTENDED as the release progresses (see `_extend_standoff`) because
    #: `_cancel_reducing_leg` makes a venue round trip per resting order and none of them are bounded by
    #: a deadline in this code — two resting stops on a slow venue and a fixed 45s expires while the exit
    #: is still genuinely in flight, at which point the reconciler re-arms underneath it. Extending
    #: without a ceiling would trade that for an unbounded naked position, which is worse.
    _EXIT_SUPPRESSION_MAX_S: float = 120.0

    def _flip_pending_rows(self) -> list[dict] | None:
        """The flip record as the frame carries it (#907): one row per (instrument, lane), sorted, plain
        JSON — or None when the last pass never evaluated the book (outside RTH, disabled, settings
        unreadable, no broker), so `[]` can only ever mean "evaluated, nothing deferred". The record
        itself is keyed by TUPLE and would raise inside `json.dumps`, taking the whole health frame
        with it — this is the one derivation of its wire shape."""
        if not self._flip_evaluated:
            return None
        return [
            {"instrument_id": iid, "strategy_id": lane, "passes": int(v["passes"]),
             "streak_started_ns": int(v["streak_started_ns"]), "last_reason": str(v["last_reason"])}
            for (iid, lane), v in sorted(self._flip_pending.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        ]

    def _active_exit_suppressions(self, now_ns: int) -> dict[str, int]:
        """Exit standoffs that still stand, pruning any that have expired.

        SEPARATE FUNCTION ON PURPOSE. This started life as a dict comprehension inline in the
        reconciler, and deleting the `> now_ns` comparison — turning every standoff permanent, which is
        a position left naked for good — kept the whole suite GREEN. The deadline was asserted; the
        EXPIRY never was, because nothing could drive it without standing up the entire reconciler.

        Pruning at the point of use rather than on a timer keeps one writer and one reader, so the
        stored state cannot drift from the check that consults it.
        """
        self._exit_suppressed = {
            iid: until for iid, until in self._exit_suppressed.items() if until > now_ns
        }
        return self._exit_suppressed

    def _extend_standoff(self, instrument_id: str, ceiling_ns: int) -> None:
        """Push the standoff out to cover the step about to run, never past the absolute ceiling.

        The first version set one 45s deadline up front and hoped the whole sequence fit. It does not
        have to: the cancel alone is one `list_orders` plus one `cancel_order` per resting order, and
        nothing in this file bounds them. When it overran, the reconciler re-armed a stop underneath a
        live exit and re-reserved the shares the release had just freed.
        """
        until = min(self.clock.timestamp_ns() + int(self._EXIT_SUPPRESSION_S * 1e9), ceiling_ns)
        self._exit_suppressed[instrument_id] = until
        # EXTEND the observation window with the standoff it describes — never open one here. This
        # method knows nothing about the exit's quantity, and the first version of #546 part 2 tried
        # to open a window here with an undefined `qty` and an invented constant; the repo's own
        # F821 guard caught it. The window is OPENED where the exit is actually released.
        win = self._exit_windows.get(instrument_id)
        if win is not None:
            win["until"] = until

    async def release_for_exit(self, instrument_id: str, qty, side: str = "SELL",
                               strategy_id: str | None = None) -> bool:
        """Take back the shares this position's own protective stop is holding, so an exit can fill.

        THE FAILURE THIS ENDS. On 2026-08-19 MOMENTUM-002 decided its first rotation since 08-14 and both
        exits were REJECTED by the venue — `FSM 933` and `VCTR 88`, each `available: 0` — while the shares
        were plainly held (`long_market_value` 74,638.76). They were RESERVED, by the `PROT-SELL-*`
        trailing stop covering the full position. The rotation could not rotate, and every other strategy
        stayed unfunded behind capital it could not release. Third live reproduction (#245, #252).

        NOTHING IN THE SEQUENCE IS NEW. It is the one already hardened by two prior incidents and already
        running at `_execute_flatten` and PEAK — the venue fallback for cache-terminal orders, the
        reservation-versus-order-status distinction, the monotonic deadlines. It was simply never
        reachable from the strategy exit path. Routing it is the fix; a fourth hand-rolled copy would
        re-lose all of it.

        NOT `Strategy.market_exit()` (nautilus 1.229, `strategy.pyx:1750`), which is the closest native
        mechanism and was checked rather than assumed. Three reasons it does not apply: it gates on
        `cache.orders_open(..., self.id)`, which is the cache blindness `_cancel_reducing_leg` exists to
        defeat; it waits on ORDER STATUS rather than on the reservation, and those are different events
        (#245); and it closes every position the strategy holds, not the one being rotated. There is no
        native concept of a venue share reservation at all — `qty_available` appears nowhere in the
        package — so nothing native can express this.

        WHOSE ORDER IS BEING CANCELLED. The caller passes MOMENTUM-002, and that is WRONG — it made
        half this sequence a no-op. Protective stops are built by THIS strategy's `order_factory`
        (`_build_order`), and this strategy is MANUAL-001 — so a MOMENTUM-002 filter excluded the very
        stop being cancelled, and `_await_reducing_orders_clear` returned True on its first poll having
        confirmed nothing. The `/orders` API shows MOMENTUM-002 on those rows because it attributes them
        to the POSITION; the Nautilus order says otherwise. Reading the projection and reporting it as
        the fact is verification by inspection, which this project's rules say does not work.

        `strategy_id` IS NOT THE FILTER, AND THIS PARAGRAPH USED TO CLAIM IT WAS GONE. It is not gone:
        it is in the signature, and as of `cb5ee79` the deployed kumo-strategies passes it on every
        exit (`nautilus/broker.py:162`, `strategy_id=req.strategy_id`). What it is NOT is the owner
        filter — see "TWO IDENTITIES, DELIBERATELY" below. `owner` stays `self.id` (MANUAL-001), which
        is who actually holds the protective stops; the lane is carried separately as the
        AUTHORISATION identity (`canceller`), and its arrival is what retires the `proxy` concession.

        So the caller passing it is the DESIGN COMPLETING, not a regression — and the conformance test
        that demanded the caller pass nothing was stale rather than protective. The invariant worth
        pinning is that the lane never becomes `owner`.

        FALSE MEANS SEND NOTHING. The position keeps whatever protects it, which is the safe end state —
        acting on an unconfirmed release is how five protective stops were lost on 2026-08-12.

        Called from `NautilusBroker.exit()` (kumo-strategies) on the exit-side SELL path only.
        """
        if str(side).upper() != "SELL":
            # The BUY branch is still REFUSED, deliberately. `_venue_shares_available` now handles a
            # short correctly (#641 — derived availability answers in absolute shares on the position's
            # own reducing side), but the release SEQUENCE below is SELL-only: `_cancel_reducing_leg`
            # and `_await_reducing_orders_clear` are both invoked with `OrderSide.SELL` hardcoded, so a
            # BUY release would cancel and wait on the wrong leg. Raising says so; defaulting to BUY
            # quietly cancelled the ENTRY leg of a long.
            raise ValueError(
                f"release_for_exit supports SELL exits only, got {side!r} — the release sequence "
                f"(cancel + confirm) is hardcoded to the SELL leg and would act on the wrong orders "
                f"for a short"
            )

        # DO NOT REMOVE PROTECTION THIS ENGINE CANNOT PUT BACK. Both re-arm routes are conditional: the
        # reconciler returns early when the flag is off (it defaults to False) and again outside RTH. A
        # release at 15:58 ET, or with protection disabled, would cancel a stop that nothing restores —
        # naked until the next session, or forever. The exit not going out is recoverable; that is not.
        from api.settings import resolve

        try:
            enabled = bool(resolve("protection").get("enabled", False))
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"{instrument_id}: protection settings unreadable ({exc}) — releasing nothing")
            return False
        if not enabled or not _us_market_open(self.clock.timestamp_ns()):
            self.log.warning(
                f"{instrument_id}: not releasing shares — protection cannot re-arm "
                f"(enabled={enabled}, market_open={_us_market_open(self.clock.timestamp_ns())}). "
                f"An exit is worth less than a position left permanently unprotected."
            )
            return False

        # A Decimal, never a float. `_await_shares_available` compares with `>=` against a quantity read
        # back from the venue, and the float/Quantity seam at exactly this kind of boundary is what left
        # the entire shrink path dead on arrival once already.
        needed = Decimal(str(qty))
        # BOTH IDENTITIES ARE THE FILTER (#840). MANUAL-001 built the aggregate stops; since #748 the
        # stop covering a lane's shares is built with that lane's own factory and carries ITS id. A
        # MANUAL-only filter matched nothing on every lane exit — the cancel still landed through the
        # venue fallback, but the wait was blind, asked the venue 1 s later, saw the stop still resting
        # (the cancel confirms ~4 s after the request) and reported not-released: 60 refused exits in
        # six sessions, every lane exit on the stack. The set matches whichever regime stamped the
        # stop, which is what the comment on `_await_reducing_orders_clear` asks for. The lane is
        # STILL not an authorisation here — that is `canceller`, below, unchanged.
        owner = {str(self.id)} | ({str(strategy_id)} if strategy_id else set())

        # Mark BEFORE cancelling, not after. The reconciler runs on its own timer and would otherwise be
        # free to re-arm in the window between the cancel and the mark — reinstating the reservation this
        # is about to clear. Integer nanoseconds throughout: `int(ts + s * 1e9)` routes ~1.8e18 through
        # float64, whose ULP there is 256ns.
        ceiling_ns = self.clock.timestamp_ns() + int(self._EXIT_SUPPRESSION_MAX_S * 1e9)
        # OPEN THE OBSERVATION WINDOW HERE, where the exit's quantity is known and the release
        # actually begins (#546 part 2). It is the observable that does not depend on an event:
        # `NautilusBroker.exit` releases and then calls `submit()`, which refuses LOCALLY on an
        # unsubscribed instrument, a missing definition, an exception, or an order absent from the
        # cache — returning ok=False with no Nautilus order and therefore no rejection to hook.
        # A window that closes having never carried an exit is that silent case, made visible.
        # A SECOND RELEASE OVERWRITES THE FIRST WINDOW, so say so rather than losing it silently
        # (review round 3). The map is keyed by instrument, and two exits on one name — two lanes
        # rotating the same pool — is a real shape here.
        prior = self._exit_windows.get(instrument_id)
        if prior is not None and not prior.get("saw_exit"):
            self.log.warning(
                f"{instrument_id}: a second exit release opened while the previous window had "
                f"carried no exit — the earlier release's outcome is no longer observable (#546)"
            )
        self._exit_windows[instrument_id] = {
            "until": self.clock.timestamp_ns() + int(self._EXIT_SUPPRESSION_S * 1e9),
            "saw_exit": False,
            "qty": int(qty or 0),
            # WHEN and WHOSE, so a replayed historical fill or another lane's exit on the same
            # instrument cannot mark this window carried (review round 3).
            "opened_ns": self.clock.timestamp_ns(),
            "lane": str(strategy_id) if strategy_id else None,
        }
        self._extend_standoff(instrument_id, ceiling_ns)

        released = False
        try:
            # Cancel through Nautilus where the cache can and at the VENUE where it cannot: an order the
            # cache holds as REJECTED still reserves shares, and `Strategy.cancel_order` refuses to act on
            # it (strategy.pyx:1651). Then wait on the RESERVATION rather than on order status — Alpaca
            # frees shares on the confirmed cancel, and those are not the same event.
            #
            # The standoff is pushed out before EACH step, because the step that just finished may have
            # taken longer than the whole original window — one venue round trip per resting order, none
            # of them bounded here.
            # PROXY: this path is handed no strategy_id, so `owner` is MANUAL-001 for EVERY lane's
            # exit. Without saying so, the attribution rule refuses a lane's own protective stop —
            # measured on the live book, PROT-SELL-BDX-XNYS-3311fe07 owned by MOMENTUM-002. The flag
            # names the gap rather than hiding it; #462 stays unfixed on this one path until the lane
            # is threaded through `NautilusBroker.exit`.
            # TWO IDENTITIES, DELIBERATELY. `owner` is the FILTER — which resting orders to cancel and
            # wait on — and stays MANUAL-001 because that is who holds the protective stops. The lane,
            # when the caller names it, is the AUTHORISATION identity and nothing else.
            #
            # `proxy` only while the lane is unknown: kumo-strategies' `NautilusBroker.exit` does not
            # pass one yet, so every exit still presents as the feed and the concession keeps a lane's
            # own stop reachable. It disappears the moment `strategy_id` arrives.
            await self._cancel_reducing_leg(
                instrument_id, owner, OrderSide.SELL,
                proxy=strategy_id is None, canceller=strategy_id,
            )
            self._extend_standoff(instrument_id, ceiling_ns)
            if await self._await_reducing_orders_clear(instrument_id, owner, OrderSide.SELL):
                self._extend_standoff(instrument_id, ceiling_ns)
                released = await self._await_shares_available(instrument_id, needed)
            if not released:
                self.log.error(
                    f"{instrument_id}: {needed} shares were not released within the timeout — sending no "
                    f"exit. Protection re-arms on the next reconciler tick."
                )
        except Exception as exc:
            # The cancel may ALREADY have gone out and cannot be un-requested, so the position may now be
            # bare. `log.exception` takes the exception as a REQUIRED second argument
            # (component.pyx:1564, `Condition.not_none(ex, "ex")`) — omitting it raises TypeError inside
            # this handler and propagates instead of returning False.
            self.log.exception(
                f"{instrument_id}: failed while releasing shares for an exit — THIS POSITION MAY NOW BE "
                f"UNPROTECTED until the reconciler re-arms it",
                exc,
            )
        finally:
            # The standoff exists to stop a NEW stop re-reserving shares an exit is about to take. If no
            # exit is going out, it protects nothing and only delays recovery — up to 45s of standoff plus
            # up to 60s to the next tick. Dropping it on every failure hands the position back to the
            # reconciler immediately. `finally`, so a cancelled task (CancelledError is a BaseException
            # and misses the `except` above) cannot leave a live mark behind either.
            if not released:
                self._exit_suppressed.pop(instrument_id, None)
                # ...and its observation window, in the SAME branch: nothing was sent, so there is
                # no missing exit to report (#546, review round 2).
                self._close_exit_window(instrument_id)
        return released

    async def _protection_status_note(
        self, reason: str, cleared_count: int, instrument_id: str, strategy_id: str, side: str
    ) -> str:
        """State what is ACTUALLY resting, rather than warning about what might be.

        This used to say "THIS POSITION MAY NOW BE UNPROTECTED. Check the broker before retrying." on every
        failure once any cancel had gone out. Two things were wrong with that. It made the operator do a
        check the engine can do in one call — it already reads the venue for exactly this on every exit
        path. And it goes STALE within a minute: the protection reconciler re-arms on its next tick, so by
        the time anyone reads the message the position is usually covered again. On 2026-08-19 an operator
        saw this on LFST while a PROT-SELL for the full 429 shares was resting the whole time.

        A warning that cries wolf is one an operator learns to scroll past, and this one sits on the path
        where a genuinely naked position must be believed.

        A separate method rather than a closure because the three branches are the whole point and a
        closure inside a 200-line handler cannot be driven by a test.
        """
        if not cleared_count:
            return reason
        try:
            covered = await self._reducing_qty_for_exit(instrument_id, strategy_id, side)
        except Exception:  # noqa: BLE001 — reporting must never raise over the failure it is reporting
            covered = None
        if covered is None:
            return (
                f"{reason} — {cleared_count} resting exit order(s) were cancelled and the broker could not "
                f"be read, so THIS POSITION MAY NOW BE UNPROTECTED. Check the broker before retrying."
            )
        if covered > 0:
            return (
                f"{reason} — {cleared_count} resting exit order(s) were cancelled, but {covered} share(s) "
                f"are still covered by a resting exit, so the position is protected. Safe to retry."
            )
        return (
            f"{reason} — {cleared_count} resting exit order(s) were cancelled and NOTHING is resting now, "
            f"so THIS POSITION IS UNPROTECTED. Protection re-arms automatically within about a minute; "
            f"check the broker before retrying."
        )

    async def _await_reducing_orders_clear(
        self, instrument_id: str, strategy_id: "str | set[str]", reducing_side, timeout_s: float | None = None
    ) -> bool:
        """Wait until no reducing order rests for this position. True if it cleared, False on timeout.

        The piece whose absence caused #245/#252: an HTTP 200 on a cancel means "request accepted", not
        "shares released". Alpaca reserves shares against a resting sell and frees them only when the
        cancel is CONFIRMED, so anything submitted in between is rejected on `available: 0` — while the
        cancel then lands anyway and takes the protection with it.

        Callers must treat False as "send nothing". The position keeps whatever was protecting it, which
        is the safe outcome; acting on an unconfirmed cancel is not.
        """
        import asyncio
        import time

        timeout_s = self._CANCEL_CONFIRM_S if timeout_s is None else timeout_s
        # `time.monotonic`, not `self.clock` — the wait is spent in `asyncio.sleep`, which is real time.
        # Measuring the deadline on a simulated clock would never expire.
        deadline = time.monotonic() + timeout_s

        # WHETHER THERE WAS EVER ANYTHING TO WAIT FOR. This function re-derives its subject from a
        # FILTER on every poll, so it cannot tell "the orders I cancelled are gone" from "my filter
        # never matched anything" — both are an empty list, and the empty list is read as success on
        # the step whose absence caused #245 and #252, where True means "the shares are free, send
        # the exit".
        #
        # The filter used to be MANUAL-001 alone, and this paragraph predicted #840: "when #748
        # stamps stops with the LANE, the same filter matches nothing". It did — on every lane exit
        # for six sessions — and this guard held (the venue said "still resting", so no exit went out
        # into reserved shares). The exit path now passes BOTH stamps (`release_for_exit`), so the
        # wait observes the stop whichever regime built it. REGIME-DEPENDENT CORRECTNESS IS THE
        # TRAP, so the guard below still does not depend on the regime.
        observed_any = bool(self._reducing_orders_open(instrument_id, strategy_id, reducing_side))

        while time.monotonic() < deadline:
            if not self._reducing_orders_open(instrument_id, strategy_id, reducing_side):
                if observed_any:
                    # Positive confirmation: this wait WATCHED something disappear.
                    return True
                return await self._venue_says_leg_is_clear(instrument_id, reducing_side)
            await asyncio.sleep(0.25)

        if self._reducing_orders_open(instrument_id, strategy_id, reducing_side):
            return False
        if observed_any:
            return True
        return await self._venue_says_leg_is_clear(instrument_id, reducing_side)

    async def _venue_says_leg_is_clear(self, instrument_id: str, reducing_side) -> bool:
        """The one plane whose answer does not depend on whose name is on the order.

        Reached only when the cache filter was EMPTY FROM THE START, i.e. this wait never observed
        anything and therefore confirmed nothing. The venue knows nothing about our sleeves — usually
        a limitation, here the whole point.

        A read that FAILS is not a clear leg. Callers treat False as "send nothing", so the position
        keeps whatever protects it, which is the safe outcome; absence of evidence is a timestamp,
        not a property.
        """
        rows = await self._venue_reducing_orders(instrument_id, _side_name(reducing_side))
        if rows is None:
            self.log.warning(
                f"exit release on {instrument_id}: nothing matched the cache filter and the broker "
                f"could not be read, so this wait CONFIRMED NOTHING — reporting not-released rather "
                f"than sending an exit into shares that may still be reserved"
            )
            return False
        if rows:
            coids = ", ".join(str(r.get("client_order_id") or r.get("id") or "?") for r in rows)
            self.log.error(
                f"exit release on {instrument_id}: nothing matched the cache filter, so this wait "
                f"CONFIRMED NOTHING — but the venue still shows {len(rows)} reducing order(s) "
                f"reserving these shares ({coids}). Reporting not-released. If this is a protective "
                f"stop owned by another strategy, the exit path's owner filter and the stop's "
                f"stamp disagree (#748)"
            )
            return False
        return True

    async def _await_protection_resting(self, coid: str, timeout_s: float = 6.0) -> bool:
        """Is the stop we just submitted actually WORKING at the venue? (#265, codex review Critical.)

        `_submit_trailing_stop` is fire-and-forget — Nautilus hands the command to the exec engine and
        returns. Under the old place-then-cancel ordering that was survivable: a rejected trail left the
        pre-existing stop untouched. Reordering for Alpaca's share reservation removed that safety net, so
        an unconfirmed submit can now report "armed" over a position with nothing resting at all — the
        operator is told they are protected precisely when they are not.

        Mirrors `_await_reducing_orders_clear`: a monotonic deadline (a simulated clock would never
        expire), polling the cache the exec engine writes into. Returns False on rejection, denial or
        cancellation as well as on timeout — every "not resting" answer is the same answer to the caller.
        """
        import asyncio
        import time

        from nautilus_trader.model.enums import OrderStatus
        from nautilus_trader.model.identifiers import ClientOrderId

        # PENDING_CANCEL is NOT working (codex review, High). An order on its way out protects nothing, and
        # counting it would let the arm commit a manager row over a stop that is about to disappear —
        # the same "reported armed over a naked position" failure this function exists to prevent, one
        # step subtler. PENDING_UPDATE stays: a modify in flight leaves the order resting throughout.
        working = {OrderStatus.ACCEPTED, OrderStatus.TRIGGERED, OrderStatus.PARTIALLY_FILLED,
                   OrderStatus.FILLED, OrderStatus.PENDING_UPDATE}
        dead = {OrderStatus.REJECTED, OrderStatus.DENIED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
                OrderStatus.PENDING_CANCEL}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                order = self.cache.order(ClientOrderId(coid))
            except Exception:  # noqa: BLE001 — a malformed id must not raise inside the arm path
                order = None
            if order is not None:
                if order.status in working:
                    return True
                if order.status in dead:
                    return False
            await asyncio.sleep(0.25)
        return False

    def _any_reducing_order_open(self, instrument_id: str, strategy_id: str, reducing_side) -> bool:
        """Any resting order that would reduce this position, identifiable or not — #46's arm-time guard:
        refuse to arm PEAK (rather than guess) if something's resting that `_bracket_protective_stop_open`
        couldn't positively identify as the position's own bracket stop."""
        return bool(self._reducing_orders_open(instrument_id, strategy_id, reducing_side))

    def _closing_order_for(
        self,
        instrument_id: str,
        strategy_id: str,
        since: datetime | None = None,
        reducing_side: OrderSide | None = None,
    ):
        """The relevant FILLED order for this instrument+strategy. WITHOUT `since`: the most recent one.
        WITH `since` (a manager's own `created_at`): the EARLIEST qualifying fill at/after it, NOT the most
        recent (codex review, #47: a stale watcher must react to the FIRST closure after its OWN attach —
        if the position reopened and closed AGAIN after this manager started watching, "most recent" would
        hand a completely unrelated later stop-out to this manager instead of correctly finding none for its
        own episode). `reducing_side` (codex review, second pass): a LONG is closed by a SELL, a SHORT by a
        BUY — WITHOUT this filter, a same-side ADD/scale-in fill after attach (e.g. pyramiding) could be
        mistaken for the position's eventual close, since both are just "a FILLED order" otherwise. Best-
        effort: the cache has no direct "which fill closed this position" link.

        KNOWN LIMITATION (codex review, disclosed not silently missed): "earliest reducing-side fill" is
        still not the same as "the fill that actually flattened the position" — a PARTIAL manual sell (still
        holding some) followed later by the real full stop-out would have this method return the earlier
        PARTIAL fill, which (having no bracket tag) gets classified as a non-stop exit by
        `_StopReenterWatch.apply`, missing a legitimate rearm. Fails closed (no rearm happens when it maybe
        should have — never an unsafe order), but a real functionality gap. A correct fix needs to correlate
        fills to actual position-flat transitions over time, which the cache doesn't expose directly."""
        from datetime import datetime as _dt

        candidates = [
            o
            for o in self.cache.orders_closed()
            if str(o.instrument_id) == instrument_id
            and str(o.strategy_id) == strategy_id
            and o.status.name == "FILLED"
            and (reducing_side is None or o.side == reducing_side)
            and (since is None or _dt.fromtimestamp((o.ts_last or 0) / 1e9, tz=UTC) >= since)
        ]
        if not candidates:
            return None
        if since is not None:
            return min(candidates, key=lambda o: o.ts_last or 0)
        return max(candidates, key=lambda o: o.ts_last or 0)

    def _mark_px(self, instrument_id: str):
        """Latest observable price for MARKET pricing — None when we have no mark, which refuses the transfer
        rather than inventing one."""
        bar_px = None
        for pos in self.cache.positions_open():
            if str(pos.instrument_id) == instrument_id:
                bar_px = self._last_price_for(instrument_id)
                break
        return Decimal(str(bar_px)) if bar_px else None

    def _last_price_for(self, instrument_id: str, max_age_ns: int | None = None):
        """Last traded price from the native cache — the same source the prices plane publishes.

        `max_age_ns` bounds how old the observation may be; `None` keeps the previous behaviour exactly,
        so no existing caller changes until it opts in. **The bound is deliberately opt-in per caller**:
        refusing a stale price is right for sizing an entry and wrong for protection, where returning
        nothing leaves a position uncovered — that is a risk decision each caller has to make, not one to
        smuggle in through a plumbing change.

        WHY THIS EXISTS. Both sources carry an observation time — `TradeTick.ts_event`, `Bar.ts_event` —
        and both were discarded here, so no caller could tell a price from this second from one cached
        before a feed outage. On 2026-08-18 the Alpaca websocket was down for 27 minutes and flapping
        either side of it; the engine served the last cached trade throughout and nothing could have
        known. A stale price sizes a stop at the wrong width and an entry at the wrong quantity, and
        neither failure announces itself.
        """
        price, ts_event = self._last_price_and_ts_for(instrument_id)
        if price is None:
            return None
        if max_age_ns is not None and ts_event is not None:
            age = self.clock.timestamp_ns() - ts_event
            if age > max_age_ns:
                self.log.warning(
                    f"{instrument_id}: last price is {age / 1e9:.0f}s old (bound {max_age_ns / 1e9:.0f}s) "
                    f"— refusing it rather than sizing off a stale observation"
                )
                return None
        return price

    def _last_price_and_ts_for(self, instrument_id: str) -> tuple[float | None, int | None]:
        """The price AND when it was observed. The timestamp is the whole point — see `_last_price_for`."""
        iid = InstrumentId.from_str(instrument_id)
        trade = self.cache.trade_tick(iid)
        if trade is not None:
            return float(trade.price), int(trade.ts_event)
        bars = self.cache.bars(bar_type(iid))
        if bars:
            return float(bars[0].close), int(bars[0].ts_event)
        return None, None

    _TIF_MAP = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
        "opg": TimeInForce.AT_THE_OPEN,  # execute in the opening auction (on-open)
        "cls": TimeInForce.AT_THE_CLOSE,  # execute in the closing auction (on-close) — market or limit only
    }

    def _handle_order_command(self, ctype: str, payload: dict) -> tuple[str, str]:
        """Order commands (#32 increment 2). DISARMED by default → nothing is submitted. Runs on the loop
        (called from _handle_command) so the Strategy calls are on-loop. `submit_order` is the ONLY line
        that places an order, and only when explicitly armed by a human (KUMO_ORDERS_ARMED)."""
        if not self._orders_armed:
            return "error", "orders disarmed (KUMO_ORDERS_ARMED off) — no order submitted"
        try:
            if ctype == "submit_order":
                # client_order_id is REQUIRED — it's the idempotency key (at-least-once delivery + PEL
                # recovery must never double-submit; Nautilus also rejects a duplicate ClientOrderId).
                coid = payload.get("client_order_id")
                if not coid:
                    return "error", "client_order_id required for submit_order"
                if coid in self._seen_orders:
                    return "ok", ""  # idempotent — already submitted
                order = self._build_order(payload)  # fail-closed: raises on any malformed field
                self._submit(order)  # <-- the only live order effect, human-armed
                self._seen_orders.add(coid)
                return "ok", ""
            if ctype == "submit_bracket":
                return self._handle_submit_bracket(payload)
            order = self._lookup_order(payload["client_order_id"])
            if order is None:
                return "error", f"order {payload.get('client_order_id')} not found"
            if ctype == "cancel_order":
                self._cancel(order)
            else:  # modify_order
                instrument = self._instrument_or_raise(order.instrument_id)
                qty = self._make_qty(instrument, payload["quantity"]) if payload.get("quantity") is not None else None
                price = self._canon_price(instrument, payload["price"]) if payload.get("price") is not None else None
                trig = (
                    self._canon_price(instrument, payload["trigger_price"])
                    if payload.get("trigger_price") is not None
                    else None
                )
                self._modify(order, qty, price, trig)
            return "ok", ""
        except Exception as exc:  # noqa: BLE001 — a bad order command acks an error, never crashes trading
            return "error", str(exc)

    # Thin wrappers over the Cython Strategy methods — so the order path is unit-testable (mock these).
    def _submit(self, order, position_id=None) -> None:
        """Submit, optionally naming the position the order acts on.

        `position_id` is what turns `reduce_only` from decoration into enforcement: `risk/engine.pyx:425`
        gates the ENTIRE reduce-only check behind `if command.position_id is not None`, so without it the
        flag is never examined for any order cockpit sends.

        FAIL-SAFE, and this is the important part. It is passed ONLY when the caller resolved a real
        position. A wrong or stale id makes `cache.position(id)` return None, and the risk engine then
        DENIES the order — which for a protective stop would mean no stop at all. Protection is the one
        mechanism in this system that has never failed; it must not become deniable because an id went
        stale. `None` reproduces today's behaviour exactly.
        """
        if position_id is None:
            self.submit_order(order)
            return
        self.submit_order(order, position_id=position_id)

    def _submit_list(self, order_list) -> None:
        self.submit_order_list(order_list)  # native bracket OrderList → OrderEmulator manages contingencies

    def _cancel_unprotectable_stops(self, now_ns: int) -> None:
        """Cancel resting protective stops that can never protect anything (#748).

        A stop stamped with a lane that holds nothing of the instrument is not weak protection — it is
        INERT AND BLOCKING. Under NETTING its fill resolves to a position that has never existed, so
        the ExecEngine rejects it while the shares still leave the broker; and because coverage is
        judged per INSTRUMENT, it permanently prevents a correctly stamped stop from being placed.

        Measured on paper 2026-08-31: 17 of 23 live protective orders were in exactly that state, and
        the fallback that created them has just been removed — which fixes new ones and does nothing
        about those. Each one that triggers repeats the damage in full.

        CANCELLING COSTS NOTHING REAL. That order could not have protected anything. The correctly
        stamped replacement follows on the next 60s tick, and `test_the_refusal_RECOVERS...` pins that
        the placement path does re-arm.

        THE EMPTY BOOK CANNOT AUTHORISE A CANCEL. With no positions visible every stop looks orphaned,
        so a pass that trusted it would cancel ALL protection on a book it merely could not see —
        empty IS the failure being guarded against. `reconcile_protection` refuses there and this
        returns without acting.
        """
        plan = self._observations.run(
            "protection_reconcile",
            reconcile_protection,
            self.cache.positions_open() or [],
            self.cache.orders_open() or [],
            ts_ns=now_ns,
        )
        if plan is None or plan.refused:
            return
        by_coid = {str(getattr(o, "client_order_id", "")): o for o in (self.cache.orders_open() or [])}
        for row in plan.cancel:
            order = by_coid.get(str(row.client_order_id))
            if order is None:
                continue
            self.log.warning(f"protection: cancelling {row.client_order_id} — {row.reason}")
            # Standing state as well as a log line, for the same reason the refusal records one: a
            # cancel nobody can see is indistinguishable from a stop that was never there.
            self._failed_requests.record("protection", str(row.instrument_id), row.reason, ts=now_ns)
            self._cancel(order)

    def _cancel(self, order) -> None:
        self.cancel_order(order)

    def _modify(self, order, quantity, price, trigger_price) -> None:
        self.modify_order(order, quantity=quantity, price=price, trigger_price=trigger_price)

    def _lookup_order(self, client_order_id: str):
        return self.cache.order(ClientOrderId(client_order_id))

    # Sizes/prices must be built at the INSTRUMENT's native precision, not the precision of the incoming
    # JSON number. The API types quantity/price as float, so `16` arrives as `16.0` — a Quantity with
    # precision 1 — which the RiskEngine DENIES against an equity's size_precision 0 ("quantity 16.0 invalid
    # (precision 1 > 0)"). make_qty/make_price quantize to the instrument's increment. A value that ISN'T a
    # clean multiple is REJECTED (fail-closed) rather than silently rounded into a different order.
    _PRICE_FIELDS = ("price", "trigger_price", "stop_trigger", "target_price")

    def _instrument_or_raise(self, iid: InstrumentId):
        instrument = self.cache.instrument(iid)
        if instrument is None:
            raise ValueError(f"instrument {iid} not in cache — cannot build order")
        return instrument

    def _make_qty(self, instrument, value):
        qty = instrument.make_qty(float(value))
        if float(qty) != float(value):
            raise ValueError(
                f"quantity {value} is not a valid multiple of the {instrument.id} size increment"
            )
        return qty

    def _tick_away_from_market(self, instrument, value: float, reducing_side: str):
        """A COMPUTED stop price, put on the instrument's tick in the safe direction (#872).

        `_canon_price` is fail-closed: a value that is not a clean tick multiple RAISES, because an
        operator's typo must never be silently rounded into a different order. That is right for a price
        somebody typed and wrong for `entry - k x ATR`, which lands between ticks as a matter of course
        — a floor at 451.685 on a penny tick would refuse to build at all, every tick, forever.

        So it is quantised here, deliberately, and DOWN for a SELL stop (up for a BUY). Nearest-tick
        rounding could move the trigger toward the market by half a tick; this direction can only ever
        make the stop less likely to fire early, and it cannot turn a placeable floor into one the venue
        rejects for sitting at or above the last price.
        """
        from decimal import ROUND_DOWN, ROUND_UP, Decimal

        increment = Decimal(str(instrument.price_increment))
        rounding = ROUND_DOWN if str(reducing_side).upper() == "SELL" else ROUND_UP
        steps = (Decimal(str(value)) / increment).to_integral_value(rounding=rounding)
        return instrument.make_price(float(steps * increment))

    def _canon_price(self, instrument, value):
        price = instrument.make_price(float(value))
        if float(price) != float(value):
            raise ValueError(f"price {value} is not a valid tick for {instrument.id}")
        return price

    def _canon_manager_params(self, handler, instrument_id: str, params: dict) -> dict:
        """Put a manager's PRICE params on the instrument's tick, at ATTACH.

        Operator, 2026-08-19: "if it is subdecimal you can round. no problem."

        `_canon_price` REFUSES an off-tick value, because silently moving an ORDER's price changes what
        the operator asked the venue to do. A manager LEVEL is a different thing: `floor_price 81.155` on
        a penny-ticked instrument is not a different intention from 81.16, it is the same intention
        written at a precision the venue cannot express.

        Doing it HERE is the point. MNDY armed with 81.155 validated, attached, sat ARMED through the
        stop-out, and only met the tick check when the rearm tried to build the re-entry's protective
        stop from it — `price 81.155 is not a valid tick`, FAILED and terminal, `rearm_count` still 0,
        with nothing on screen. The operator watched the chart for a re-entry abandoned hours earlier.

        `make_price` is the instrument's OWN canonicalisation, the same one every order goes through, so
        the level stored here is exactly the level the rearm will later build from — the two cannot round
        differently. Returns a COPY: the caller's dict is also what the idempotency hash is taken over,
        and that hash must cover the values actually armed, not the ones typed.
        """
        price_params = getattr(handler, "PRICE_PARAMS", ())
        if not price_params:
            return params
        instrument = self._instrument_or_raise(InstrumentId.from_str(instrument_id))
        out = dict(params)
        for field in price_params:
            if out.get(field) is None:
                continue
            asked = float(out[field])
            canon = float(instrument.make_price(asked))
            if canon != asked:
                # Logged, not applied invisibly: the operator typed a number and a different one is now
                # armed. One line is the difference between a rounding and a surprise.
                _log.warning(
                    "manager %s %s: %s %s is not on the instrument's tick — armed at %s",
                    type(handler).__name__, instrument_id, field, asked, canon,
                )
            out[field] = canon
        return out

    def _quantize_price_fields(self, instrument, payload: dict) -> dict:
        """Return a copy of `payload` with every price-ish field canonicalised to the instrument's tick, so
        the (pure) action descriptors build Price objects at the correct precision."""
        params = dict(payload)
        for field in self._PRICE_FIELDS:
            if params.get(field) is not None:
                params[field] = str(self._canon_price(instrument, params[field]))
        return params

    def _order_owner_of(self, client_order_id: str) -> str | None:
        """The lane that placed an order, FROM THE CACHE, or None (#748).

        Feeds `coverage_by_lane` / `oversize_by_lane`, which decide how much protection each lane has
        resting and therefore whose stop may be shrunk. Both refuse to act when any resting order
        cannot be attributed, and that refusal only works if this returns None honestly.

        NEVER FROM THE PREFIX, and this is the whole reason it is a separate lookup from
        `_owner_of`/`owner_from_prefix`. Those answer CANCEL AUTHORISATION — "may this caller cancel
        it" — where treating a `PROT-` id as account protection MANUAL-001 may touch is the safe
        direction. Here the question is "whose shares are reserved", and a prefix cannot know.

        The failure that fallback produces is silent and bidirectional. A cache-terminal per-lane stop
        — genuinely MOMENTUM's, invisible to the cache — would be named MANUAL-001, so MOMENTUM reads
        NAKED (the next pass rests a duplicate over reserved shares, which oversells on trigger) and
        MANUAL reads OVER-COVERED (oversize shrinks a stop that is not there to shrink), with
        `is_complete` True throughout so nothing reports a problem.

        AN EMPTY OWNER IS NOT AN OWNER. `""` would land in `by_lane[""]` and be counted as a lane —
        the blank-lane shape this whole area exists to eliminate.

        AND IT CANNOT RAISE. This runs inside the 60s protection reconciler; a lookup that threw would
        take the tick with it, turning an attribution question into a protection outage.
        """
        coid = str(client_order_id or "")
        if not coid:
            return None
        try:
            # TYPED, like `_lookup_order` two functions down. Nautilus's `Cache.order` is Cython and
            # REFUSES a raw str — "Argument 'client_order_id' has incorrect type". Passing one made
            # this raise TypeError on EVERY call in production, swallowed by the except below into
            # None: the producer was dead on arrival while its twelve tests stayed green, because the
            # double accepted strings. A double that cannot represent production's type refusal is
            # the bug — the class this file's own commit message cited one paragraph before shipping
            # it.
            order = self.cache.order(ClientOrderId(coid))
        except Exception:  # noqa: BLE001 — an attribution question must not stop protection
            return None
        lane = str(getattr(order, "strategy_id", "") or "") if order is not None else ""
        return lane or None

    def _lane_order_factory(self, lane: str | None):
        """An `OrderFactory` stamped with the LANE that owns the order, not with this strategy (#748).

        WHY THE STAMP DECIDES WHETHER A FILL IS REAL. Under NETTING the execution engine derives the
        position a fill belongs to as `PositionId(f"{fill.instrument_id}-{fill.strategy_id}")`
        (`execution/engine.pyx`), and `fill.strategy_id` is the ORDER's — read from the cached order on
        the adapter path (`execution/client.pyx:881`) and, the one that matters here, on the
        reconciliation path (`live/reconciliation.py:410,526`, `strategy_id=order.strategy_id`). This
        account has no trade-updates socket, so reconciliation IS the fill path.

        A protective stop built by THIS strategy's factory is stamped MANUAL-001, so its fill resolves
        to `{instrument}-MANUAL-001` — a position that has never existed. `reduce_only` then applies the
        fill to NO position (`_reject_reduce_only_netting_position_open`) and the ≤10s position poll
        fabricates the difference as a synthetic sell at a price that never traded. That is the mint.

        MEASURED, NOT ASSUMED, in the pinned package: `OrderFactory.__init__` takes `strategy_id` as a
        plain constructor argument (`common/factories.pyx`), and `Strategy.submit_order` validates only
        that the order is INITIALIZED — there is no `order.strategy_id == self.id` check
        (`trading/strategy.pyx`). Submitting another lane's order is a supported shape, not a trick.

        `None` RETURNS THIS STRATEGY'S OWN FACTORY, and that is a considered fallback rather than a
        lazy one: every non-protection caller (a discretionary click, a bracket) genuinely IS this
        strategy's order, so the default is as correct as the primary. It is stated here because the
        next reader cannot otherwise tell the two apart.
        """
        if not lane:
            return self.order_factory
        cached = self._lane_factories.get(lane)
        if cached is None:
            from nautilus_trader.common.factories import OrderFactory

            cached = OrderFactory(
                trader_id=self.trader_id,
                strategy_id=StrategyId(lane),
                clock=self.clock,
            )
            self._lane_factories[lane] = cached
        return cached

    def _build_order(self, payload: dict):
        """Construct a Nautilus order from a command payload via the MANUAL-lane order factory. FAIL-CLOSED:
        any missing/invalid field raises (an armed malformed command must NOT place an unintended order) —
        no silent defaulting of side/type/TIF."""
        iid = InstrumentId.from_str(payload["instrument_id"])
        side_s = str(payload["side"]).upper()
        if side_s not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {payload['side']!r}")
        side = OrderSide.BUY if side_s == "BUY" else OrderSide.SELL
        instrument = self._instrument_or_raise(iid)
        qty = self._make_qty(instrument, payload["quantity"])
        tif_s = str(payload.get("time_in_force", "day")).lower()
        if tif_s not in self._TIF_MAP:
            raise ValueError(f"invalid time_in_force {payload.get('time_in_force')!r}")
        tif = self._TIF_MAP[tif_s]
        coid = ClientOrderId(payload["client_order_id"]) if payload.get("client_order_id") else None
        otype = str(payload["order_type"]).lower()  # required — no default (fail-closed)
        # Extended-hours (pre/post-market): carried as an order tag the exec adapter reads. FAIL-CLOSED —
        # strict bool (a stray "false"/"0"/"no" string must NOT arm it), and Alpaca only accepts
        # extended-hours on a DAY limit order → reject any other combination rather than let the venue bounce.
        ext_raw = payload.get("extended_hours", False)
        if not isinstance(ext_raw, bool):
            raise ValueError("extended_hours must be a boolean")
        if ext_raw and (otype != "limit" or tif_s != "day"):
            raise ValueError("extended_hours requires a DAY limit order")
        tags: list[str] = []
        if ext_raw:
            tags.append("extended_hours")
        if payload.get("bracket_group"):  # tag entry + protective legs so the blotter groups the trio (#34)
            tags.append(f"bracket:{payload['bracket_group']}")
        if payload.get("manager_id"):  # traceable to the manager that placed it (#72 ADR, #55)
            tags.append(f"MGR-{payload['manager_id']}")
        # Caller-supplied tags (#872: `mode:entry_floor`, so the UI labels a floor from the order that
        # placed it rather than re-deriving the kind). FAIL-CLOSED on the shape: a bare string would
        # otherwise be spread into one tag per character.
        extra = payload.get("extra_tags") or []
        if not isinstance(extra, list) or not all(isinstance(t, str) for t in extra):
            raise ValueError("extra_tags must be a list of strings")
        tags.extend(extra)
        tags = tags or None
        # On-open/on-close auction TIFs are venue-valid only for market or limit orders (never stops).
        if tif_s in ("opg", "cls") and otype not in ("market", "limit"):
            raise ValueError(f"{tif_s} time-in-force requires a market or limit order")
        # Pluggable order type (#50): the effective action is DERIVED from the validated `order_type` (legacy
        # `stop` → `stop_market`) so construction and every gate above key off the SAME type. An explicit
        # `action_id` that DISAGREES is rejected (fail-closed) — until the UI drives the whole path off the
        # action, a divergent action_id must never build a type the validation didn't check (codex-flagged).
        action_id = "stop_market" if otype == "stop" else otype
        explicit = payload.get("action_id")
        if explicit is not None and str(explicit) != action_id:
            raise ValueError(f"action_id {explicit!r} must match order_type {otype!r}")
        descriptor = get_action(action_id)
        descriptor.validate(payload)  # fail-closed on the type's required params (e.g. limit needs price)
        params = self._quantize_price_fields(instrument, payload)  # prices at the instrument's tick
        # THE ORDER'S OWNER, not the submitter's. See `_lane_order_factory`: this is what decides
        # which position a reduce-only fill lands on. Absent key => this strategy, which is correct
        # for every path except protection.
        ctx = OrderBuildContext(
            self._lane_order_factory(payload.get("strategy_id")),
            iid, side, qty, tif, coid, tags,
        )
        return descriptor.build(ctx, params)

    _ENTRY_TYPE_MAP = {"market": OrderType.MARKET, "limit": OrderType.LIMIT}

    def _handle_submit_bracket(self, payload: dict) -> tuple[str, str]:
        """Bracket (#34): build a NATIVE Nautilus bracket OrderList (entry + protective STOP_MARKET +
        take-profit LIMIT) and submit it via submit_order_list. Nautilus's OrderEmulator manages the
        contingencies natively — OTO (entry fill releases the children), OUO/OCO (a leg fill cancels/reduces
        the sibling), including partial-fill qty sync — so we DON'T hand-roll any of that. Emulated on
        LAST_PRICE (our feed is trade ticks); each child is released to the venue as a normal single order
        the Alpaca adapter already reconciles. Fail-closed + idempotent by entry coid."""
        entry_coid = payload.get("client_order_id")
        if not entry_coid:
            return "error", "client_order_id required for submit_bracket"
        if entry_coid in self._seen_orders:
            return "ok", ""  # idempotent — bracket already submitted
        if payload.get("stop_trigger") is None:
            return "error", "bracket requires a stop_trigger (protective stop)"
        if payload.get("target_price") is None:
            return "error", "bracket requires a target_price (native bracket = entry + stop + target)"
        if not payload.get("sl_client_order_id") or not payload.get("tp_client_order_id"):
            return "error", "sl_client_order_id and tp_client_order_id are required"
        side = str(payload["side"]).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {payload['side']!r}")
        entry_type_s = str(payload.get("entry_order_type", "market")).lower()
        if entry_type_s not in self._ENTRY_TYPE_MAP:
            raise ValueError(f"bracket entry must be market or limit, got {entry_type_s!r}")
        if entry_type_s == "limit" and payload.get("price") is None:
            raise ValueError("limit entry requires a price")
        group = payload.get("group_id") or entry_coid
        # Route the OrderList build through the `bracket` action descriptor (#50) — validation above unchanged;
        # the descriptor only does the factory.bracket call → behaviour-identical.
        biid = InstrumentId.from_str(payload["instrument_id"])
        instrument = self._instrument_or_raise(biid)
        ctx = OrderBuildContext(
            self.order_factory,
            biid,
            OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            self._make_qty(instrument, payload["quantity"]),
            self._TIF_MAP[str(payload.get("time_in_force", "day")).lower()],
            ClientOrderId(entry_coid),
            None,
        )
        order_list = get_action("bracket").build(
            ctx,
            {
                "entry_order_type": self._ENTRY_TYPE_MAP[entry_type_s],
                "price": str(self._canon_price(instrument, payload["price"])) if entry_type_s == "limit" else None,
                "stop_trigger": str(self._canon_price(instrument, payload["stop_trigger"])),
                "target_price": str(self._canon_price(instrument, payload["target_price"])),
                "sl_client_order_id": ClientOrderId(payload["sl_client_order_id"]),
                "tp_client_order_id": ClientOrderId(payload["tp_client_order_id"]),
                "group": group,
            },
        )
        self._submit_list(order_list)  # only live effect — human-armed; emulator manages the rest
        self._seen_orders.add(entry_coid)
        return "ok", ""

    # R-multiple beyond the entry for the re-entry bracket's take-profit leg (#47). Nautilus's installed
    # `OrderFactory.bracket()` does NOT actually support a stop-only (no-TP) list despite its `tp_price=None`
    # default — confirmed by CALLING it directly (codex review, round 3: `inspect.signature` alone lied; the
    # implementation still tries to construct a TP `Price` and raises `TypeError` on `None`) — and the
    # Alpaca exec client separately rejects any submitted list missing a take-profit leg. Rather than hand-
    # roll an OTO order list (this codebase's own standing rule: check for a native mechanism before hand-
    # rolling order/exec logic, and CONFIRM it by running it, not just reading a signature), this reuses the
    # EXISTING, already-proven `bracket` action with a wide, intentionally-unlikely-to-fill target — the
    # ratchet's own ethos ("let it run") argues against a tight fixed take-profit anyway; a wide one is
    # closer to "no real target" in practice than a hand-rolled 2-leg contingency would be worth the risk of.
    _REENTRY_TARGET_R = 3.0

    def _submit_stop_bracket_reentry(
        self,
        *,
        instrument_id: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        entry_coid: str,
        manager_id: str,
    ) -> None:
        """Entry LIMIT + protective STOP_MARKET + a wide take-profit LIMIT — the EXISTING, tested `bracket`
        action (#47, STOP-AND-REENTER's re-entry leg; codex review: a naked plain-limit re-entry violated
        the platform's "every position has a protective stop" invariant; a genuinely stop-only 2-leg list
        is not actually buildable in this Nautilus/Alpaca stack today, see `_REENTRY_TARGET_R`'s comment).
        Same construction shape as `_handle_submit_bracket` (native OrderList, `_submit_list`, broker-
        enforced legs) but called from a `ManagerHandler.apply()`, not a command payload — no idempotent-
        command wrapping here since `_seen_orders` (below) is the SAME de-dup primitive every other manager-
        placed order in this file already uses (`_DeferredFlatten.apply`). Stop trigger = the rearm zone's
        own `floor_price` — if price falls back through the floor after re-entering, that is exactly the
        WALK signal this feature already treats as thesis-dead."""
        risk = abs(entry_price - stop_price)
        target_price = entry_price + self._REENTRY_TARGET_R * risk if side == "BUY" else entry_price - self._REENTRY_TARGET_R * risk
        # Preflight (codex review, round 4) — BEFORE any instrument/cache lookup: a SHORT with a very wide
        # stop (floor far above entry) can drive 3R past zero — a non-positive limit price would otherwise
        # build without raising (Nautilus doesn't reject it) and only fail once it reaches the broker,
        # leaving an orphaned watch behind it. Fail closed HERE instead, before ever calling `_submit_list`.
        if target_price <= 0:
            raise ValueError(
                f"computed re-entry take-profit {target_price} is non-positive (stop geometry too wide) — refusing"
            )

        biid = InstrumentId.from_str(instrument_id)
        instrument = self._instrument_or_raise(biid)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        sl_coid = f"{entry_coid}-SL"[:36]
        tp_coid = f"{entry_coid}-TP"[:36]
        group = entry_coid
        ctx = OrderBuildContext(
            self.order_factory,
            biid,
            order_side,
            self._make_qty(instrument, quantity),
            TimeInForce.DAY,
            ClientOrderId(entry_coid),
            None,
        )
        order_list = get_action("bracket").build(
            ctx,
            {
                "entry_order_type": OrderType.LIMIT,
                "price": str(self._canon_price(instrument, entry_price)),
                "stop_trigger": str(self._canon_price(instrument, stop_price)),
                "target_price": str(self._canon_price(instrument, target_price)),
                "sl_client_order_id": ClientOrderId(sl_coid),
                "tp_client_order_id": ClientOrderId(tp_coid),
                "group": group,
            },
        )
        self._submit_list(order_list)
        self._seen_orders.add(entry_coid)

    def _submit_trailing_stop(
        self, *, instrument_id: str, side: str, quantity: float, trail_bps: float, coid: str,
        manager_id: str | None, strategy_id: str | None = None,
    ):
        """Submit a resting, BROKER-NATIVE trailing stop (#46, PEAK) via the `trailing_stop` order action —
        Alpaca tracks the high-water-mark itself once it rests (NOT engine-emulated; see that action's own
        docstring for why). Returns the built order (not awaited/confirmed — submission is fire-and-forget
        into Nautilus's async execution engine, same as every other manager-placed order in this file)."""
        # NOT DURING AN EXIT RELEASE (#358). PEAK's arm, the manager tick's tighten and
        # `_replace_trailing_stop` all place through here, so this is the one place the standoff can be
        # honoured for them. A PEAK-managed position could otherwise get a fresh trail resting INSIDE
        # the release window, re-reserving exactly the shares the release had just freed, and the exit
        # would be refused on `available: 0` with its protection already cancelled.
        #
        # THE PROTECTION RECONCILER DOES NOT ROUTE THROUGH HERE. This comment used to say it did, and
        # that false claim is why this path went unexamined while the reconciler half of the same
        # defect was fixed twice. The reconciler builds via `_build_order`; the two paths share
        # nothing but the venue.
        #
        # `raise`, not `return None`: the callers below submit BEFORE they cancel the order being
        # replaced, so a silent None would cancel the old stop and place nothing — turning a refusal into
        # a naked position. An exception stops the sequence before anything is given up.
        if instrument_id in self._active_exit_suppressions(self.clock.timestamp_ns()):
            raise ExitReleaseInFlight(
                f"{instrument_id}: not placing a trailing stop — an exit is releasing its shares right "
                f"now, and a new stop would reserve them again"
            )
        # STAMPED WITH THE LANE THAT HOLDS IT, NOT WITH THIS STRATEGY (#748).
        #
        # `self.order_factory` is MANUAL-001's. Under NETTING the position a fill belongs to is
        # derived from the ORDER's strategy_id, so a stop built with it on a lane-held instrument
        # resolves to `{instrument}-MANUAL-001` — a position that has never existed. The fill is
        # rejected, the shares still leave the broker, and the sale never reaches the cache. Measured
        # on paper 2026-08-31: three lanes starved of budget by exactly that.
        #
        # A caller that KNOWS the owner passes it; otherwise it is resolved from the open book.
        owner = strategy_id or stamp_for(instrument_id, self.cache.positions_open() or [])
        if not owner:
            why = stamp_diagnosis(instrument_id, self.cache.positions_open() or [])
            # RAISE, for the reason the exit-release refusal above raises: the callers submit the
            # replacement BEFORE cancelling the order being replaced, so returning None would cancel
            # the old stop and place nothing — turning a refusal into a naked position.
            raise UnstampableProtection(
                f"{instrument_id}: refusing to place a trailing stop with no resolvable owner — an "
                f"unstamped stop mints a phantom when it fills. {why['reason']}"
            )
        biid = InstrumentId.from_str(instrument_id)
        instrument = self._instrument_or_raise(biid)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        tags = [f"MGR-{manager_id}"] if manager_id else None
        ctx = OrderBuildContext(
            self._lane_order_factory(owner),
            biid,
            order_side,
            self._make_qty(instrument, quantity),
            TimeInForce.GTC,
            ClientOrderId(coid),
            tags,
        )
        order = get_action("trailing_stop").build(ctx, {"trail_bps": str(trail_bps)})
        self._submit(order)
        self._seen_orders.add(coid)
        return order

    def _replace_trailing_stop(
        self,
        *,
        instrument_id: str,
        side: str,
        quantity: float,
        trail_bps: float,
        new_coid: str,
        old_coid: str | None,
        manager_id: str,
    ):
        """Retighten (or, in principle, widen) a resting trailing stop — SUBMIT the new one FIRST, cancel
        the old one SECOND, never the reverse (#46). A resting order costs nothing to cancel/replace (no
        spread crossed, no execution) until it actually fires, so there's no cost penalty to this ordering
        — only a safety upside: briefly having TWO stops resting is redundant protection, briefly having
        ZERO is a real naked window. The old order is looked up by client_order_id via the cache Nautilus
        already tracks; if it can't be found or isn't open anymore (already fired, or never existed — e.g.
        this is the FIRST tighten with no prior `current_trail_coid`), the cancel is skipped as a no-op,
        not an error — the new stop resting is what actually matters."""
        new_order = self._submit_trailing_stop(
            instrument_id=instrument_id, side=side, quantity=quantity, trail_bps=trail_bps,
            coid=new_coid, manager_id=manager_id,
        )
        old_order = self._lookup_order(old_coid) if old_coid else None
        if old_order is not None and old_order.is_open:
            self._cancel(old_order)
        return new_order

    # --- publish to Redis (via the bounded queue + writer thread) ---------------------------------
    def on_trade_tick(self, tick: TradeTick) -> None:
        """Real-time last price — every execution. Published as a lightweight `price` frame (separate from
        the `bar` candle plane, so the live price is decoupled from chart granularity)."""
        # Feed-freshness (#26): ticks stop when the feed dies. `ts_init`, never `ts_event`, and monotonic (#917)
        # — the rule and its reasons are at `on_bar`.
        self._last_tick_ts = max(self._last_tick_ts, tick.ts_init)
        self._subscriptions.bound("trades", str(tick.instrument_id), tick.ts_init)
        self._publish(
            "price",
            {
                "instrument_id": str(tick.instrument_id),
                "price": float(tick.price),
                "size": float(tick.size),
                "ts_event": tick.ts_event,
            },
        )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Real-time NBBO bid/ask (#40) — the spread/mid plane. Published as a lightweight `quote` frame,
        one per instrument, for the order ticket's marketable-limit/mid prefill + auto-select rules."""
        # A QUOTE IS EVIDENCE THE FEED IS ALIVE (#608). `ts_init`, monotonic (#917); the rule is at `on_bar`.
        self._last_tick_ts = max(self._last_tick_ts, tick.ts_init)
        self._subscriptions.bound("quotes", str(tick.instrument_id), tick.ts_init)
        self._publish(
            "quote",
            {
                "instrument_id": str(tick.instrument_id),
                "bid": float(tick.bid_price),
                "ask": float(tick.ask_price),
                "bid_size": float(tick.bid_size),
                "ask_size": float(tick.ask_size),
                "ts_event": tick.ts_event,
            },
        )

    def on_order_filled(self, event) -> None:
        """Publish a `fill` frame when an order fills (#32) — the UI's entry/exit markers + trade blotter.
        Surfacing only; placing orders is separate + human-armed.

        THIS IS NAUTILUS'S PER-STRATEGY CALLBACK, so it fires for MANUAL's own orders only — sibling
        lanes' fills arrive via the `events.order.*` wildcard (`_on_any_order_event`), which routes
        through the same `_publish_fill` (#651 item 5). ONE frame builder for both paths, because two
        derivations of one frame will drift."""
        self._publish_fill(event)
        # Bracket OTO/OCO/OUO is handled natively by Nautilus's OrderEmulator (#34) — no hand-rolled fill hook.

    def _publish_fill(self, event) -> None:
        """The one `fill`-frame builder, for MANUAL's native callback and the lane wildcard alike."""
        # RECORDED HERE because this is the single builder both fill paths reach — a hook on
        # `on_order_filled` alone would miss every sibling lane's fills, which arrive via the
        # `events.order.*` wildcard. Same reason the frame itself is built here and not twice.
        if getattr(event, "reconciliation", False):
            iid = str(getattr(event, "instrument_id", ""))
            lane = str(getattr(event, "strategy_id", "") or "")
            # LANDED means the fill is ON a position this lane owns — open or CLOSED (#901). The first
            # version asked "does the lane hold an OPEN position" AFTER the fill was applied, so every
            # full exit that arrived by inference read unlanded, stamped with the lane that had just
            # correctly exited; on this adapter every fill is inferred, so the #807 detector had no
            # signal at all. Nautilus keeps closed positions in the cache (`Cache.position` reads the
            # same index `positions_closed()` does) and writes the position BEFORE publishing the fill
            # (`engine.pyx:1333` before `:1341`), so the fill's own position id resolves here.
            #
            # THREE CONJUNCTS, each a case the others do not cover: the id resolves (a REDUCE-ONLY fill
            # stamped with a lane holding nothing derives an id no position has — the mint; a fill that
            # is not reduce-only OPENS a position at that id and lands, which is pre-existing and is
            # why protective stops carry `reduce_only=True`); the position is
            # this lane's (a corrupt id); and the fill's trade id is ON the position — a reduce-only
            # fill against an already-closed position resolves to that closed position under the same
            # lane, is refused by `_reject_reduce_only_netting_position_open`, applied to nothing and
            # published anyway: the mint one step over (review). `position.pyx:574` adds the trade id
            # before publish, after the FLAT reset, so a closing fill's own id survives.
            def _record_inferred_fill_against_its_lane() -> None:
                # DIRECT ATTRIBUTE ACCESS, not getattr: this decision IS about ownership and the
                # ownership guard cannot see a `strategy_id` hidden behind a getattr string.
                pid = event.position_id
                # Guarded explicitly: `Cache.position(None)` raises, the observation wrapper would
                # absorb it, and the row would silently never be written. Production assigns the id
                # before both publishes (`_determine_position_id`), so this names a state it cannot
                # reach rather than crashing on it.
                position = self.cache.position(pid) if pid is not None else None
                landed = (
                    position is not None
                    and str(position.strategy_id) == lane
                    and event.trade_id in position.trade_ids
                )
                self._inferred_fills.record(iid, landed=landed, strategy_id=lane)

            self._observations.run("inferred_fills", _record_inferred_fill_against_its_lane,
                                   ts_ns=self._safe_now())

        # A FILL ON A TERMINAL ORDER (#807 item 4). Nautilus refuses it on the order (`InvalidStateTrigger`)
        # and applies it to the position anyway (`engine.pyx:1586`, `return True  # Continue processing`).
        # PATH's REJECTED stop took five such fills on 2026-09-04 and the lane read SHORT 192 for five
        # days. The cache's own order is the arbiter of "terminal" — not the event, which cannot know.
        def _record_fill_on_terminal_order() -> None:
            order = self.cache.order(event.client_order_id)
            if order is None:
                return
            # THE PREDICATE IS NAUTILUS'S OWN, NOT A STATUS LIST (codex review): an ordinary final fill
            # reaches this hook with the order already FILLED, because the engine applies the fill to
            # the order BEFORE publishing it. What distinguishes a refused fill is that the ORDER PLANE
            # DOES NOT CARRY IT — `Order.apply` raised, so the trade id never joined `order.trade_ids`.
            if event.trade_id in order.trade_ids:
                return
            self._terminal_fills.record(
                str(event.client_order_id), order.status_string(), str(event.instrument_id),
                str(event.strategy_id), float(event.last_qty), ts_ns=int(event.ts_event),
            )

        self._observations.run("fills_on_terminal_orders", _record_fill_on_terminal_order,
                               ts_ns=self._safe_now())
        self._publish(
            "fill",
            {
                "instrument_id": str(event.instrument_id),
                "side": event.order_side.name,
                "quantity": float(event.last_qty),
                "price": float(event.last_px),
                "ts_event": int(event.ts_event),
                "order_type": event.order_type.name,  # OrderFilled carries it directly (1.229)
                "strategy_id": str(event.strategy_id),
            },
        )

    def _order_frame(self, order) -> dict:
        """Serialize a Nautilus order → the Orders blotter frame (#33). `trigger_price` exists only on
        stop-type orders (guard via has_trigger_price); avg_px is 0.0 (not None) until a fill. `reason`
        is the deny/reject text (Nautilus carries it on the event, not the order) — kept per client_order_id
        so it survives snapshot re-publishes and the order detail can always show WHY an order died."""
        return {
            "client_order_id": str(order.client_order_id),
            "venue_order_id": str(order.venue_order_id) if order.venue_order_id else None,
            "instrument_id": str(order.instrument_id),
            "side": order.side.name,
            "order_type": order.order_type.name,
            "quantity": float(order.quantity),
            "filled_qty": float(order.filled_qty),
            "leaves_qty": float(order.leaves_qty),
            "price": float(order.price) if order.has_price else None,
            "trigger_price": float(order.trigger_price) if order.has_trigger_price else None,
            "time_in_force": order.time_in_force.name,
            "status": order.status.name,  # ACCEPTED / SUBMITTED / FILLED / CANCELED / REJECTED …
            "avg_px": float(order.avg_px) if order.avg_px else None,
            "ts_last": int(order.ts_last),
            "strategy_id": str(order.strategy_id),
            "tags": list(order.tags) if order.tags else [],
            "reason": self._deny_reasons.get(str(order.client_order_id)),
        }

    def on_order_event(self, event) -> None:
        """Nautilus's own callback — fires ONLY for this strategy's orders.

        `Strategy.on_start` subscribes `events.order.{self.id}` (nautilus_trader/trading/strategy.pyx:322),
        and the exec engine publishes to `events.order.{strategy_id}` per order. This strategy is
        registered as MANUAL, so this path sees the operator's own clicks and nothing else. Everything
        automated arrives via `_on_any_order_event` below.
        """
        self._handle_order_event(event)

    def _on_any_order_event(self, event) -> None:
        """Every strategy's order events, via the `events.order.*` wildcard subscription (#207).

        MANUAL is skipped here because `on_order_event` has already delivered it — subscribing to the
        wildcard does not replace the per-strategy subscription, it adds to it, so without this guard
        each manual order would publish its blotter frame twice.
        """
        if str(getattr(event, "strategy_id", "")) == str(self.id):
            return
        # A LANE'S FILL IS A FILL (#651 item 5). `on_order_filled` is per-strategy and sees MANUAL
        # only, so without this every automated lane's rotation drew NO markers on the chart — the
        # book moved and the surface said nobody traded. The MANUAL guard above keeps the operator's
        # own fills from publishing twice.
        if isinstance(event, OrderFilled):
            self._publish_fill(event)
        self._handle_order_event(event)

    def _close_exit_window(self, instrument_id: str) -> None:
        """Drop a release window WITHOUT reporting it (#546, review round 2).

        Used where the release itself failed: nothing was sent, protection still stands, the
        instrument is already back with the reconciler, and `release_for_exit` has logged it. That
        is the designed recovery — leaving the window open made the sweep announce later that the
        position had been unprotected all along, which is a false page on a handled condition.
        """
        try:
            self._exit_windows.pop(instrument_id, None)
        except Exception:  # noqa: BLE001
            pass

    def _event_is_our_exit(self, event, window: dict | None = None) -> bool:
        """Is this order event about an EXIT this engine sent for the instrument? (#546)

        THE ONE PREDICATE. Both the release-window mark and the naked-after-reject escalation ask
        this question, and the first version asked it twice with different answers — the escalation
        read the order from the cache and required a SELL that is not protective, while the mark
        accepted any event whose client order id merely lacked the `PROT-` prefix. Two derivations
        of one fact, in one file, disagreeing (CLAUDE.md).

        UNKNOWN IS NOT YES: an order absent from the cache cannot be shown to be our exit, so it
        does not mark the window and does not escalate.
        """
        coid = str(getattr(event, "client_order_id", "") or "")
        if not coid or coid.startswith("PROT-"):
            # A protective order's own lifecycle is the reconciler's business, never an exit's.
            return False
        order = self.cache.order(getattr(event, "client_order_id", None))
        if order is None:
            return False
        if str(getattr(getattr(order, "side", None), "name", "")).upper() != "SELL":
            return False
        if window is None:
            return True
        # WHOSE EXIT, AND WHEN (review round 3). A side check alone leaves four SELLs that mark a
        # window they have nothing to do with: another LANE's exit (BCTROT and MOMENTUM rotate one
        # pool and exit the same names in the same minutes), the snapshot RE-PUBLISH of historical
        # fills (engine_node.py:771 — six false receipts on 2026-08-24), a bracket protective leg
        # carrying an Alpaca-generated id, and a MANUAL discretionary sell. Each makes the sweep
        # silent about a position that never got its exit.
        #
        # The exit's own client order id would be exact, but the caller submits AFTER
        # `release_for_exit` returns, so it cannot be recorded here. Time and lane are what the
        # window can know.
        ts = getattr(event, "ts_event", None)
        opened = window.get("opened_ns")
        if ts is not None and opened is not None and int(ts) < int(opened):
            return False
        lane = window.get("lane")
        ev_lane = str(getattr(event, "strategy_id", "") or "")
        # UNKNOWN LANE IS NOT A MISMATCH. Not every order event carries a strategy id (a
        # reconciliation-generated order carries EXTERNAL or nothing), and treating "did not say"
        # as "somebody else" would make the sweep page on a successful exit — an alarm on the
        # normal path. Only a STATED, DIFFERENT lane disqualifies.
        if lane and ev_lane and ev_lane != str(lane):
            return False
        return True

    def _sweep_exit_windows(self) -> None:
        """Report release windows that CLOSED having never carried an exit (#546 part 2).

        The rejection hook covers a venue that refused the sell. It cannot cover a sell that never
        reached the venue: `NautilusBroker.submit` refuses locally on an unsubscribed instrument, a
        missing instrument definition, an exception, or an order absent from the cache after submit
        — returning ok=False without Nautilus ever creating an order, so no event exists to hook.
        In every one of those the protection was already cancelled.

        A window is only a finding once it has EXPIRED: an exit in flight is the normal path, and
        paging on it would train the operator to ignore this channel.
        """
        try:
            now = self.clock.timestamp_ns()
            for iid, win in list(self._exit_windows.items()):
                if win.get("until", 0) > now:
                    continue
                self._exit_windows.pop(iid, None)
                if win.get("saw_exit"):
                    continue
                self.log.error(
                    f"{iid}: a protection-release window closed and NO exit order ever reached the "
                    f"venue — the sell was refused locally (unsubscribed instrument, missing "
                    f"definition, or a submit that never cached), so nothing rejected it and "
                    f"nothing re-armed. This position was unprotected for the whole window (#546)."
                )
                self._naked_positions = ([{
                    "instrument_id": iid,
                    "reason": "no exit order ever reached the venue (local refusal)",
                    "qty": win.get("qty"),
                    "ts": now,
                }] + list(self._naked_positions))[:20]
        except Exception as exc:  # noqa: BLE001 — a reporting sweep must not break the reconciler
            self.log.warning(f"exit-window sweep failed: {exc!r}")

    def _naked_after_reject(self, event) -> None:
        """A rejected exit INSIDE the release window leaves the position bare — collapse it, loudly (#546).

        The exit sequence releases protection first on purpose: a resting stop reserves the shares,
        so the sell cannot go out while it stands (cancelling AFTER is #245's oversell). The window
        is marked in `_exit_suppressed` so the reconciler will not re-arm mid-exit, and it is
        TTL-bounded because every entry is a position deliberately left naked.

        When the venue REJECTS that sell, nothing used to collapse the window: the suppression stood
        for its full TTL, the reconciler stayed told-not-to-re-arm, and the only trace was an order
        frame. Now the mark is dropped immediately — the next reconciler tick re-arms — and the
        state is reported as ITS OWN condition rather than as a failed order.

        Never raises: it runs inside the event handler that feeds the blotter.
        """
        try:
            # WHICH REJECTIONS COUNT (review, 2026-08-29). FOUR Nautilus event classes carry
            # `reason` — Rejected, Denied, CancelRejected, ModifyRejected — and `reason or
            # str(None)` means the field is never falsy, so the seam's `if reason:` gate admits all
            # four. None of them carries `order_side`; the ORDER does. So the discriminator is the
            # order this event is about:
            #
            #   * a rejected CANCEL of a protective stop is the release sequence's OWN normal path
            #     (a cancel of an order the venue already filled is a routine 422). Collapsing the
            #     window there hands the instrument back to the reconciler MID-RELEASE, which can
            #     re-arm a stop that re-reserves the shares the exit is still waiting for — exactly
            #     what `_extend_standoff` exists to prevent — while paging that a protected
            #     position is naked.
            #   * a rejected ENTRY is not a failed exit, and the window is keyed on the instrument
            #     alone, so a buy's rejection would collapse a live exit's standoff.
            #
            # UNKNOWN IS NOT NAKED: if the order is not in the cache we cannot tell what was
            # rejected, and guessing here pages on the normal path.
            if not self._event_is_our_exit(event):
                return
            iid = str(getattr(event, "instrument_id", "") or "")
            if not iid:
                return
            until = (getattr(self, "_exit_suppressed", None) or {}).get(iid)
            if until is None or until <= self.clock.timestamp_ns():
                # No live window: an ordinary rejection (an entry, or a sell on a position whose
                # protection was never released). Alarming here would fire on the normal path.
                return
            self._exit_suppressed.pop(iid, None)
            reason = str(getattr(event, "reason", "") or "unstated")
            self.log.error(
                f"{iid}: the exit was REJECTED ({reason}) while its protection was already released "
                f"— the position is UNPROTECTED now. The no-re-arm window has been dropped so the "
                f"reconciler re-arms on its next tick (#546)."
            )
            self._naked_positions = ([{
                "instrument_id": iid,
                "reason": reason,
                "strategy_id": str(getattr(event, "strategy_id", "") or ""),
                "ts": self.clock.timestamp_ns(),
            }] + list(self._naked_positions))[:20]
        except Exception as exc:  # noqa: BLE001 — reporting must not break the blotter feed
            self.log.warning(f"naked-after-reject escalation failed: {exc!r}")

    def _handle_order_event(self, event) -> None:
        """Instant Orders-blotter feed (#33): publish an `order` frame on every lifecycle event
        (accepted / updated / canceled / rejected / filled). The snapshot in _on_snapshot is the
        reconciling backstop. Surfacing only — order submission is separate + human-armed."""
        # Capture the deny/reject reason — Nautilus carries it on the event, not the order. Kept keyed by
        # client_order_id so every subsequent frame (incl. snapshot re-publish) can surface WHY it died.
        # AN EXIT REACHED THE VENUE FOR THIS INSTRUMENT (#546 part 2): mark the window, so the
        # sweep can tell "the exit failed" from "no exit was ever sent". ONE PREDICATE, shared with
        # the rejection hook — the first version marked on ANY non-PROT- event, so another lane's
        # entry BUY, a MANUAL click, the snapshot re-publish of historical fills, or a bracket leg
        # carrying a venue-generated id all marked the window carried and the sweep went blind
        # exactly when the instrument was busy (review, round 2).
        try:
            win = self._exit_windows.get(str(getattr(event, "instrument_id", "") or ""))
            if win is not None and self._event_is_our_exit(event, window=win):
                win["saw_exit"] = True
        except Exception:  # noqa: BLE001 — bookkeeping must not break the blotter
            pass
        reason = getattr(event, "reason", None)
        if reason:
            self._deny_reasons[str(event.client_order_id)] = str(reason)
            # A REJECTED EXIT IS NOT AN ORDINARY ORDER FRAME (#546). Only events carrying a reason
            # reach here, which is exactly the reject/deny family.
            self._naked_after_reject(event)
        order = self.cache.order(event.client_order_id)
        if order is not None:
            self._publish("order", self._order_frame(order))
            # RECEIPT HERE, where the order exists. It used to be the last line of
            # `_apply_budget_transfer`, which never receives one — `self._maybe_receipt(order)` was a
            # plain NameError from 2026-08-10 (88b896b) to 2026-08-24, scheduled fire-and-forget so
            # the exception was never retrieved and nothing logged. Fourteen sessions, no receipts.
            #
            # That site was also SELL-ONLY: it is reached from `_maybe_transfer_budget`, which returns
            # unless `event.order_side == SELL`, so a filled BUY could not have been reported even
            # with `order` bound. Here both directions arrive, and `_maybe_receipt`'s own guards
            # (FILLED only, never MANUAL, once per client_order_id) do the filtering they were
            # written to do.
            self._maybe_receipt(order)
        self._maybe_transfer_budget(event)
        # An entry order accepted/canceled while flat flips a cycle ARMED↔CLOSED (#73) — re-fold.
        self._publish_trades()

    def _maybe_transfer_budget(self, event) -> None:
        """A sell filled — hand capital to the receiving strategy if this one is over its target (#320).

        Only the EXCESS moves. A strategy at or under target that sells is rotating, not shrinking: it
        turned stock into cash inside its own sleeve and will buy something else. The pure layer decides
        that; this only supplies the fill.

        NO GATE, deliberately. The standing "new automation defaults off" rule is about automation that
        ACTS on the book; this places no order and moves only an accounting row. A gate here would guard
        nothing while adding a failure mode of its own: with it off, `actual` never updates, so every
        sleeve number is silently STALE — and stale is worse than absent once anything reads it, because
        it looks like data. The gates that matter are `deployable()`, which bounds what a strategy may
        spend, and the lifecycle, which bounds whether it may act at all.

        Never raises out. A budget-accounting failure must cost the ledger a row, not the trading loop:
        this runs inside a Nautilus event handler, and the same reasoning that quarantines the display
        projection applies with more force to something that is not on the trading path at all.
        """
        try:
            from nautilus_trader.model.enums import OrderSide

            if not isinstance(event, OrderFilled) or event.order_side != OrderSide.SELL:
                return
            # PROCEEDS, not basis: `actual` is a net asset value, so the sleeve keeps its own P&L.
            proceeds = float(event.last_qty) * float(event.last_px)
            seller = str(event.strategy_id)
            # The venue's own fill identity is the idempotency key. `trade_id` is unique per fill at the
            # venue, which is exactly what a redelivered fill repeats — a client order id would collapse
            # partial fills of one order into a single transfer.
            fill_id = str(getattr(event, "trade_id", "") or "")
            if not fill_id or proceeds <= 0:
                return
            recipient = _budget_transfer_recipient()
            if self._loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._apply_budget_transfer(seller, proceeds, fill_id, recipient), self._loop)
        except Exception as exc:  # noqa: BLE001 — accounting must never take down the trading loop
            _log.error("budget transfer skipped for %s: %r", event, exc)

    async def _apply_budget_transfer(self, seller: str, proceeds: float, fill_id: str,
                                     recipient: str | None) -> None:
        """Persist one transfer. Its own session, committed independently of anything else in flight."""
        try:
            from api.budget_store import on_sell_fill
            from api.db.engine import session_factory

            async with session_factory() as session:
                transfer = await on_sell_fill(session, seller_id=seller, proceeds=proceeds,
                                              fill_id=fill_id, recipient_id=recipient)
                await session.commit()
            if transfer is not None:
                self._publish("budget", {
                    "from_strategy": transfer.from_strategy, "to_strategy": transfer.to_strategy,
                    "amount": transfer.amount, "reason": transfer.reason,
                    "ts": self.clock.timestamp_ns(),
                })
        except Exception as exc:  # noqa: BLE001
            _log.error("budget transfer failed (%s -> %s, %.2f): %r", seller, recipient, proceeds, exc)

    def _maybe_receipt(self, order) -> None:
        """Telegram receipt for a COMPLETED automated order (#199 E).

        A machine that trades on your behalf should say so. Deliberately per completed ORDER rather
        than per fill: on 6 Aug Alpaca returned 12 fill events for 7 symbols, and AFL alone filled in
        five pieces (16/45/2/3/14 shares) for one intended sell of 80. A message per fill is a
        shredder, not a receipt — and it buries the fact that mattered that morning, which was that
        six positions left and nothing replaced them.

        MANUAL orders are the operator's own clicks and need no receipt back.

        Best effort throughout, and scheduled onto the node's loop rather than awaited: a notifier
        must never sit in an execution callback.
        """
        try:
            if order is None or self._loop is None:
                return
            sid = str(getattr(order, "strategy_id", ""))
            if not sid or sid.startswith("MANUAL"):
                return
            if str(getattr(order.status, "name", order.status)) != "FILLED":
                return          # still working: partials are not a completed trade
            coid = str(order.client_order_id)
            if coid in self._receipted:
                return          # FILLED is terminal, but a snapshot re-publish can revisit it
            # PRE-BOOT FILLS ARE NOT NEWS (#520). Remembered as well as skipped, so the snapshot's
            # repeated re-publishes do not re-evaluate the same order forever.
            #
            # FAIL OPEN, and deliberately the opposite of the drift rule: a missing `ts_last` means we
            # cannot tell old from new, and staying silent would drop a REAL fill. A spurious receipt
            # costs one message; a missing one costs the operator the knowledge that a machine traded
            # on their behalf, which is the entire point of the feature.
            ts_last = int(getattr(order, "ts_last", 0) or 0)
            if ts_last and ts_last < self._started_ns:
                self._receipted.add(coid)
                return
            self._receipted.add(coid)

            sym = str(order.instrument_id).split(".")[0]
            side = str(getattr(order.side, "name", order.side))
            qty = float(order.filled_qty)
            avg = float(order.avg_px) if order.avg_px is not None else None
            body = (f"*{sym}* {side} {qty:g}"
                    + (f" @ {avg:,.4f} avg" if avg else "")
                    + f"\n{sid}")
            asyncio.run_coroutine_threadsafe(self._send_receipt(sym, side, coid, body), self._loop)
        except Exception as exc:                                        # noqa: BLE001
            _log.debug("order receipt skipped: %r", exc)

    async def _send_receipt(self, sym: str, side: str, coid: str, body: str) -> None:
        try:
            from api.notify import Alert, Notifier
            if self._notifier is None:
                self._notifier = Notifier()
            # Keyed by client_order_id: every completed order is its own news, and the dedupe only
            # guards against the same order being reported twice.
            await self._notifier.send(f"trade:{coid}",
                                      Alert(title=f"{side.title()} {sym}", body=body))
        except Exception as exc:                                        # noqa: BLE001
            _log.debug("order receipt not sent: %r", exc)

    def on_position_event(self, event) -> None:
        """Trade-cycle fold (#73): re-project on every position transition (opened/changed/closed). The fold
        MUST see each transition to assign cycle boundaries — a snapshot-cadence-only projection would miss a
        close→reopen inside one 2s window and wrongly continue the old cycle_id instead of minting a new one.

        The closed-leg registry (#846) is NOT fed from here: Nautilus delivers this hook for MANUAL's own
        positions only. Every lane's closes arrive through `_on_any_position_event` (the wildcard)."""
        self._publish_trades()

    def _subscribe_position_events(self) -> None:
        """Ask the bus for EVERY lane's position events (#846). Its own method so a test can hand it a
        real `MessageBus` and prove delivery, rather than grep `on_start` for the topic's name."""
        self.msgbus.subscribe(_POSITION_EVENTS_TOPIC, self._on_any_position_event)

    def _on_any_position_event(self, event) -> None:
        """Every strategy's position events, via the `events.position.*` wildcard (#846).

        A CLOSE IS A LEG. Registered from the EVENT: Nautilus queues position events and flushes them at
        the end of the fill that caused them, after the cache is updated — under NETTING the cache will
        replace this position on reopen, and on a flip it already has. The event carries `realized_pnl`
        and both timestamps as they were at the close. Opened/changed register nothing."""
        if isinstance(event, PositionClosed):
            self._register_closed_leg(CycleLeg.from_event(event))

    def _register_closed_leg(self, leg: CycleLeg) -> None:
        """Keep a closed leg under (position id, open time); the LATEST close wins a collision."""
        if leg.ts_closed is None:
            return
        key = (leg.position_id, int(leg.ts_opened))
        prior = self._closed_legs.get(key)
        if newer_close_wins(None if prior is None else prior.ts_closed, leg.ts_closed):
            self._closed_legs[key] = leg

    def _publish_trades(self) -> None:
        """Publish the trade-cycle plane (#73) — the projection of native positions/snapshots/orders into
        TradeDTOs. A `ui:state:trades` latest-state key (like positions), so the 2s cadence can't evict bars.
        Runs inside engine event handlers, so it must NEVER crash the trading loop — a projection bug is a
        display defect, quarantined here (the next event/snapshot re-projects)."""
        # THREE kinds of empty, and they must never render identically (#298). On 2026-08-14 a NameError
        # in the projection made every tick raise; the engine survived and logged, but published nothing,
        # so the UI showed an empty book while eight positions worth $68k were held at the broker. An empty
        # book is indistinguishable from a LIQUIDATED book, and those demand opposite reactions.
        if not self._trade_cycles or not self._cycles_seeded:
            # Legitimate empty: reconciliation has not finished. `held: 0` here is lag, not liquidation —
            # a rule that lived in a handoff document rather than on the screen.
            self._publish(
                "trades",
                # THE LAST KNOWN BOOK, NOT AN EMPTY ONE. An empty array is a CLAIM — the tile renders
                # it as "you hold nothing" — and during a restart it is a false one. `_last_good_trades`
                # is already maintained on every successful projection and was simply unreachable from
                # here. `status` still says `seeding`, so the rows are labelled not-current rather than
                # passed off as live.
                {"trades": self._last_good_trades or [], "ts": self.clock.timestamp_ns(),
                 "status": "seeding", "error": None},
            )
            return
        try:
            now_ns = self.clock.timestamp_ns()
            with self._projection_lock:
                dtos = [d for proj in self._trade_cycles.values()
                        for d in proj.project(self.cache, now_ns)]
            self._mark_cycle_financials(dtos)
            self._mark_broker_stop_prices(dtos)
            payload = [t.model_dump() for t in dtos]
            # Kept so a later failure can show the last known book marked stale, rather than blanking it.
            # Preserving the operator's mental model beats destroying it.
            self._last_good_trades = payload
            windows = self._realized_windows()
            self._publish(
                "trades",
                {"trades": payload, "ts": self.clock.timestamp_ns(), "status": "ok", "error": None,
                 # Per-lane NET INVESTED per ET session from the cache's own filled orders (#699 a):
                 # the flows half of `net(W) = ΔMV − invested(W)`. Read by `/pnl/unrealized-base`,
                 # which knows each window's base date and does the subtraction there.
                 "lane_flows": self._lane_flows(),
                 # ONE DERIVATION for Home and the panel (#846): the session figure IS the 1D window.
                 "realized_session": windows["1D"],
                 # Per-strategy realized per window from Nautilus's own closed legs — on EVERY venue.
                 # `source` names the derivation so a reader can tell it from the broker sweep below.
                 "realized_periods": {**windows, "source": "legs"},
                 # The broker's fill sweep (Alpaca only; None elsewhere). Its account total carries fees
                 # and withholding the legs cannot see. Published BESIDE the legs, never averaged: the
                 # two disagreeing is what found #846.
                 "realized_periods_swept": self._realized_periods,
                 # ...and WHY a window has no number, when it has none: the refusal is a condition a
                 # banner can read, not a zero the tile can print.
                 "realized_legs": {**self._realized_legs_status(), "error": windows["1D"].get("error")}},
            )
            # Persist each emitted cycle's envelope (engine authors it) on the node loop — the store is
            # idempotent + monotonic, so a dropped/duplicate write converges. NOT truly fire-and-forget: a
            # done-callback surfaces store failures, else the engine could look durable while writes silently
            # fail (codex-flagged).
            if self._cycle_store is not None and self._loop is not None:
                # ONE WRITE PER CHANGE, NOT ONE PER CYCLE PER TICK (#564). This runs on the event-driven
                # fold AND on the 2s snapshot cadence, so it issued 31 upserts per tick until all 30 pool
                # slots were held and every further write sat 30s for a connection that never came —
                # 21,731 failures in 20 minutes at the 2026-08-26 open. The engine's own HTTP calls and
                # MOMENTUM-002's source refresh timed out behind them and the lane refused to trade on
                # 32% bar coverage. `CycleEnvelopeStore.upsert` already documented "call on transitions";
                # only this caller disagreed.
                #
                # Coalesced on `envelope_key`, NOT on row equality — the fold advances `last_event_ts` on
                # every emit by design, so equal rows do not exist in production (see `envelope_key`).
                #
                # `_pending` is what bounds the failure case: at most ONE in-flight write per cycle, so a
                # slow or dead database costs one write per cycle per round-trip instead of one per cycle
                # per tick. Without it this fix re-creates the storm in exactly the condition it exists
                # to survive.
                live: set[str] = set()
                # RLOCK, NOT LOCK. `add_done_callback` on an ALREADY-COMPLETED future runs the callback
                # synchronously on THIS thread, inside this block; a plain Lock would deadlock. When the
                # future is still pending the callback instead runs on the event-loop thread — measured,
                # both paths are real, which is why this state is locked at all.
                with self._envelope_lock:
                    for dto in dtos:
                        row = envelope_from_dto(dto)
                        key = envelope_key(row)
                        live.add(row.cycle_id)
                        if self._envelope_written.get(row.cycle_id) == key:
                            continue
                        # ANY in-flight write for this cycle suppresses, not just one carrying the SAME
                        # key. Suppressing only on an equal key is not a bound at all: a cycle churning
                        # HELD->ARMED->HELD against a hung database overwrites its own slot and enqueues
                        # one future per tick, which is the storm again on the one cycle that is moving.
                        # Nothing is lost by waiting — the projection re-emits every tick, so the next
                        # tick after this write returns sees `written != key` and writes the newer row.
                        if row.cycle_id in self._envelope_pending:
                            continue
                        self._attempt_seq += 1
                        attempt = self._attempt_seq
                        self._envelope_pending[row.cycle_id] = (attempt, key)
                        fut = asyncio.run_coroutine_threadsafe(
                            self._cycle_store.upsert(row), self._loop
                        )
                        # Recorded by the CALLBACK on success, never here. Caching the attempt would make
                        # a failed write permanent — the engine would look durable while the envelope
                        # diverged, the hazard the callback was added for.
                        fut.add_done_callback(
                            partial(self._on_envelope_write, row.cycle_id, attempt, key))
                    # Cycles that left the book leave the state; this engine runs for weeks. The dead list
                    # is materialised BEFORE popping: a synchronous callback re-enters under the RLock and
                    # mutating during iteration would raise `dictionary changed size during iteration`.
                    for dead in [c for c in self._envelope_written if c not in live]:
                        self._envelope_written.pop(dead, None)
                    for dead in [c for c in self._envelope_pending if c not in live]:
                        self._envelope_pending.pop(dead, None)
        except Exception as exc:  # noqa: BLE001 — display projection must not take down the engine
            # Survive, log AND SURFACE. The first two were already right; the third is what was missing,
            # and its absence is why a broken projection looked exactly like a flat book.
            self.log.error(f"trade-cycle projection failed (skipped): {exc!r}")
            self._publish(
                "trades",
                {
                    "trades": self._last_good_trades,
                    "ts": self.clock.timestamp_ns(),
                    "status": "failed",
                    "error": repr(exc)[:300],
                },
            )

    def _publish_external(self) -> None:
        """Publish the external-activity quarantine plane (#79) — positions/orders NOT owned by the cockpit's
        strategy, so the UI can surface them without them joining strategy P&L. Only when there's an exec
        client (the account these appear under). Never crashes the trading loop."""
        if self._exec_client_id is None:
            return
        try:
            # Every strategy THIS node runs, not just the feed strategy. Anything else on the account
            # is genuinely external and still quarantined.
            owned = set(self._trade_cycles) or {str(self.id)}
            rows = classify_external(self.cache, owned, str(self._exec_client_id))
            # THE BROKER'S QUANTITY ON EVERY POSITION ROW (#807 item 3). 0.0 when the broker answered and
            # holds none — the row is a phantom the UI must not offer to "move to a strategy"; None when
            # the broker has not answered yet, which is not the same thing.
            broker_qty = self._broker_qty
            for r in rows:
                if r.source == "POSITION":
                    # `rsplit`, not `split`: the venue never carries a dot, the symbol can (BRK.B.XNYS).
                    # Same key the exec adapter aggregates on (`iid.symbol.value`) and Alpaca reports.
                    r.venue_qty = None if broker_qty is None else float(broker_qty.get(r.instrument_id.rsplit(".", 1)[0], 0.0))
            self._enrich_position_financials(rows)
            self._publish(
                "external_activity",
                {"external": [r.model_dump() for r in rows], "ts": self.clock.timestamp_ns()},
            )
        except Exception as exc:  # noqa: BLE001 — display plane must not take down the engine
            self.log.error(f"external-activity classification failed (skipped): {exc!r}")

    async def _handle_eod_backfill_command(self, cid: str, payload: dict):
        """Write reconstructed history for the requested sessions (#734). OPERATOR-DRIVEN, GATED OFF.

        THE GATE IS CHECKED HERE, not only at the endpoint. The endpoint is a convenience; this is the
        guard. A hand-crafted bus message must not be able to reach a write path the operator never
        enabled — checking only in the api process would make the endpoint's absence cosmetic.

        `ledger_start` is REQUIRED from the caller and never inferred: it states what the fetch
        COVERS, which is a property of the fetch and not of the account. A ledger truncated by a
        lookback window would otherwise report its own truncation point as the account's inception —
        the inference is most confident exactly where it is most wrong.
        """
        from api.eod_backfill_job import run_backfill
        from api.eod_hook import _known_lanes
        from api.eod_capture import capture_enabled
        from api.eod_observation_store import EodObservationStore

        if not capture_enabled(os.environ, var="KUMO_EOD_BACKFILL"):
            return "error", (
                "eod backfill is not enabled on this instance (KUMO_EOD_BACKFILL). It writes history "
                "and is deliberately opt-in per operator action."
            )
        sessions = list(payload.get("sessions") or [])
        ledger_start = payload.get("ledger_start")
        if not sessions or not ledger_start:
            return "error", "eod backfill needs both `sessions` and `ledger_start`"

        accounts = self.cache.accounts()
        currency = None
        if accounts:
            currency = _single_currency_amount_and_ccy(accounts[0].balances_free(), prefer=USD)[1]
        if currency is None:
            # The same refusal as the live capture: a plausible currency on a P&L row is worse than
            # none, and `observation_rows` has no default for exactly that reason.
            return "error", "no account currency could be established without guessing"

        marks = self._daily_close_marks()
        report = await run_backfill(
            self, EodObservationStore(),
            sessions=sessions,
            ledger_start=str(ledger_start),
            # TODAY, because the gate compares against the LIVE cache. Never the last requested
            # session — see `run_backfill`.
            gate_as_of=_et_date(self.clock.timestamp_ns()),
            # NOT `marks.get(d, {})`. That lambda CANNOT RAISE, which made the runner's
            # refuse-on-marks-failure branch UNREACHABLE in production — I wrote the refusal and
            # then wired past it. A session with no cached bars would have written rows with
            # `mark_px` NULL, silently and PERMANENTLY: base rows are append-only, so at
            # method_version v1 those days are unfixable without a version bump, and one unpriced
            # symbol at the 1W base kills that lane's headline window.
            #
            # Raising here reaches the refusal, which refuses that SESSION and writes nothing for it
            # — recoverable, because a re-run after seeding writes normally. Partial coverage within
            # a priced day still lands as NULL and is reported per session as `unpriced`, which the
            # caller must read.
            marks_for=_marks_or_raise(marks),
            currency=str(currency),
            snapshot_ts_for=lambda d: self.clock.timestamp_ns(),
            # Every REGISTERED strategy, flat ones included — `positions_open()` cannot name a
            # lane that holds nothing, which made "observed, 0 positions" unreachable.
            known_lanes=_known_lanes(self.cache),
        )
        if report.refusal or not report.gate.agrees:
            return "error", report.summary
        return "ok", None

    def _daily_close_marks(self) -> dict[str, dict[str, float]]:
        """session date -> {bare symbol: that day's close}, from the engine's OWN daily bars.

        A symbol with no bar for a day is simply ABSENT, and `reconstruct_lanes` records that as an
        unknown valuation rather than zero — the holding is a fact, the valuation is not.

        Keyed by BARE symbol because `marks` is looked up before resolution; the row that carries it
        is keyed by the resolved instrument id. Two namespaces, one seam, stated so the next reader
        does not "fix" one of them.
        """
        out: dict[str, dict[str, float]] = {}
        for iid in self.cache.instrument_ids():
            symbol = str(getattr(iid, "symbol", "") or "")
            if not symbol:
                continue
            try:
                bars = self.cache.bars(bar_type(iid, "1d"))
            except Exception:  # noqa: BLE001 — a symbol with no daily series is simply unpriced
                continue
            for b in bars or ():
                ts = getattr(b, "ts_event", None)
                close = getattr(b, "close", None)
                if ts is None or close is None:
                    continue
                out.setdefault(_et_date(ts), {})[symbol] = float(close)
        return out

    def _on_realized_tick(self, event) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_realized_periods(), self._loop)

    def _publish_equity_curve_from_cache(self) -> None:
        """The equity curve, from NAUTILUS's own account history (#559).

        `broker.equity_curve` had exactly ONE publisher — the Alpaca exec client, hitting
        `/v2/account/portfolio/history`. On an IBKR stack nothing ever wrote the topic, so the tile
        rendered "No account history yet" over an account holding $1,001,292 with months of history.
        Operator: "we can't keep the alpaca hardcoding — nothing in nautilus?"

        THERE IS, AND IT WAS ALREADY FULL. `Account.events` is `list[AccountState]`, and Nautilus
        persists it. Measured 2026-08-26: 284,398 events on paper, 22,007 on staging. The IBKR curve
        was in the cache the whole time; nobody read it.

        Same lesson as the market compass one hour earlier: stop asking a vendor, ask Nautilus.
        """
        try:
            from api.account_curve import build_curves

            accounts = list(self.cache.accounts() or [])
            if not accounts:
                return
            events = list(getattr(accounts[0], "events", []) or [])
            curves = build_curves(events, self.clock.timestamp_ns())
            if not curves:
                return
            self._publish("equity_curve", {"curves": curves})
        except Exception as exc:  # noqa: BLE001 — a display plane must never touch the trading loop
            self.log.warning(f"account curve failed, keeping last known: {exc}")

    def _on_rotation_tick(self, _event) -> None:
        """Timer callback — hands off to the loop. Nautilus timers fire on the engine thread."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_rotation(), self._loop)
        self._publish_equity_curve_from_cache()


    # `_state_equity_curve_unavailable` LIVED HERE AND IS GONE (2026-08-26). It said "this stack's
    # broker publishes no account history — the equity curve comes from the Alpaca portfolio-history
    # endpoint, and nothing supplies an equivalent here". That sentence was true of the ARCHITECTURE
    # and false about the world: `Account.events` had 22,007 AccountState events on the IBKR stack the
    # whole time. `_publish_equity_curve_from_cache` reads them, so there is nothing left to be
    # unavailable and the claim would now be a lie on every stack.
    def _resolve_from_cache(self, symbols) -> list:
        """Symbol -> `InstrumentId`, from the instruments the ATTACHED adapter loaded (#622).

        Replaces `strategies.momentum._instrument_ids`, which built ids from ALPACA's asset list on
        every venue — a 6.4 MB synchronous GET that could not run at all on an IBKR-only node, and
        the reason `build_node()` raised there.

        The venue is the adapter's answer, not ours. Nautilus loads it before anything here runs:
        `system/kernel.py:1024` awaits connect before `:1039` starts the trader, and IBKR's
        `data.py:147` pushes every instrument into the Cache inside `_connect()`.

        A DICT LOOKUP, so the off-thread worker below is now cheap rather than load-bearing. It is
        kept because the CONTRACT it enforces still matters — nothing on the Nautilus thread may
        block on resolution — and re-introducing a blocking resolver behind a synchronous call is
        exactly how #598 parked both engines.

        Unresolvable symbols are simply absent, which the caller already reports as "still short".
        """
        wanted = set(symbols)
        return [i for i in self.cache.instrument_ids() if i.symbol.value in wanted]

    def _rotation_ids_cached(self, wanted: tuple, resolver=None):
        """`{ticker: InstrumentId}` from cache, or None while a worker resolves it (#598).

        THE FETCH MUST NEVER RUN ON THIS THREAD. `_instrument_ids` bottoms out in a synchronous
        HTTPS GET of a 6.4 MB asset list, and `urlopen(timeout=60)` guards socket IDLE time rather
        than total time — a trickling chunked body resets it indefinitely. One slow response
        therefore still parks `node.run()`, which is what py-spy caught: MainThread in `ssl.read`,
        log frozen, 1.4% CPU, nothing published, on both tenants.

        REFUSES RATHER THAN WAITS. A cold cache returns None and the caller skips this tick. It does
        not block, and it does not report an unresolved universe as an EMPTY one — which is what
        `_rotation_instrument_id`'s `except Exception: return None` did, making "could not resolve"
        indistinguishable from "nothing to resolve".

        ONE WORKER AT A TIME, keyed on the ticker set: a slow fetch must not stack up behind itself
        across ticks. That amplification is what made the original defect fatal rather than slow.

        `resolver` IS INJECTABLE FOR TESTS, and that is not decoration. The first version of this
        spawned a worker that really called `_instrument_ids`, which populated the module-level
        universe singleton and broke five unrelated tests downstream — a background thread reaching
        into shared state is exactly the pollution a test must not create.
        """
        cached = getattr(self, "_rotation_iids", None)
        if cached is not None and getattr(self, "_rotation_iids_key", None) == wanted:
            return cached
        thread = getattr(self, "_rotation_id_thread", None)
        if thread is not None and thread.is_alive():
            return None

        def _resolve():
            try:
                if resolver is None:
                    resolved = self._resolve_from_cache(list(wanted))
                else:
                    resolved = resolver(list(wanted))
                self._rotation_iids = {
                    i.symbol.value: i for i in resolved if i.symbol.value in set(wanted)
                }
                self._rotation_iids_key = wanted
            except Exception as exc:                                    # noqa: BLE001
                # REPORTED, not swallowed, and the cache is left ALONE: a transient failure must not
                # blank a universe that resolved fine a minute ago.
                _log.warning("rotation: instrument resolution failed (%r); keeping last ids", exc)

        self._rotation_id_thread = threading.Thread(
            target=_resolve, name="rotation-universe-resolve", daemon=True,
        )
        self._rotation_id_thread.start()
        if not getattr(self, "_rotation_cold_logged", False):
            self._rotation_cold_logged = True
            self.log.info(
                f"rotation: resolving {len(wanted)} instrument ids off-thread; the compass sits this "
                f"tick out rather than blocking the node (#598)"
            )
        return None

    async def _refresh_rotation(self) -> None:
        """The market compass, graded off bars NAUTILUS gives us — on whatever provider this node has.

        WHY THIS IS NOT A FILE, and not a vendor client either. It began as a sidecar shelling out to a
        host tool and writing `rotation.json`, which the API read back. Operator, 2026-08-21: "it should
        not feed from a file". Three things were wrong with that, and all three are worth keeping
        written down because each has recurred in a different costume:

          * SECOND DATA SOURCE. The tool pulled daily OHLC from Yahoo, so the cockpit graded rotations
            off one feed while trading off another, and two feeds disagree about the same session.
          * NO RELATIONSHIP TO ENGINE LIVENESS. The bind mount went stale, every refresh failed with
            `FileNotFoundError`, and the Market tab served a four-hour-old payload until someone looked.
          * A FILE IS NOT A PLANE. Everything else the UI reads is published by the engine on the bus.

        The file went. Then the maths moved into this repo (`strategies/rotation_grade.py`). What was
        left was the last vendor dependency: bars fetched through `self._http`, an `AlpacaHttpClient`
        built only when `data_provider == "alpaca"`. So the compass was Alpaca-only and, on any other
        provider, `if self._http is None: return` made it VANISH SILENTLY — the same shape as the env
        var that switched it off for days, one layer down.

        NAUTILUS IS THE ONE BAR SOURCE EVERY PROVIDER SHARES. `request_bars` fills the cache from
        whichever adapter is connected; `cache.bars()` reads it back. Alpaca, IBKR, Databento — the
        compass no longer knows or cares.

        OFF THE CONNECT PATH, deliberately, and that has already cost us once: an earlier attempt wired
        ticker resolution into startup and put a third concurrent call in a race with the Data and Exec
        clients' own `_connect`, and the node came up RUNNING with both disconnected. This runs on a
        timer, after the node is up.

        BEST EFFORT, BUT NEVER SILENT. A failed refresh keeps the last good payload rather than blanking
        it — a transient failure must not turn a live read into an empty market. A provider that cannot
        serve the history at all is reported, not swallowed.
        """
        try:

            from api.bar_spec import bar_type
            from strategies.rotation_from_cache import (
                bars_to_tuples,
                build_payload,
                rotation_tickers,
            )

            wanted = rotation_tickers()
            # THE WHOLE UNIVERSE IN ONE CALL (#598). This was a per-ticker comprehension, and each
            # `_rotation_instrument_id` reached `TradableUniverse().exchanges()` — a fresh instance,
            # so a 6.4 MB asset download PER TICKER. 27 tickers = 173 MB and ~1.4 minutes, and it
            # runs on the Nautilus main thread, so the rotation tick fired again before it finished
            # and the node never caught up: py-spy showed MainThread parked in `ssl.read`, the log
            # frozen, 1.4% CPU, nothing published, on BOTH tenants.
            #
            # Keyed off the RETURNED ids rather than the input, because `_instrument_ids` drops a
            # symbol whose exchange does not map instead of raising, so input and output can differ.
            # NEVER ON THIS THREAD (#598). Collapsing 27 fetches to 1 made the wedge survivable; it
            # did not make it impossible — the fetch is still a synchronous 6.4 MB HTTPS GET whose
            # `timeout=60` guards socket IDLE time, not total time. So the tick reads a CACHE and a
            # worker fills it; a cold cache means the compass sits this tick out and says so.
            iids = self._rotation_ids_cached(tuple(wanted))
            if iids is None:
                return
            unresolved = [t for t in wanted if t not in iids]

            # ASK NAUTILUS ONCE PER SESSION. `request_bars` is asynchronous in effect — it fills the
            # cache and returns; the grade happens on this or a later tick once the bars land. A ratio
            # needs 400+ aligned daily bars, so the lookback is generous.
            # REQUESTED WHILE THE CACHE IS SHORT, not once and never again.
            #
            # This used to mark a ticker `_rotation_requested` on the first tick and skip it forever.
            # `request_bars` delivers ASYNCHRONOUSLY, so a request that never lands leaves the cache
            # empty with nothing to retry it. Measured 2026-08-26: test-alpaca sat at 0 of 25 axes for
            # fifteen minutes with ZERO RequestBars in that window — every ticker was marked "asked"
            # and none had bars. "Asked" is not "have".
            #
            # Gating on what the cache actually HOLDS is self-healing and idempotent: a satisfied
            # ticker is never re-requested, and an unsatisfied one is retried every tick.
            start = self.clock.utc_now() - pd.Timedelta(days=_ROTATION_LOOKBACK_DAYS)
            counts = {t: len(self.cache.bars(bar_type(iid, "1d"))) for t, iid in iids.items()}
            # THREE STATES, NEVER TWO (#606): seeded, short-but-expected, and never-told-us. The old
            # gate had only the first two, so a symbol the venue can never serve kept `short`
            # non-empty forever and the compass stayed dark for the life of the process. A member
            # that gains NO bars for `_ROTATION_ABSENT_TICKS` consecutive ticks while a sibling is
            # fully seeded is ABSENT for this tick: it stops gating publication but is still
            # re-requested and re-classified from the cache every tick, so late bars heal it.
            a_sibling_is_seeded = any(c >= _ROTATION_MIN_BARS for c in counts.values())
            stall = getattr(self, "_rotation_stall", None)
            if stall is None:
                stall = self._rotation_stall = {}
            short, absent = [], []
            for ticker, iid in iids.items():
                n = counts[ticker]
                if n >= _ROTATION_MIN_BARS:
                    stall.pop(ticker, None)
                    continue
                try:
                    self._request_bars_paced(bar_type(iid, "1d"), start=start)
                except Exception as exc:                                # noqa: BLE001
                    self.log.warning(f"rotation: could not request {ticker} bars: {exc}")
                prev = stall.get(ticker)
                if prev is None:
                    # First observation is the BASELINE, never a count: one look proves nothing
                    # about "never" (codex on #606 — a one-observation filter would have
                    # blacklisted 21 instruments IBKR serves fine).
                    stall[ticker] = (n, 0)
                elif a_sibling_is_seeded and n <= prev[0]:
                    stall[ticker] = (n, prev[1] + 1)
                else:
                    # Progress, or no seeded sibling to prove delivery is even possible (a cold
                    # cache after a restart) — no evidence of "never", so the count starts over.
                    stall[ticker] = (n, 0)
                (absent if stall[ticker][1] >= _ROTATION_ABSENT_TICKS else short).append(ticker)

            def bars_for(ticker: str) -> list[tuple]:
                iid = iids.get(ticker)
                if iid is None:
                    return []
                # `cache.bars()` returns NEWEST FIRST; `bars_to_tuples` sorts ascending, which the
                # grading requires — `weekly` folds in order and `window_stat` indexes from the end.
                #
                # TRUNCATED AT THE FETCH, NOT IN THE GRADING. `read_pair` is a verbatim port whose
                # agreement with the original is recorded bar-for-bar, and the original is gone — so
                # changing what it does with the series it is handed would invalidate the only evidence
                # the two ever agreed. Slicing the INPUT leaves the maths untouched and removes the
                # non-determinism where it actually lives. The TAIL, because a leading slice would
                # grade a window that ended months ago and still produce 25 confident axes.
                return bars_to_tuples(self.cache.bars(bar_type(iid, "1d")))

            # SEEDING IS NOT AN EMPTY MARKET. `request_bars` is asynchronous, so for the first ticks
            # after a recreate the cache is legitimately short — and publishing 0 axes then is the
            # same lie as every other absence in this feature's history. Keep the last good payload
            # and say what is happening.
            if short:
                self._publish("rotation", {
                    **(self._rotation or {"axes": [], "errors": []}),
                    "seeding": (
                        f"waiting on daily history for {len(short)} of {len(iids)} instruments — "
                        f"requested, not yet delivered"
                    ),
                })
                self.log.info(
                    f"rotation: seeding, {len(short)} of {len(iids)} instruments still short")
                return

            payload = build_payload(bars_for, source="engine:nautilus-cache")
            # WHICH SESSION THE GRADED BARS COVER (#616). This grade is "fixed so two stacks
            # agree" — but two stacks on different providers grade over DIFFERENT daily bars by
            # venue construction: IBKR's fold pre/post-market into the OHLC, Alpaca's are the
            # regular session, so ADX/Ichimoku legitimately differ on identical closes whenever a
            # premarket move reversed. The payload says which it graded, so a cross-stack
            # disagreement is attributable to the declared dimension instead of reading as a
            # broken compass. 'undeclared' is the third state — a feed built without the spec
            # (hand-built doubles only) must not read as either real answer.
            payload["daily_bars_cover"] = self._daily_bars_cover or "undeclared"

            # DEGRADED LOUDLY, NEVER SILENTLY (#606). A frame graded from fewer members than the
            # compass is configured with must SAY WHICH are missing — a 23-axis payload that looks
            # exactly like a full one is silent universe drift, the more dangerous failure. Both
            # kinds of missing member are named: never-resolved (`unresolved`) and
            # resolved-but-bars-never-arrive (`absent`).
            missing = sorted(set(absent) | set(unresolved))
            if missing:
                payload["absent"] = {
                    "symbols": missing,
                    "reason": (
                        f"graded without {len(missing)} of {len(wanted)} configured instruments — "
                        f"no daily history after {_ROTATION_ABSENT_TICKS} refreshes while siblings "
                        f"were seeded; still re-requested every tick"
                    ),
                }
                self.log.warning(
                    f"rotation: graded without {len(missing)} of {len(wanted)} instruments — "
                    f"{', '.join(missing)} (absent this session; still re-requested)"
                )

            # THE PROVIDER CANNOT SERVE THIS, SAID OUT LOUD. Previously an unresolvable universe left
            # `if self._http is None: return` — no payload, no error, no log, and a Market tab that
            # looked like a quiet market. If nothing graded and nothing is even resolvable, say why.
            if unresolved and not payload.get("axes"):
                payload["unavailable"] = (
                    f"this stack's data provider does not carry {len(unresolved)} of the "
                    f"{len(wanted)} instruments the compass needs, so no rotation can be graded"
                )
                self.log.warning(
                    f"rotation: {len(unresolved)} of {len(wanted)} tickers unresolved on this "
                    f"provider — {', '.join(sorted(unresolved)[:6])}"
                )

            self._rotation = payload
            self._publish("rotation", payload)
        except Exception as exc:  # noqa: BLE001 — a display plane must never touch the trading loop
            self.log.warning(f"rotation refresh failed, keeping last known: {exc}")

    def _rotation_instrument_id(self, ticker: str):
        """The node's InstrumentId for a compass ticker, or None if this provider does not carry it.

        THE SHARED RESOLVER, not a venue string. `strategies.momentum._instrument_ids` looks the MIC up
        per symbol, and its own docstring says why that matters: Alpaca is multi-venue, and a
        config-wide default built `BAC.XNAS` for NYSE names. Nothing about that fails loudly — the id
        matches no instrument, `subscribe_bars` yields nothing, and the caller sits there looking
        healthy. A hardcoded suffix here would reproduce it in the compass.

        It RAISES on a symbol it cannot map, which is right for a strategy universe and wrong here: one
        ETF this provider does not carry must cost its own axes, not the whole market view. Caught per
        ticker, and the count is reported in the payload rather than swallowed.
        """
        try:
            resolved = self._resolve_from_cache([ticker])
            return resolved[0] if resolved else None
        except Exception:                                               # noqa: BLE001
            return None

    async def _refresh_realized_periods(self) -> None:
        """Realized P&L per period, swept from the BROKER's fills (#322) — the ACCOUNT figure.

        Since #846 the per-strategy split the cockpit renders comes from Nautilus's own persisted
        position legs (`_realized_windows`), on every venue, restart-safe. This sweep remains for what
        the legs cannot see: cash that moved with no fill behind it. Alpaca's ledger showed a $999.09
        gap between fills and the account — one PTP withholding (-$990.46) and 38 fee rows (-$8.63) —
        and Nautilus models none of that (`FillReport.commission` is its only cash event). So this is
        published BESIDE the legs as `realized_periods_swept`, never averaged with them: the two
        disagreeing is a detector, and it is how #846 was found.

        Best effort. A failed sweep leaves the LAST GOOD answer standing rather than blanking it: a
        transient 429 must not make a month's P&L vanish and reappear.

        DELIBERATELY NOT PORTED to the typed venue reads (#641). Nautilus models fills only — no
        withholding, fees, dividends or journal entries on any adapter
        (`test_realized_pnl_needs_a_LEDGER_which_nautilus_does_not_model`) — and on IBKR through the
        Gateway `reqExecutions` answers for today only (IB: "limited to only executions since
        midnight"), so a venue fill sweep could not replace this even for the fill half. On a venue with
        no Alpaca REST client it stays OFF — but LOUDLY, once, naming the venue: a silent bare return
        here is the "fallback that degrades silently" failure, and it cost two deploys on the notifier.
        """
        if self._http is None:
            if self._realized_periods_degraded is None:
                venue = str(self._exec_client_id) if self._exec_client_id is not None else "<no venue>"
                self._realized_periods_degraded = (
                    f"{venue} supplies no activity ledger this engine can read — multi-period realized "
                    f"P&L will not update (session realized is unaffected)"
                )
                self.log.warning(f"realized periods DEGRADED: {self._realized_periods_degraded}")
            return
        try:
            from api.realized_broker import (
                FILL_TYPE,
                cash_adjustments,
                fill_cashflow,
                open_lots_after,
                realized_by_period,
                reconcile,
            )

            # EVERY activity type, not just FILL (#345 item 1). Realized-from-fills read +$3,574.04
            # with `unmatched: 0` while the broker's own arithmetic implied +$2,574.95, and the whole
            # $999.09 gap was cash that moved with no fill behind it — one PTP withholding on PAA
            # (-$990.46) and 38 fee rows (-$8.63). `unmatched` is structurally blind to that: it counts
            # sales missing their opening buy, and a withholding is not a sale, so it read clean.
            everything = await self._http.list_activities(activity_type=None)
            fills = [a for a in everything if str(a.get("activity_type") or "") == FILL_TYPE]
            day_start_ns, _ = _et_day_bounds_ns(self.clock.timestamp_ns())
            # PER-STRATEGY ATTRIBUTION (#345 item 3). Alpaca's FILL activities carry `order_id` — the
            # VENUE id — and no strategy tag at all. Nautilus's cached orders carry both `venue_order_id`
            # and `strategy_id`, and survive restarts via the AOF, so the join is one hop and needs no
            # extra broker call.
            #
            # MEASURED COVERAGE, 2026-08-21, because this decides what the number can honestly claim:
            #
            #     2026-08-17 onward   130 of 130 fills join to a cached order   100%
            #     before 2026-08-17     2 of 191                                 ~1%
            #
            # The cache does not reach past its horizon, so 1D and 1W are fully attributable and the
            # longer windows are not. That is why `unclaimed` exists and why it is published rather than
            # distributed — see `realized_by_period`.
            by_venue_order: dict[str, str] = {}
            for o in self.cache.orders():
                vid = getattr(o, "venue_order_id", None)
                if vid is None:
                    continue
                by_venue_order[str(vid)] = str(o.strategy_id)

            def _strategy_of(fill: dict) -> str | None:
                return by_venue_order.get(str(fill.get("order_id") or ""))

            periods = realized_by_period(
                fills,
                day_start_ns=day_start_ns,
                ns_of=_iso_to_ns,
                adjustments=everything,
                strategy_of=_strategy_of,
                # ONE window-floor predicate for the sweep and the legs (#846), or the two derivations
                # disagree by construction across a DST change.
                floor_of=_et_window_start_ns,
            )

            # THE GUARD #345 ASKED FOR, and the reason this is not just a bigger fetch: an assertion
            # about the record that does not go through the matcher. Equity minus baseline minus
            # unrealized shares no code with FIFO matching, so agreement establishes something neither
            # derivation can establish about itself. Disagreement is reported, never absorbed.
            adj_total, by_type = cash_adjustments(everything)
            try:
                if not _ACCOUNT_BASELINE:
                    raise ValueError("ACCOUNT_BASELINE_EQUITY unset — reconciliation disabled, not failed")
                acct = await self._http.get_account()
                positions = await self._http.list_positions()
                unrealized = sum(float(p.get("unrealized_pl") or 0) for p in positions)
                # THE LOT CHECK RUNS FIRST, because `reconcile` needs its answer to say what a
                # residual MEANS. `open_lots_after` was written to find the PENG `sell_short` defect,
                # found it, and was then called by nothing — the fifth mechanism in this session
                # built, tested, and never wired. What it sees that no arithmetic on totals can:
                # realized-from-fills is wrong by exactly the basis of any lot the matcher holds and
                # the account does not, and a split moves quantity while moving no cash at all.
                phantom: dict[str, tuple[float, float]] = {}
                try:
                    ours = open_lots_after(fills)
                    theirs = {str(p["symbol"]): float(p["qty"]) for p in positions}
                    phantom = {
                        sym: (ours.get(sym, 0.0), theirs.get(sym, 0.0))
                        for sym in set(ours) | set(theirs)
                        if abs(ours.get(sym, 0.0) - theirs.get(sym, 0.0)) > 1e-6
                    }
                    if phantom:
                        self.log.warning(
                            f"MATCHER LOTS DISAGREE WITH THE ACCOUNT: {phantom} (ours, theirs). "
                            f"Realized-from-fills is wrong by the basis of the difference"
                        )
                except Exception as exc:  # noqa: BLE001 — a cross-check must not cost the figure
                    self.log.warning(f"open-lot cross-check unavailable: {exc}")

                rec = reconcile(
                    reported_realized=periods["all"]["total"],
                    adjustments=adj_total,
                    equity=float(acct["equity"]),
                    baseline=_ACCOUNT_BASELINE,
                    unrealized=unrealized,
                    cash=float(acct["cash"]),
                    fill_cashflow=fill_cashflow(everything),
                    lots_diverge=bool(phantom),
                )
                rec["by_type"] = by_type
                rec["phantom_lots"] = phantom
                periods["reconciliation"] = rec
                if not rec["reconciled"]:
                    # WHAT THE RESIDUAL MEANS, not merely that there is one (2026-08-23). For hours
                    # this said "something moved this account that the activity record does not
                    # explain" while the record reconciled to Alpaca's own `cash` TO THE CENT. It was
                    # BETA: flat at 15:35:22 on 2026-08-20, reopened at 16:00:10, and Alpaca's
                    # `avg_entry_price` never reset — so its cost basis, and `unrealized` after it, and
                    # `implied` after that, were $33.53 out. Ours was right. An alarm that cries theft
                    # over the broker's own rounding is an alarm that gets muted before the day it
                    # matters, which is the whole reason `verdict` exists.
                    cash_res = rec.get("cash_residual")
                    meaning = {
                        "BROKER_BASIS": (
                            "the record is COMPLETE — every dollar in and out matches the broker's "
                            "cash. The gap is in the broker's cost basis or marks, not in our fills"
                        ),
                        "LOTS_DIVERGE": (
                            "cash reconciles but the SHARE COUNT does not — a split, exchange or "
                            "symbol change moves quantity without moving a dollar, and every lot "
                            "basis from the fills is stale until it is re-derived"
                        ),
                        "RECORD_INCOMPLETE": (
                            "CASH DOES NOT MATCH EITHER — money moved that the activity record does "
                            "not show. This is the $999.09 shape (#345)"
                        ),
                    }.get(str(rec.get("verdict")), "cash arm unavailable — cause not localised")
                    self.log.warning(
                        f"REALIZED DOES NOT RECONCILE [{rec.get('verdict')}]: fills "
                        f"{rec['reported']:.2f} + adjustments {rec['adjustments']:.2f} vs "
                        f"equity-implied {rec['implied']:.2f} — residual {rec['residual']:.2f}; cash "
                        f"residual {'n/a' if cash_res is None else format(cash_res, '.2f')}. "
                        f"{meaning}. Breakdown by type: {by_type}"
                    )
            except Exception as exc:  # noqa: BLE001 — the reconciliation must never cost the figure
                self.log.warning(f"realized reconciliation unavailable, periods still published: {exc}")

            self._realized_periods = periods
        except Exception as exc:  # noqa: BLE001 — a display figure must never touch the trading loop
            self.log.warning(f"realized period sweep failed, keeping last known: {exc}")

    def _session_realized(self) -> dict:
        """Today's realized P&L per strategy — the 1D window of `_realized_windows` (#233, #846).

        `REALIZED $0.00` was not a wrong number, it was an unreachable one: a CLOSED cycle is emitted
        once and dropped, and the tile summed only live cycles — so P&L left the screen the moment a
        position closed (#233). Reading `cache.positions_closed()` fixed that and had its own hole: a
        NETTING reopen replaces the position, and the first round trip left the figure (#846). The
        windows read native closed positions UNION the legs Nautilus persisted, so both are one number.
        """
        return self._realized_windows()["1D"]

    def _realized_windows(self) -> dict[str, dict]:
        """Realized per strategy over 1D/1W/1M/3M/all from Nautilus's own closed legs (#846).

        Best effort — a failure here costs display fields on a frame, never the trading loop, and it
        costs them LOUDLY: every window is returned empty with `error` set, never as zero P&L.
        """
        try:
            return realized_windows(
                positions_closed=self.cache.positions_closed(),
                positions_open=self.cache.positions_open(),
                legs=list(self._closed_legs.values()),
                now_ns=self.clock.timestamp_ns(),
            )
        except Exception as exc:  # noqa: BLE001 — a display field must not take down the projection
            _log.warning("realized windows unavailable (%r)", exc)
            return empty_windows(repr(exc)[:120])

    def _lane_flows(self) -> dict:
        """Per-lane net invested per ET session over the cache's orders (#699 a) — best effort, and a
        failure costs the field LOUDLY (`error` set, map empty), never a partial map."""
        try:
            return lane_flows_by_day(self.cache.orders(), now_ns=self.clock.timestamp_ns(),
                                     positions=self.cache.positions_open())
        except Exception as exc:  # noqa: BLE001 — a display field must not take down the projection
            _log.warning("lane flows unavailable (%r)", exc)
            return {"by_day": {}, "earliest": None, "horizon_days": None, "error": repr(exc)[:160]}

    def _realized_legs_status(self) -> dict:
        """What the windows were computed over. `complete` is False while the seed's Redis scan stopped
        early — a figure over a partial seed is a wrong number wearing a complete label (#846)."""
        stopped = self._legs_seed_stopped_at
        # THREE STATES (scope review #2): never ran (store down, thread timed out) is its own condition,
        # not "complete with nothing to restore". `0 of 0` is not `0 of 4`.
        state = "never_ran" if not self._legs_seeded else ("stopped_early" if stopped else "complete")
        return {
            "state": state,
            "restored": self._legs_restored,
            "held": len(self._closed_legs),
            "live": max(0, len(self._closed_legs) - self._legs_restored),
            "seed_stopped_at": stopped or None,
            "complete": state == "complete",
        }

    def _mark_broker_stop_prices(self, dtos) -> None:
        """Fill each protective order's trigger price from the BROKER (#289).

        A trailing stop carries an offset, not a trigger: Alpaca derives the stop and moves it upward with
        its own high-water mark, and that never reaches the order object we hold. `securedValue` reads
        `trigger_price ?? price`, got null on every stop, and reported $0.00 protected while eleven stops
        rested — under any reading of "secured" that number was unreachable from the book's real state.

        Only FILLS what is missing. A bracket leg submitted with an explicit trigger already carries the
        right value, and the venue's echo of our own number should not overwrite it.
        """
        # Broker truth about protection, per cycle (#285). Marked BEFORE the early return below: a book
        # with no stop prices to fill still needs to know what the broker is protecting, and returning
        # first is exactly how this stayed broken.
        protected = getattr(self, "_broker_protected", None)
        for dto in dtos:
            if protected is None:
                # NEVER ASKED. Three states, not two — this must not render as NAKED.
                dto.broker_protected = None
                continue
            side = str(getattr(dto, "side", "") or "").upper()
            if side not in ("LONG", "SHORT"):
                # A position whose side this read cannot describe is not a protected one. The
                # previous instrument-level answer said True for it whenever any sell rested.
                dto.broker_protected = None
                continue
            reducing = "SELL" if side == "LONG" else "BUY"
            dto.broker_protected = (str(dto.instrument_id), reducing) in protected

        prices = getattr(self, "_broker_stop_prices", None)
        if not prices:
            return
        for dto in dtos:
            for order in getattr(dto, "working_orders", None) or []:
                # A TRAILING STOP'S TRIGGER IS THE VENUE'S, ALWAYS. Measured on the live book
                # 2026-08-23: ours read 176.356908 while Alpaca held 184.487823, and the venue's number
                # is exactly `hwm * (1 - trail_percent)` — 192.295 * 0.9594 — while ours corresponds to
                # an earlier high of 183.820. $8.13 a share, $447.20 on the 55 held.
                #
                # We never submit a trigger for one of these; we submit an OFFSET, and Alpaca computes
                # and ratchets the trigger against its own high-water mark. Our value is a snapshot
                # that is stale from the first tick that makes a new high, and stale DOWNWARD — the
                # direction that makes a protected book look more exposed than it is.
                #
                # The guard below stays for a FIXED stop, where the reasoning it was written with holds:
                # we chose that number, the venue is echoing it back, and overwriting buys nothing and
                # risks float drift. #289 fixed the FETCH and left this guard in front of the assignment.
                trailing = str(getattr(order, "order_type", "") or "").upper() in _TRAILING_ORDER_TYPES
                if not trailing and getattr(order, "trigger_price", None) is not None:
                    continue
                stop = prices.get(str(getattr(order, "client_order_id", "")))
                if stop is not None:
                    order.trigger_price = stop

    #: Relative gap above which the engine's basis and the broker's are treated as DISAGREEING rather
    #: than as rounding. The live case was 2.58% (71.78 vs 73.68); float/rounding noise between the two
    #: sources is orders of magnitude under this. Deliberately not tighter — a flag that fires on the
    #: last decimal is a flag operators learn to ignore.
    BASIS_DIVERGENCE_TOL = 0.001

    def _marking_basis(self, instrument_id: str, engine_avg: float | None):
        """Which cost basis to mark against, and whether the two sources disagree (#370).

        ONE FUNCTION, TWO CALLERS, BECAUSE TWO DERIVATIONS OF ONE FACT WILL DISAGREE. Unrealized P&L is
        computed in two places — the cycle DTOs the managed book reads and the POSITION rows — and before
        this they each reached for `avg_px_open` independently. A fix applied to one would have left the
        other reporting the number it was meant to remove, which is exactly the shape that has already
        bitten leash validation and manager-armed derivation.

        THE BROKER WINS. WHD carried avg_px_open 71.78 against a venue basis of 73.68 and reported
        +$263.84 unrealized while Alpaca said +$9.52 — $254.32 of gain that did not exist. Nautilus
        DETECTED the divergence at startup, logged it as "incomplete reconciliation data from the venue",
        and left the wrong value in place. The venue was correct; the engine was wrong. The cause was
        attribution — a flatten submitted under MANUAL opened a phantom SHORT instead of closing
        MOMENTUM's LONG, so MOMENTUM's position was never touched and kept its original entry.

        That cause is fixed (the flatten now routes to the owning strategy), but a basis already diverged
        stays diverged until the position closes, and nothing else corrects it.

        Returns `(basis, contested)`. `contested` is None when the broker has not been asked — a state
        that must not read as agreement, the same three-state `broker_protected` uses.
        """
        if engine_avg is None:
            return None, None
        known = getattr(self, "_broker_avg_entry", None)
        if known is None:
            return engine_avg, None
        venue = known.get(str(instrument_id))
        if venue is None or venue <= 0 or engine_avg <= 0:
            # The broker was asked and does not hold this instrument, or quotes no basis for it. Not a
            # disagreement — there is nothing to disagree with.
            return engine_avg, False
        key = str(instrument_id)
        if abs(venue - engine_avg) / venue <= self.BASIS_DIVERGENCE_TOL:
            # Agreement CLEARS the latch, so a basis that diverges, heals and diverges again warns
            # again. A latch that never reset would report the second episode as the first one still
            # running, which is a different claim.
            self._basis_divergence_warned.discard(key)
            return engine_avg, False
        # ONE WARN PER EPISODE PER INSTRUMENT (#386). This ran once per position per feed tick and
        # emitted a line every two seconds for as long as the divergence lasted. That is the failure
        # this file already fixed once: `_venue_has_account` became 1200 of 1287 log lines — 93% of the
        # log — "drowning the only signal that tells a working strategy from a stuck one". Reintroduced
        # two functions away from `_equity_estimate_warned`, which got this right for #382.
        #
        # The divergence stays VISIBLE: `contested` is returned unchanged on every call, the UI carries
        # it continuously as the `basis contested` badge, and the marking still uses the broker's figure.
        # Only the repetition is dropped. A contested basis that logged nothing would be the opposite
        # defect, and worse.
        if key not in self._basis_divergence_warned:
            self._basis_divergence_warned.add(key)
            self.log.warning(
                f"basis divergence for {instrument_id}: engine={engine_avg:.4f} broker={venue:.4f} "
                f"({abs(venue - engine_avg) / venue:.4%}) — marking against the broker. "
                f"The disagreement is stated, not attributed: this does not establish which side is stale. "
                f"Logged ONCE per episode; the `basis contested` badge carries it continuously."
            )
        return venue, True

    def _mark_cycle_financials(self, dtos) -> None:
        """Mark HELD cycles to market so the managed book shows UNREALIZED P&L / value, not just realized.
        Same source as the unclaimed rows — the last bar close (feed gives bars, not always quotes). Direct
        calc on the cycle's own avg + qty; signed by side. Best effort — no price leaves the fields None."""
        from api.ownership import display_signed_qty

        price_by_sym = {str(iid): px for iid, px in self._last_close.items()}
        for d in dtos:
            # MAGNITUDE, not the raw number (#855): the contract is unsigned `quantity` + `side`, and
            # `display_signed_qty` treats the side as the authority, so a signed spelling — which no
            # producer emits today, and a contract test pins — marks the same as the unsigned one.
            if (not d.is_capital_deployed or d.avg_px_open is None
                    or display_signed_qty(d.side, d.quantity) == 0.0):
                continue
            # THE CONTESTED FLAG IS SET BEFORE THE PRICE CHECK, because it is not a fact about price.
            # Placing it after the `continue` below meant that with the market closed and no bars
            # streaming, every position reported `basis_contested: null` — the exact "nobody has checked"
            # state the three-state exists to distinguish, published for a basis that had in fact been
            # checked and found wrong. Caught by querying the running engine, not by any test: the unit
            # was right and the placement was not.
            basis, contested = self._marking_basis(d.instrument_id, d.avg_px_open)
            d.venue_avg_px = (getattr(self, "_broker_avg_entry", None) or {}).get(d.instrument_id)
            d.basis_contested = contested

            price = price_by_sym.get(d.instrument_id)
            if price is None or basis is None:
                continue
            last = price.as_double() if hasattr(price, "as_double") else float(price)
            signed = display_signed_qty(d.side, d.quantity)
            d.last_px = last
            # SIGNED — `signed` is computed on the line above and was then ignored by this one, while
            # the unrealized-P&L line below used it correctly (2026-08-21). This is the path that feeds
            # `/trades`, so it is the one that rendered WHD's flat +28/-28 book as 56 shares and ~$3,849
            # of exposure that did not exist. The method's own docstring already says "signed by side".
            # The identical defect exists in `_enrich_position_financials` for `/positions`; both are
            # fixed, and `test_BOTH_marking_paths_sign_market_value` now covers any third one.
            d.market_value = last * signed
            d.unrealized_pl = (last - basis) * signed
            cost = basis * abs(signed)
            if cost > 0:
                d.unrealized_plpc = d.unrealized_pl / cost

    def _venue_has_account(self, venue) -> bool:
        """Does Portfolio hold an account for this venue? Cached — the answer is fixed by the node's
        client config at startup, and the question is asked once per position per feed tick."""
        known = self._venue_account_cache.get(venue)
        if known is None:
            known = self.portfolio.account(venue) is not None
            self._venue_account_cache[venue] = known
        return known

    def _enrich_position_financials(self, rows) -> None:
        """Attach cost basis / market value / unrealized P&L to POSITION rows — all computed by NAUTILUS
        (Portfolio.net_exposure / unrealized_pnl), marked with the last bar close we already stream (the feed
        gives bars, not always quotes). The engine owns the math; this only supplies the price it lacks. Best
        effort — a missing price leaves the P&L fields None (the UI shows '—')."""
        # Nautilus returns a mix of floats (Position.avg_px_open) and objects (Money/Price with .as_double()).
        def _num(v) -> float | None:
            if v is None:
                return None
            return v.as_double() if hasattr(v, "as_double") else float(v)

        # KEYED BY (instrument, strategy), NOT BY INSTRUMENT (#807 item 3). Under NETTING one instrument
        # holds one position PER STRATEGY, and a dict keyed by instrument keeps whichever came last: on
        # 2026-09-09 every EXTERNAL row was marked off its lane's mirrored SHORT — `mkt -$2,484` on a
        # LONG 10, the short's avg on the long's row. A row is marked from ITS OWN position or not at all.
        by_key = {(str(p.instrument_id), str(p.strategy_id)): p for p in self.cache.positions_open()}
        for r in rows:
            if r.source != "POSITION":
                continue
            pos = by_key.get((r.instrument_id, str(getattr(r, "strategy_id", ""))))
            if pos is None:
                continue
            r.avg_px = _num(pos.avg_px_open)
            price = self._last_close.get(pos.instrument_id)
            if price is None:
                continue
            r.last_px = _num(price)
            # Portfolio marks a position against the ACCOUNT registered for the INSTRUMENT's venue.
            # The Alpaca account is registered under venue ALPACA while the instruments are XNYS and
            # XNAS, so both calls below found no account, logged "Cannot calculate unrealized PnL: no
            # account registered for XNYS" at ERROR, and returned None. That was 1200 of the last 1287
            # log lines — 93% of the log, emitted every 2s per position, drowning the only signal that
            # tells a working strategy from a stuck one. P&L still arrived via the fallback below;
            # market value had no fallback, so every position rendered an empty market value.
            #
            # Ask Portfolio only when an account actually exists for that venue. Otherwise mark
            # directly off Nautilus's own avg_px_open and the streamed close — same math Portfolio
            # would do, minus the account lookup that cannot succeed in this venue layout.
            if self._venue_has_account(pos.instrument_id.venue):
                r.market_value = _num(self.portfolio.net_exposure(pos.instrument_id, price))
                r.unrealized_pl = _num(self.portfolio.unrealized_pnl(pos.instrument_id, price))
            # Portfolio.unrealized_pnl can return None for a reconciled/external position; fall back to the
            # direct calc on Nautilus's own avg + the mark price (signed_qty is + long / − short).
            # THE SAME BASIS DECISION AS THE CYCLE DTOs, through the same function. Portfolio's own
            # answer is discarded when the broker contradicts the cache, because Portfolio marks against
            # exactly the `avg_px_open` that is in dispute (#370).
            basis, contested = self._marking_basis(r.instrument_id, r.avg_px)
            if contested:
                r.unrealized_pl = None
            if r.unrealized_pl is None and basis is not None and r.last_px is not None:
                r.unrealized_pl = (r.last_px - basis) * float(pos.signed_qty)
            if r.market_value is None and r.last_px is not None:
                # SIGNED, like the unrealized-P&L line three lines up (2026-08-21). This read
                # `abs(pos.quantity)`, so a SHORT reported POSITIVE exposure. On the live paper book
                # WHD was long 28 under BCTROT-004 and short 28 under MOMENTUM-002 — genuinely flat,
                # and the broker held zero. Unsigned, the two legs summed to +$3,849 of exposure that
                # did not exist, `/trades` rendered "56 held", and the session monitor counted the
                # short as an unprotected long. The comment directly above already stated the rule:
                # `signed_qty` is + long / − short. Only this line disagreed.
                r.market_value = r.last_px * float(pos.signed_qty)
            cost = (basis or 0.0) * abs(float(pos.quantity))
            if r.unrealized_pl is not None and cost > 0:
                r.unrealized_plpc = r.unrealized_pl / cost

    #: How long the restart seed gets before the book stops waiting for it.
    #:
    #: MEASURED 2026-08-26: the seed failed with `TimeoutError()` after 4m30s on test-alpaca and 5m25s
    #: on staging-ibkr, and `_publish_trades` served an EMPTY book for that whole window. The per-socket
    #: `socket_timeout` inside it is not a bound on the seed — it retries, reconnects, and reads
    #: Postgres as well.
    #:
    #: The seed is a best-effort restore of cycle BOUNDARIES. It must never be able to hold the book.
    _SEED_TIMEOUT_SECS = 20.0

    def _start_seed_thread(self) -> threading.Thread:
        """Run the restart seed on its OWN thread, because the node loop cannot be relied on to schedule it.

        MEASURED, kumo-paper 2026-08-26: queued at 14:47:01, its 20s budget expired at 14:51:48 — four
        minutes forty-seven waiting for a slot. `on_start` also kicks off the backfill for ~500
        instruments at ~289 daily bars each, and that saturates the loop. The seed's own work is 60ms
        (22ms Redis, 38ms Postgres, across 83 snapshot keys).

        `asyncio.wait_for` measures WALL CLOCK, so on the loop the budget measured the SCHEDULER QUEUE
        rather than the work. Off the loop it measures the work, which is what a budget is for.

        The cost of it never running is not cosmetic: the seed loads the active envelope rows (31 on
        that boot), and without them the projection mints a FRESH cycle_id for every instrument that
        already has a live envelope row, so every write violates `uq_trade_cycle_active_key` (#564).
        """
        t = threading.Thread(target=self._seed_worker, name="trade-cycle-seed", daemon=True)
        self._seed_thread = t
        t.start()
        return t

    def _seed_worker(self) -> None:
        """LOAD off-loop, then APPLY under the projection lock.

        The split is the point. Loading touches Redis and Postgres and is what must escape the starved
        loop. APPLYING mutates the projections, and `project()` mutates too — `current_cycle_for` is a
        WRITER, called from manager drift guards on the message-bus thread — so the claim that nothing
        else touches the projection during the seed was false (codex). The lock, not the thread, is
        what makes the apply safe.

        `_cycles_seeded` is set in EVERY exit path: a seed failure means "mint fresh", never "hold the
        book hostage in `seeding` forever".
        """
        try:
            payload = asyncio.run(self._load_seed_bounded())
        except Exception as exc:  # noqa: BLE001 — a seed failure must not take down the engine
            self.log.error(f"trade-cycle restart seed failed to load (starting fresh): {exc!r}")
            self._cycles_seeded = True
            return
        try:
            # `_stopping` is a threading.Event, NOT a bool — `not getattr(self, "_stopping", False)`
            # would read the Event object, which is always truthy, and the seed would never apply at
            # all. Wrong-type truthiness, the same shape as `nan <= 0` and the falsy-`or` account
            # fallback. `.is_set()` is the only correct read.
            if payload is not None and not self._stopping.is_set():
                self._apply_seed(payload)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"trade-cycle restart seed failed to apply (starting fresh): {exc!r}")
        finally:
            self._cycles_seeded = True
            # KICK THE DISPATCH THE MOMENT THE GATE OPENS (codex round 4). `_startup_manager_check`
            # runs once and returns early if it loses the race to the seed; without this, a
            # deferred_flatten whose trigger is ALREADY met waits for the next 30s timer tick. The seed
            # is ~60ms, so the gate is the only thing that made "fires at open" mean "up to 30s after".
            if self._loop is not None:
                try:
                    self._spawn(self._dispatch_all_managers(), "manager dispatch (post-seed kick)")
                except Exception as exc:  # noqa: BLE001 - a failed kick must not undo the seed
                    self.log.warning(f"post-seed manager dispatch could not be scheduled: {exc!r}")

    async def _load_seed_bounded(self):
        """The budget, around the WORK — see `_start_seed_thread` for why that distinction is the fix."""
        try:
            return await asyncio.wait_for(self._load_seed(), self._SEED_TIMEOUT_SECS)
        except TimeoutError:
            self.log.error(
                f"trade-cycle restart seed exceeded {self._SEED_TIMEOUT_SECS}s of its OWN work "
                f"(starting fresh) — the book projects from the live cache; cycle boundaries mint afresh")
            return None

    async def _load_seed(self):
        """Read durable state. NO projection mutation here — that is `_apply_seed`, under the lock.

        ITS OWN ENGINE, WITH NullPool. `asyncio.run` gives this thread a NEW event loop, and asyncpg
        binds a connection to the loop that created it. Borrowing from the shared `session_factory`
        would check a connection out of the shared pool, bind it here, return it, and then leave it
        behind when this loop closes — poisoning the pool the whole engine uses. `NullPool` keeps no
        connection at all, and `dispose()` in the `finally` closes the transient one before the loop
        goes away. Codex flagged this as worse than the bug it fixes, and it was.
        """
        if not self._trade_cycles:
            return None
        snap_redis = self._snapshot_redis()
        # THE LEGS DO NOT DIE WITH THE CYCLE STORE (#846, scope review). They live in Redis; an
        # unreachable Postgres at boot used to mean zero legs, the windows silently reverting to
        # `positions_closed()` — the wrong number — with `restored: 0` reading like an account that never
        # closed anything. With no store the cycles mint fresh (as before); the legs are still read.
        seed_engine = None if self._cycle_store is None else create_async_engine(
            database_url(), poolclass=NullPool, pool_pre_ping=True)
        try:
            # THE ENVELOPE FIRST. It is the CRITICAL half and it is cheap — 38ms of Postgres across
            # five lanes, against 22ms+ of Redis for the legs, which is optional P&L detail.
            #
            # This was the other way round until codex round 4. If Redis spent the budget, the first
            # `await store.load_active(...)` was cancelled, the seed returned nothing, `_cycles_seeded`
            # flipped true anyway, and the projection minted FRESH cycle ids over instruments that
            # already had live envelope rows — every later write colliding on
            # `uq_trade_cycle_active_key`. That is exactly the storm #568 exists to end, reintroduced
            # by the ordering of the fix for it.
            _deadline = _time.monotonic() + self._SEED_TIMEOUT_SECS
            cycles: dict[str, list] = {}
            if seed_engine is not None:
                store = CycleEnvelopeStore(
                    session_factory_=async_sessionmaker(seed_engine, expire_on_commit=False))
                for sid in self._trade_cycles:
                    cycles[sid] = await store.load_active(
                        client_id=str(self._exec_client_id), strategy_id=sid)
            # The legs get WHAT IS LEFT of the same budget, not a fresh one — two full budgets would
            # together exceed the bound and the bound would stop bounding.
            legs = read_restored_legs(snap_redis, str(self.trader_id), deadline=_deadline)
            return {"legs": legs, "cycles": cycles,
                    "legs_stopped_at": getattr(legs, "stopped_at", None)}
        finally:
            try:
                snap_redis.close()
            except Exception:  # noqa: BLE001 — best effort; the dispose below is what matters
                pass
            if seed_engine is not None:
                await seed_engine.dispose()

    def _snapshot_redis(self):
        """The sync Redis client the seed scans snapshots with — its own, closed by the caller."""
        return redis.Redis(
            host=os.environ.get("KUMO_REDIS_HOST", self._bridge.get("redis_host", "127.0.0.1")),
            port=int(os.environ.get("KUMO_REDIS_PORT", self._bridge.get("redis_port", 6379))),
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )

    def _apply_seed(self, payload) -> None:
        """Hand the loaded state to the projections, UNDER THE PROJECTION LOCK.

        Legs first, then cycle boundaries — a seeded cycle's `opened_ts` window must already have its
        legs to include. Each projection seeds under ITS OWN strategy id; the envelope is scoped by
        strategy, so seeding MOMENTUM's projection with MANUAL's cycles would restore the wrong ones.
        """
        with self._projection_lock:
            # THE LEGS ARE KEPT, not only handed on (#846). The cycle projections consume them for cycle
            # P&L; the realized windows need the same legs, and forgetting them here is how a reopened
            # position's first round trip vanished from the day's figure.
            for pos_id, legs in payload["legs"].items():
                for leg in legs:
                    self._register_closed_leg(leg)
            self._legs_restored = sum(len(v) for v in payload["legs"].values())
            # INDEXED, not `.get(...) or ""`: a payload without the key is a loader that never said
            # whether its scan finished, and reading that as "complete" is absence as permission.
            self._legs_seed_stopped_at = payload["legs_stopped_at"] or ""
            self._legs_seeded = True
            for sid, proj in self._trade_cycles.items():
                for pos_id, legs in payload["legs"].items():
                    proj.seed_restored_legs(pos_id, legs)
                for row in payload["cycles"].get(sid, ()):
                    proj.seed_cycle(
                        row.account_id, row.instrument_id, row.cycle_id,
                        row.opened_ts, row.last_event_ts)

    def _on_envelope_write(self, cycle_id: str, attempt: int, key: tuple, fut) -> None:
        """Log a cycle-envelope write failure instead of silently swallowing it (an unobserved failure would
        make the engine look durable while the envelope diverges), and record the write as durable ONLY if
        it succeeded AND is still the attempt we are waiting for (#564).

        MATCHED ON THE ATTEMPT, NOT ON THE KEY. Keys repeat — a cycle can return to a state it already held
        (HELD -> ARMED -> HELD), so a stale future's key can equal a NEWER pending write's key and satisfy
        this guard by coincidence. `upsert` returns success for a monotonic no-op too, so the old attempt
        would mark the new write's key as durable while that write is still in flight; if it then failed,
        nothing would ever retry it. The counter makes each write distinguishable from every other.

        The guard is also what rejects a callback for a cycle that has since left the book — recording it
        would resurrect a pruned entry — and what releases the in-flight slot so the next tick may retry.
        """
        try:
            fut.result()
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"cycle-envelope write failed: {exc!r}")
            with self._envelope_lock:
                held = self._envelope_pending.get(cycle_id)
                if held is not None and held[0] == attempt:
                    self._envelope_pending.pop(cycle_id, None)
            return
        with self._envelope_lock:
            held = self._envelope_pending.get(cycle_id)
            if held is None or held[0] != attempt:
                return
            self._envelope_pending.pop(cycle_id, None)
            self._envelope_written[cycle_id] = key

    def on_bar(self, bar: Bar) -> None:
        # A BAR IS EVIDENCE THE FEED IS ALIVE, and on some venues it is the ONLY evidence (#608).
        #
        # `_last_tick_ts` — which the UI's "Market-data feed stale" banner reads — was written ONLY by
        # `on_trade_tick`. IBKR delivers bars and no trade ticks, because tick-by-tick is capped at
        # the venue (#578, 73 hits in one session), so the signal was structurally unreachable there.
        # Measured 2026-08-27 with both tenants on one commit:
        #
        #   paper   (Alpaca)  trade ticks flowing    last_tick_ts live   banner: feed live
        #   staging (IBKR)    1,663 bars in 10 min   last_tick_ts = 0    banner: NO FEED
        #
        # staging had NEVER had a non-zero value — the banner was wrong for the instance's entire
        # life, and it was read as evidence repeatedly while the engine sat there perfectly healthy.
        # Freshness means market data ARRIVED; which shape it arrives in is the venue's choice.
        #
        # `ts_init`, NOT `ts_event`, AND THAT DISTINCTION IS THE WHOLE POINT HERE. `ts_event` is the
        # bar's OWN timestamp — the session it covers — so a daily bar delivered now carries
        # yesterday's close. The first version of this fix used it and staging's freshness read 39
        # HOURS OLD the moment it went live: non-zero, correct-looking, and still stale.
        #
        # WHAT `ts_init` IS depends on who built the object, and it is NOT simply "when we received it"
        # (#917 corrected that sentence): IB stamps live ticks/quotes `max(now, ts_event)` — arrival —
        # but its catch-up bars through the SUBSCRIPTION path carry bar start + period, and our own
        # Alpaca parsers write the frame's own time into BOTH fields for every data type, so on paper a
        # `dailyBars` frame delivered at 16:01 ET says 00:00 ET here. Measured 2026-09-11: that one frame
        # dragged this clock back 16 h and `/health.status` read `degraded` through the next pre-market
        # with nothing wrong (#917).
        #
        # MONOTONIC (#917): `max()`, never a bare assignment. Each stamp reads as "a datum arrived at or
        # after this instant" — true under every stamping above — and a datum whose stamp is OLDER than
        # what we already saw is not evidence the feed went quiet. If the Alpaca parsers ever stamp
        # arrival, this line does not change (that is what IB already does). `on_historical_data` must
        # NOT write this clock at all — see the comment there. `test_feed_freshness_is_monotonic.py`
        # pins all of it, including that no writer anywhere in this class is a bare assignment.
        self._last_tick_ts = max(self._last_tick_ts, bar.ts_init)
        # The bar type, not the symbol: `1-HOUR-LAST-INTERNAL` arriving says nothing about whether
        # `1-DAY-LAST-EXTERNAL` on the same instrument ever bound (#618).
        self._subscriptions.bound("bars", str(bar.bar_type), bar.ts_init)
        # Keep the latest close per instrument as the mark price for Nautilus P&L — our data feed streams bars,
        # not always quotes, so this is the reliable price to hand Portfolio.unrealized_pnl / net_exposure.
        self._last_close[bar.bar_type.instrument_id] = bar.close
        self._publish_bar(bar, historical=False)
        self._update_vwap_from_bar(bar, historical=False)

    def on_historical_data(self, data) -> None:
        # NEVER writes `_last_tick_ts` (#917). A `request_bars` response is routed here by Nautilus
        # (actor.pyx: "to the `on_historical_data` handler"), and on IB a DAY response's LAST bar carries
        # `ts_init` = END of that day (bar start + 1 day - 1 ns, market_data.py `_ib_bar_to_ts_init`) —
        # in the FUTURE at request time. A monotonic write here would pin freshness at end-of-day
        # and a warmup after a recreate would mark a dead feed live. Historical data is not evidence
        # the feed is alive NOW; only the live handlers are.
        if isinstance(data, Bar):
            # Seed the mark price from backfill so P&L is available immediately after a restart, not only once
            # the next LIVE bar arrives (which on a 30m candle can be far off).
            self._last_close[data.bar_type.instrument_id] = data.close
            self._publish_bar(data, historical=True)
            # Same helper as the live path (below) — a restart mid-session must rebuild VWAP from the
            # session's actual 1m bars, not start over from whenever the process happens to come back up.
            self._update_vwap_from_bar(data, historical=True)

    def _publish_bar(self, bar: Bar, historical: bool) -> None:
        self._publish(
            "bar",
            {
                "instrument_id": str(bar.bar_type.instrument_id),
                "granularity": granularity_of(str(bar.bar_type)),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "ts_event": bar.ts_event,
                "historical": historical,
            },
            # History waits for queue space; a dropped backfill bar is a permanent hole in the series.
            backfill=historical,
        )

    def _update_vwap_from_bar(self, bar: Bar, historical: bool) -> None:
        """Session VWAP off 1m bars only — both `on_bar` and `on_historical_data` route here so a restart
        mid-session rebuilds correctly from the backfilled 1m history (feed.toml's 1m lookback is 7 days,
        well past one session), not just from whatever live bar happens to arrive first.

        Extended-hours/weekend bars are dropped before touching any state, so they can never smear the
        RTH-only VWAP — see `_et_session_ts`. Contributions are keyed by `ts_event` (REPLACE, not append),
        so a corrected live bar or a refetch-heal replaying an already-seen minute can't double-count.
        """
        if granularity_of(str(bar.bar_type)) != "1m":
            return
        et_ts = _et_session_ts(bar.ts_event)
        if et_ts is None:
            return  # extended hours / weekend — not part of the RTH session VWAP
        key = str(bar.bar_type.instrument_id)
        session_date = et_ts.date().isoformat()
        if self._vwap_session_date.get(key) != session_date:
            self._vwap_bars[key] = {}
            self._vwap_session_date[key] = session_date
            # Tombstone: clear any value the consumer/WS layer is still holding from the PRIOR session.
            # The real-value publish below only fires once real volume lands THIS session — for an
            # illiquid symbol (or right at the open) that can be well after the session boundary, and
            # without this the API/UI would keep showing yesterday's VWAP as if it were today's
            # (codex review, Phase 2: cross-process staleness — the engine's own gate isn't enough
            # because `RedisConsumer`/`app.py` persist whatever they last received).
            self._publish(
                "vwap",
                {"instrument_id": key, "vwap": None, "session_date": session_date, "ts_event": bar.ts_event, "historical": historical},
            )
        typical_price = (bar.close.as_double() + bar.high.as_double() + bar.low.as_double()) / 3.0
        volume = bar.volume.as_double()
        self._vwap_bars[key][bar.ts_event] = (typical_price, volume)
        contributions = self._vwap_bars[key].values()
        total_volume = sum(v for _, v in contributions)
        if total_volume <= 0:
            # No real volume yet this session (or every bar seen so far was zero-volume) — don't publish
            # a "VWAP" that isn't actually volume-weighted.
            return
        total_price_volume = sum(p * v for p, v in contributions)
        self._publish(
            "vwap",
            {
                "instrument_id": key,
                "vwap": total_price_volume / total_volume,
                "session_date": session_date,
                "ts_event": bar.ts_event,
                "historical": historical,
            },
        )

    def _check_vwap_session_rollover(self, now_ns: int) -> None:
        """Clock-driven tombstone for a symbol that gets NO 1m bar at all in the new session (halted all
        day, or simply illiquid) — the bar-driven tombstone in `_update_vwap_from_bar` can't fire for it,
        since nothing ever calls that method for the new session (codex review, Phase 2 round 2). Runs off
        the snapshot timer (~2s cadence), so a stale cross-session value clears within seconds of the open
        regardless of whether this symbol ever trades today.
        """
        et_ts = _et_session_ts(now_ns)
        if et_ts is None:
            return  # not currently in a session — nothing to roll into yet
        session_date = et_ts.date().isoformat()
        for key, stale_date in list(self._vwap_session_date.items()):
            if stale_date == session_date:
                continue
            self._vwap_bars[key] = {}
            self._vwap_session_date[key] = session_date
            self._publish(
                "vwap",
                {"instrument_id": key, "vwap": None, "session_date": session_date, "ts_event": now_ns, "historical": False},
            )

    def _on_snapshot(self, _event) -> None:
        self._check_vwap_session_rollover(self.clock.timestamp_ns())
        positions = [
            {
                "instrument_id": str(p.instrument_id),
                "side": p.side.name,
                "quantity": float(p.quantity),
                "avg_px_open": float(p.avg_px_open),
                "realized_pnl": str(p.realized_pnl),
                "strategy_id": str(p.strategy_id),
                # THE STALENESS LOCK A TRANSFER IS CHECKED AGAINST (#785). Absent here, the api had
                # nothing to send and reported 0, which the engine compared against the real value —
                # so a transfer OUT OF A LANE was refused as stale every time and had never once
                # succeeded. `ts_last` is what makes "the position moved since you read it"
                # answerable at all.
                "ts_last": int(getattr(p, "ts_last", 0) or 0),
            }
            for p in self.cache.positions()
        ]
        self._publish("positions", self._positions_frame(positions, now_ns=self.clock.timestamp_ns()))
        self._publish_trades()  # #73 — trade-cycle plane, reconciling backstop for the event-driven fold
        self._publish_external()  # #79 — quarantine plane (activity the cockpit didn't originate)
        self._publish_account()
        # Orders snapshot (#33) — the reconciling backstop for the blotter's working set. Event frames
        # (on_order_event) give instant status; this full snapshot of open orders heals any missed event.
        # `ts` = when this snapshot was taken. The api reconcile is now decoupled from the streamed `order`
        # events (#56: snapshot polled off a key, events off the stream), so it must NOT drop/revert a
        # working order whose last streamed event is NEWER than this snapshot — the ts gates that.
        # Open working set + recent CLOSED orders → the blotter is a HISTORY view, not just the working set.
        # Filled/canceled/rejected orders (incl. reconciled ones synthesized on restart, which never stream a
        # live event) must show; else the tab reads "No orders" while the account clearly traded. Bounded to
        # the most recent closed orders so the snapshot stays small.
        recent_closed = sorted(self.cache.orders_closed(), key=lambda o: o.ts_last or 0, reverse=True)[:100]
        self._publish(
            "orders",
            {
                "orders": [self._order_frame(o) for o in (*self.cache.orders_open(), *recent_closed)],
                "ts": self.clock.timestamp_ns(),
            },
        )
        # Health frame (#26): the engine's own liveness + market-data freshness. Published on the snapshot
        # cadence — if these STOP arriving the api knows the engine/bridge is down; `last_tick_ts` tells the
        # api whether the market-data feed is live (ticks keep flowing) or dead (age grows) independent of
        # the position snapshots, which re-push regardless.
        self._publish(
            "health",
            {
                # STILL "did a frame arrive", and deliberately unchanged: flipping it on a partial
                # start would take the whole UI dark over one stopped lane, and the banner it drives
                # means "the engine is gone". The counts below carry the finer fact instead.
                "engine_ok": True,
                # HOW MANY LANES CAN ACTUALLY TRADE (#454, codex round 2). A boot where NOTHING starts
                # already self-reports: no strategy runs, so this frame is never published and the
                # bridge goes stale. A PARTIAL start does not — the feed is added first and runs, a
                # trading strategy raises in `on_start` and does not, and every surface stays green
                # while zero lanes can trade. TECHIVOL-005 and QC345-003 exactly.
                # ARMED, PER LANE, because RUNNING does not imply able to decide. Nothing served
                # this before 2026-08-23: the probe existed on both sides of the contract and was
                # unreachable, so an unarmed lane was invisible on every surface a human reads.
                "armed_lanes": armed_by_lane(self._sibling_strategies),
                # WHAT THE REALIZED WINDOWS WERE COMPUTED OVER (#846): `complete: false` while the seed's
                # snapshot scan stopped early — the figures understate and this is where it says so.
                # Rides the health frame like every other health key; the trades frame carries it too.
                "realized_legs": self._realized_legs_status(),
                # When each lane is next due to decide, from its OWN alert (#537 follow-up).
                # The deploy gate reads this so it cannot recreate the engine inside a
                # decision window — which cost QC345-003 its 2026-08-25 session.
                "next_fire_ns": next_fire_by_lane(self._sibling_strategies),
                # LANES THAT SHOULD EXIST AND DO NOT (#539) — {name: reason}. The node is the only
                # thing that knows: `build_optional_strategy` catches the transport failure, and
                # without this the absence is a log line and nothing else.
                "lanes_absent": skipped_builds(),
                "automated_lanes_registered": strategy_run_counts(
                    self._sibling_strategies, skipped_builds())[0],
                "automated_lanes_running": strategy_run_counts(
                    self._sibling_strategies, skipped_builds())[1],
                "last_tick_ts": self._last_tick_ts,
                # The feed's print type (#834) beside its last-tick stamp, because the one is
                # uninterpretable without the other: 900s old is dead for REALTIME and healthy for DELAYED.
                "market_data_type": self._market_data_type,
                # Broker-vs-cache position drift (#26): non-empty → the broker holds positions the cockpit
                # can't reconcile/display. Surfaced as a loud banner so an empty book is never mistaken for
                # a flat account.
                "reconcile_drift": self._reconcile_drift,
                # Orders reconciliation had to SKIP because no OrderStatusReport can represent them
                # (#643) — e.g. a dollar-based order whose `qty` is null. Non-empty → those orders are
                # INVISIBLE to the engine: shares they reserve and protection they provide are not
                # being counted. Surfaced here so the skip that saved the batch from the #613 inert
                # shape is a condition an operator sees, not a silent drop.
                "unreconciled_orders": self._unreconciled_orders,
                # Protective orders RESTING AT THE BROKER that this engine cannot see (#454). Distinct
                # from drift above, which compares POSITIONS: on 2026-08-22 an Alpaca 503 made Nautilus
                # mark eleven stops REJECTED while all eleven rested untouched, and `reconcile_drift`
                # was correctly empty the whole time — the positions were fine, the protection was not.
                # The detector already existed and its finding died in a log line.
                "protection_divergence": self._protection_divergence,
                # Exits rejected while their protection was already released (#546) — an operator
                # must see these; the position is bare until the reconciler's next tick.
                "naked_after_reject": list(self._naked_positions),
                # REQUESTS THAT FAILED (#757). A request that failed is not one never made, and
                # neither is one that succeeded — the middle state had no home, so a venue refusing
                # half the book read exactly like a quiet market.
                "failed_requests": self._failed_requests.as_rows(),
                # A FLIP DEFERRED PASS AFTER PASS, as a number (#907): each refusal above is correct
                # and cleared every pass, so a lane deferred a hundred times read like one deferred
                # once. `[]` is "evaluated, nothing pending"; None is "the last pass never evaluated
                # the book" (outside RTH, disabled, unreadable settings, no broker) — and the api
                # reports None too when this frame is absent. Three states, one level inside.
                "flip_pending": self._flip_pending_rows(),
                # #873 phase 1: per-lane market-aware readings, the contract's presence NAMED, and the
                # count of emits that failed. A dict from construction — None means a build without
                # the plane; `lanes[sid]` None means not yet polled; `contract.state == "absent"` means
                # the strategies pin does not carry the vocabulary (every deployed pin today).
                "market_aware": self._market_aware_frame(),
                # REQUESTED VERSUS BOUND (#618) — the pair, and the NAMES of the dark ones. A count
                # alone cannot tell an operator which 117 of 392 are silent, which was the whole cost.
                "subscriptions": self._subscription_summary(),
                # CACHE VERSUS VENUE, with the NAMES (#758). Four wrong claims were made about this
                # stack on 2026-08-31 because the answer existed only in ~100k log lines.
                "book_truth": dict(self._book_truth),
                # IB shortability (#857): three states; None on a node that has no such plane.
                "shortable": ({**self._shortable_plane.health(now_ns=self.clock.timestamp_ns()),
                               "subscribe_failures": self._shortable_subscribe_failures,
                               "pending": len(self._shortable_pending)}
                              if self._shortable_plane is not None else None),
                # Fills the engine FABRICATED — per the operating notes, the phantom's mint.
                "inferred_fills": self._inferred_fills.as_rows(),
                # Fills on orders the cache holds TERMINAL (#807 item 4) — the mint's other face.
                "fills_on_terminal_orders": self._terminal_fills.as_rows(),
                # Rows the registry could not keep past its cap — a defence that hides what it
                # discarded is a second bug.
                "fills_on_terminal_orders_dropped": self._terminal_fills.dropped,
                # Venue questions nobody answered (#354): None = not told, 0 = told and none.
                "venue_unanswered_lookups": self._venue_unanswered_lookups,
                # THE OBSERVERS THEMSELVES (#758). A check that fails must eventually report itself,
                # or the fail-soft is just off — and one that NEVER RAN must not read as one that ran
                # clean.
                "observations": self._observations.summary(),
                # What the last compaction pass did — None until it has run once, which is not the
                # same as "nothing to do".
                "log_compaction": self._log_compaction,
                # Held positions the engine cannot price. It is an INPUT to protection sizing, exit
                # sizing and every displayed P&L; its absence was a log line. Counted here so
                # "11 of 22 unpriceable" is a number an operator can read rather than infer.
                "unpriced_positions": self._unpriced_positions(),
                # Is the feed stale ON TRADING TIME (#757)? Computed HERE because this process owns
                # the venue calendar; the API asking a node for a calendar object would be a second
                # derivation, and the first version of this check did exactly that and could only
                # ever answer None. True / False / None — unknown is not healthy and not a fault.
                "feed_stale": self._feed_stale(),
                # Which commit is publishing this frame (#329). Rides the health plane because that is the
                # one frame whose whole job is "what is the engine doing", and because it means the answer
                # reaches the UI without a new endpoint. Constant for the life of the process — it is a
                # property of the IMAGE, so re-sending it costs nothing and makes every captured frame
                # self-describing after the fact.
                "build": build_stamp(),
                "ts": self.clock.timestamp_ns(),
            },
        )

    def _feed_stale(self) -> bool | None:
        """Whether the feed has been silent while the venue was OPEN.

        A raw age cannot answer it: 59 hours across a weekend is healthy and 59 hours on a Tuesday is
        a dead feed, and a fixed threshold pages every Monday until someone mutes it. This asks the
        calendar how many of those minutes the market was actually trading.

        None where the calendar cannot say — a boot before the open, or a day the venue never
        described. Unknown must not read as healthy OR as broken.
        """
        from api.feed_staleness import feed_is_stale

        # None means UNKNOWN either way here — the calendar could not say, or the observer failed —
        # and the two are distinguished by whether `observations` carries a `feed_stale` row.
        return self._observations.run(
            "feed_stale",
            lambda: feed_is_stale(int(getattr(self, "_last_tick_ts", 0) or 0),
                                  self.clock.timestamp_ns(),
                                  getattr(self, "_venue_calendar", None)),
            ts_ns=self._safe_now(),
        )

    def _record_book_truth(self, position_reports) -> None:
        """Store cache-vs-venue truth from a venue read that has already been paid for.

        NO TRY/EXCEPT HERE. The first version had one, and its except branch called
        `self.clock.timestamp_ns()` — so on any object without a clock the HANDLER raised and took
        down the whole protection pass. `Observations.run` owns absorbing and reporting, in one place
        that is written to touch only built-in types, so the failure surfaces as a broken OBSERVER
        rather than as an outage of the thing observed.
        """
        if not hasattr(self, "_disagreements"):
            return          # a node built without the ledgers (synthetic/backtest)
        self._observations.run("book_truth", self._compute_book_truth, position_reports,
                               ts_ns=self._safe_now())

    def _trader_id_str(self) -> str:
        """This node's trader id, for the protective-order identity (#762).

        Never raises and never guesses: a node that cannot name itself returns "", which STILL
        differs from a real trader id, so the collision does not silently return.
        """
        try:
            return str(self.trader_id)
        except Exception:  # noqa: BLE001
            return ""

    def _safe_now(self) -> int:
        """A timestamp for the recorder, or 0. Never raises — the recorder must not depend on a clock."""
        try:
            return int(self.clock.timestamp_ns())
        except Exception:  # noqa: BLE001
            return 0

    def _compute_book_truth(self, position_reports) -> None:
        """The observation itself, free to raise: `Observations.run` is what makes that safe."""
        rows = position_truth(self.cache.positions_open() or [], position_reports)
        # Streaks observe FIRST, so this read's own disagreement counts toward being stuck.
        self._disagreements.observe(rows)
        rows = position_truth(
            self.cache.positions_open() or [],
            position_reports,
            abandoned=self._disagreements.stuck(),
        )
        summary = truth_summary(rows)
        summary["ts_ns"] = self._safe_now()
        self._book_truth = summary
        # THE BROKER'S QUANTITY PER SYMBOL, FROM THIS SAME READ (#834). `_publish_external` stamps
        # `venue_qty` from `self._broker_qty`, and until now that was set ONLY by the subscriber on
        # `_RECONCILE_TOPIC` — whose only publisher is the ALPACA exec client. On IBKR nobody ever
        # published it, so every unclaimed position rendered "broker unconfirmed, cannot be moved
        # yet" for the life of the process while this very pass reported the venue readable:
        # staging2, 2026-09-09, `book_truth: instruments 17, venue_unreadable 0` beside seven rows an
        # operator could neither claim nor close.
        #
        # THREE STATES. `None` reports = unreadable -> None, never `{}`: an empty dict says the broker
        # answered and holds nothing, which turns every row into a phantom the UI refuses to move.
        # Keyed by the BARE symbol via `rsplit(".", 1)[0]` — the exact expression `_publish_external`
        # looks up with, so BRK.B.XNYS folds to BRK.B on both sides. Signed, and summed across venues,
        # because a short is not a holding and a symbol listed twice is one position at the broker.
        # The Alpaca reconcile subscriber still writes this too; on that provider the two agree.
        self._broker_qty = _broker_qty_by_symbol(position_reports)

    def _subscription_summary(self) -> dict:
        """Requested-versus-bound, read through the venue's own calendar (#618).

        Wrapped like `_feed_stale`: a health read must never take the frame down. An exception here
        returns the SHAPE with a stated error rather than an empty dict — "we could not ask" is not
        "nothing is wrong", and an absent key would read as the latter on every surface.
        """
        out = self._observations.run(
            "subscriptions",
            lambda: self._subscriptions.summary(
                self.clock.timestamp_ns(), getattr(self, "_venue_calendar", None)
            ),
            ts_ns=self._safe_now(),
        )
        # None from `run` means it raised and the reason is recorded under `observations`. The SHAPE
        # is still returned with null counts rather than an empty dict: "we could not ask" must not
        # render as "asked, nothing subscribed".
        return out if out is not None else {
            "requested": None, "bound": None, "silent": None, "unknown": None,
            "silent_subjects": [], "error": "see observations",
        }

    def _unpriced_positions(self) -> list[str]:
        """Held instruments with no price the engine can find.

        NOT a count of missing subscriptions — this asks the question the consequences depend on: can
        we price what we HOLD. A symbol we never subscribed to and never held is not a problem; a
        position we cannot value is one, because protection sizes from it, exits size from it, and
        every displayed figure derives from it.

        Measured 2026-08-31: staging held 22 and could price 11. `/health` said ok.
        """
        out: list[str] = []
        try:
            for pos in (self.cache.positions_open() or []):
                try:
                    if not float(getattr(pos, "signed_qty", 0) or 0):
                        continue
                except (TypeError, ValueError):
                    continue
                iid = str(pos.instrument_id)
                if self._last_price_for(iid) is None:
                    out.append(iid)
        except Exception:  # noqa: BLE001 — a health read must never take the frame down
            return out
        return sorted(set(out))

    def _on_broker_account(self, snapshot: dict) -> None:
        """Cache the exec adapter's Alpaca account snapshot (#41) — the source of truth for equity/
        buying_power/multiplier (Nautilus AccountState only carries cash).

        REFUSES OUR OWN DERIVATION. On a node with no broker publisher (IBKR) `_publish_account`
        derives a snapshot and puts it on this topic so the strategies can see it. Caching it here
        would make the next `_publish_account` return from the cache instead of recomputing, and the
        published equity would freeze at its first value — with the daily-loss halt anchored to it.
        """
        if snapshot.get(_DERIVED):
            return
        self._broker_account = snapshot

    def _on_broker_equity_curve(self, snapshot: dict) -> None:
        """Cache + forward the account's equity curve (#243). Published straight through to the UI plane
        rather than folded into `account`: it refreshes on a 2-minute cadence against `account`'s 3
        seconds, so sharing a frame would either stall the account or spam the curve."""
        self._equity_curve = snapshot
        self._publish("equity_curve", snapshot)

    def _on_broker_reconcile(self, snapshot: dict) -> None:
        """Cache the exec adapter's broker-vs-cache position drift (#26) → folded into the health frame so
        the UI can warn when the broker holds positions the cockpit can't display."""
        self._reconcile_drift = snapshot.get("drift", [])
        # The broker's per-symbol map (#807 item 3). `.get` with no default: a producer that does not
        # send it (an older build, another venue) leaves None, which the rows render as "not told".
        self._broker_qty = snapshot.get("broker_qty")
        self._venue_unanswered_lookups = snapshot.get("unanswered_lookups")

    def _on_broker_unreconciled(self, snapshot: dict) -> None:
        """Cache the exec adapter's SKIPPED-order records (#643) → folded into the health frame so a
        skipped order is a named condition the alert plane announces, never a log line that dies
        unread. The provider publishes every batch, [] included, so a fixed offender CLEARS here."""
        self._unreconciled_orders = snapshot.get("orders", [])

    def _run_boot_gate_once(self, equity) -> None:
        """Probe every enabled lane once, the first time equity is known. Never raises.

        A gate that could take the node down at boot would be the #377 shape returning — QC345
        resolving its universe over HTTP at build time took MANUAL, MOMENTUM and BCTROT down with it.
        """
        try:
            from api.boot_gate import run_boot_gate, should_run

            if not should_run(self._boot_gate, equity=equity):
                return
            degraded = run_boot_gate(
                self._sibling_strategies,
                broker=self._broker_account,
                # A RESOLVER, NOT A DICT: the probes are PER LANE, and `run_boot_gate`
                # is the only thing that knows which lane it is looking at.
                platform=self._boot_gate_platform,
                enabled_ids=list(self._sibling_strategies),
                journal=lambda sid, why: self.log.error(
                    f"PREFLIGHT DEGRADED {sid}: {why}. The lane is running and something it needs to "
                    f"trade did not answer"
                ),
            )
            self._boot_gate.degraded = degraded
            if not degraded:
                self.log.info(
                    f"preflight: {len(self._sibling_strategies)} lane(s) probed at first account "
                    f"snapshot, none degraded"
                )
        except Exception as exc:  # noqa: BLE001 — the gate must never be able to kill the boot
            self.log.error(f"boot gate raised and was suppressed: {exc!r}")

    def _boot_gate_platform(self, strategy_id: str) -> dict:
        """Cockpit's own three probes FOR ONE LANE. The lane reports what it can OBSERVE (equity,
        price, owned, armed); the platform supplies what only it knows, and asking the strategy for
        these would be asking it to attest to something it does not own.

        THIS RETURNED `{}` AND THAT MADE THE WHOLE GATE UNABLE TO PASS (#532). `preflight.py:55`
        counts a missing platform probe against readiness, so every lane read DEGRADED on three counts
        on every tenant, always — while the message said "platform-side, not the lane's". The gate has
        had three fixes; none could make it report clean, because these were missing by construction.

        READ FROM A CACHE, NOT INLINE. `lifecycle` and `budget` are database reads and this runs on a
        msgbus callback inside `_publish_account` — sync, on the trading path. `_refresh_platform_probes`
        fills the cache on the loop, the same shape `_refresh_rotation` already uses.

        AN UNMEASURED LANE GETS `{}`, NEVER ZEROS. `budget -> 0` is a real failure this gate exists to
        catch (TECHIVOL's budget was never set), so defaulting an unread probe to 0 would make "not
        measured yet" indistinguishable from "measured, and it is zero" — the silencing direction.
        """
        try:
            cache = getattr(self, "_platform_probes", None)
            probes = cache.get(str(strategy_id)) if isinstance(cache, dict) else None
            return dict(probes) if isinstance(probes, dict) else {}
        except Exception:  # noqa: BLE001 — runs on a msgbus callback; #377, never take the node down
            return {}

    def _on_platform_probe_tick(self, _event=None) -> None:
        """Timer callback — hands the DB reads to the loop. Never raises onto the clock thread."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._refresh_platform_probes(), self._loop)

    async def _refresh_platform_probes(self) -> None:
        """Fill `_platform_probes` — {strategy_id: {lifecycle, budget, slot_size}}. Never raises.

        One read of the lifecycle table and one of the sleeve book, shared across lanes, rather than
        two queries per lane. `slot_size` is derived from the sleeve the same way sizing does it, so a
        collapsed slot (0 shares) is visible to the gate for the reason `preflight.py` records: "cap ==
        book size -> 0 on a full book".
        """
        try:
            from api.budget_store import last_decisions, lifecycle_states, load_book
            from api.db.engine import session_factory

            async with session_factory() as session:
                book = await load_book(session)
                decisions = await last_decisions(session)
                states = await lifecycle_states(session)

            probes: dict[str, dict] = {}
            for sid in map(str, self._sibling_strategies):
                sleeve = book.sleeves.get(sid)
                entry: dict = {}
                # LIFECYCLE from the table the operator writes (#638 seam). The previous read asked
                # `last_decisions` for a key it has never returned — always None, always TRADING —
                # so a DISABLED row reached nothing and the short-circuit was unreachable from
                # production. An absent row is TRADING (Operator, 2026-08-19), stated here, not hidden
                # in a falsy `or` over a value that could legitimately be empty.
                state = states.get(sid)
                entry["lifecycle"] = state if state is not None else "TRADING"
                if sleeve is not None:
                    # THE ONE SIZING PREDICATE (#648) — this was the third copy of the falsy-target
                    # escape; a wound-down lane's platform budget probe read its full actual.
                    budget = min(float(sleeve.actual), float(sleeve.target))
                    entry["budget"] = budget
                    # SLOT SIZE as sizing computes it: the sleeve's deployable share of one position.
                    entry["slot_size"] = budget
                probes[sid] = entry
            self._platform_probes = probes
        except Exception as exc:  # noqa: BLE001 — a probe refresh must never stop the node
            self.log.warning(f"platform probes unavailable ({exc!r}) — the boot gate will report them missing")

    def _publish_account(self) -> None:
        """Account plane (#41): equity (net-liq) / cash / buying_power / multiplier for %-of-equity sizing
        + affordability in the order ticket. PREFERS the broker's own Alpaca fields (off the bus) — a local
        cash+positions estimate diverges (margin, pending orders, marks). Falls back to the Nautilus
        Portfolio only for a non-Alpaca/synthetic node with no broker snapshot."""
        broker = self._broker_account
        if broker is not None:
            self._publish(
                "account",
                {
                    "equity": broker["equity"],
                    "cash": broker["cash"],
                    "buying_power": broker["buying_power"],
                    "multiplier": broker.get("multiplier", 1.0),
                    "long_market_value": broker.get("long_market_value", 0.0),
                    # NET = realized + Δunrealized (#596). PASSED THROUGH EXPLICITLY, because a
                    # field the engine publishes and this frame omits is the fourth instance of
                    # that shape here (#233/#322/#336). `.get(..., None)` and not `or 0.0`: unknown
                    # must stay unknown all the way to the panel, which renders it as an em dash.
                    "unrealized_standing_total": broker.get("unrealized_standing_total"),
                    "unrealized_intraday_total": broker.get("unrealized_intraday_total"),
                    # Previous session's close, so the UI can derive NET for 1D (#336). Passed through as
                    # None when the broker did not report it — the tile renders "unknown" rather than
                    # treating a missing prior close as zero, which would print the whole equity as the
                    # day's move.
                    "last_equity": broker.get("last_equity"),
                    # Passed through when the adapter reports one. Alpaca's frames are USD and do not
                    # carry it, so this is usually None — and None is the honest answer rather than a
                    # confident "USD" that would be wrong the moment a non-USD adapter takes this
                    # branch.
                    "currency": broker.get("currency"),
                    "ts": broker.get("ts", self.clock.timestamp_ns()),
                },
            )
            # THE BOOT GATE RUNS ON THIS BRANCH TOO, AND FOR MOST OF ITS LIFE IT DID NOT (#515).
            #
            # This branch ends in a `return`, and the gate's other call site is ~120 lines below it.
            # On an Alpaca node the exec client publishes `broker.account`, so `_broker_account` is
            # populated and EVERY tick returns here — the gate was wired into the FALLBACK branch and
            # test-alpaca, the tenant that does most of the trading, never ran preflight at boot.
            # staging-ibkr ran it only because nothing on an IBKR node publishes that topic.
            #
            # `test_boot_gate_is_wired.py` was green throughout. It asserts via AST that the gate has
            # a caller reachable from `_publish_account`, and all of that is true — an AST test walks
            # the syntax tree and cannot see a `return`. The gate existed, was enabled, was
            # mutation-bitten, and had a passing test named `..._IS_WIRED`; none of that was connected
            # to the branch that mattered.
            #
            # TWO CALL SITES CANNOT BOTH RUN IN ONE INVOCATION — the branches are exclusive, and this
            # one returns immediately below.
            #
            # NOT "once per node", which is what this comment said until codex read it (2026-08-25).
            # `17f5c41` made a DEGRADED verdict RETRYABLE, so the accurate rule is ONCE AFTER CLEAN,
            # RETRY WHILE DEGRADED: `should_run` blocks re-probing only when `state.ran and not
            # state.degraded`. A comment that reads as a safety property and is wrong survives review
            # by sounding careful, which is why this one is spelled out rather than trimmed.
            self._run_boot_gate_once(broker.get("equity"))
            return
        # Fallback (synthetic/non-Alpaca): derive from the Nautilus cache. Cash is the safe floor; equity
        # adds priced positions where the portfolio can (may understate on a venue mismatch).
        accounts = self.cache.accounts()
        if not accounts:
            return
        account = accounts[0]
        # THE SAME CURRENCY RULE AS EQUITY BELOW, and it has to be here rather than there: this read
        # ran FIRST and returned silently, so on the SGD-only IB account `_publish_account` never
        # reached the equity logic at all. The IBKR tests passed against a double that answered any
        # currency, while production returned three lines earlier — the defect and the reason it was
        # invisible, in the same place.
        cash, cash_ccy = _single_currency_amount_and_ccy(account.balances_free(), prefer=USD)
        if cash is None:  # no USD leg and no unambiguous single leg — #382 refuses below anyway
            return
        equity: float | None = None
        issuer = str(account.id.get_issuer())

        if issuer in _TOTAL_IS_NET_LIQUIDATION:
            # THE VENUE-KEYED LOOKUP CANNOT SUCCEED ON IBKR, EVER. The account is registered under
            # venue INTERACTIVE_BROKERS while every instrument carries XNYS/XNAS/ARCX from the Alpaca
            # data feed, so `portfolio.equity(venue)` finds no account for as long as the node runs.
            # On Alpaca the two coincide, which is why this never surfaced.
            #
            # NET LIQUIDATION IS ALREADY THERE, natively: the IB adapter maps IB's own
            # `NetLiquidation` tag onto `AccountBalance.total` (interactive_brokers/execution.py:1668),
            # so the number is READ, not derived — no cash+marks sum of ours to be wrong.
            #
            # AND NOT VIA `Portfolio.equity`, either keyed by venue or by `account_id`: for a MARGIN
            # account it adds unrealized PnL to `balance.total`, and IB's NetLiquidation ALREADY
            # includes it, so it double-counts the book (codex, 2026-08-24).
            #
            # WHATEVER CURRENCY THE ACCOUNT IS IN. The live IB paper account reports a single SGD leg
            # (`base_currency=None`, `total=999_216.15 SGD`) while every instrument is a USD equity, so
            # a USD-only read finds nothing and publishes nothing — the same silence, one layer down.
            # Publishing the account's own figure is safe HERE and not a general licence: sizing does
            # not use it (`pgrunner` takes `limits.allocated_equity`, set per session from the
            # strategy's settings target), and the daily-loss halt compares this number against ITSELF
            # at session start, so a consistent unit cancels out of the ratio.
            try:
                totals = account.balances_total()
                equity = _single_currency_amount(totals, prefer=USD)
                if equity is not None and USD not in totals:
                    only_ccy = next(iter(totals))
                    if not self._foreign_equity_warned:
                        self._foreign_equity_warned = True
                        self.log.warning(
                            f"account: equity is reported in {only_ccy} and published unconverted — "
                            f"instruments are USD, so this figure is NOT dollars. Sizing is unaffected "
                            f"(it uses the strategy's allocated_equity) and the daily-loss ratio "
                            f"cancels the unit, but any consumer treating it as USD is wrong.")
                # More than one leg and no USD: ambiguous, and a guess here is a wrong number on the
                # channel the daily-loss halt anchors on. Stay None; #382 refuses below.
            except Exception as exc:  # noqa: BLE001 — no balances yet; #382 still applies below
                _log.debug("net liquidation unavailable: %s", exc)
        # NO `Portfolio.equity` FALLBACK FOR ANYTHING ELSE, AND THAT IS THE POINT OF THIS CHANGE.
        #
        # It reads `balance.total + Σ unrealized_pnl(open positions)` for a MARGIN account. That is
        # correct only where `total` is NET LIQUIDATION. Cockpit's Alpaca client sets
        # `AccountBalance(total=cash, locked=0, free=cash)` (providers/alpaca/exec_client.py:423-425),
        # so on the live paper account it computes:
        #
        #     cash 73,393.33 + unrealized 1,154.73 = 74,548.06   against a true equity of 103,500.35
        #                                                        -> 28.0% LOW, whenever anything is held
        #
        # That is the number #382 removed, arriving by a different route, on the channel #380
        # established the DAILY-LOSS HALT anchors on — a plausible trigger to halt every strategy while
        # nothing is wrong. It is wrong whether or not the book can be priced: unpriced it returns cash
        # exactly, priced it returns cash plus PnL, and neither is equity.
        #
        # It is not right for IB either, by venue OR by account_id: IB's NetLiquidation ALREADY contains
        # the unrealized PnL this would add, so it double-counts the book (codex, 2026-08-24).
        #
        # So there is no adapter we run where this call yields equity, and the honest fallback is the
        # #382 refusal below. `float(eq)` on its dict return had made this branch dead since it was
        # written; making it merely CORRECT would have made a 28% understatement live.

        if equity is None:
            # CASH IS NOT EQUITY WHILE ANYTHING IS HELD (#382).
            #
            # This line used to read `equity = cash`, and the health monitor duly announced
            # "eq $77,480.59" against a real equity of $103,229.30 — a phantom $25,748 loss, 25% of the
            # book, reported as a CHANGE event. The account's own contract says why it is wrong:
            # AccountDTO documents `cash + long_market_value == equity`, and this branch publishes cash
            # with no market value at all. It satisfies the arithmetic by leaving out the term that
            # makes it false.
            #
            # The window is a restart, before the broker snapshot arrives, and it self-corrects within
            # seconds. That is exactly what makes it dangerous: it fires on EVERY deploy, and an alarm
            # that cries a 25% loss on every deploy is one an operator learns to scroll past — so the
            # time it is real, it looks the same as the noise.
            #
            # Flat, cash IS equity and there is no estimate involved. Holding anything, the only honest
            # answers are the true figure or none, and none is what #343 and #370 already do elsewhere:
            # unknown is an answer, and a number known to be wrong must not render as a number.
            if self.cache.positions_open():
                if not self._equity_estimate_warned:
                    self._equity_estimate_warned = True
                    self.log.warning(
                        "account: portfolio equity unavailable while positions are held — publishing no "
                        "account frame rather than reporting cash as equity (#382). Cash is "
                        f"{cash:.2f}; the market value of the book is missing, not zero."
                    )
                return
            equity = cash

        self._equity_estimate_warned = False
        # THE BOOT GATE (#440), and this is its only caller — it had none until 2026-08-23, so
        # "preflight runs at boot" was untrue for as long as it was claimed.
        #
        # HERE because preflight needs EQUITY, and equity does not exist at registration: the account
        # snapshot arrives afterwards. This is the first moment the questions can be answered, it is
        # still "shortly after boot", and it is hours before any lane decides — QC345 decides at 09:35
        # and this frame lands at connection.
        #
        # `should_run` makes it fire ONCE and refuses to spend that run on an equity-less snapshot: on a
        # restart holding positions the account frame is withheld rather than reporting cash as equity
        # (#382), and probing then would fail every lane for a reason about the BROKER.
        #
        # REPORTS, never refuses. A gate that can stop a lane trading on its own judgement is a bigger
        # risk than the one it guards against — it journals and the operator decides.
        self._run_boot_gate_once(equity)
        # THE STRATEGIES READ THIS OFF THE BUS, NOT OUT OF THE UI PLANE. `_publish` enqueues to the
        # Redis writer; the rotation lanes take `_broker_account` from the `broker.account` MSGBUS
        # topic, whose only publisher is the Alpaca exec client. On an IBKR node nothing publishes it,
        # so `broker_equity()` raises and the session dies before forming an order — measured on
        # staging 2026-08-24, where `GET /account` served a real equity while the boot gate reported
        # both lanes DEGRADED with "'NoneType' object has no attribute 'equity'".
        #
        # `_DERIVED` MARKS IT AS OURS so `_on_broker_account` refuses it. Without that the next tick
        # reads our own snapshot back, takes the cached branch, and never recomputes: equity would
        # freeze at its first value and the daily-loss halt would anchor on a number from boot.
        # THE Δ HALF OF NET, ON A NODE WITH NO BROKER PUBLISHER (#596). The Alpaca client sums its
        # own per-position fields; nothing does that here, so staging published null for both and
        # every NET period rendered an em dash. `intraday` stays None deliberately — IBKR reports no
        # day figure and borrowing Alpaca's semantics is the #573 mistake in another costume.
        standing = _standing_unrealized_total(
            getattr(self, "cache", None), getattr(self, "portfolio", None),
        )
        # ONE BUILDER FOR BOTH FRAMES (#591): the msgbus snapshot and the UI frame carried separately
        # maintained literals, and the lmv 0.0 written for a flat book survived into the held-book
        # path — staging served equity 999,200.54 / cash 973,807.42 / lmv 0.00 over a held book,
        # violating the DTO's own `cash + lmv == equity` by 25,393.12.
        frame = derived_account_frame(equity=equity, cash=cash, cash_ccy=cash_ccy,
                                      standing=standing, ts=self.clock.timestamp_ns())
        self.msgbus.publish(_ACCOUNT_TOPIC, {**frame, _DERIVED: True})
        self._publish("account", frame)

    def _positions_frame(self, positions, *, now_ns: int) -> dict:
        """The positions frame, WITH the timestamp the inert watchdog requires (#644).

        The frame used to be `{"positions": [...]}` — no `ts` — while `_writer_is_publishing`
        requires a fresh positive `ts` and treats absence as "not publishing". So every healthy node
        reported ENGINE INERT at ERROR every 15s from 180s after boot, overwriting the real health
        frame forever: the detector built to make an inert node visible made itself the noise, and
        the day reconciliation genuinely refuses, the alarm carries no information.

        Its own function so the watchdog test can drive the REAL frame through the REAL probe. The
        test that let this ship asserted only that the probe's source contained the string "ts".
        """
        return {"positions": positions, "ts": int(now_ns)}

    def _publish(self, kind: str, payload: dict, *, backfill: bool = False) -> None:
        """Enqueue for the writer thread.

        LIVE frames never wait: the trading loop must not stall on Redis backpressure, and a dropped
        live tick is superseded by the next one a second later.

        BACKFILL frames do wait, briefly. They are not on the trading path, and dropping them is not
        self-correcting — a discarded history bar never comes again, it just leaves a hole the UI
        renders as a flat line or a confident 0.00%. If the writer is genuinely stuck, blocking is
        suspended for a cooldown so a dead Redis cannot turn a 127k-frame backfill into an hours-long
        startup.
        """
        blocking = backfill and (time.monotonic() - self._backfill_stalled_at > _BACKFILL_STALL_COOLDOWN_S)
        try:
            if blocking:
                self._queue.put((kind, json.dumps(payload)), timeout=_BACKFILL_PUT_TIMEOUT_S)
            else:
                self._queue.put_nowait((kind, json.dumps(payload)))
        except queue.Full:
            if blocking:
                # Waited and still no room — the writer is not draining. Stop waiting for a while.
                self._backfill_stalled_at = time.monotonic()
            # One line per dropped frame produced 98,352 log lines in a single startup — the 180-day
            # backfill fills the queue far faster than the writer drains it. That buried every other
            # line in the log and made the drop COUNT, which is the part that matters, unreadable.
            # Summarise on an interval instead: same information, bounded volume, and it now says how
            # much of the backfill the UI never received rather than repeating that one frame went.
            self._dropped[kind] = self._dropped.get(kind, 0) + 1
            now = time.monotonic()
            if now - self._dropped_at >= _DROP_REPORT_INTERVAL_S:
                total = sum(self._dropped.values())
                detail = ", ".join(f"{n} {k}" for k, n in sorted(self._dropped.items()))
                _log.warning("ui bridge queue full — dropped %d frames in the last %.0fs (%s)",
                             total, now - self._dropped_at, detail)
                self._dropped.clear()
                self._dropped_at = now

    def _drain(self) -> None:
        """Writer thread: pull frames off the queue and XADD them. All Redis latency lives here."""
        while not self._stopping.is_set():
            try:
                kind, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._redis is None:
                continue
            try:
                if kind in STATE_KINDS:
                    # Latest-state → overwrite a key (no history, no stream flood, no bar eviction). TTL so a
                    # dead engine's keys expire instead of being read as live state.
                    self._redis.set(f"{STATE_KEY_PREFIX}{kind}", payload,
                                    ex=_STATE_KEY_TTL_OVERRIDES.get(kind, _STATE_KEY_TTL_SECS))
                elif kind in BAR_KINDS:
                    # Its own stream, its own cap — a quote can no longer push a bar out (#309).
                    self._redis.xadd(
                        self._bar_stream_key,
                        {"type": kind, "payload": payload},
                        maxlen=self._bar_maxlen,
                        approximate=True,
                    )
                else:
                    self._redis.xadd(
                        self._stream_key,
                        {"type": kind, "payload": payload},
                        maxlen=self._maxlen,
                        approximate=True,
                    )
            except redis.RedisError as exc:  # a UI-bus hiccup must never break trading
                _log.warning("ui bridge publish failed (%s): %s", kind, exc)


def durable_cache_enabled() -> bool:
    """Opt-in (default OFF). Off → the node runs with an in-memory cache exactly as before; the paper stack is
    unaffected until this is switched on. On → Nautilus persists orders/positions to Redis and reloads them on
    restart (#74). All new gates default False (three-overnight-bug rule)."""
    return os.environ.get("KUMO_DURABLE_CACHE", "").strip().lower() in ("1", "true", "yes")


def _reconcile_skip_coids() -> list:
    """Client order ids to EXCLUDE from startup reconciliation (#363).

    THE PROBLEM THIS SOLVES. Nautilus refuses to reconcile when a venue fill would overfill a cached
    order, and it is right to: guessing which side is correct could double a position. But the node then
    runs on indefinitely with an EMPTY BOOK, reports RUNNING, and every subsequent restart fails the
    same way. There is no path back that does not involve editing Redis by hand — which #363 describes
    a human doing at 23:00, and calls "not a procedure anyone should have to invent".

    2026-08-21, live: a 28-share fill was applied to an already-complete 136-share order
    (`order.filled_qty=136, fill.last_qty=28, would result in 164`), reconciliation failed, and
    `/positions` returned 0 against 9 held at the broker. Both cached orders were verified CORRECT
    against Alpaca — 136 and 28, matching venue ids — so the recorded "delete the corrupt row" remedy
    did not apply. Nothing was corrupt; the attribution inside reconciliation was wrong.

    NAUTILUS ALREADY HAS THE LEVER. `LiveExecEngineConfig.filtered_client_order_ids` skips an order
    report AND its fills (`live/execution_engine.py:1889`). Reading the config object's fields found in
    one minute what an evening of cache archaeology did not — the CLAUDE.md rule, again.

    WHY AN ENV VAR AND NOT A CODE EDIT. The whole failure is that recovery required a deploy or a
    hand-edited cache while the book was invisible. An operator must be able to unblock a boot by
    naming the order the log already printed, and restart. Skipping an order's RECONCILIATION does not
    touch the position: `/v2/positions` still reconciles, so the book comes back whole.

    Comma-separated. Empty/unset → no filtering, which is the correct default: this is an escape hatch,
    not a routine setting, and a stale entry here would silently stop reconciling a live order.
    """
    from nautilus_trader.model.identifiers import ClientOrderId

    raw = (os.environ.get("KUMO_RECONCILE_SKIP_COIDS") or "").strip()
    if not raw:
        return []
    coids = [ClientOrderId(c.strip()) for c in raw.split(",") if c.strip()]
    _log.warning(
        "reconciliation is SKIPPING %d client order id(s) from KUMO_RECONCILE_SKIP_COIDS: %s — "
        "positions still reconcile from the broker; remove these once the underlying order is settled",
        len(coids), ", ".join(str(c) for c in coids),
    )
    return coids


def _durable_configs() -> tuple[CacheConfig | None, LiveExecEngineConfig | None]:
    """Native restart persistence (#74). Redis cache + exec-engine snapshots + reconciliation, so a node
    restart reloads its own orders/positions and re-anchors to the broker. Points at the SAME Redis the UI
    bridge uses (KUMO_REDIS_HOST) but Nautilus namespaces under `trader-{id}:*` (bus keys are `ui:*`); the
    caches never collide. NOTE: this reloads positions/orders, NOT the in-memory close-reopen position-snapshot
    archive #73 sums for cycle P&L — that archive is rehydrated separately from Nautilus's persisted
    `snapshots:positions` keys (the gap-fill). `snapshot_positions=True` is what persists those closed-leg
    states in the first place."""
    if not durable_cache_enabled():
        return None, None
    host = os.environ.get("KUMO_REDIS_HOST", "localhost")
    port = int(os.environ.get("KUMO_REDIS_PORT", "6379"))
    cache = CacheConfig(
        database=DatabaseConfig(type="redis", host=host, port=port),
        encoding="msgpack",
        flush_on_start=False,  # restart truth lives in Redis + the broker — never wipe it on boot
        use_trader_prefix=True,
        use_instance_id=False,  # stable keyspace across restarts (same trader_id → same keys)
    )
    exec_engine = LiveExecEngineConfig(
        load_cache=True,
        snapshot_orders=True,
        snapshot_positions=True,  # persists each closed leg's state → the source for the #73 P&L gap-fill
        reconciliation=True,
        # 7 days — bounded, broker is the hard anchor on restart.
        #
        # NOT SHORTENED, AND NOTHING IS PURGED (#613). A change to 1-day lookback plus a 48h purge was
        # built and reverted the same night: it was aimed at a "corrupted cached order", and the
        # corruption did not exist. Measured against Alpaca for the order that blocked the boot —
        # kumo-8752e6b52827e14e99f1 — the cache, the order endpoint and the fill activities ALL agree
        # at 85 (fills 14+40+7+24, cum_qty 85). Purging would have deleted a CORRECT order to stop
        # reconciliation tripping over it, trading away order history and the forensic trail to hide a
        # symptom. #363 reached the same conclusion on 2026-08-21: "nothing was corrupt; the
        # attribution inside reconciliation was wrong."
        # EXPLICIT, not the default (#807): the synthetic remainder a cache repair books relies on
        # Nautilus refusing a later venue fill that would overfill the same order (seen working at the
        # 07:14 boot on 2026-09-09: U and VEEV re-reports refused). Stated so an edit cannot flip it by omission.
        allow_overfills=False,
        reconciliation_lookback_mins=10080,
        # THE ESCAPE HATCH FOR A NODE THAT CANNOT BOOT (#363). See `_reconcile_skip_coids`.
        filtered_client_order_ids=_reconcile_skip_coids(),
        # CONTINUOUS reconciliation (not just at startup). Alpaca fills arrive via polling — there is no
        # trade-updates WebSocket yet — so without these the cockpit only reflects the broker at boot and
        # stalls until a restart (fills placed after startup never surface). These make the ExecEngine
        # periodically re-poll the exec client's report generators, so orders/fills/positions stay live:
        open_check_interval_secs=5.0,       # re-poll orders every 5s → fills/cancels surface within ~5s
        # HOW LONG A VENUE MAY BE UNREACHABLE BEFORE THE ENGINE DISOWNS ITS OWN RESTING ORDERS.
        # Nautilus defaults this to 5, and 5 x the 5s interval above is TWENTY-FIVE SECONDS — which
        # on 2026-09-04 was less than one Alpaca DNS outage: 33 live orders were rejected
        # ORDER_NOT_FOUND_AT_VENUE at 08:46:03Z while resting at the broker (#791). Nautilus's own
        # default for `open_check_interval_secs` is None — the check is OFF — so the 25s window was
        # one this repo opted into when it turned polling on for fast fill surfacing, and never
        # priced. 24 x 5s = two minutes.
        #
        # THE GUARD IS THE FIX, THIS IS THE MARGIN. `api/exec_read_guard.py` refuses to resolve
        # missing orders at all while a read is failing; this only decides how long a venue may be
        # unreachable before an order that is genuinely gone is finally resolved. Widening it alone
        # would have moved the 2026-09-04 outage rather than survived it.
        open_check_missing_retries=24,
        open_check_open_only=False,         # ALSO re-cache filled/closed orders so their fills can reconcile
                                            # into a position (else the "order not yet cached" defer-loop that
                                            # left held positions invisible)
        position_check_interval_secs=10.0,  # re-reconcile positions every 10s → net/held stays truthful
        # Keep the broker's real orders (incl. unclaimed/external) in the cache so the blotter mirrors the
        # broker's order history — else reconciliation drops them and only synthetic net orders remain, and
        # the Orders tab doesn't reflect what's actually at Alpaca. External orders are still classified into
        # the quarantine plane (#79); this only stops them being discarded.
        filter_unclaimed_external_orders=False,
        # Broker is the hard anchor: synthesize the order for a real broker fill we don't have cached (a
        # lost-cache restart) rather than leave the position INVISIBLE. The drift banner (#26) surfaces any
        # residual mismatch for a human, so this is safe for a manual cockpit — an unseen held position is
        # far more dangerous than a reconstructed order that mirrors a real broker execution.
        generate_missing_orders=True,
    )
    return cache, exec_engine


#: Lanes whose build was SKIPPED because their data dependency was unreachable — {name: reason}.
#:
#: MODULE STATE BECAUSE THE BUILD IS MODULE-LEVEL. `build_node` calls the builders once, before any
#: node object exists to hang this on, and health needs it afterwards. Cleared explicitly rather than
#: on each build so a caller that rebuilds one lane does not erase another's absence.
_SKIPPED_BUILDS: dict[str, str] = {}


def skipped_builds() -> dict[str, str]:
    """{strategy: reason} for lanes that failed to build. Empty on a healthy node."""
    return dict(_SKIPPED_BUILDS)


def clear_skipped_builds() -> None:
    """Forget recorded skips. For tests, and for a deliberate full rebuild."""
    _SKIPPED_BUILDS.clear()


def build_optional_strategy(name: str, builder):
    """Build a strategy that must not be able to take the NODE down with it (#377, QC345 2026-08-19).

    THE TENSION THIS RESOLVES. `build_qc345_strategy` raises rather than returning None when its gate is
    on but its wiring is incomplete, and that is right: "a strategy that is switched on and silently
    absent is the worst outcome available" — the operator sees the flag set, the node boots clean, and
    nothing trades or says why.

    But that rule is about MISCONFIGURATION, and it was also catching TRANSIENT FAILURE. On 2026-08-19
    enabling QC345 crash-looped the entire node: `_instrument_ids` resolves each symbol's exchange via
    `TradableUniverse().exchanges()`, which fetches Alpaca's whole ~13k asset list over HTTP inside
    `build_node`. The fetch timed out, the RuntimeError propagated, and MANUAL/MOMENTUM/BCTROT — none of
    which had anything to do with it — never registered either. One slow HTTP call took the book offline.

    So the two are separated by TYPE, not by guesswork:

      OSError family    the network. `urllib.error.URLError`, `TimeoutError` and `socket.timeout` are all
                        OSError subclasses, so this catches the transport and nothing else. The strategy
                        does not register, the failure is logged at ERROR, and the node comes up with
                        every other strategy intact.

      everything else   configuration, wiring, a missing universe. Propagates, exactly as before, because
                        a node that boots without a strategy its operator switched on is the failure the
                        original rule exists to prevent.

    LOUD, NOT SILENT. The skip is an ERROR log naming the strategy and the cause. "Degraded and saying so"
    is the outcome being bought here; "quietly absent" is still not acceptable, and a caller that wants to
    know can see the None.
    """
    try:
        strategy = builder()
    except OSError as exc:
        _log.error(
            "%s did not build — its data dependency is unreachable (%s: %s). The node is starting "
            "WITHOUT it; every other strategy is unaffected. Retry by restarting once the dependency "
            "is back.",
            name, type(exc).__name__, exc,
        )
        # RECORDED, NOT ONLY LOGGED (#539). This is the one place that knows the lane is absent, and
        # it used to throw the fact away after printing it. Health then counted `len(siblings)` as
        # "registered", so a lane that never existed could not be counted as missing and the node
        # reported `3/3, ok` with QC345-003 gone for an entire session.
        _SKIPPED_BUILDS[name] = f"{type(exc).__name__}: {exc}"
        return None
    # A lane that builds is no longer absent — clears a stale entry from an earlier attempt.
    _SKIPPED_BUILDS.pop(name, None)
    return strategy


#: Credentials that belong to exactly one execution provider. A node trading a DIFFERENT venue has no
#: use for these, and holding one is not neutral — see `refuse_foreign_credentials`.
_PROVIDER_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "alpaca": ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
}


def refuse_foreign_credentials(exec_provider: str, env: dict) -> None:
    """Refuse to boot holding a credential for a venue this instance does not trade (#756).

    A CREDENTIAL THAT IS PRESENT IS A CREDENTIAL THAT WILL BE USED, and that is measured rather than
    asserted: the moment staging-ibkr had an Alpaca key, the realized-P&L sweep polled an Alpaca
    brokerage account holding none of its positions — 45 calls against 12 for market data (#573).
    Nothing selected that behaviour. Code that gates on "do we have a client" simply found one.

    THE EXISTING PROTECTION DOES NOT COVER THE WAY CONTAINERS ACTUALLY GET STARTED. The instances
    repo's secret resolver unsets every known secret and supplies only what an instance's manifest
    names — but it runs inside `make up` alone, and `compose.paper.yml` interpolates these BARE
    (`${APCA_API_KEY_ID}`, four places) rather than with a `:-` default. So a `docker compose up` from
    a shell that carries the keys hands an IBKR instance an Alpaca credential. That bypass was
    performed on 2026-08-31; aimed one project over, it would have booted staging holding the key.

    EMPTY IS ABSENT, and this is the half that decides whether the guard survives contact. Compose
    interpolates an UNSET variable to the EMPTY STRING, so `APCA_API_KEY_ID=""` is exactly what a
    correctly-configured IBKR instance looks like from inside its own container — measured: staging
    reports len=1 for both, the empty value plus a newline. A guard that read that as "present" would
    refuse every healthy IBKR boot, and a guard that fails healthy boots gets deleted rather than
    fixed.

    RAISES, NEVER WARNS. Where a value cannot be established this codebase refuses and says which
    input was wrong. A warning here is a line in a log nobody reads, on a stack whose notifications
    may not even be armed — and the failure it precedes is silent by construction.
    """
    wanted = str(exec_provider or "none").strip().lower()
    for provider, names in _PROVIDER_CREDENTIALS.items():
        if provider == wanted:
            continue  # its own credential, on the instance that trades there
        present = [n for n in names if str(env.get(n) or "").strip()]
        if present:
            raise RuntimeError(
                f"this node trades {wanted!r} but was handed a {provider.upper()} credential: "
                f"{', '.join(present)}. A credential that is present is a credential that WILL be "
                f"used — when staging-ibkr last held one, the realized sweep polled an Alpaca "
                f"account holding none of its positions (#573). Unset it, or deploy through the "
                f"instances repo, whose secret resolver supplies only what this instance declares"
            )


def _logging_config() -> LoggingConfig:
    """Durable, rotating log files — NAUTILUS'S OWN, not a hand-rolled writer (#758).

    CHECK NAUTILUS FIRST. `LoggingConfig` already carries `log_directory`, `log_file_name`,
    `log_file_format`, `log_file_max_size` and `log_file_max_backup_count`. Every one of them was
    UNSET, so the engine wrote no file at all: everything went to stdout, Docker's json-file driver
    captured it with `Config: {}` — no max-size, no max-file — and `make up` recreated the container
    and destroyed it. That is why Friday's session could not be examined on Monday, and it is why an
    evening went into reconstructing one symbol's history by counting grep matches.

    I was one step from writing a rotating writer by hand. That would have been the fourth time this
    repo broke its own rule.

    SIZE-BASED, NOT AGE-BASED — AND THAT IS A REAL LIMIT, NOT A DETAIL. Nautilus rotates when a file
    reaches `log_file_max_size` and keeps `log_file_max_backup_count` of them. It has no notion of
    days. "Keep 10 days" is therefore APPROXIMATED by sizing, and a quiet fortnight keeps more than
    ten days while a loud afternoon keeps fewer. If genuine age-based retention is ever required it
    needs a reaper, and this docstring is where that decision should be recorded rather than assumed.

    MEASURED 2026-08-31 on paper: 1,805,706 bytes over 13 minutes, 11,180 lines, 161 bytes/line —
    boot and backfill, which is the loudest the engine gets. The defaults below give roughly a fifty-
    fold headroom on that rate before the oldest file is dropped.

    JSON, because these files exist to be QUERIED. The whole ticket is that the information was
    present and only reachable by grep.
    """
    directory = os.environ.get("KUMO_LOG_DIR") or None
    return LoggingConfig(
        log_level=os.environ.get("KUMO_LOG_LEVEL", "WARNING").upper(),
        # None leaves file logging OFF, which is the current behaviour — so an instance that has not
        # mounted a log volume is unchanged rather than writing into a container-local path that
        # silently disappears on recreate.
        log_directory=directory,
        log_file_name=os.environ.get("KUMO_LOG_FILE", "engine") if directory else None,
        log_file_format="JSON" if directory else None,
        # The file gets its own level: stdout stays terse for a human watching, while the durable
        # record keeps what an investigation needs. INFO is where the inferred fills live.
        log_level_file=os.environ.get("KUMO_LOG_LEVEL_FILE", "INFO").upper() if directory else None,
        log_file_max_size=int(os.environ.get("KUMO_LOG_MAX_BYTES", 200 * 1024 * 1024)),
        log_file_max_backup_count=int(os.environ.get("KUMO_LOG_BACKUPS", 10)),
        # PER-COMPONENT LEVELS — Nautilus's own field, and it was unset. Lets one subsystem run at
        # DEBUG or TRACE while the rest stays at INFO, which is the difference between pinpointing a
        # defect and paying for every component's verbosity to see one of them.
        #
        # Boot-time only: Nautilus's `Logger` exposes no level setter and `init_logging` runs once,
        # so this needs a restart to change. Measured, not assumed — see the test.
        log_component_levels=_component_levels(),
    )


def _component_levels() -> dict:
    """Per-component log levels from `KUMO_LOG_COMPONENTS`, e.g. "ExecEngine=DEBUG,MOMENTUM=TRACE".

    A STRING, not a settings domain, because this has to be settable on an instance that will not
    boot far enough to read settings — which is exactly when a component needs turning up.

    A malformed entry is SKIPPED AND NAMED rather than failing the boot or silently applying half the
    map. Losing the engine to a logging typo is a worse outcome than losing the verbosity, and a
    half-applied map that looks complete is the shape this repo keeps paying for.
    """
    raw = (os.environ.get("KUMO_LOG_COMPONENTS") or "").strip()
    if not raw:
        return {}
    valid = {"OFF", "TRACE", "DEBUG", "INFO", "WARNING", "ERROR"}
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, level = part.partition("=")
        name, level = name.strip(), level.strip().upper()
        if not name or level not in valid:
            print(f"[kumo] ignoring malformed KUMO_LOG_COMPONENTS entry {part!r}; "
                  f"levels are {sorted(valid)}", flush=True)
            continue
        out[name] = level
    return out


def _attach_shortable_plane(feed, spec) -> None:
    """Shortability, where the CONNECTOR declares it (#857): `DataClientSpec.shortable_plane` is a
    factory the venue's own module supplies, so the engine never names a broker here
    (test_import_boundary). The factory runs after `node.build()` — the IB client it wraps is cached
    by then and the reader thread that dispatches to the wrapper does not exist until connect. A
    factory that returns None has already said why at ERROR; nothing is attached and the CRSISHORT
    builder refuses for want of a provider — loud, not inert."""
    factory = getattr(spec, "shortable_plane", None)
    if factory is None:
        return
    plane = factory(lambda data_type, data: feed.publish_data(data_type, data),
                    lambda: feed.clock.timestamp_ns())
    if plane is None:
        return
    feed.attach_shortable(plane)


def _timeout_s(var: str, default: str) -> float:
    """A node timeout from the environment, in seconds (#954). ONE helper for every timeout the node
    reads, so the two cannot drift. Blank or unset is "never told us" — compose interpolates an unset
    variable to the EMPTY STRING (#581) and a shell can hand us `KUMO_X=` — and yields the default.
    Anything else that is not a FINITE positive number REFUSES at boot NAMING THE VARIABLE the
    operator set. Nothing downstream would: Nautilus's `timeout_*` fields are plain `float` and
    accept 0.0, -5.0, nan and inf (measured on the installed package, impl review). `nan` is the
    spelling that matters — `nan <= 0` is False, so a sign check alone lets it through this gate AND
    the boot budget (`nan >= 330` is False too): the `nan <= 0`-disarms-a-halt class, at the config
    layer. `float()` also accepts Python's numeric underscores (`"1_0"` → 10.0) and exponents
    (`"1e-9"`) — the size floor lives in `_boot_budget_or_refuse`, not here. The way out is in the
    message, because a refusing engine crash-loops under `restart: unless-stopped` and the inert
    watchdog never gets to run."""
    import math

    raw = os.environ.get(var)
    text = default if raw is None or not raw.strip() else raw.strip()
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{var}={raw!r} is not a number of seconds — unset it to use the default "
                         f"({default} s) or set a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{var}={raw!r} must be a finite positive number of seconds — unset it to use "
                         f"the default ({default} s)")
    return value


#: Nautilus's own `timeout_connection` default: no configured value may go BELOW it (#954 floor).
#: THE FLOOR PERMITS THE VALUE THAT CAUSED THE TICKET (l21, #977 review): the boot that failed took
#: ~66 s and 60 was the default that failed it. 60 is the floor because it is the vendor's number and
#: catches nonsense (0.5, 1e-9); it does not make 60 safe. The default is 180 and nobody has to touch it.
_CONNECTION_TIMEOUT_FLOOR_S = 60.0


def _boot_budget_or_refuse(*, connection: float, reconciliation: float, portfolio: float) -> float:
    """The boot budget — connect, then reconciliation, then portfolio init, SERIALLY (Nautilus
    `kernel.start_async`) before any strategy can publish — must fit inside the #613 inert watchdog,
    which alarms `_INERT_AFTER_SECS` after start and cannot tell a slow boot from a dead node. The
    code-level pin lives in `test_connection_timeout.py`; this is the RUNTIME half: an instance that
    sets `KUMO_CONNECTION_TIMEOUT_S=900` would arm a false ENGINE INERT on every healthy boot, and a
    node that boots into that is a fallback that reads as health. Refuse, naming the inputs."""
    # FLOOR (l21, #977 review): a connect timeout BELOW the real connect time is #954 itself, arriving
    # through the knob that was added to fix it — `KUMO_CONNECTION_TIMEOUT_S=0.5` is finite, positive
    # and inside the budget, and it makes every boot time out. Below Nautilus's own 60 s default is
    # refused by name; the measured clean boot on staging2 was 50.5 s.
    if connection < _CONNECTION_TIMEOUT_FLOOR_S:
        raise ValueError(
            f"KUMO_CONNECTION_TIMEOUT_S={connection:g} is below the floor {_CONNECTION_TIMEOUT_FLOOR_S:g} s "
            f"(Nautilus's default; staging2's clean boot took 50.5 s) — a connect timeout shorter than the "
            f"connect is the #954 failure with a knob on it")
    budget = connection + reconciliation + portfolio
    if budget >= _INERT_AFTER_SECS:
        raise ValueError(
            f"boot budget {budget:.0f} s (KUMO_CONNECTION_TIMEOUT_S={connection:.0f} + "
            f"KUMO_RECONCILIATION_TIMEOUT_S={reconciliation:.0f} + portfolio {portfolio:.0f}) is not below "
            f"the inert watchdog ({_INERT_AFTER_SECS:.0f} s) — lower the timeouts, or raise "
            f"_INERT_AFTER_SECS with its reasoning (#954)")
    return budget


def build_node(cfg: FeedConfig | None = None) -> TradingNode:
    """Build the engine node from config (does not run it). No msgbus-streaming config — the UI feed is
    the in-node Redis bridge above."""
    cfg = cfg or load_feed_config()
    # BEFORE ANYTHING ELSE IS BUILT. This is the one seam every boot crosses regardless of how the
    # container was started, which is the entire point — the protection it backstops lives in a
    # script that only `make up` runs.
    refuse_foreign_credentials(cfg.exec_provider, dict(os.environ))
    if not cfg.ui_bridge.get("enabled"):
        raise RuntimeError("[ui_bridge].enabled is false — the engine node needs the UI bridge on")

    # START FROM A CLEAN SLATE (#539). `_SKIPPED_BUILDS` records lanes whose build failed, and a
    # successful build clears its own entry — but a lane REMOVED from the config never builds again
    # and would carry its absence forever, reporting a lane that is not supposed to exist as missing.
    # An alarm about a lane nobody wants is one an operator learns to ignore.
    clear_skipped_builds()

    provider_config = cfg.provider_config
    if cfg.data_provider == "alpaca":
        # Precedence: feed.toml (operator default) < alpaca settings override (UI). Only override when the
        # user EXPLICITLY set the feed in the settings file — so an empty/missing/broken settings file leaves
        # the operator's feed.toml value intact (never silently clobbers it with the schema default).
        from api.settings import get_override

        feed_override = get_override("alpaca", "feed")
        if feed_override is not None:
            provider_config = {**provider_config, "feed": feed_override}
            # UiFeedStrategy reads its OWN feed off `cfg.provider_config` (on_start, for the Today's
            # Range snapshot fetch) — it must see this SAME effective config, not the
            # original feed.toml value, or the budget/data-fetch feed can diverge from what the actual
            # Alpaca WS client connects on (codex review: an explicit "iex" override while feed.toml says
            # "sip" would otherwise leave the strategy budgeting as unlimited while the socket is capped,
            # reopening the whole-stream 405 this change exists to prevent).
            #
            # THE BUDGET NO LONGER COMES FROM THIS FIELD (#619) — it is built into the DataClientSpec
            # below, from this same overridden `provider_config`. That is why the override must stay
            # ABOVE `build_data_client_spec`: one derivation of the feed instead of two.
            cfg = dataclasses.replace(cfg, provider_config=provider_config)
    spec = build_data_client_spec(cfg.data_provider, provider_config)
    exec_clients = {}
    exec_spec = None
    if cfg.exec_provider != "none":
        exec_spec = build_exec_client_spec(cfg.exec_provider, cfg.exec_config)
        exec_clients = {exec_spec.client_id: exec_spec.config}

    cache_config, durable_exec_engine = _durable_configs()
    # Continuous reconciliation must be ON regardless of durable persistence. Alpaca has no trade-updates
    # WebSocket here, so the ExecEngine has to RE-POLL orders/positions on a timer — otherwise the cockpit
    # only reflects the broker at startup and stalls (fills placed after boot never surface; the only refresh
    # is a restart). When durable persistence is on, `_durable_configs` already carries these same intervals
    # plus its snapshot/load fields; when it's off (the default) we still need a config with the intervals, so
    # fall back to a plain LiveExecEngineConfig instead of Nautilus's default (whose check intervals are None).
    exec_engine_config = durable_exec_engine or LiveExecEngineConfig(
        reconciliation=True,
        reconciliation_lookback_mins=1440,  # 1 day — cover today's orders/fills on a fresh (non-durable) boot
        open_check_interval_secs=5.0,       # re-poll orders every 5s → fills/cancels surface live
        # THE SAME MARGIN AS THE DURABLE CONFIG ABOVE, and it must be on BOTH. The first version of
        # #791 set it only there, so the fallback config — the one a non-durable boot actually uses —
        # kept Nautilus's default of 5 and the whole 25s window survived the fix. A change that
        # anchors on one of two constructions does nothing, quietly.
        open_check_missing_retries=24,
        open_check_open_only=False,         # ALSO re-cache filled/closed orders — else a filled order is never
                                            # cached and its fill can't reconcile into a position (the exact
                                            # "order not yet cached" defer-loop that hid held positions)
        position_check_interval_secs=10.0,  # re-reconcile positions every 10s → net/held stays truthful
        generate_missing_orders=True,       # broker is the hard anchor: synthesize the order for a real broker
                                            # fill we don't have cached (a lost-cache restart), rather than
                                            # leave the position invisible
    )
    node_config_kwargs: dict = dict(
        trader_id=cfg.trader_id,
        logging=_logging_config(),
        data_clients={spec.client_id: spec.config},
        exec_clients=exec_clients,
        exec_engine=exec_engine_config,  # always set — continuous reconciliation is not optional
        # STARTUP RECONCILIATION NEEDS LONGER THAN NAUTILUS'S 30s DEFAULT.
        #
        # On 2026-08-19 the node came up, spent the full 30 seconds in `Generating ExecutionMassStatus`,
        # and gave up: "Cannot reconcile execution state". Nautilus then runs on with an EMPTY BOOK — no
        # positions, no strategy trading — while reporting RUNNING. It is not a data conflict and there
        # is nothing to repair; the venue simply had not finished answering.
        #
        # The mass status is one Alpaca round trip per order in the lookback, and this account's order
        # count grows through a session, so the window that was comfortable at 09:30 is not at 16:00.
        # A timeout that scales with nothing is a timeout that expires on the busiest day.
        #
        # 120s, and the failure it prevents is asymmetric: reconciling slowly costs a slower boot, while
        # failing to reconcile costs every position and every strategy until someone notices. Overridable
        # for an operator who needs longer still, because the alternative to a knob here is a rebuild.
        timeout_reconciliation=_timeout_s("KUMO_RECONCILIATION_TIMEOUT_S", "120"),
        # THE CONNECT ITSELF HAS A TIMEOUT, AND IT WAS NAUTILUS'S 60 s DEFAULT UNTIL #954.
        #
        # staging2, 2026-09-11 07:18:13Z: STARTING, then sixty seconds later "Timed out (60.0s) waiting
        # for engines to connect and initialize", then RUNNING with NEITHER client connected; both
        # connected six seconds after that. Past this timeout Nautilus does not fail — `start_async`
        # returns without starting the trader and the node logs RUNNING anyway — so reconciliation
        # never ran, zero strategies started, and the node sat inert for 8 minutes pre-open. The
        # instrument provider qualifies ~250 contracts at boot (8 of them unresolvable, each waiting
        # out IB); the clean boot of that composition took 50.5 s, the failed one ~66 s.
        #
        # 180 s: at least twice the boot that missed by six seconds and three times the clean one — a
        # busy IB day is slower than the failed boot, not faster. Paper (Alpaca, no qualification)
        # connects in 2–3 s over 11 measured boots; the value is for the class. NOT a round number:
        # trimming it to 90 "because 60 was too low" re-runs the race on the next universe growth.
        # The pair (180, `_INERT_AFTER_SECS` 330) is one decision — see `_boot_budget_or_refuse`.
        timeout_connection=_timeout_s("KUMO_CONNECTION_TIMEOUT_S", "180"),
        # A RESTART MUST NOT BE A KILL (#381).
        #
        # Nautilus's default is 10s, and on 2026-08-20 a restart issued while the node was still in
        # startup produced exactly that:
        #
        #     [WARN] TradingNode: Timed out (10.0s) waiting for node to stop
        #     DataEngine.check_disconnected() == False
        #     ExecEngine.check_disconnected() == False
        #
        # Both engines still connected when the budget expired, so the node stopped being asked and was
        # torn down mid-flight instead. Startup here runs ~90 seconds — 346 instrument and bar requests
        # across MOMENTUM's 92, BCTROT's 90 and QC345's 164 — and every deploy passes through that
        # window with live positions held.
        #
        # 30s is not the startup time and is not meant to be: what has to fit is the DISCONNECT, which
        # is the engines closing their in-flight requests, not the requests completing. The asymmetry
        # decides the number — waiting a few seconds longer costs a slower deploy, while giving up early
        # tears down a node holding positions in an unknown state.
        #
        # This is HALF of the fix. `stop_grace_period` in compose.paper.yml is the other half and must
        # stay larger than the sum of the three timeouts below, or docker SIGKILLs the process partway
        # through the shutdown this budget was widened to allow. `test_shutdown_budget.py` pins that
        # relationship, because two numbers that must agree and live in different files will not.
        #
        # A CONSTANT, NOT A KNOB (#954). This used to read an environment variable that no compose
        # file forwarded — a knob nobody could turn, baselined as debt in #740. Forwarding it would
        # have been worse: `stop_grace_period` is a compose LITERAL, so a value an instance raised
        # through the environment would reintroduce #381 through a setting nobody would find. The
        # two numbers are pinned to each other in the test, and the environment is not consulted.
        timeout_disconnection=30.0,
    )
    # RUNTIME half of the boot-budget invariant (#954): the code-level half is in
    # `test_connection_timeout.py`, which pins connect + reconciliation + portfolio < `_INERT_AFTER_SECS`
    # on the literals above; this refuses an instance whose environment breaks it.
    _boot_budget_or_refuse(
        connection=node_config_kwargs["timeout_connection"],
        reconciliation=node_config_kwargs["timeout_reconciliation"],
        portfolio=float(TradingNodeConfig().timeout_portfolio),   # accepted Nautilus default, read not assumed
    )
    if cache_config is not None:
        node_config_kwargs["cache"] = cache_config
    node = TradingNode(config=TradingNodeConfig(**node_config_kwargs))
    node.add_data_client_factory(spec.client_id, spec.factory)
    if exec_spec is not None:
        node.add_exec_client_factory(exec_spec.client_id, exec_spec.factory)
    node.build()
    # api_key is Databento-specific (its window calc); IBKR and others have none.
    exec_client_id = ClientId(exec_spec.client_id) if exec_spec is not None else None
    feed = UiFeedStrategy(
        cfg,
        ClientId(spec.client_id),
        getattr(spec.config, "api_key", ""),
        exec_client_id=exec_client_id,
        # The print type the data client was BUILT with (#834), read off the same config object so
        # the health frame cannot disagree with the gateway. None for a provider that has no such
        # notion (Alpaca, Databento) — not "REALTIME", which would be a claim nobody made.
        market_data_type=market_data_type_name(getattr(spec.config, "market_data_type", None)),
        # The provider's OWN declared limit (#619). `spec` was already in hand here and its semantics
        # were thrown away, which is how an Alpaca plan constant came to gate an IBKR node.
        realtime_symbol_budget=spec.realtime_symbol_budget,
        historical_requests_per_minute=spec.historical_requests_per_minute,
        supplies_trading_calendar=spec.supplies_trading_calendar,
        # Which session the provider's daily bars aggregate over (#616) — REQUIRED on the spec, so
        # every node built here carries a real answer; only a hand-built double can leave it None.
        daily_bars_cover=spec.daily_bars_cover,
        # Whether this venue streams a TRADE TAPE (#612) — REQUIRED on the spec, because the
        # granularities Nautilus aggregates internally are built from exactly that.
        streams_trade_ticks=spec.streams_trade_ticks,
        # The quote plane's own declaration (#812) — forwarded beside the trade one, because a
        # required field nobody forwards is #581's shape.
        streams_quote_ticks=spec.streams_quote_ticks,
    )
    _attach_shortable_plane(feed, spec)
    # THE VENUE, READ THROUGH NAUTILUS RATHER THAN THROUGH ALPACA'S REST CLIENT.
    #
    # A `Strategy` has no standard handle to the execution client, so this is the one explicit wiring
    # that makes `generate_order_status_reports` reachable from the feed. `node.kernel` is set in
    # `TradingNode.__init__` (an instance attribute, not on the class), and the client exists only after
    # `node.build()` — which is why this sits here and not in the constructor.
    #
    # Why it is worth wiring: the exit and protection paths currently parse Alpaca's JSON by hand, a
    # SECOND time, beside the typed parse the provider already does. That duplicate parse is where the
    # broker coupling lives — 32 raw field reads — and the typed form is what the shipped Interactive
    # Brokers adapter produces too. `None` stays a legitimate value: callers treat an unreadable venue as
    # "assume nothing is safe", and a missing client must not become a confident empty list.
    #
    # RESOLVE THE CLIENT, NOT ITS ID. `ExecutionEngine.default_client` is typed `ClientId | None` in
    # Nautilus's own docstring — it returns the IDENTIFIER, and `ClientId` has exactly one public
    # member, `value`. Assigning it here SUCCEEDED, so the `try` never fired and the miswire was
    # invisible: every typed read then raised `AttributeError` inside the caller's own try, which
    # aborted the read BEFORE the `if reports is not None` fallback could run. The REST fallback was
    # unreachable code and the native cancel silently did nothing. Found by codex, not by our tests —
    # the double provided `generate_order_status_reports`, so it agreed with the caller instead of
    # with production. `_clients` is private because Nautilus exposes only ids publicly; there is no
    # public accessor for the instance.
    #
    # The capability check is the part that generalises. A handle that cannot answer the question is
    # refused and becomes `None` — which callers already treat as "assume nothing is safe" — rather
    # than being kept and failing at the call site, where the failure is indistinguishable from an
    # unreadable venue.
    try:
        _eng = node.kernel.exec_engine
        _cid = _eng.default_client
        if _cid is None:
            _registered = list(getattr(_eng, "registered_clients", []) or [])
            _cid = _registered[0] if len(_registered) == 1 else None
        _client = getattr(_eng, "_clients", {}).get(_cid) if _cid is not None else None
        if _client is not None and not hasattr(_client, "generate_order_status_reports"):
            _log.warning(
                "execution handle %r cannot generate order status reports — refusing it so venue "
                "reads report UNKNOWN rather than failing at the call site", type(_client).__name__)
            _client = None
        if _client is None:
            _log.warning("no execution client resolved — typed venue reads will fall back to REST")
        feed._exec_client = _client
        # A FAILED VENUE READ IS NOT AN EMPTY VENUE (#791). Installed here because this is where the
        # exec engine and its constructed clients are both in hand. On 2026-09-04 a twenty-second
        # Alpaca outage made `generate_order_status_reports` raise; Nautilus logged it, dropped it,
        # and handed `_handle_missing_orders_at_venue` an empty `venue_reported_ids` — which rejected
        # 33 orders that were resting at the broker the whole time.
        from api.exec_read_guard import install_venue_read_guard

        install_venue_read_guard(_eng, log=_log)
    except Exception as exc:  # noqa: BLE001 — a display strategy must not fail to start over this
        _log.warning("could not resolve the execution client for typed venue reads: %r", exc)
    if exec_spec is not None:
        # The provider's DECLARED venue fact (#641), never inferred from a name (#619): whether this
        # venue holds shares against resting orders decides whether the exit path waits on the
        # availability read at all. Without this assignment the spec field is the #574 shape —
        # declared, documented, consulted by nothing — and every venue is treated as reserving, which
        # is precisely the dead-exit state on staging-ibkr this exists to end. The class default
        # (True: wait and refuse) covers only a node with no exec provider.
        feed._exec_reserves_shares = exec_spec.reserves_shares_against_resting_stop
        # The venue's reference-asset ledger (#647), declared by the exec provider — None on a venue
        # that has none (IBKR). Consumed by qc345's delisting detection, which reports None as
        # "delisting detection OFF" at ERROR rather than treating absence as a clean ledger.
        feed._reference_assets = exec_spec.reference_assets
    # The IB client for instrument search (#837): the data engine's single client, if it is the IB
    # adapter (it carries `_client`, the connected InteractiveBrokersClient). Resolved lazily so a
    # client that connects after build still answers; a node without one refuses the command.
    def _ib_client():
        try:
            _deng = node.kernel.data_engine
            _dcid = _deng.default_client
            if _dcid is None:
                _regs = list(getattr(_deng, "registered_clients", []) or [])
                _dcid = _regs[0] if len(_regs) == 1 else None
            _dclient = getattr(_deng, "_clients", {}).get(_dcid) if _dcid is not None else None
            return getattr(_dclient, "_client", None)
        except Exception:  # noqa: BLE001 — a search must never take a boot down; the command refuses
            return None
    feed._ib_client_ref = _ib_client
    node.trader.add_strategy(feed)
    # MOMENTUM-001 (kumo-strategies). Off unless KUMO_MOMENTUM_ENABLED is set; when it is on, the
    # builder raises rather than returning None, because a strategy that is switched on and silently
    # absent looks exactly like one that is holding.
    # REGISTERED FIRST, DELIBERATELY (2026-09-11). Historical `request_bars` go through IB's
    # PACED queue at ~6/min in REGISTRATION order, so the LAST lane registered waits for every
    # earlier lane's universe before its own requests are issued. Measured tonight: ~515 requests
    # across six lanes = ~85 minutes, and SMHGLD — which needs TWO instruments — sat last and
    # could not warm before its rungs while 130-name lanes went first.
    # Cost of ordering it first is borne by lanes whose next slot is a different session.
    # The real fix is to order the queue by what each lane NEEDS (#1021); this is that fix's
    # cheapest possible form and it should be removed when #1021 lands.
    # SMHGLD-007 (kumo-strategies#177, cockpit#953/#965) — the fixed-weight SMH/GLD sleeve. Registered
    # ONLY through `build_smhgld_strategy`, which attaches the delta-executing gateway (#953); the
    # upstream adapter refuses at construction a runner that cannot execute deltas, so a strategy
    # that comes back here is one whose decisions can be acted on. `SMHGLD_ENABLED` (settings,
    # default false) is the last gate; registration writes a SHADOW lifecycle row.
    from strategies.smhgld import build_smhgld_strategy

    smhgld = build_optional_strategy("SMHGLD-007", lambda: build_smhgld_strategy(feed=feed))
    if smhgld is not None:
        node.trader.add_strategy(smhgld)
        feed.register_strategy(smhgld.id, smhgld)

    from strategies.momentum import build_bctrot_strategy, build_momentum_strategy

    # BCTROT-004 registers alongside MOMENTUM-002 (kumo-strategies#32, the operator 2026-08-16). Registering
    # IS trading. An absent lifecycle row reads as TRADING (Operator, 2026-08-19), so a lane that
    # reaches this line with no row SUBMITS. This said the opposite -- promising a second operator
    # gate between the deploy and the first order. The deploy IS the gate. Built first so a failure here surfaces before
    # MOMENTUM is added rather than leaving a half-registered node.
    bctrot = build_bctrot_strategy(feed=feed)

    momentum = build_momentum_strategy(feed=feed)
    if momentum is not None:
        node.trader.add_strategy(momentum)
        if bctrot is not None:
            node.trader.add_strategy(bctrot)
            feed.register_strategy(bctrot.id, bctrot)
        # Tell the feed strategy this one is OURS, so its positions project as managed cycles instead
        # of surfacing as unclaimed broker activity.
        feed.register_strategy(momentum.id, momentum)

    # QC345-003 (#324). Registered ONLY through `build_qc345_strategy`, which is the sole thing that
    # attaches the session gateway. Constructing `QC345RotationStrategy` here directly would not fail
    # or look wrong — the adapter simply falls back to `_decide_for` and submits live orders itself,
    # with no lifecycle, journal, idempotency, risk or budget in the path.
    #
    # Registering IS trading: with no lifecycle row QC345 reads as TRADING (Operator, 2026-08-19).
    from strategies.qc345 import build_qc345_strategy

    # Wrapped (#377): QC345 resolves its universe's exchanges over HTTP at BUILD time, so a slow or
    # unreachable Alpaca took the whole node down with it — MANUAL, MOMENTUM and BCTROT included. A
    # transport failure now costs QC345 and nothing else; a misconfiguration still raises.
    qc345 = build_optional_strategy("QC345-003", lambda: build_qc345_strategy(feed=feed))
    if qc345 is not None:
        node.trader.add_strategy(qc345)
        feed.register_strategy(qc345.id, qc345)

    # TECHIVOL-005 (kumo-strategies#33, #63). Registered ONLY through `build_qc27_strategy`, which is
    # the sole thing that attaches `QC27SessionRunner`. Constructing `QC27RotationStrategy` here
    # directly would not fail or look wrong -- the adapter simply falls back to `_decide_for` and
    # submits live orders itself, with no lifecycle, journal, idempotency, risk or budget in the path.
    #
    # Unlike QC345, registering IS trading here once the gate is on: an absent lifecycle row means
    # TRADING, so `QC27_ENABLED` is the last gate rather than the first of two.
    from strategies.qc27 import build_qc27_strategy

    # Wrapped (#377) for the same reason as QC345: `_instrument_ids` resolves each symbol's exchange
    # over HTTP at BUILD time, so a slow or unreachable Alpaca would otherwise take the whole node
    # down -- MANUAL, MOMENTUM, BCTROT and QC345 included.
    qc27 = build_optional_strategy("TECHIVOL-005", lambda: build_qc27_strategy(feed=feed))
    if qc27 is not None:
        node.trader.add_strategy(qc27)
        feed.register_strategy(qc27.id, qc27)

    # CRSISHORT-006 (kumo-strategies#123, #858) — the SHORT book. Registered ONLY through
    # `build_crsi_short_strategy`, which attaches the SHADOW-only gateway: with no runner the adapter
    # decides and logs by itself, with no journal, lifecycle or idempotency in the path. Registering
    # here is NOT trading for this lane — the gateway refuses TRADING until kumo-strategies#131 (a
    # resting short LIMIT) exists in the installed order layer.
    from strategies.crsi_short import build_crsi_short_strategy

    crsi = build_optional_strategy("CRSISHORT-006", lambda: build_crsi_short_strategy(feed=feed))
    if crsi is not None:
        node.trader.add_strategy(crsi)
        feed.register_strategy(crsi.id, crsi)

    return node


def _ib_contracts_to_matches(contracts) -> list[dict]:
    """US equities from a reqMatchingSymbols answer, as InstrumentMatch rows (#837). Everything else
    (options, non-USD listings, an exchange the adapter cannot name) is dropped, never guessed."""
    from nautilus_trader.adapters.interactive_brokers.parsing.instruments import (
        exchange_to_mic_venue,
        ib_contract_to_instrument_id,
    )

    rows: list[dict] = []
    seen: set[str] = set()
    for c in contracts:
        if getattr(c, "secType", None) != "STK" or getattr(c, "currency", None) != "USD":
            continue
        exchange = getattr(c, "primaryExchange", "") or ""
        venue = exchange_to_mic_venue(exchange) or exchange
        if not venue:
            continue
        iid = str(ib_contract_to_instrument_id(c, venue))
        if iid in seen:
            continue
        seen.add(iid)
        symbol = str(c.symbol)
        rows.append({"instrument_id": iid, "symbol": symbol, "name": symbol, "venue": venue})
    return rows


def _iso_to_ns(value) -> int:
    """Alpaca's `transaction_time` -> epoch ns. Unparseable sorts to the EPOCH, deliberately.

    A fill whose timestamp cannot be read is excluded from every bounded window and included only in
    `all`. The alternative — sorting it to "now" — would silently place an unreadable row inside TODAY's
    P&L, which is the one window an operator checks against their own memory of the session.
    """
    try:
        import pandas as _pd

        ts = _pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return 0
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return int(ts.value)


#: ET calendar-day bounds and window floors live in `api.realized` (#846) so the legs, the session
#: figure and the broker sweep share ONE predicate. Kept under these names for every existing caller.
_et_day_bounds_ns = et_day_bounds_ns
_et_window_start_ns = et_window_start_ns


def _refusal_provenance(refusal) -> str:
    """The lane and the policy's provenance, rendered onto the standing refusal row (#1029, #965).

    `opted_out` on SMHGLD-007 reads ` — SMHGLD-007, stance declared` when the tenant's file is silent
    and ` — SMHGLD-007, stance settings` when an operator wrote the key: the same value, two
    different facts, and this suffix is the only place an operator can tell them apart. A refusal
    with no lane renders nothing extra; one with a lane but no policy (`lane_unattributable` never
    has one) renders the lane alone. Empty string, never None, so the row's note stays one string.
    """
    lane = getattr(refusal, "lane", None)
    source = getattr(refusal, "source", None)
    if not lane:
        return ""
    return f" — {lane}" + (f", stance {source}" if source else "")


def _et_session_ts(ts_ns: int):
    """The ET-localized timestamp for an engine ts, or None outside the US regular session
    (Mon-Fri 09:30-16:00 ET). Holiday-UNAWARE, same as the UI guard it mirrors: on a holiday it would
    allow a market order the venue then cancels, which is visible and harmless, rather than blocking a
    legitimate exit.

    Doubles as the VWAP session key (`_update_vwap_from_bar`): its ET calendar date IS the session id for
    RTH-filtered data — the natural convention for a US equity session, not a workaround for a UTC-day
    bug (RTH bars never cross UTC midnight either, since 09:30-16:00 ET falls entirely within one UTC
    calendar day). VWAP doesn't use the native `VolumeWeightedAveragePrice` indicator at all — that was
    dropped for an unrelated reason (no revision/idempotency support for corrected bars); see
    `_update_vwap_from_bar`.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return None
    minutes = et.hour * 60 + et.minute
    if not (9 * 60 + 30 <= minutes < 16 * 60):
        return None
    return et


#: Client-order-id prefixes this system places protective stops under. A stop we placed is one we may
#: cancel and replace when a manager arms; anything else is not ours to touch.
_OURS = OUR_STOP_PREFIXES


def _is_identifiable_protective_stop(order) -> bool:
    """Is this resting order recognisably THIS system's protective stop on the position (#303)?

    Three shapes qualify: a bracket leg (`STOP_MARKET`/`STOP_LIMIT` tagged `bracket:`), the #239
    backstop's trailing stop, and a manager's own trail. All are stops on the reducing side that we
    placed, so a manager arming over them may cancel and replace them.

    A take-profit LIMIT does NOT qualify — it is not protection, it sits above the market, and cancelling
    it would silently discard an operator's target.
    """
    from nautilus_trader.model.enums import OrderType

    if order.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        if any(str(t).startswith("bracket:") for t in (order.tags or [])):
            return True
    if order.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_MARKET):
        coid = str(getattr(getattr(order, "client_order_id", None), "value", "") or "")
        return coid.startswith(_OURS)
    return False


def _budget_transfer_recipient() -> str | None:
    """Which strategy receives capital freed by a strategy that is over its target.

    A SETTING, not an environment variable (#323). Which strategy is receiving a hand-over is an
    operator decision about a transition in progress — it changes while the system runs, and it was
    absurd that it needed a container restart. Read per call so a change takes effect on the next
    fill rather than the next deploy.

    Deliberately a single named recipient rather than "whoever has headroom": routing loose capital by
    search would make a transition's outcome depend on iteration order over the book. Empty means
    freed capital returns to UNALLOCATED — visible in the account total, granted to nobody.
    """
    try:
        from api import settings

        value = str((settings.resolve("strategies") or {}).get("TRANSFER_TO", "") or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable setting must not stop a fill being recorded
        return None
    return value or None


def _us_market_open(ts_ns: int) -> bool:
    """US regular session (Mon-Fri 09:30-16:00 ET) for the given engine timestamp."""
    return _et_session_ts(ts_ns) is not None


def _rth_seconds_remaining(ts_ns: int) -> float | None:
    """Seconds until the 16:00 ET close, or None outside the regular session. ONE derivation with
    `_us_market_open` — both read `_et_session_ts` — so a flip gate and the pass gate cannot disagree
    about whether the market is open."""
    et = _et_session_ts(ts_ns)
    if et is None:
        return None
    return (16 * 3600) - (et.hour * 3600 + et.minute * 60 + et.second + et.microsecond / 1e6)


def _marks_or_raise(marks: dict[str, dict[str, float]]):
    """A marks source that REFUSES a day it has no bars for, instead of returning nothing.

    The runner distinguishes "no bar for that symbol" (an empty dict — a real answer about prices,
    recorded per position as an unknown valuation) from "we do not know whether bars exist" (a
    RAISE, which refuses the session and writes nothing). That distinction only works if the source
    can actually raise, and the first wiring passed `lambda d: marks.get(d, {})`, which cannot.

    So the refusal branch was unreachable in production: a session with no cached bars would write
    rows with `mark_px` NULL, silently. Base rows are append-only, so at one `method_version` those
    days are unfixable without a version bump — and a single unpriced symbol at the window base
    makes that lane's delta an em dash for the life of the row.

    Refusing is recoverable: nothing is written for that session, and a re-run after the bars are
    seeded writes it normally.
    """
    def _for(session_date: str) -> dict[str, float]:
        if session_date not in marks:
            raise LookupError(
                f"no cached daily bars cover {session_date}, so it is unknown whether this session "
                f"can be priced at all. Refusing rather than writing an append-only row with every "
                f"mark NULL — seed the symbols and re-run."
            )
        return marks[session_date]

    return _for


def _et_date(ts_ns: int) -> str:
    """ET calendar date (YYYY-MM-DD) for an engine timestamp — UNCONDITIONAL, unlike `_et_session_ts`
    (Today's Range needs "what date is it in ET" even outside RTH — the range stays valid all evening,
    it doesn't need a live session to mean something)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _et_date_from_iso(iso_ts: str) -> str | None:
    """ET calendar date for an Alpaca RFC-3339 timestamp string (e.g. `dailyBar.t`), or None if
    unparseable — used to detect a `dailyBar` that's still yesterday's (or older), not actually "today"
    yet (codex review, Phase 3: a thin symbol, a holiday, or an outage must not let a stale cross-day
    value keep rendering as current)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()


class _DeferredFlatten:
    """The `deferred_flatten` manager kind (#170 first slice) — the one manager registered today.

    Trigger = the session becoming regular hours; apply = the SAME `flatten.decide()` the immediate flatten
    path already uses, re-run against live state (never the params captured at attach). `params` carries only
    what the operator SAW at confirm — `expected_side`/`expected_qty` — the live position, resting orders, and
    now the session too, are re-read fresh, exactly as if the human had just re-confirmed the slide.
    """

    #: This kind may be attached for a SIBLING strategy's position (#646): its apply routes the close
    #: through `owner.close_position`, exactly like the immediate flatten path, so the order and the
    #: position id both belong to the owner. Kinds without this flag stay refused for non-self
    #: strategy ids at `_reserve_attach_command` — their apply submits through the MANUAL order factory.
    SUBMITS_VIA_OWNER = True

    def validate_params(self, params: dict) -> str | None:
        if "expected_side" not in params:
            return "deferred_flatten requires expected_side"
        if str(params["expected_side"]).upper() not in ("LONG", "SHORT"):
            return f"invalid expected_side {params.get('expected_side')!r}"
        return None

    def client_order_id_for(self, manager_id: str) -> str:
        # Nautilus caps identifiers at 36 chars including a possible suffix — mirrors the "FL-" immediate
        # path's own truncation so both are recognizable in the blotter as the same feature's output.
        return f"FL-{manager_id[:20]}"

    async def trigger_met(self, strategy, row) -> bool:
        return _us_market_open(strategy.clock.timestamp_ns())

    async def apply(self, strategy, row):
        from api import flatten as fl

        instrument_id = row.instrument_id
        strategy_id = row.strategy_id
        expected_side = str(row.params["expected_side"]).upper()
        expected_qty = row.params.get("expected_qty")

        # Cycle-drift guard (code review): a manager attached against one cycle must not silently apply
        # against a DIFFERENT one that happens to match on instrument/strategy/side/qty — the position could
        # have closed and reopened while this sat ARMED, and that is not the intent that was confirmed.
        if row.cycle_id is not None and strategy._trade_cycles:
            # Scoped by strategy too — selecting on instrument alone could match another strategy's
            # cycle for the same name and fail this row as stale (codex review, High).
            current = strategy.current_cycle_for(instrument_id, strategy_id)
            if current is None or current.cycle_id != row.cycle_id:
                return "FAILED", "the position's cycle changed since this was queued — re-read and retry"

        # Route to the OWNER exactly like the immediate path (#646): under NETTING only the owning
        # strategy may submit against its position — a self-submitted order is attributed to MANUAL-001
        # and OPENS a short beside the position it was meant to close (WHD, 2026-08-19), replayed here
        # at the open with nobody watching. Resolved BEFORE any cancel goes out: the replay must not
        # strip protection and then discover it has nowhere to send the close, which is the exact
        # stranding this manager exists to prevent.
        owner = None
        if strategy_id != str(strategy.id):
            owner = strategy._sibling_strategies.get(strategy_id)
            if owner is None:
                return "FAILED", (
                    f"{instrument_id} belongs to {strategy_id}, which this node does not run — nothing "
                    f"to route the close to; nothing was cancelled and nothing was sent"
                )

        pos = strategy._position_for(instrument_id, strategy_id, expected_side)
        if pos is None:
            pos = next(
                (
                    q
                    for q in strategy.cache.positions_open()
                    if str(q.instrument_id) == instrument_id and str(q.strategy_id) == strategy_id
                ),
                None,
            )
        if pos is None:
            return "FAILED", "nothing to flatten — the position is already closed"

        decision = fl.decide(
            position_side=pos.side.name,
            live_qty=Decimal(str(pos.quantity)),
            expected_side=expected_side,
            expected_qty=Decimal(str(expected_qty)) if expected_qty is not None else None,
            resting_reducing_qty=await strategy._reducing_qty_for_exit(
                instrument_id, strategy_id, pos.side.name
            ),
            market_open=True,  # trigger_met already confirmed this; decide() cannot re-QUEUE from here
        )
        if decision.action != "EXECUTE":
            return "FAILED", decision.reason or "the position no longer qualifies for this flatten"

        # The queued replay inherits the cancel-first requirement (codex review, High). A close sent at
        # the open while a bracket still rests hits exactly the hazard the immediate path guards against:
        # rejected on `available: 0`, or — worse — filled alongside an exit that then fires against a
        # position this close already removed, opening the opposite side.
        if decision.cancel_resting_first:
            reducing_side = OrderSide.SELL if pos.side.name == "LONG" else OrderSide.BUY
            # Same one path as the immediate flatten: cancel natively where the cache can and at the venue
            # where it cannot, then wait on the reservation rather than on order status.
            await strategy._cancel_reducing_leg(instrument_id, strategy_id, reducing_side)
            if not await strategy._await_reducing_orders_clear(
                instrument_id, strategy_id, reducing_side
            ) or not await strategy._await_shares_available(
                instrument_id, Decimal(str(decision.order.quantity))
            ):
                # Stay ARMED rather than terminalize: the next trigger retries, and the position keeps
                # whatever was protecting it in the meantime.
                return "FAILED", (
                    "the resting exit orders did not finish cancelling in time — nothing was sent; "
                    "re-arm to retry"
                )

        coid = self.client_order_id_for(row.manager_id)
        if owner is not None:
            # The OWNER submits (#646), same as the immediate path: `close_position` builds the closing
            # market order from that strategy's own factory, so both the order and the position id
            # belong to it — the only form Nautilus accepts under NETTING.
            owner.close_position(pos, tags=[f"flatten:{coid}"], time_in_force=TimeInForce.DAY)
            strategy._seen_orders.add(coid)
            try:
                await strategy._drop_claim(strategy_id, instrument_id.rpartition(".")[0])  # #923, same as the immediate path
            except Exception as exc:  # noqa: BLE001 — reported, never swallowed
                return "FAILED", f"close submitted for {instrument_id} but {strategy_id}'s claim was NOT dropped: {type(exc).__name__}: {exc}"
            return "APPLIED", None
        order = strategy._build_order(
            {
                "instrument_id": instrument_id,
                "side": decision.order.side,
                "quantity": float(decision.order.quantity),
                "order_type": "market",
                "time_in_force": "day",
                "client_order_id": coid,
                "extended_hours": False,
                # Traceable to the manager that placed it (#72 ADR: manager_id stamped on orders from day
                # one). Reading this back into TradeDTO.manager_id is separate follow-up, not done here.
                "manager_id": row.manager_id,
            }
        )
        # THE POSITION, NOT JUST THE INSTRUMENT — `risk/engine.pyx:425` gates the whole reduce-only
        # check behind `command.position_id is not None`; the immediate path passes it and the replay of
        # the same intent must not silently drop it.
        strategy._submit(order, position_id=pos.id)
        strategy._seen_orders.add(coid)
        return "APPLIED", None


register_manager("deferred_flatten", _DeferredFlatten())


_SRR_REQUIRED_PARAMS = ("expected_side", "reclaim_price", "base_price", "floor_price", "qty", "rearm_count", "rearm_max")


def _validate_stop_reenter_params(params: dict) -> str | None:
    """Shared by both STOP-AND-REENTER (#47) manager kinds — same params travel the whole chain unchanged
    (Phase 1 hands its exact `params` to Phase 2; Phase 2 only bumps `rearm_count` when re-attaching Phase 1
    for the next cycle), so one validator, not two copies that could drift."""
    missing = [k for k in _SRR_REQUIRED_PARAMS if k not in params]
    if missing:
        return f"stop_reenter requires {', '.join(missing)}"
    if str(params["expected_side"]).upper() not in ("LONG", "SHORT"):
        return f"invalid expected_side {params.get('expected_side')!r}"
    return None


class _StopReenterWatch:
    """Phase 1 of STOP-AND-REENTER (#47) — watches a HELD position for its protective stop firing, then
    hands off to `stop_reenter_rearm` to manage the re-entry decision. Every position always carries a
    resting protective stop as a platform-safety invariant, independent of this feature; this manager does
    NOT place or manage that stop itself, only detects when it fires (Operator, 2026-08-02: "of course — every
    position has a protective stop in case the platform fails").

    `params` (identical shape used by `stop_reenter_rearm`, see `_validate_stop_reenter_params`):
    `expected_side`, `reclaim_price` (≈ the stop/exit level — momentum re-entry, NEVER re-enter above this),
    `base_price` (a lower support — value re-entry), `floor_price` (structural level; breaking it = WALK,
    no re-entry), `qty` (re-entry size, same as the stopped-out position), `rearm_count`/`rearm_max` (ratchet
    progress across the whole chain of manager instances this kind + `stop_reenter_rearm` form together).
    """

    #: Params carrying a PRICE, checked against the instrument's tick at attach (#375). Declared per
    #: handler rather than guessed from the name: `base_price` is a price and `rearm_max` is not, and a
    #: heuristic on "_price" would be a rule nobody can see.
    PRICE_PARAMS = ("reclaim_price", "base_price", "floor_price")

    def validate_params(self, params: dict) -> str | None:
        return _validate_stop_reenter_params(params)

    def client_order_id_for(self, manager_id: str) -> str:
        # This phase never places an order itself — kept only for protocol conformance (mirrors
        # _DeferredFlatten's truncation convention in case a future audit path needs one).
        return f"SRW-{manager_id[:20]}"

    async def trigger_met(self, strategy, row) -> bool:
        expected_side = str(row.params["expected_side"]).upper()
        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is not None:
            return False  # still held — nothing to decide yet
        reducing_side = OrderSide.SELL if expected_side == "LONG" else OrderSide.BUY
        order = strategy._closing_order_for(
            row.instrument_id, row.strategy_id, since=row.created_at, reducing_side=reducing_side
        )
        # Flat AND a closing fill has been observed since attach — fires regardless of WHAT kind of fill it
        # was (stop-out / target-hit / manual flatten). `apply()` does the discrimination (codex review,
        # #47): a manager that only fires on a bracket-stop fill would otherwise stay ARMED FOREVER on a
        # target-hit or manual exit, a stale row that could later misattribute a completely unrelated FUTURE
        # stop-out (if the instrument+strategy reopens and closes again) to itself.
        return order is not None

    async def apply(self, strategy, row):
        import uuid

        from api import managers as mg
        from api.db.engine import session_factory

        # Cycle-drift guard (same discipline as _DeferredFlatten.apply) — a manager attached against one
        # cycle must not act against a DIFFERENT one that's since opened on this instrument+strategy: the
        # position could have reopened (by manual action or otherwise) while this sat ARMED, and a stale row
        # must not treat that as its own episode.
        if row.cycle_id is not None and strategy._trade_cycles:
            current = strategy.current_cycle_for(row.instrument_id, row.strategy_id)
            if current is not None and current.cycle_id != row.cycle_id:
                return "FAILED", "a different cycle is now open on this instrument — stale watch, refusing"

        # `since=row.created_at` + `_closing_order_for`'s "earliest, not most recent" semantics (see its own
        # docstring) is what actually pins this to THIS manager's own episode, not a later unrelated one.
        # `reducing_side` (codex review, 2nd pass) excludes a same-side ADD/scale-in fill from being
        # mistaken for the position's close.
        expected_side = str(row.params["expected_side"]).upper()
        reducing_side = OrderSide.SELL if expected_side == "LONG" else OrderSide.BUY
        closing_order = strategy._closing_order_for(
            row.instrument_id, row.strategy_id, since=row.created_at, reducing_side=reducing_side
        )
        if closing_order is None:
            return "FAILED", "flat but no closing fill found — cannot classify, refusing"

        # Discriminate a stop-out (rearm-eligible) from a target-hit (a WIN, part of the same bracket but the
        # LIMIT leg) or a manual flatten (FL- prefixed, no bracket tag — an explicit human exit). Either of
        # the latter TERMINATES this manager cleanly (APPLIED, no hand-off) rather than leaving it ARMED.
        #
        # KNOWN LIMITATION (codex review, disclosed not silently missed): this reads `order.tags` and
        # `order.ts_last` off the in-memory Nautilus cache. Alpaca's reconciliation path
        # (`_parse_order_report`, exec_client.py) rebuilds `OrderStatusReport`s with NO tags at all and
        # `ts_last` = the RECONCILIATION timestamp, not the real fill time. After an engine restart, a
        # manager still ARMED when its stop later fires would see a reconciled order with an empty `tags`
        # list here — `is_stop_out` below evaluates False (fails CLOSED: no misfire, no wrong rearm) but the
        # feature then silently never rearms for that manager. Not a safety hazard, but a real functionality
        # gap — needs a durable, restart-proof stop-vs-target signal (e.g. a persisted bracket-leg registry)
        # before this can be trusted across restarts, not just within one engine session.
        is_stop_out = closing_order.order_type.name == "STOP_MARKET" and any(
            str(t).startswith("bracket:") for t in (closing_order.tags or [])
        )
        if not is_stop_out:
            return "APPLIED", "flat via a non-stop exit (target/manual) — not a stop-out, no rearm"

        # reclaim_price gets OVERWRITTEN here with the ACTUAL observed fill price, not whatever placeholder
        # the UI sent at toggle-ON time. The UI can't know a position's real stop-trigger price today (no
        # way yet to distinguish a resting protective stop from any other working order — PositionDetail.tsx
        # says so explicitly, pending #73/#77), so the value captured at ATTACH is necessarily a guess;
        # `never chase higher than the ACTUAL exit` only means something once the real exit is known.
        params = dict(row.params)
        if closing_order.avg_px:
            params["reclaim_price"] = float(closing_order.avg_px)

        # `qty` gets the same treatment, and for the same reason. It was captured at toggle-ON and is a
        # SNAPSHOT: the position can be trimmed (PEAK does exactly this), added to, or partially closed
        # before the stop-out ever happens. FIG carried `qty: 424` on a position that had since become
        # 233 and then 128 — a re-entry would have bought 3.3x what was actually stopped out.
        #
        # This direction is not symmetric with the sell side. An oversized SELL is refused by the broker
        # ("insufficient qty available"), which is how every stale-quantity bug so far has failed safely.
        # An oversized BUY has no such brake — it simply fills, and the operator owns a position they
        # never sized. So the re-entry is sized from the quantity that actually CLOSED, which is what
        # "buys back what was stopped out" was always supposed to mean.
        closed_qty = float(getattr(closing_order, "filled_qty", 0) or 0)
        if closed_qty > 0:
            params["qty"] = closed_qty

        new_manager_id = str(uuid.uuid4())
        # The operator may have toggled this off while the apply was in flight. A CHAINING kind must
        # check before handing off, or OFF can never stop it — cancelling the one row the UI named
        # leaves the successor armed and the toggle springs back to ON. PEAK already carries this;
        # `family_of` makes the watch/rearm pair count as ONE chain.
        async with session_factory() as session:
            if await mg.chain_cancelled(
                session, "stop_reenter_watch", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at
            ):
                return "APPLIED", "stop-out seen, but STOP-AND-REENTER was turned off — not arming a re-entry watch"
        async with session_factory() as session:
            await mg.attach(
                session,
                manager_id=new_manager_id,
                kind="stop_reenter_rearm",
                account_id=row.account_id,
                client_id=row.client_id,
                instrument_id=row.instrument_id,
                strategy_id=row.strategy_id,
                cycle_id=row.cycle_id,
                leash=row.leash,
                params=params,
                command_id=f"SRW-{row.manager_id}",
            )
        return "APPLIED", f"handed off to {new_manager_id}"


class _StopReenterRearm:
    """Phase 2 of STOP-AND-REENTER (#47) — watches the rearm zone after a stop-out: reclaim (momentum, back
    to the exit price) / base (value, lower) / floor-break (thesis dead — WALK, no re-entry). Re-entry is a
    LIMIT at whichever level fired — never market-on-cross (Operator: safer if there's competing automation) —
    and re-checks live state immediately before submitting, the same "never trust the captured trigger
    snapshot, re-read fresh" discipline `_DeferredFlatten.apply` already uses.
    """

    def validate_params(self, params: dict) -> str | None:
        return _validate_stop_reenter_params(params)

    def client_order_id_for(self, manager_id: str) -> str:
        return f"SRR-{manager_id[:20]}"

    #: See `_StopReenterWatch.PRICE_PARAMS` — the identical params travel the whole chain, so both phases
    #: declare the same set rather than one trusting the other to have checked.
    PRICE_PARAMS = ("reclaim_price", "base_price", "floor_price")

    async def trigger_met(self, strategy, row) -> bool:
        price = strategy._last_price_for(row.instrument_id)  # NOT _mark_px — that only returns a price when
        # a position is OPEN, and this row exists specifically because the position is flat.
        if price is None:
            return False
        long_side = str(row.params["expected_side"]).upper() == "LONG"
        floor = float(row.params["floor_price"])
        reclaim = float(row.params["reclaim_price"])
        base = float(row.params["base_price"])
        if long_side:
            return price <= floor or price >= reclaim or price <= base
        # SHORT: the zone mirrors — floor break is a move ABOVE the structural level, reclaim/base are below.
        return price >= floor or price <= reclaim or price >= base

    async def apply(self, strategy, row):
        import uuid

        from api import managers as mg
        from api.db.engine import session_factory

        expected_side = str(row.params["expected_side"]).upper()
        long_side = expected_side == "LONG"
        floor = float(row.params["floor_price"])
        reclaim = float(row.params["reclaim_price"])
        base = float(row.params["base_price"])
        rearm_count = int(row.params["rearm_count"])
        rearm_max = int(row.params["rearm_max"])

        # Cycle-drift guard (same discipline as _StopReenterWatch/_DeferredFlatten) — if something else has
        # already opened a NEW cycle on this instrument+strategy while this sat ARMED, this row's watch is
        # stale; refuse rather than layering a second re-entry on top of one that already happened another
        # way. (The "already reopened" competing-automation re-check below still applies to the specific
        # instant just before a re-entry submit — this catches drift earlier, before evaluating the zone.)
        if row.cycle_id is not None and strategy._trade_cycles:
            current = strategy.current_cycle_for(row.instrument_id, row.strategy_id)
            if current is not None and current.cycle_id != row.cycle_id:
                return "FAILED", "a different cycle is now open on this instrument — stale rearm watch, refusing"

        # Re-read fresh, don't trust the trigger_met snapshot from a possibly-earlier tick.
        price = strategy._last_price_for(row.instrument_id)
        if price is None:
            return "FAILED", "no live price to evaluate the rearm zone"

        floor_broken = (price <= floor) if long_side else (price >= floor)
        if floor_broken:
            return "APPLIED", "WALK — floor broken, no re-entry"

        reclaim_hit = (price >= reclaim) if long_side else (price <= reclaim)
        base_hit = (price <= base) if long_side else (price >= base)
        if not (reclaim_hit or base_hit):
            return "FAILED", "re-checked at apply time — neither level was actually crossed"

        if rearm_count >= rearm_max:
            return "APPLIED", "cap reached — no further re-entry"

        entry_level = reclaim if reclaim_hit else base
        # "Never chase higher [lower for SHORT] than the original exit" — the core guard from the ticket,
        # checked against the ACTUAL computed level, not just trusting the pre-set reclaim_price.
        if long_side and entry_level > reclaim:
            return "FAILED", "computed re-entry level is above the exit price — refused"
        if not long_side and entry_level < reclaim:
            return "FAILED", "computed re-entry level is below the exit price — refused"

        # Competing-automation guard (the operator's concern) — re-confirm still flat immediately before submitting;
        # something else (manual, or another manager) may have already reopened this position.
        if strategy._position_for(row.instrument_id, row.strategy_id, expected_side) is not None:
            return "FAILED", "position already reopened — not re-entering on top of it"

        # Protective-stop geometry guard: the floor becomes the re-entry's protective stop (below), so it
        # MUST sit on the correct side of the entry price or the broker-side bracket is invalid/backwards.
        # A misconfigured floor at or past the entry level would otherwise build a nonsense (or instantly-
        # triggering) stop.
        if long_side and floor >= entry_level:
            return "FAILED", "floor is not below the re-entry level — refusing to build an invalid stop"
        if not long_side and floor <= entry_level:
            return "FAILED", "floor is not above the re-entry level — refusing to build an invalid stop"

        # Re-entry is a bracket (entry LIMIT + protective STOP_MARKET at the floor), NOT a naked limit order
        # (codex review: a plain limit re-entry would violate this platform's "every position always has a
        # protective stop" invariant — that invariant doesn't hold automatically just because v1 elsewhere
        # is watch-only). Stop at the floor is the natural choice: price falling back through it after
        # re-entering is exactly the WALK signal this feature already treats as thesis-dead, so the SAME
        # level does double duty. Broker-enforced (native Alpaca bracket, see `_stop_bracket`'s docstring),
        # not held by this process the way a plain resting order would need engine uptime to matter.
        coid = self.client_order_id_for(row.manager_id)
        strategy._submit_stop_bracket_reentry(
            instrument_id=row.instrument_id,
            side="BUY" if long_side else "SELL",
            quantity=float(row.params["qty"]),
            entry_price=entry_level,
            stop_price=floor,
            entry_coid=coid,
            manager_id=row.manager_id,
        )

        # KNOWN LIMITATION (codex review, disclosed not silently missed): the fresh `stop_reenter_watch`
        # below is attached immediately after SUBMITTING the entry, not after confirming it actually
        # FILLED. If the entry rests unfilled, gets canceled, or is denied, the fresh Watch has nothing real
        # to watch — it just sits ARMED indefinitely (Watch's own trigger_met requires BOTH flat AND a
        # closing fill since ITS OWN attach, so this doesn't misfire, but it does leak an orphaned manager
        # row with no cleanup path today). Lower severity now that the entry itself is bracket-protected —
        # an unfilled entry never creates an unprotected position, it just means no re-entry happened yet.
        next_rearm_count = rearm_count + 1
        if next_rearm_count < rearm_max:
            next_params = {**row.params, "rearm_count": next_rearm_count}
            # The operator may have toggled this off while the apply was in flight. A CHAINING kind must
            # check before handing off, or OFF can never stop it — cancelling the one row the UI named
            # leaves the successor armed and the toggle springs back to ON. PEAK already carries this;
            # `family_of` makes the watch/rearm pair count as ONE chain.
            async with session_factory() as session:
                if await mg.chain_cancelled(
                    session, "stop_reenter_rearm", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at
                ):
                    return "APPLIED", "re-entry submitted, but STOP-AND-REENTER was turned off — chain stops here"
            async with session_factory() as session:
                await mg.attach(
                    session,
                    manager_id=str(uuid.uuid4()),
                    kind="stop_reenter_watch",
                    account_id=row.account_id,
                    client_id=row.client_id,
                    instrument_id=row.instrument_id,
                    strategy_id=row.strategy_id,
                    cycle_id=row.cycle_id,
                    leash=row.leash,
                    params=next_params,
                    command_id=f"SRR-{row.manager_id}",
                )
        return "APPLIED", None


register_manager("stop_reenter_watch", _StopReenterWatch())
register_manager("stop_reenter_rearm", _StopReenterRearm())


# --- PEAK (#46) signal helpers — bar-derived, no Ichimoku port needed (backend has none today; only the
# frontend, ui/src/lib/ichimoku.ts, computes it) --------------------------------------------------------


def _today_session_bars_1m(strategy, instrument_id: str) -> list:
    """Today's RTH 1-minute bars, OLDEST FIRST. `cache.bars()` returns newest-first; reversed here because
    HoD/slope/fade-detection all read more naturally forward in time. Session boundary reuses the SAME
    ET-calendar-date convention `_et_session_ts` already established for VWAP — extended-hours bars are
    excluded (a premarket/afterhours spike isn't what PEAK is watching for)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    bars = strategy.cache.bars(bar_type(InstrumentId.from_str(instrument_id), "1m"))
    if not bars:
        return []
    today = (
        datetime.fromtimestamp(strategy.clock.timestamp_ns() / 1e9, tz=UTC)
        .astimezone(ZoneInfo("America/New_York"))
        .date()
    )
    session = []
    for b in bars:
        s = _et_session_ts(b.ts_event)
        if s is not None and s.date() == today:
            session.append(b)
    return list(reversed(session))


def _session_high(strategy, instrument_id: str, since_ns: int | None = None) -> float | None:
    """The high to fade FROM: today's RTH high, or — when `since_ns` is given — the high since that
    instant. None until at least one qualifying bar exists (nothing to watch yet — the same "wait for
    real data, never fabricate" convention every other manager follows).

    `since_ns` is what makes PEAK's fade mean "this move rolled over" rather than "the price is below
    some level set earlier today" (#253). Measured against the whole session, the fade is true for any
    name that is off its opening high — which is most names, most afternoons — so arming PEAK fired a
    trim within seconds:

        OKTA 2026-08-12: armed 13:48:12, trimmed 13:48:16 and 13:48:46, at 150.75.
                         The first ladder had sold 52 shares at ~148.61 that morning.
        FIG  2026-08-12: armed 14:05:16, trimmed 14:05:46 and 14:06:16.

    Both were selling into strength against a high set minutes after the open. Scoping the window to the
    manager's own arm time means a fresh arm starts from a fresh high, so a re-arm cannot inherit the
    previous chain's reason to sell.
    """
    return max((float(b.high) for b in _bars_since(strategy, instrument_id, since_ns)), default=None)


_ONE_MINUTE_NS = 60_000_000_000


def _bars_since(strategy, instrument_id: str, since_ns: int | None) -> list:
    """Today's RTH 1m bars from the bar CONTAINING `since_ns` onward, oldest first.

    Floored to the minute rather than compared directly: Alpaca stamps a minute bar at the LEFT edge of
    its interval, so an arm at 13:48:12 sits inside the 13:48:00 bar. A plain `>=` drops that bar along
    with any post-arm high inside it, so the scoped high reads too low — or None until the next minute
    prints, which would silently disable the fade for up to a minute after arming. (codex review, High.)
    """
    bars = _today_session_bars_1m(strategy, instrument_id)
    if since_ns is None:
        return bars
    floor_ns = (since_ns // _ONE_MINUTE_NS) * _ONE_MINUTE_NS
    return [b for b in bars if b.ts_event >= floor_ns]


#: How long a successor will wait for the previous trim's fills before proceeding anyway (#255).
#:
#: Five minutes: comfortably longer than a market order's fill latency (seconds — OKTA's trim filled
#: across 32 seconds in five pieces on 2026-08-12), and long enough not to fight the signal cadence,
#: since the fade it guards needs several consecutive 1-minute bars to form. Short next to a session, so
#: a stranded chain resolves itself well inside the day rather than sitting out the afternoon.
_AWAIT_FILL_TIMEOUT_NS = 300 * 1_000_000_000

#: Quantity comparison tolerance — smaller than any tradable fraction, larger than float noise.
_QTY_EPSILON = 1e-6


def _any_position_open(strategy, instrument_id: str, strategy_id: str) -> bool:
    """Is ANY position open for this instrument+strategy, on either side?

    Distinguishes "the position closed" from "the position flipped", which `_position_for` cannot: it
    filters by expected side, so both look like None to it.
    """
    for side in ("LONG", "SHORT"):
        if strategy._position_for(instrument_id, strategy_id, side) is not None:
            return True
    return False


def _awaiting_fill(strategy, row, pos) -> bool:
    """Is this successor still waiting for the PREVIOUS trim's fills to land? (#255)

    A trim is a market order; its fills arrive asynchronously. The successor attached by that trim records
    `awaiting_qty` — the size the position should read once the trim has actually executed. Until the live
    position has come DOWN to that number, any sizing done here would be computed off a quantity the
    broker is about to reduce.

    Only a position still LARGER than expected means "not landed yet". Equal means the fill arrived; and
    smaller means something else reduced the position too (a manual sell, another manager), in which case
    waiting would be wrong — act on what is actually held.
    """
    awaiting = row.params.get("awaiting_qty")
    if awaiting is None:
        return False

    # Bounded in TIME, not only by the quantity (codex review, High x2). A quantity-only wait strands the
    # chain forever in three realistic ways: the trim partially fills and never reaches the exact
    # remainder; a PYRAMID add or an upward reconciliation puts the position back above the expected
    # number; or the fill event is simply lost. In every one of those the position is left running with a
    # trail sized for a smaller remainder and no manager willing to act — the opposite of the protection
    # this is for. After the deadline we proceed and size off whatever is actually held, which is never
    # worse than not acting at all.
    #
    # Checked BEFORE the parse (#651 item 6), so the malformed-marker WAIT below is bounded by the same
    # deadline and a permanently unreadable row cannot strand the chain.
    armed_ns = _armed_at_ns(row)
    if armed_ns is not None and strategy.clock.timestamp_ns() - armed_ns > _AWAIT_FILL_TIMEOUT_NS:
        return False

    try:
        held, expected = float(pos.quantity), float(awaiting)
    except (TypeError, ValueError) as exc:
        # A wait-marker (or a position quantity) the code cannot read is NOT evidence the fill
        # landed. `return False` here turned a malformed marker into "not waiting" — re-opening the
        # double-trim that `awaiting_qty` exists to prevent, silently. Unknown means WAIT, said out
        # loud; the time bound above still releases the chain (#651 item 6).
        _log.error(
            "manager %s: awaiting_qty=%r / position qty=%r unreadable (%r) — treating as STILL "
            "WAITING until the fill-wait deadline, not as landed",
            getattr(row, "manager_id", "?"), awaiting, getattr(pos, "quantity", None), exc,
        )
        return True

    # A tolerance, not `>`: quantities arrive as Decimal/float and a value that should equal the expected
    # remainder can render a hair above it, which an exact comparison would read as "still pending".
    # (codex review, Medium.)
    if held - expected <= _QTY_EPSILON:
        return False  # the reduction landed (or something cut deeper) — nothing to wait for

    # An UPPER bound too, so only the trim's own window counts as waiting (codex review, Medium).
    #
    # "Held is above the expected remainder" is not by itself the signature of an unlanded fill — a
    # PYRAMID add or an upward reconciliation produces the same reading, and would re-suppress PEAK on a
    # wait that had already completed. The real signature is narrower: held is still at or below what the
    # position measured BEFORE the trim. Above that is someone adding, not the broker lagging.
    #
    # Consuming the param instead would not work: `row` is loaded per tick, so popping it mutates only
    # this tick's copy while the stored row keeps it.
    from_qty = row.params.get("awaiting_from")
    if from_qty is not None:
        try:
            if held - float(from_qty) > _QTY_EPSILON:
                return False  # bigger than before the trim — an add, not a pending fill
        except (TypeError, ValueError):
            pass
    return True


def _armed_at_ns(row) -> int | None:
    """The manager row's arm instant in epoch nanoseconds, or None if it cannot be read.

    None deliberately means "fall back to the session high" rather than "no high": a row whose timestamp
    is unreadable should behave as it did before, not silently stop protecting.
    """
    created = getattr(row, "created_at", None)
    if created is None:
        return None
    try:
        return int(created.timestamp() * 1e9)
    except (AttributeError, TypeError, ValueError, OSError):
        return None


def _sma_daily(strategy, instrument_id: str, period: int = 20) -> float | None:
    """A `period`-day SMA of daily closes — the ext% baseline. None if fewer than `period` days of history
    are cached (a recently-added symbol) rather than computing off a short, misleading window."""
    bars = strategy.cache.bars(bar_type(InstrumentId.from_str(instrument_id), "1d"))  # newest-first
    # Drop TODAY's still-forming daily bar (codex review, #255). Its close is the current price, so leaving
    # it in makes the baseline contain the very spike it is measuring: the SMA is dragged up, ext% comes out
    # lower than it should, and PEAK's blowoff trigger is SUPPRESSED near the 40% threshold — the trigger
    # fails exactly when the move is biggest, which is when it matters. The baseline must be the base the
    # price is extended FROM, so it can only be built from completed sessions.
    if bars and _is_todays_session(strategy, bars[0]):
        bars = bars[1:]
    if len(bars) < period:
        return None
    window = bars[:period]
    return sum(float(b.close) for b in window) / period


def _is_todays_session(strategy, bar) -> bool:
    """Does this DAILY bar belong to the current ET session?

    Compares ET calendar dates directly, and deliberately does NOT go through `_et_session_ts` (codex
    review, High). That helper returns None outside 09:30-16:00 ET, and Alpaca stamps daily bars at
    **00:00 ET** — verified against the live feed: `2026-08-06T04:00:00Z` is `2026-08-06 00:00 -04:00`.
    Routing daily bars through an RTH filter therefore answers False for every real bar, which would have
    left this fix passing its tests and doing nothing whatsoever in production. The tests only went green
    because their fixtures used 10:00 ET timestamps that no daily bar ever carries.

    An intraday helper wants the RTH filter; a daily bar IS a session by construction and only needs its
    date.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    bar_date = datetime.fromtimestamp(bar.ts_event / 1e9, tz=UTC).astimezone(et).date()
    today = datetime.fromtimestamp(strategy.clock.timestamp_ns() / 1e9, tz=UTC).astimezone(et).date()
    return bar_date == today


def _ext_pct(price: float, sma: float | None) -> float | None:
    """% extension of `price` above/below a baseline SMA — a bar-only proxy for 'how stretched is this from
    its recent base' (the ticket's ext% signal), without needing Ichimoku's Kijun/cloud machinery."""
    if sma is None or sma == 0:
        return None
    return (price - sma) / sma * 100


def _vertical_slope_pct(strategy, instrument_id: str, bars_back: int = 3) -> float | None:
    """% price change over the last `bars_back` RTH 1m bars — a cheap stand-in for 'moving too fast to be a
    normal trend' (the ticket's vertical-slope signal). None if there isn't enough session history yet."""
    session = _today_session_bars_1m(strategy, instrument_id)
    if len(session) < bars_back + 1:
        return None
    recent = session[-(bars_back + 1) :]
    start, end = float(recent[0].close), float(recent[-1].close)
    if start == 0:
        return None
    return (end - start) / start * 100


def _sustained_fade(
    strategy,
    instrument_id: str,
    hod: float,
    *,
    off_hod_pct: float,
    lower_high_bars: int,
    since_ns: int | None = None,
) -> bool:
    """Anti-noise fade-off-HoD confirmation (PENG's own lesson, #46 ticket: "a single red bar off a fresh
    HoD is NOT an exit — the −1.5% wiggle resumed"). Fires on EITHER `lower_high_bars` consecutive bars each
    making a lower high than the one before, OR price sustained `off_hod_pct`+ below HoD for 2+ consecutive
    bars — never a single-bar/single-tick check."""
    # The SAME window the caller derived `hod` from. Scoping only the high left the lower-high branch and
    # the close comparisons reading pre-arm bars, so a fresh arm could still trim on a fade sequence that
    # completed before it existed — the very thing #253 is about. (codex review, High.)
    session = _bars_since(strategy, instrument_id, since_ns)
    if hod <= 0:
        return False
    if len(session) >= lower_high_bars:
        recent = session[-lower_high_bars:]
        if all(float(recent[i].high) < float(recent[i - 1].high) for i in range(1, len(recent))):
            return True
    if len(session) >= 2:
        last_two = session[-2:]
        if all((hod - float(b.close)) / hod * 100 >= off_hod_pct for b in last_two):
            return True
    return False


_PEAK_REQUIRED_PARAMS = (
    "expected_side", "qty", "trail_wide_bps", "trail_tight_bps",
    "ext_pct_threshold", "slope_pct_threshold", "off_hod_pct_threshold", "lower_high_bars",
    "trim_max", "trim_fraction",
)


# --- PYRAMID (#38) signal helpers — confirmation-scaling on a running winner. Reuses PEAK's
# `_today_session_bars_1m`/`_session_high`/`_sustained_fade` for the intraday side; adds daily-bar range-
# break + volume confirmation (Operator, 2026-08-03: a new proxy metric, not full base/consolidation pattern
# detection — same build shape as PEAK's ext%/slope proxies) and a faithful Python port of Wilder's ADX
# (the operator's call: port rather than drop, unlike PEAK which dropped Ichimoku/IV-percentile for v1). ----------


def _daily_bars(strategy, instrument_id: str, count: int) -> list:
    """Up to `count` most-recent COMPLETED daily bars, OLDEST FIRST (chronological — `_wilder_adx` and
    `_daily_range_high` both read forward in time). `cache.bars()` returns newest-first; reversed here, same
    convention `_today_session_bars_1m` already established for 1m bars.

    codex review (High): explicitly EXCLUDES today's own daily bar. Alpaca streams a live, still-forming
    daily bar (`bar_spec.py`'s daily channel) whose high updates intraday — if included, `_daily_range_high`
    would compare today's session high against a "prior" range that already contains today's OWN high
    (self-referential: the range effectively grows to match whatever today prints, so a real breakout could
    never clear it), and `_wilder_adx` would smooth in a partial, not-yet-final day's close. Filtering to
    STRICTLY BEFORE today by ET calendar date — deliberately NOT `_et_session_ts` (that helper also rejects
    anything outside the 09:30-16:00 RTH window, an intraday-tick rule that doesn't apply to a DAILY bar
    representing a whole session; a daily bar timestamped at the close, 16:00:00, would be wrongly rejected
    by that helper's exclusive upper bound)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et_zone = ZoneInfo("America/New_York")
    bars = strategy.cache.bars(bar_type(InstrumentId.from_str(instrument_id), "1d"))  # newest-first
    today = datetime.fromtimestamp(strategy.clock.timestamp_ns() / 1e9, tz=UTC).astimezone(et_zone).date()
    completed = [
        b for b in bars
        if datetime.fromtimestamp(b.ts_event / 1e9, tz=UTC).astimezone(et_zone).date() < today
    ]
    return list(reversed(completed[:count]))


def _daily_range_high(strategy, instrument_id: str, lookback_days: int) -> float | None:
    """Max daily HIGH over the prior `lookback_days` daily bars — the #38 base-break proxy's 'prior range'
    (PENG's June 60-72 consolidation, in bar-only form). None with insufficient history, same 'wait for real
    data, never fabricate' convention every other signal helper in this file follows."""
    bars = _daily_bars(strategy, instrument_id, lookback_days)
    if len(bars) < lookback_days:
        return None
    return max(float(b.high) for b in bars)


def _relative_volume(strategy, instrument_id: str, lookback_bars: int = 20) -> float | None:
    """Most recent RTH 1m bar's volume vs. the trailing average of the `lookback_bars` before it. None
    without enough session history yet, or if the trailing average is zero (an illiquid/pre-open stretch —
    a ratio against zero volume is meaningless, not a real confirmation)."""
    session = _today_session_bars_1m(strategy, instrument_id)
    if len(session) < lookback_bars + 1:
        return None
    recent = session[-(lookback_bars + 1):]
    trailing_avg = sum(float(b.volume) for b in recent[:-1]) / lookback_bars
    if trailing_avg == 0:
        return None
    return float(recent[-1].volume) / trailing_avg


def _range_high_break(
    strategy,
    instrument_id: str,
    *,
    daily_lookback_days: int,
    min_rel_volume: float,
    sustained_bars: int,
    volume_lookback_bars: int = 20,
) -> bool:
    """#38's base-break proxy: fires when today's session high clears the prior `daily_lookback_days` daily
    range high AND volume has been >= `min_rel_volume`x the trailing average for the last `sustained_bars`
    1m bars (never a single-bar check — same anti-noise discipline `_sustained_fade` already established for
    fade detection). The trailing average is computed ONCE from the window strictly BEFORE the sustained
    window (not recomputed bar-by-bar, which would let the sustained bars' own volume pollute their own
    baseline) — mirrors `_sustained_fade`'s own shape: one fixed reference point (HoD there, the pre-window
    average here), then check the recent window against it. A thin vertical poke with no real prior range to
    break, or a break on thin volume, correctly does NOT fire — this is what discriminates PENG's forbidden
    09:30 chase (no base, thin volume) from its missed 72-breakout add (real range broken, volume >=1.5x)."""
    prior_high = _daily_range_high(strategy, instrument_id, daily_lookback_days)
    today_high = _session_high(strategy, instrument_id)
    if prior_high is None or today_high is None or today_high <= prior_high:
        return False
    session = _today_session_bars_1m(strategy, instrument_id)
    if len(session) < volume_lookback_bars + sustained_bars:
        return False
    baseline_window = session[-(volume_lookback_bars + sustained_bars) : -sustained_bars]
    trailing_avg = sum(float(b.volume) for b in baseline_window) / volume_lookback_bars
    if trailing_avg == 0:
        return False
    recent = session[-sustained_bars:]
    return all(float(b.volume) >= trailing_avg * min_rel_volume for b in recent)


def _wilder_ema(values: list, period: int) -> list:
    """Wilder smoothing (`ewm(alpha=1/period, adjust=False)`), faithful port of `ui/src/lib/adx.ts`'s
    `wilderEma`: `y[0]=x[0]`, `y[t]=alpha*x[t]+(1-alpha)*y[t-1]`. A `None` input carries the prior smoothed
    value forward UNCHANGED (matching pandas' ewm NaN handling — the TS port's own docstring, #181) rather
    than injecting a fabricated data point that would pull the series toward it."""
    alpha = 1.0 / period
    out: list = []
    prev = None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def _wilder_adx(bars: list, period: int = 9) -> dict:
    """Wilder's ADX/+DI/-DI — faithful line-by-line port of `ui/src/lib/adx.ts`'s `computeAdx` (already
    Wilder-correct and parity-verified against kumo-trader's original pandas scanner per that file's own
    docstring, #181). `bars` must be CHRONOLOGICAL (oldest first, e.g. via `_daily_bars`) with
    `.high`/`.low`/`.close` — Nautilus `Bar` objects already expose these. Returns `{"adx": [...],
    "plus_di": [...], "minus_di": [...]}`, index-aligned 1:1 with `bars`: index 0 has no previous bar, so
    TR = high-low only and +DM/-DM = 0 (matches pandas' own NaN-comparison-is-False semantics at that
    boundary — the TS port's own documented behavior, not a shortcut taken here)."""
    if not bars:
        return {"adx": [], "plus_di": [], "minus_di": []}

    tr: list = []
    plus_dm: list = []
    minus_dm: list = []
    for i, b in enumerate(bars):
        high, low = float(b.high), float(b.low)
        if i == 0:
            tr.append(high - low)
            plus_dm.append(0.0)
            minus_dm.append(0.0)
            continue
        prev = bars[i - 1]
        prev_close = float(prev.close)
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up = high - float(prev.high)
        down = float(prev.low) - low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    atr = _wilder_ema(tr, period)

    def _di_from(dm: list) -> list:
        smoothed = _wilder_ema(dm, period)
        return [
            None if (v is None or a is None or a == 0) else 100 * v / a
            for v, a in zip(smoothed, atr)
        ]

    plus_di = _di_from(plus_dm)
    minus_di = _di_from(minus_dm)

    dx: list = []
    for p, m in zip(plus_di, minus_di):
        if p is None or m is None:
            dx.append(None)
            continue
        s = p + m
        dx.append(None if s == 0 else 100 * abs(p - m) / s)

    return {"adx": _wilder_ema(dx, period), "plus_di": plus_di, "minus_di": minus_di}


def _adx_is_rising(series: list, lookback: int = 3) -> bool:
    """`series[-1] > series[-1-lookback]` — False (never raises) if the series is too short or either value
    is `None`/non-finite. Port of `ui/src/lib/adx.ts`'s `isRising`."""
    import math

    n = len(series)
    if n <= lookback:
        return False
    last, prior = series[-1], series[-1 - lookback]
    if last is None or prior is None:
        return False
    return math.isfinite(last) and math.isfinite(prior) and last > prior


def _adx_is_falling(series: list, lookback: int = 3) -> bool:
    """Mirror of `_adx_is_rising` (`series[-1] < series[-1-lookback]`) — #38's exit-gate 'ADX rolls over'
    condition. NOT part of the TS port (`isRising` has no falling counterpart there) but the same symmetric
    shape. Declining is a distinct, meaningful rollover signal — NOT the same as merely 'not rising': flat/
    plateaued ADX is the normal resting state for most of a trend's life, not a rollover, so gating the exit
    on `not _adx_is_rising(...)` would fire almost immediately after every add and defeat the point of
    letting a confirmed pyramid ride."""
    import math

    n = len(series)
    if n <= lookback:
        return False
    last, prior = series[-1], series[-1 - lookback]
    if last is None or prior is None:
        return False
    return math.isfinite(last) and math.isfinite(prior) and last < prior


_PYRAMID_ADX_PERIOD = 9  # George's setting (ui/src/config/checklists.ts:16), not the textbook 14
_PYRAMID_ADX_RISING_LOOKBACK = 3  # matches ui/src/lib/adx.ts's isRising default


def _validate_leash(leash: object) -> str | None:
    """Only AUTO is real (codex review, #255).

    `mg.claim` is an unconditional `ARMED -> APPLYING` update with no leash predicate, so a row armed
    CONFIRM or ALERT is claimed and applied exactly like AUTO — the reducer's PROPOSED branch is
    unreachable from the dispatch loop. AUTO-only is the deliberate model here (arming IS the gate; there
    is no per-action confirmation), so the defect was never a missing gate. It was that the attach API
    advertised two safety settings that silently did nothing, which is worse than not offering them.

    The reducer keeps its CONFIRM/ALERT branches and its tests: the state machine is meant to outlive this
    restriction, and pinning behaviour that has no caller yet is what makes adding one safe later.

    EXACT match, deliberately — no case folding. Normalizing instead would mean rewriting `leash` before
    the attach idempotency hash is computed over it, so a command first processed as "auto" and later
    redelivered would hash differently and be rejected as a changed payload rather than mirroring its
    original outcome (codex review). Tolerating alternate casing buys nothing here: the only caller is our
    own UI, which sends "AUTO".
    """
    if leash != "AUTO":
        return (
            f"leash {leash!r} is not honoured — this engine applies every armed manager automatically "
            "(arming is the gate). Only AUTO is accepted."
        )
    return None


def _validate_peak_params(params: dict) -> str | None:
    missing = [k for k in _PEAK_REQUIRED_PARAMS if k not in params]
    if missing:
        return f"peak_watch requires {', '.join(missing)}"
    side = str(params["expected_side"]).upper()
    if side not in ("LONG", "SHORT"):
        return f"invalid expected_side {params.get('expected_side')!r}"
    if side == "SHORT":
        # codex review (Medium): every signal formula below (HoD, lower-highs, off-HoD%) is LONG-only math —
        # mirroring for SHORT (LoD, higher-lows, off-LoD%) is real unbuilt work, not a trivial sign flip.
        # Refuse rather than silently apply wrong-direction signals to a short position (v1 scope).
        return "peak_watch v1 is LONG-only — signal math isn't mirrored for SHORT yet"
    return _validate_peak_ranges(params)


def _validate_peak_ranges(params: dict) -> str | None:
    """Range-check the numeric params (codex review, #255). Presence and side were checked; the VALUES were
    not, and several out-of-range combinations invert PEAK's behaviour rather than merely degrading it:

    * `trail_tight_bps >= trail_wide_bps` makes the blowoff "tighten" WIDEN the stop — the opposite of the
      one thing that branch exists to do.
    * `trim_max <= 0` turns the first fade straight into a full exit, with no laddering at all.
    * `lower_high_bars <= 1` makes the fade condition trivially true, so PEAK trims on arm.
    * A negative threshold forces its trigger permanently on.

    The UI only ever sends `peakDefaults()`, so none of this is reachable from the toggle today — but the
    attach command takes arbitrary params, and a manager that silently does the reverse of its name is a
    worse failure than one that refuses to arm.
    """
    try:
        wide = float(params["trail_wide_bps"])
        tight = float(params["trail_tight_bps"])
        trim_fraction = float(params["trim_fraction"])
        qty = float(params["qty"])
        # Counts are read as FLOAT first. `int(float("inf"))` raises OverflowError — which the old except
        # clause did not catch — and `int(2.7)` silently truncates to 2, so a fractional count would be
        # accepted as something other than what was sent. Check finiteness and integrality, then convert.
        # (codex review, #255.)
        trim_max_f = float(params["trim_max"])
        lower_high_bars_f = float(params["lower_high_bars"])
        thresholds = {
            k: float(params[k])
            for k in ("ext_pct_threshold", "slope_pct_threshold", "off_hod_pct_threshold")
        }
    except (TypeError, ValueError, OverflowError) as exc:
        return f"peak_watch has a non-numeric parameter: {exc}"

    for name, value in {"trim_max": trim_max_f, "lower_high_bars": lower_high_bars_f}.items():
        if not isfinite(value) or value != int(value):
            return f"{name} must be a whole number (got {value})"
    trim_max, lower_high_bars = int(trim_max_f), int(lower_high_bars_f)

    numeric = {
        "trail_wide_bps": wide, "trail_tight_bps": tight, "trim_fraction": trim_fraction,
        "qty": qty, **thresholds,
    }
    for name, value in numeric.items():
        # NaN and inf survive float() and defeat every comparison below — NaN makes each one False, so a
        # NaN threshold would pass validation and then never (or always) trigger. (codex review.)
        if not isfinite(value):
            return f"{name} must be a finite number (got {value})"
    if wide <= 0 or tight <= 0:
        return f"trail widths must be positive (wide={wide}, tight={tight})"
    if tight >= wide:
        return (
            f"trail_tight_bps ({tight}) must be TIGHTER than trail_wide_bps ({wide}) — "
            "otherwise the blowoff branch widens the stop instead of tightening it"
        )
    if trim_max < 1:
        return f"trim_max must be at least 1 (got {trim_max}) — 0 would make the first fade a full exit"
    if not 0 < trim_fraction <= 1:
        return f"trim_fraction must be in (0, 1] (got {trim_fraction})"
    if lower_high_bars < 2:
        return (
            f"lower_high_bars must be at least 2 (got {lower_high_bars}) — "
            "fewer cannot describe a sequence of lower highs and the fade fires immediately"
        )
    for name, value in thresholds.items():
        if value < 0:
            return f"{name} must not be negative (got {value}) — a negative threshold is always met"
    return None


class _PeakWatch:
    """PEAK (#46) — adaptive trailing stop on a winning position, sell-the-spike exit. Origin: PENG
    2026-07-09, rode 62→89.86 (+49% in 2 days), "let it run" faded −9.5% before the stop caught it —
    ~$290 left on the table because the peak was un-timeable by hand.

    ONE manager kind (not a phase chain like #47's watch→rearm) — blowoff-tighten and fade-exit are two
    INDEPENDENT conditions evaluated concurrently while riding, neither a prerequisite for the other.
    `trigger_met` fires on either; `apply()` re-checks fresh and dispatches to whichever actually fired,
    fade-exit taking priority if (rare) both hold simultaneously — exit is more urgent than a mere retighten.

    `params`: `expected_side`, `qty` (current remaining, ratchets down on each trim), `trail_wide_bps`/
    `trail_tight_bps` (basis points — 150 = 1.5%), `tightened` (bool, blowoff already fired once — the
    tighten is a ONE-TIME step per the ticket's binary framing, not a continuous gradient), `current_trail_coid`
    (which resting order IS the trail right now, for cancel-on-replace), `ext_pct_threshold`/
    `slope_pct_threshold` (blowoff triggers), `off_hod_pct_threshold`/`lower_high_bars` (fade-exit
    confirmation), `trim_count`/`trim_max`/`trim_fraction` (partial-exit ratchet, transaction-cost capped —
    Operator, 2026-08-03: a resting-stop replace is free, but each partial trim crosses the spread for real).

    KNOWN LIMITATION (codex review round 2, disclosed not silently missed; Operator, 2026-08-03: reorder trail-
    before-trim over the alternatives — a repair hook or full multi-coid tracking): the partial-trim branch
    of `apply()` submits TWO orders (the new, tighter trail, THEN the market trim) in one call, but the
    framework's crash-recovery contract (`_reconcile_stuck_managers`) only tracks ONE coid per row — the one
    `client_order_id_for` returns, deliberately assigned to the TRAIL (the side effect that actually
    protects the remainder), not the trim. If the process crashes between the two submits, restart recovery
    finds the trail in cache and marks this row APPLIED but does NOT re-run the chain — the fresh
    `peak_watch` row for the remainder is never attached, so the ratchet silently stops. Unlike the original
    (trim-first) ordering, the remainder is NOT left naked in this window — the trail is already resting,
    sized for the post-trim quantity — but the trim itself may never have executed (position stays at its
    pre-trim size, still protected by a trail sized smaller than what's held, which is safe but means extra
    shares ride further than intended) and the watcher stops regardless. A human needs to notice and re-arm
    manually. A full fix needs multi-coid intent tracking or a kind-specific recovery-repair hook, neither of
    which the framework has yet (out of scope for v1).
    """

    def validate_params(self, params: dict) -> str | None:
        return _validate_peak_params(params)

    def client_order_id_for(self, manager_id: str) -> str:
        return f"PK-{manager_id[:20]}"

    async def trigger_met(self, strategy, row) -> bool:
        expected_side = str(row.params["expected_side"]).upper()
        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is None:
            # Flat — the position closed, by this chain or any other route. FIRE so that `apply()` runs
            # and terminalizes the row (#255). Returning False here left the row ARMED forever: the
            # comment claimed "apply() will find nothing to do and fail cleanly", but apply() is never
            # reached. `peak_watch` on SAP has sat ARMED at qty 16 since the position closed on
            # 2026-08-12, which is the live proof.
            return True
        if _awaiting_fill(strategy, row, pos):
            return False
        price = strategy._last_price_for(row.instrument_id)
        if price is None:
            return False
        # No manager acts outside regular hours (#255 High 4). After the close a stale same-day fade
        # reading can still cancel protection and submit a DAY market order with `extended_hours=False` —
        # an order that cannot fill and, per #252, is not checked for acceptance either. So PEAK could
        # strip a stop at 16:30 and leave the position naked overnight, having sold nothing.
        #
        # It matters more now that #239 rests backstop stops at the venue: a backstop that rests during RTH
        # while PEAK cancels it after hours is not protection.
        #
        # The gate lives HERE and not in `apply` on purpose. `apply` has two outcomes and FAILED is
        # TERMINAL (#255 High 2, still open), so declining there would permanently disarm the manager.
        # Returning False leaves the row ARMED, waiting for the next session.
        if not _us_market_open(strategy.clock.timestamp_ns()):
            return False
        # Scoped to THIS manager's arm time (#253) — see `_session_high`. Derived ONCE and reused: two
        # derivations of one fact will disagree, and the high and the fade must read one window.
        armed_ns = _armed_at_ns(row)
        hod = _session_high(strategy, row.instrument_id, since_ns=armed_ns)
        if hod is None:
            return False
        if _sustained_fade(
            strategy, row.instrument_id, hod,
            off_hod_pct=float(row.params["off_hod_pct_threshold"]),
            lower_high_bars=int(row.params["lower_high_bars"]),
            since_ns=armed_ns,
        ):
            return True
        if not bool(row.params.get("tightened", False)):
            ext = _ext_pct(price, _sma_daily(strategy, row.instrument_id))
            slope = _vertical_slope_pct(strategy, row.instrument_id)
            if (ext is not None and ext >= float(row.params["ext_pct_threshold"])) or (
                slope is not None and slope >= float(row.params["slope_pct_threshold"])
            ):
                return True
        return False

    async def apply(self, strategy, row):
        import uuid

        from api import managers as mg
        from api.db.engine import session_factory

        # FLAT FIRST, before the cycle-drift guard (codex review, High). When a position closes, the
        # projection emits the CLOSED cycle and then POPS it, so by the next dispatch tick
        # `current_cycle_for()` returns None and the drift guard would report FAILED — for a manager whose
        # position simply finished. That is the zombie surfacing as an error instead of a completion. A
        # flat position is unambiguous and needs no cycle to interpret it.
        # FLAT means flat — not merely "no position on the side I expected". `_position_for` filters by
        # side, so a LONG row whose position has FLIPPED short also reads as None, and calling that a
        # clean completion would hide a reopen behind a green APPLIED. Only a genuinely empty book for
        # this instrument+strategy terminalizes here; anything still held falls through to the
        # cycle-drift guard, which is what says "re-read and retry". (codex review, High.)
        if strategy._position_for(row.instrument_id, row.strategy_id, str(row.params["expected_side"]).upper()) is None:
            if not _any_position_open(strategy, row.instrument_id, row.strategy_id):
                return "APPLIED", "position closed — nothing left to watch"

        # Cycle-drift guard (codex review, High #2; same discipline as `_StopReenterWatch`/
        # `_StopReenterRearm`/`_DeferredFlatten`) — a manager attached against one cycle must not act
        # against a DIFFERENT one that's since opened on this instrument+strategy: the position could have
        # closed and reopened (manually or otherwise) while this sat ARMED, and a stale row must not treat
        # that reopened position as its own episode.
        if row.cycle_id is not None and strategy._trade_cycles:
            current = strategy.current_cycle_for(row.instrument_id, row.strategy_id)
            if current is None or current.cycle_id != row.cycle_id:
                return "FAILED", "the position's cycle changed since this was queued — re-read and retry"

        expected_side = str(row.params["expected_side"]).upper()
        long_side = expected_side == "LONG"
        close_side = "SELL" if long_side else "BUY"

        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is None:
            # Reachable only when the flat-first check above found something still open on the OTHER
            # side and the cycle guard then passed — i.e. the position FLIPPED within the same cycle, or
            # the row carries no cycle_id to check. That is a reopen, not a completion, and returning
            # APPLIED here would report it as "position closed". (codex review, round 3, High.)
            #
            # A genuine close is already handled above, before the cycle guard can turn it into a drift
            # failure. Anything arriving here is a side change this row has no mandate over.
            return "FAILED", "the position flipped side since this was queued — re-read and retry"

        price = strategy._last_price_for(row.instrument_id)
        if price is None:
            return "FAILED", "no live price to evaluate"
        # The SAME arm-scoped window `trigger_met` used (#253). Two derivations of one fact will
        # disagree — that has already bitten leash validation and the manager-armed check — and here a
        # disagreement means apply() acts on a fade the trigger never saw, or refuses one it did.
        armed_ns = _armed_at_ns(row)
        hod = _session_high(strategy, row.instrument_id, since_ns=armed_ns)
        if hod is None:
            return "FAILED", "no session high available yet"

        # Fade-exit takes priority — re-checked fresh (don't trust the trigger_met snapshot from a possibly
        # earlier tick, same discipline #47's rearm phase already established).
        sustained_fade = _sustained_fade(
            strategy, row.instrument_id, hod,
            off_hod_pct=float(row.params["off_hod_pct_threshold"]),
            lower_high_bars=int(row.params["lower_high_bars"]),
            since_ns=armed_ns,
        )
        if sustained_fade:
            trim_count = int(row.params["trim_count"])
            trim_max = int(row.params["trim_max"])
            trim_fraction = float(row.params["trim_fraction"])
            # codex review (High #5): size off the LIVE position, not `row.params["qty"]` — a stale,
            # manager-tracked snapshot that can drift from reality (a fill elsewhere, a manual action). `pos`
            # above is already the live position; this is the same "re-read live, never trust a stale
            # snapshot" discipline the fresh HoD re-derivation above already follows.
            qty = float(pos.quantity)
            # Cancel-THEN-act here, unlike `_replace_trailing_stop`'s submit-then-cancel rule — deliberately
            # different, not an inconsistency: a pure retighten swaps ONE resting order for another with
            # nothing else happening, so submitting first has zero downside. Here a MARKET sell (near-
            # instant fill, not a resting order) is happening regardless — the real hazard isn't the brief
            # gap before it fills, it's leaving the OLD (now wrong-sized) stop resting IN ADDITION to a
            # freshly-placed one: two differently-sized stops both live could each independently fire and
            # jointly oversell past the newly-reduced position. Canceling first, then trimming, then placing
            # the correctly-sized replacement avoids that qty-mismatch class of bug at the cost of a
            # sub-second gap bounded by how fast the market trim order fills. Confirmed via Perplexity
            # (Alpaca's own docs) this isn't theoretical: Alpaca does NOT auto-reconcile — two resting sell
            # orders whose combined quantity exceeds the position both stay independently valid, and a
            # stale-quantity trailing stop just sits there after a partial fill elsewhere unless explicitly
            # canceled/replaced.
            old_coid = row.params.get("current_trail_coid")
            old_order = strategy._lookup_order(old_coid) if old_coid else None
            if old_order is not None and old_order.is_open:
                strategy._cancel(old_order)

            trim_qty = round(qty * trim_fraction) if trim_fraction < 1.0 else qty
            remaining_qty = qty - trim_qty
            full_exit = trim_count >= trim_max or trim_fraction >= 1.0 or trim_qty <= 0 or remaining_qty <= 0

            if full_exit:
                # codex review (High #3): MUST equal `self.client_order_id_for(row.manager_id)` — the
                # dispatch loop records THAT exact value as this row's INTENT_RECORDED coid before calling
                # apply(). `_reconcile_stuck_managers` on restart trusts the recorded coid as "the SOLE
                # source of truth" for what this manager was about to place; if apply() actually submits
                # under a DIFFERENT coid (the old `PKX-{manager_id}` did), crash-recovery's
                # `_lookup_order(coid)` check can never find it, always reverts to ARMED, and the next
                # dispatch tick re-submits a SECOND market order on top of one that may have already filled.
                coid = self.client_order_id_for(row.manager_id)
                order = strategy._build_order(
                    {
                        "instrument_id": row.instrument_id,
                        "side": close_side,
                        "quantity": qty,
                        "order_type": "market",
                        "time_in_force": "day",
                        "client_order_id": coid,
                        "extended_hours": False,
                        "manager_id": row.manager_id,
                    }
                )
                strategy._submit(order)
                strategy._seen_orders.add(coid)
                return "APPLIED", "full exit — sustained fade off HoD"

            # codex re-review (High, the operator's call): trail FIRST, market trim second — flipped from the
            # original trim-then-trail order. The trail is what actually PROTECTS the remainder; the trim is
            # a one-shot execution that either happens or doesn't. Placing the trail first and giving IT the
            # coid `client_order_id_for` tracks means a crash between the two submits leaves recovery able to
            # find the trail (the position is NOT naked) even if the trim itself didn't go through or the
            # chain to the next `peak_watch` row never happened — strictly safer than the old ordering, where
            # a crash after the trim but before the trail left the remainder with NO resting stop at all.
            # Sizing the trail for `remaining_qty` (post-trim) while the position still holds the full `qty`
            # (pre-trim) is safe either way — a resting sell stop for FEWER shares than currently held can
            # never oversell, whether the trim has executed yet or not.
            new_trail_coid = self.client_order_id_for(row.manager_id)
            strategy._submit_trailing_stop(
                instrument_id=row.instrument_id, side=close_side, quantity=remaining_qty,
                trail_bps=float(row.params["trail_tight_bps"]), coid=new_trail_coid, manager_id=row.manager_id,
            )

            # Untracked by the recovery mechanism (a disclosed, accepted gap) — a crash between the trail
            # above and this market trim leaves the trim un-retried on restart (recovery finds the tracked
            # trail coid, marks this row APPLIED, no re-chain); the remainder is protected by the trail but
            # never got trimmed down. Fails toward "extra shares still held, but protected", not naked.
            trim_coid = f"{new_trail_coid}-M{trim_count}"
            trim_order = strategy._build_order(
                {
                    "instrument_id": row.instrument_id,
                    "side": close_side,
                    "quantity": trim_qty,
                    "order_type": "market",
                    "time_in_force": "day",
                    "client_order_id": trim_coid,
                    "extended_hours": False,
                    "manager_id": row.manager_id,
                }
            )
            strategy._submit(trim_order)
            strategy._seen_orders.add(trim_coid)

            # Record the spend on THIS row, immediately — do not rely on the successor to carry it
            # (#266, codex review High). The successor is attached further down, AFTER `chain_cancelled`
            # is consulted, so a PEAK toggled off while this apply is in flight spends a trim that no row
            # ever records. `max_trim_count` would then miss it and a later re-arm of the same cycle
            # would hand back a budget that was already used. The row that PERFORMED the trim owns the
            # count, whether or not a successor follows it.
            async with session_factory() as session:
                trim_recorded = await mg.record_trim(session, row.manager_id, trim_count + 1)

            next_params = {
                **row.params, "qty": remaining_qty, "trim_count": trim_count + 1,
                "tightened": True, "current_trail_coid": new_trail_coid,
                # What the position must read before this successor is allowed to act (#255). The trim
                # above is a MARKET order whose fills arrive asynchronously; the successor's own trigger
                # can fire seconds later, while the position still reports its pre-trim size.
                #
                # That is not hypothetical. OKTA, 2026-08-12: trim 1 sold 26 at 13:33:16, trim 2 fired at
                # 13:33:46 and sold 26 AGAIN — sizing off 58 because the first trim's fills (13:33:45
                # onward) had not landed. It should have sold 14. The chain even recorded qty=32 for that
                # successor while the sell went out sized off the stale live read.
                #
                # Re-reading the position more often cannot fix this; the live read IS the stale thing.
                # The successor has to wait for the position to CONFIRM the reduction it expects.
                "awaiting_qty": remaining_qty,
                # The size the position read BEFORE this trim — the upper edge of the wait window.
                "awaiting_from": qty,
            }
            # The operator may have toggled PEAK off while this apply was in flight — a chaining kind
            # must check before handing off, or OFF can never stop it (see `mg.chain_cancelled`).
            async with session_factory() as session:
                if await mg.chain_cancelled(session, "peak_watch", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at):
                    # The OFF-mid-apply branch is EXACTLY the case with no successor to carry the
                    # count, so it needs the consequence spelled out, not the short form.
                    note = "" if trim_recorded else " (trim budget NOT recorded — a re-arm may over-count by one)"
                    return "APPLIED", (
                        f"trimmed {trim_qty}, {remaining_qty} remaining — chain stopped, "
                        f"PEAK was turned off{note}"
                    )
            new_manager_id = str(uuid.uuid4())
            async with session_factory() as session:
                await mg.attach(
                    session, manager_id=new_manager_id, kind="peak_watch",
                    account_id=row.account_id, client_id=row.client_id, instrument_id=row.instrument_id,
                    strategy_id=row.strategy_id, cycle_id=row.cycle_id, leash=row.leash, params=next_params,
                    command_id=f"PKT-{row.manager_id}",
                )
            note = "" if trim_recorded else " (trim budget NOT recorded — a re-arm may over-count by one)"
            return "APPLIED", (
                f"trimmed {trim_qty}, {remaining_qty} remaining — handed off to {new_manager_id}{note}"
            )

        # Blowoff-tighten branch — one-time, only reached if fade didn't already fire above.
        if not bool(row.params.get("tightened", False)):
            ext = _ext_pct(price, _sma_daily(strategy, row.instrument_id))
            slope = _vertical_slope_pct(strategy, row.instrument_id)
            blowoff = (ext is not None and ext >= float(row.params["ext_pct_threshold"])) or (
                slope is not None and slope >= float(row.params["slope_pct_threshold"])
            )
            if blowoff:
                # Same coid discipline as the exit/trim branches above — this replace IS the only order this
                # branch places, so it gets `client_order_id_for`'s value directly (previously `PKW-{id}-
                # tight`, which never matched what recovery looks for).
                new_coid = self.client_order_id_for(row.manager_id)
                strategy._replace_trailing_stop(
                    instrument_id=row.instrument_id, side=close_side, quantity=float(pos.quantity),
                    trail_bps=float(row.params["trail_tight_bps"]), new_coid=new_coid,
                    old_coid=row.params.get("current_trail_coid"), manager_id=row.manager_id,
                )
                # `awaiting_qty` is dropped, not inherited: it belonged to the trim that set it, and
                # that wait is over by the time a blow-off can fire. Carrying it forward would let a
                # later PYRAMID add or an upward reconciliation re-trip the wait on a stale number.
                # (codex review, High.)
                next_params = {
                    k: v for k, v in row.params.items() if k != "awaiting_qty"
                } | {"tightened": True, "current_trail_coid": new_coid}
                async with session_factory() as session:
                    if await mg.chain_cancelled(session, "peak_watch", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at):
                        return "APPLIED", "tightened trail — chain stopped, PEAK was turned off"
                new_manager_id = str(uuid.uuid4())
                async with session_factory() as session:
                    await mg.attach(
                        session, manager_id=new_manager_id, kind="peak_watch",
                        account_id=row.account_id, client_id=row.client_id, instrument_id=row.instrument_id,
                        strategy_id=row.strategy_id, cycle_id=row.cycle_id, leash=row.leash, params=next_params,
                        command_id=f"PKB-{row.manager_id}",
                    )
                return "APPLIED", f"tightened trail — handed off to {new_manager_id}"

        return "FAILED", "re-checked at apply time — neither blowoff nor sustained fade actually confirmed"


register_manager("peak_watch", _PeakWatch())


# --- PYRAMID (#38) manager kind -----------------------------------------------------------------------


_PYRAMID_REQUIRED_PARAMS = (
    "expected_side", "qty", "driver_instrument_id", "add_r_multiple", "max_rungs", "trail_bps",
    "daily_lookback_days", "min_rel_volume", "sustained_bars", "off_hod_pct_threshold", "lower_high_bars",
)


def _looks_like_instrument_id(value: str) -> bool:
    """Is this a `SYMBOL.VENUE` id rather than a bare ticker?

    "Contains a dot" is not enough (codex review, High). `BRK.B` is a real TICKER with a dot in it and no
    venue at all, and `SMH.` has an empty one — both would pass that test and reach
    `InstrumentId.from_str`, where the raw constructor error leaks out through the generic command catch.
    That is the exact error this validation exists to replace.

    Split on the LAST dot, since the symbol may legitimately contain one, and require the venue to look
    like a MIC: uppercase letters, at least three of them (`XNAS`, `XNYS`, `ARCX`). Alphabetic alone is
    not enough — `BRK.B` splits into symbol `BRK` and "venue" `B`, which passes every weaker test.
    """
    symbol, _, venue = value.rpartition(".")
    return bool(symbol) and len(venue) >= 3 and venue.isalpha() and venue.isupper()


def _validate_pyramid_params(params: dict) -> str | None:
    missing = [k for k in _PYRAMID_REQUIRED_PARAMS if k not in params]
    if missing:
        return f"pyramid_watch requires {', '.join(missing)}"
    side = str(params["expected_side"]).upper()
    if side not in ("LONG", "SHORT"):
        return f"invalid expected_side {params.get('expected_side')!r}"
    if side == "SHORT":
        # Same v1 scope cut PEAK made: add-higher/base-break/durability are all long-biased math, not
        # mirrored for SHORT yet. Refuse rather than silently apply wrong-direction signals.
        return "pyramid_watch v1 is LONG-only — signal math isn't mirrored for SHORT yet"
    driver = str(params["driver_instrument_id"]).strip()
    if not driver:
        return "driver_instrument_id must not be empty"
    if not _looks_like_instrument_id(driver):
        # A human sentence, not the raw constructor error. Typing `SMH` used to reach Nautilus and come
        # back as "invalid `InstrumentId` value 'SMH': missing '.' separator between symbol and venue
        # components" — which tells the operator nothing about what to do. The operator hit exactly this on
        # 2026-08-13. The UI now offers a picker so this is unreachable from the toggle, but the attach
        # command takes arbitrary params and a bad one should still explain itself.
        ticker = driver.rpartition(".")[0] or driver
        return (
            f"driver symbol {driver!r} needs its venue — use the full id like {ticker}.XNAS "
            "(the driver picker fills this in for you)"
        )
    return None


class _PyramidWatch:
    """PYRAMID (#38) — progressive confirmation-scaling on a running winner: adds a tranche on each real
    breakout confirmation, raises the trailing stop with each add, caps at `max_rungs`. Origin: MPC
    2026-07-08 (the add made right, +$90 — "I could have bought MORE as it was confirming more"), PENG
    2026-07-08 (the add MISSED — an anti-chase reflex misfired on a real 72-breakout), semis 2026-06-30 (the
    add missed via under-sizing a correctly-read/exited winner, "+$122 vs. +$400-500 proper sizing would've
    made"). PENG's chase-vs-breakout discrimination is the ticket's own named "core test".

    ONE manager kind (mirrors PEAK, not a phase chain) — ADD and EXIT are two branches of one continuous
    "riding" state; `trigger_met` fires on either, `apply()` re-checks fresh and dispatches, EXIT taking
    priority if both hold (same "exit is more urgent than a mere action" precedent PEAK set for
    fade-vs-blowoff).

    `params`: `expected_side` (LONG only), `qty` (current total — kept in sync with LIVE holdings by the
    resync step below, never assumed), `initial_qty` (the ORIGINAL qty at arm time — fixed, tranche sizing
    is pegged to this, not the growing `qty`, so successive adds shrink as a fraction of the current
    position — "equal or decreasing size", ticket's own words), `driver_instrument_id` (a subscribed
    reference symbol, e.g. SMH.XNAS for a semis name — its OWN HoD durability gates the add, its OWN fade
    gates the exit — Operator, 2026-08-03: manual, no auto sector mapping exists), `add_r_multiple` (fraction of
    the ORIGINAL R sized per tranche — R = risk-per-share × `initial_qty`, so `tranche_qty =
    round(add_r_multiple * initial_qty)`, matching the design mock's "+0.75R" label), `initial_risk_per_share`
    (R, computed ONCE at arm time from the position's existing bracket stop — `avg_entry -
    stop.trigger_price` — carried unchanged through the chain, never recomputed), `max_rungs`/`rung_count`
    (budget cap, mirrors PEAK's `trim_max`/`trim_count`), `trail_bps` (width the trail is set to whenever it
    gets resynced), `current_trail_coid` (which resting order IS the stop right now).

    THREE branches, checked in priority order — exit > resync > add:
    1. **Exit**: driver rolls, own fade, or ADX rollover — full market exit, terminal.
    2. **Resync** (`_trail_needs_resync`, codex review Critical): if the resting trail's OWN submitted
       quantity no longer matches the LIVE position (a prior add's fill has landed since the trail was last
       sized), replace it to the VERIFIED live quantity — comparing ground truth (the order's real
       quantity) against ground truth (`pos.quantity`), never a params snapshot or an assumed post-fill
       total. This is what makes the add branch below safe.
    3. **Add**: submits ONLY the market BUY tranche, carrying the tracked `client_order_id_for` coid. The
       trail is DELIBERATELY left untouched — sizing a SELL stop to an ASSUMED post-add total in the SAME
       call as the BUY would risk that stop exceeding live holdings before the fill actually lands (this
       fire-and-forget engine has no synchronous fill confirmation to wait on) — an oversell/short risk that
       does NOT mirror PEAK's trim branch (there, a pre-sized SMALLER stop is always safe; here, a
       pre-sized LARGER one is not). The NEXT tick's resync step catches the trail up once the fill is
       confirmed.

    KNOWN LIMITATIONS (disclosed, not silently missed — codex review):
    - If the process crashes between the add's BUY submit and the chain-attach of the next `pyramid_watch`
      row, recovery finds the tracked BUY coid in cache and marks THIS row APPLIED — but with no
      continuation row, nothing is left watching this position: the resync step never runs, so a filled add
      can ride with its shares uncovered by any dedicated stop indefinitely (the PREVIOUS, already-resting
      stop stays exactly as it was — still valid, just not sized for the new shares). Same class of gap as
      PEAK's own accepted trim-branch limitation (narrow crash window, fails toward stalled-but-not-naked,
      not fixed here — needs multi-coid intent tracking or a recovery-repair hook the framework doesn't have
      yet). A human needs to notice and re-arm.
    - `_replace_trailing_stop` (shared with PEAK) submits the new stop before canceling the old one — during
      a resync this means aggregate RESTING sell-stop quantity can briefly exceed live holdings until the
      old cancel is accepted. Inherited from PEAK's own already-reviewed retighten design this session (a
      resting stop that never fires costs nothing to replace); not new to this branch, not re-litigated here.
    """

    def validate_params(self, params: dict) -> str | None:
        return _validate_pyramid_params(params)

    def client_order_id_for(self, manager_id: str) -> str:
        return f"PY-{manager_id[:20]}"

    async def _add_condition(self, strategy, row: object) -> bool:
        if int(row.params["rung_count"]) >= int(row.params["max_rungs"]):
            return False
        instrument_id = row.instrument_id
        driver_id = str(row.params["driver_instrument_id"])
        off_hod_pct = float(row.params["off_hod_pct_threshold"])
        lower_high_bars = int(row.params["lower_high_bars"])
        if not _range_high_break(
            strategy, instrument_id,
            daily_lookback_days=int(row.params["daily_lookback_days"]),
            min_rel_volume=float(row.params["min_rel_volume"]),
            sustained_bars=int(row.params["sustained_bars"]),
        ):
            return False
        # Scoped to THIS manager's arm, not to the session open (#283 — the defect #253 fixed in PEAK).
        # Measured session-wide, "2% off the high" is true of any name that is off its opening high, so
        # arming on a name that peaked hours ago finds the fade already satisfied and never adds at all.
        # Derived ONCE and reused for all four checks: two derivations of one fact will disagree.
        armed_ns = _armed_at_ns(row)
        hod = _session_high(strategy, instrument_id, since_ns=armed_ns)
        if hod is None or _sustained_fade(
            strategy, instrument_id, hod, off_hod_pct=off_hod_pct, lower_high_bars=lower_high_bars,
            since_ns=armed_ns,
        ):
            return False  # not durable at its own HoD
        driver_hod = _session_high(strategy, driver_id, since_ns=armed_ns)
        if driver_hod is None or _sustained_fade(
            strategy, driver_id, driver_hod, off_hod_pct=off_hod_pct, lower_high_bars=lower_high_bars,
            since_ns=armed_ns,
        ):
            return False  # driver not confirming its own HoD
        daily = _daily_bars(strategy, instrument_id, _PYRAMID_ADX_PERIOD * 3)
        adx = _wilder_adx(daily, period=_PYRAMID_ADX_PERIOD)
        return _adx_is_rising(adx["adx"], lookback=_PYRAMID_ADX_RISING_LOOKBACK)

    async def _exit_condition(self, strategy, row: object) -> bool:
        instrument_id = row.instrument_id
        driver_id = str(row.params["driver_instrument_id"])
        off_hod_pct = float(row.params["off_hod_pct_threshold"])
        lower_high_bars = int(row.params["lower_high_bars"])
        # Same arm scoping as `_add_condition` (#283), and this is the damaging direction: measured
        # session-wide, arming on a name that already faded fires the exit on the first dispatch tick —
        # the manager sells the position it was armed to scale INTO. Derived once, used four times.
        armed_ns = _armed_at_ns(row)
        hod = _session_high(strategy, instrument_id, since_ns=armed_ns)
        if hod is not None and _sustained_fade(
            strategy, instrument_id, hod, off_hod_pct=off_hod_pct, lower_high_bars=lower_high_bars,
            since_ns=armed_ns,
        ):
            return True
        driver_hod = _session_high(strategy, driver_id, since_ns=armed_ns)
        if driver_hod is not None and _sustained_fade(
            strategy, driver_id, driver_hod, off_hod_pct=off_hod_pct, lower_high_bars=lower_high_bars,
            since_ns=armed_ns,
        ):
            return True
        daily = _daily_bars(strategy, instrument_id, _PYRAMID_ADX_PERIOD * 3)
        adx = _wilder_adx(daily, period=_PYRAMID_ADX_PERIOD)
        return _adx_is_falling(adx["adx"], lookback=_PYRAMID_ADX_RISING_LOOKBACK)

    async def _trail_needs_resync(self, strategy, row) -> object:
        """codex review (Critical): the resting trail's quantity must NEVER exceed the LIVE position — a
        SELL stop sized above current holdings risks an oversell/short if it triggers before a pending BUY
        add has actually filled (Alpaca has no synchronous fill-confirmation this fire-and-forget engine can
        wait on). Compares the order's OWN submitted quantity (ground truth) against `pos.quantity` (also
        live) — NOT any params snapshot, which could itself be a stale projection. Returns the resting
        trail order if it's out of sync (needs replacing to match live qty), else `None`. This is what makes
        the add branch below safe: it submits the BUY WITHOUT touching the trail at all; THIS check, run on
        every subsequent tick, catches the trail up ONLY once the fill has actually landed and `pos.quantity`
        genuinely reflects it — never a moment sooner."""
        coid = row.params.get("current_trail_coid")
        if not coid:
            return None
        order = strategy._lookup_order(coid)
        if order is None or not order.is_open:
            return None
        expected_side = str(row.params["expected_side"]).upper()
        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is None:
            return None
        return order if float(order.quantity) != float(pos.quantity) else None

    async def trigger_met(self, strategy, row) -> bool:
        expected_side = str(row.params["expected_side"]).upper()
        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is None:
            # Flat — the position closed, by this chain or any other route. FIRE so `apply()` runs and
            # terminalizes the row; it returns a terminal FAILED on flat. Returning False here is the
            # defect #255 High 1 fixed in PEAK and never applied to PYRAMID: the row sat ARMED forever and
            # "is this manager alive?" could not be answered from state alone.
            #
            # DELIBERATELY ABOVE THE RTH GATE. A position closing while the market is shut is the ordinary
            # case, and gating it would leave the row waiting for a session it will never care about
            # (codex review, High). Trail resync can wait for RTH; stale flat state cannot.
            return True
        # No manager ACTS outside regular hours (#255 High 4, applied to the CLASS not the instance).
        # PYRAMID cancels a resting trail and submits a market order exactly as PEAK does, so the same
        # after-hours reading strips protection and leaves the position naked overnight having sold
        # nothing. Fixing only the manager the ticket named is how the fade-window bug came to exist in
        # two managers at once (#253, then #283).
        if not _us_market_open(strategy.clock.timestamp_ns()):
            return False
        if strategy._last_price_for(row.instrument_id) is None:
            return False
        if await self._exit_condition(strategy, row):
            return True
        if await self._trail_needs_resync(strategy, row) is not None:
            return True
        return await self._add_condition(strategy, row)

    async def apply(self, strategy, row):
        import uuid

        from api import managers as mg
        from api.db.engine import session_factory

        # Cycle-drift guard (same discipline as `_PeakWatch`/`_StopReenterWatch`/`_StopReenterRearm`) — a
        # manager attached against one cycle must not act against a DIFFERENT one that's since opened.
        if row.cycle_id is not None and strategy._trade_cycles:
            current = strategy.current_cycle_for(row.instrument_id, row.strategy_id)
            if current is None or current.cycle_id != row.cycle_id:
                return "FAILED", "the position's cycle changed since this was queued — re-read and retry"

        expected_side = str(row.params["expected_side"]).upper()
        close_side = "SELL" if expected_side == "LONG" else "BUY"
        add_side = "BUY" if expected_side == "LONG" else "SELL"

        pos = strategy._position_for(row.instrument_id, row.strategy_id, expected_side)
        if pos is None:
            # A FLIP is not a CLOSE, and `_position_for` cannot tell them apart — it filters by expected
            # side, so both look like None (codex review, High). Both retire the row, because the manager's
            # premise is void either way, but they must not be reported as the same event: an action log
            # that calls a reversal "already flat" misleads whoever reads it about whether the name was
            # exited or turned around. PEAK has drawn this distinction since #274.
            if _any_position_open(strategy, row.instrument_id, row.strategy_id):
                return "FAILED", "position flipped side since this was armed — the watch no longer applies"
            return "FAILED", "position already flat — nothing to watch (exited via another path)"
        if strategy._last_price_for(row.instrument_id) is None:
            return "FAILED", "no live price to evaluate"

        # Exit takes priority — re-checked fresh (don't trust the trigger_met snapshot from a possibly
        # earlier tick, same discipline every manager in this framework already follows).
        if await self._exit_condition(strategy, row):
            old_coid = row.params.get("current_trail_coid")
            old_order = strategy._lookup_order(old_coid) if old_coid else None
            if old_order is not None and old_order.is_open:
                strategy._cancel(old_order)
            qty = float(pos.quantity)
            coid = self.client_order_id_for(row.manager_id)
            order = strategy._build_order(
                {
                    "instrument_id": row.instrument_id,
                    "side": close_side,
                    "quantity": qty,
                    "order_type": "market",
                    "time_in_force": "day",
                    "client_order_id": coid,
                    "extended_hours": False,
                    "manager_id": row.manager_id,
                }
            )
            strategy._submit(order)
            strategy._seen_orders.add(coid)
            return "APPLIED", "full exit — driver rolled, own fade, or ADX rollover"

        # codex review (Critical): resync takes priority over a NEW add — if a prior add's fill has landed
        # since this row was attached, the resting trail is now UNDER-sized (protects fewer shares than
        # actually held); true it up to the VERIFIED live quantity before considering anything else. Re-
        # checked fresh, not trusted from trigger_met's snapshot.
        stale_trail = await self._trail_needs_resync(strategy, row)
        if stale_trail is not None:
            live_qty = float(pos.quantity)
            new_coid = self.client_order_id_for(row.manager_id)
            # codex review (Medium, disclosed not silently missed): `_replace_trailing_stop` submits the new
            # stop THEN cancels the old one (PEAK's own already-reviewed retighten ordering, reused as-is
            # here) — aggregate RESTING sell-stop quantity can briefly exceed live holdings until that
            # cancel is accepted. Inherited from a shared, already-accepted design choice, not new to this
            # call site; a true fix would need to change how `_replace_trailing_stop` itself sequences for
            # every caller (PEAK included), out of scope for this fix.
            strategy._replace_trailing_stop(
                instrument_id=row.instrument_id, side=close_side, quantity=live_qty,
                trail_bps=float(row.params["trail_bps"]), new_coid=new_coid,
                old_coid=row.params.get("current_trail_coid"), manager_id=row.manager_id,
            )
            next_params = {**row.params, "qty": live_qty, "current_trail_coid": new_coid}
            new_manager_id = str(uuid.uuid4())
            # The operator may have toggled this off while the apply was in flight. A CHAINING kind must
            # check before handing off, or OFF can never stop it — cancelling the one row the UI named
            # leaves the successor armed and the toggle springs back to ON. PEAK already carries this;
            # `family_of` makes the watch/rearm pair count as ONE chain.
            async with session_factory() as session:
                if await mg.chain_cancelled(
                    session, "pyramid_watch", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at
                ):
                    # This branch REPLACED THE TRAIL above — say that, not "added" (#652 item 3: the
                    # two branches' outcome strings were swapped, so a real market BUY was durably
                    # journaled as a resync and vice versa).
                    return "APPLIED", "trail resynced, but PYRAMID was turned off — chain stops here"
            async with session_factory() as session:
                await mg.attach(
                    session, manager_id=new_manager_id, kind="pyramid_watch",
                    account_id=row.account_id, client_id=row.client_id, instrument_id=row.instrument_id,
                    strategy_id=row.strategy_id, cycle_id=row.cycle_id, leash=row.leash, params=next_params,
                    command_id=f"PYR-{row.manager_id}",
                )
            return "APPLIED", f"resynced trail to live qty {live_qty} — handed off to {new_manager_id}"

        if await self._add_condition(strategy, row):
            rung_count = int(row.params["rung_count"])
            initial_qty = float(row.params["initial_qty"])
            add_qty = round(float(row.params["add_r_multiple"]) * initial_qty)
            if add_qty <= 0:
                return "FAILED", "re-checked at apply time — computed add quantity is zero, refusing"

            # codex review (Critical): the trail is DELIBERATELY left untouched here — it stays sized for
            # whatever it was already correctly protecting (never more than live holdings). Growing it to
            # the ASSUMED post-add total in this same synchronous call would size a SELL stop above what's
            # actually held until the fill lands (this engine has no way to wait for that fill), risking an
            # oversell/short if the stop triggers first. The BUY gets the tracked, crash-recovery-visible
            # coid instead (it's now the position-increasing, highest-consequence action this tick).
            # `_trail_needs_resync`, re-checked every subsequent tick, catches the trail up to the VERIFIED
            # live quantity once — and only once — the fill has actually landed.
            add_coid = self.client_order_id_for(row.manager_id)
            add_order = strategy._build_order(
                {
                    "instrument_id": row.instrument_id,
                    "side": add_side,
                    "quantity": add_qty,
                    "order_type": "market",
                    "time_in_force": "day",
                    "client_order_id": add_coid,
                    "extended_hours": False,
                    "manager_id": row.manager_id,
                }
            )
            strategy._submit(add_order)
            strategy._seen_orders.add(add_coid)

            next_params = {**row.params, "rung_count": rung_count + 1}  # qty/current_trail_coid: resync's job
            new_manager_id = str(uuid.uuid4())
            # The operator may have toggled this off while the apply was in flight. A CHAINING kind must
            # check before handing off, or OFF can never stop it — cancelling the one row the UI named
            # leaves the successor armed and the toggle springs back to ON. PEAK already carries this;
            # `family_of` makes the watch/rearm pair count as ONE chain.
            async with session_factory() as session:
                if await mg.chain_cancelled(
                    session, "pyramid_watch", row.instrument_id, row.strategy_id, row.cycle_id, row.created_at
                ):
                    # This branch SUBMITTED A MARKET BUY above — say that, not "trail resynced"
                    # (#652 item 3, the other half of the swap).
                    return "APPLIED", "added, but PYRAMID was turned off — chain stops here"
            async with session_factory() as session:
                await mg.attach(
                    session, manager_id=new_manager_id, kind="pyramid_watch",
                    account_id=row.account_id, client_id=row.client_id, instrument_id=row.instrument_id,
                    strategy_id=row.strategy_id, cycle_id=row.cycle_id, leash=row.leash, params=next_params,
                    command_id=f"PYA-{row.manager_id}",
                )
            return "APPLIED", f"added {add_qty} (rung {rung_count + 1}) — handed off to {new_manager_id}"

        return "FAILED", "re-checked at apply time — neither exit, resync, nor add actually confirmed"


register_manager("pyramid_watch", _PyramidWatch())


#: How long a node may take to bring its strategies up before silence is a fault rather than boot.
#:
#: 330 = 180 (`timeout_connection`) + 120 (`timeout_reconciliation`) + 10 (Nautilus's
#: `timeout_portfolio`) + 15 (one `_INERT_CHECK_SECS` period) + 5 slack — THE SUM OF THE SERIAL BOOT
#: PHASES, not a round number (#954). Until #954 this was 180 against a 190 s budget (60 + 120 + 10),
#: already undersized: a boot that used its full reconciliation budget would have been declared
#: INERT and then "recovered". The two errors are not equally bad. Firing LATE costs 150 s of silence
#: on a node that is already dead (measured remediation on #954 was 8 minutes from the alarm, by a
#: human — nothing restarts on this signal; it writes ui:state:health and logs). Firing EARLY declares
#: a node inert at the moment it was about to become ready, which is strictly worse than doing
#: nothing, and a false alarm here teaches the operator to ignore the real one. `test_connection_timeout.py`
#: pins the arithmetic against the literals in `build_node`; `_boot_budget_or_refuse` enforces it at
#: runtime. Trim one number and the other has to move.
_INERT_AFTER_SECS = 330.0
_INERT_CHECK_SECS = 15.0


def _start_inert_watchdog(node) -> None:
    """Report a node that is up but has published nothing (#613).

    THE FAILURE THIS EXISTS FOR, measured 2026-08-27. Reconciliation refused a quantity mismatch —
    correctly, since guessing which side is right could double a position — and the node then:

        [ERROR] TradingNode: Execution state could not be reconciled
        [INFO]  TradingNode: RUNNING                     <- logged anyway
        COCKPIT-001.MANUAL: READY                        <- and never RUNNING

    Strategies stop at READY, so `on_start` never runs, so the Redis writer thread is never created,
    so NOT ONE `ui:state:*` key is ever written. `/positions` served 0 against 22 held at the broker
    and a lane due in 30 minutes would not have fired. The engine kept polling the account every
    3.5s, so the log looked alive; py-spy showed ONE thread where a healthy node has six. It was
    found by a human looking at a screenshot.

    IT WATCHES THE SYMPTOM, NOT NAUTILUS INTERNALS. The first version called
    `node.trader.strategy_states()` from this thread; Nautilus does not document that as thread-safe,
    so it could read a torn state or race internal mutation (codex, implementation review). The
    observable fault IS "the writer never published", so that is what is measured — a Redis key, from
    a thread that owns nothing.

    `ui:state:positions` is the probe because the REAL writer owns it and this watchdog never writes
    it. Probing `ui:state:health` would see our own frame and call the fault healthy.

    WHY THE ORDINARY HEALTH FRAME CANNOT DO THIS. It is published BY the thread that never starts —
    the reporting path is downstream of the break.
    """
    import threading

    def _watch() -> None:
        started = _time.time()
        announced = False
        # ONE CLIENT for the life of the thread. A fresh connection per check is needless churn on a
        # path whose whole job is to stay boring while everything else is broken.
        r = _inert_redis()
        while True:
            _time.sleep(_INERT_CHECK_SECS)
            try:
                alive = _writer_is_publishing(r)
                if alive:
                    if announced:
                        _log.warning("engine recovered: the data plane is publishing again")
                        _clear_inert_state(r)
                        announced = False
                    continue
                if _time.time() - started < _INERT_AFTER_SECS:
                    continue
                _log.error(
                    "ENGINE INERT: the process is up but no ui:state frame has ever been published. "
                    "Reconciliation almost certainly refused — see #613. No lane will fire until "
                    "this is resolved."
                )
                # REPUBLISHED EVERY CYCLE, not once, because the frame carries a TTL. A one-shot
                # announcement plus a TTL would expire into silence, which is the state that started
                # all this.
                _publish_inert_state(r)
                announced = True
            except Exception as exc:  # noqa: BLE001 — a watchdog must never take down the node
                _log.warning("inert watchdog check failed (%r)", exc)

    threading.Thread(target=_watch, name="inert-watchdog", daemon=True).start()


def _inert_redis():
    import redis

    return redis.Redis.from_url(os.environ.get("KUMO_REDIS_URL", "redis://redis:6379"))


#: How stale the real writer's plane may be before it counts as not publishing. The writer emits on
#: every snapshot tick, so a minute of silence is already far outside normal.
_WRITER_STALE_SECS = 90


def _writer_is_publishing(r) -> bool:
    """Is the REAL writer thread publishing NOW?

    FRESHNESS, NOT EXISTENCE, and that distinction is load-bearing (codex, second review). `exists()`
    proves only that some writer wrote the key at some point — and Redis outlives the process, so a
    stale `ui:state:positions` from the PREVIOUS run would mask an inert node completely. The alarm
    would be suppressed by the corpse of the last healthy boot.

    `ui:state:positions` specifically: the real writer owns it and this watchdog never writes it, so
    the probe cannot be satisfied by our own alarm frame.

    Unreadable, unparseable or undated all count as NOT publishing. This is a fault detector; the
    safe direction is to report rather than to assume health.
    """
    try:
        raw = r.get("ui:state:positions")
        if not raw:
            return False
        ts = json.loads(raw).get("ts")
        if not isinstance(ts, (int, float)) or ts <= 0:
            return False
        return (_time.time_ns() - ts) / 1e9 < _WRITER_STALE_SECS
    except Exception:                                                   # noqa: BLE001
        return False


#: The inert frame expires. A node that dies while inert must not leave a permanent alarm behind, and
#: a fault that has genuinely cleared must not need a human to delete a key.
_INERT_TTL_SECS = 60


def _publish_inert_state(r) -> None:
    """Write the health frame DIRECTLY, bypassing the writer thread that never started.

    TTL'd and OWNED. Without a TTL a stale ENGINE_INERT outlives the process; without the ownership
    marker, clearing it could delete a healthy frame the real writer had since published (codex,
    implementation review).
    """
    try:
        r.set("ui:state:health", json.dumps({
            "engine_ok": False,
            "condition": "ENGINE_INERT",
            "written_by": _INERT_OWNER,
            "reason": (
                "the process is up but no data plane has ever been published — reconciliation "
                "refused, so on_start never ran and no lane will fire (#613)"
            ),
            "last_tick_ts": 0,
            "ts": _time.time_ns(),
        }), ex=_INERT_TTL_SECS)
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not publish the inert health frame (%r)", exc)


#: Marks a health frame as this watchdog's, so recovery clears only our own alarm.
_INERT_OWNER = "inert-watchdog"


#: Compare-and-delete, server side. A GET-then-DELETE has a window in which the real writer publishes
#: a healthy frame and the watchdog deletes it — the exact class of bug being fixed here (codex,
#: second review), so it does not get to survive in the fix.
#:
#: DECODES AND COMPARES THE FIELD. The first version used `string.find` on the raw frame, which would
#: delete ANY health frame containing that text anywhere — weaker than it looked, and the same "close
#: enough" that produced the blind delete it replaced (codex, third review).
_CLEAR_IF_OURS = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, frame = pcall(cjson.decode, raw)
if ok and frame['written_by'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _clear_inert_state(r) -> None:
    """Delete the alarm ONLY if it is still ours, atomically."""
    try:
        r.eval(_CLEAR_IF_OURS, 1, "ui:state:health", _INERT_OWNER)
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not clear the inert health frame (%r)", exc)


def main() -> None:
    # FIRST line of the process (#329). Before build_node(), because a node that fails to construct is
    # precisely when "what code is this?" matters most — and on 2026-08-18 that question cost two wrong
    # diagnoses and a rollback that could not have helped, since the running image was 59 lines behind main.
    logging.getLogger(__name__).warning(build_stamp_line())
    node = build_node()
    # BEFORE run(), because run() blocks and the state this watches for is one the node reaches
    # while inside it (#613).
    _start_inert_watchdog(node)
    try:
        node.run()  # blocking; owns its loop + signal handling (standalone process)
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
