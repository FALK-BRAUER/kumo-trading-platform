"""The engine must issue one envelope write per CHANGE, not one per cycle per tick (#564).

MEASURED, kumo-paper, 2026-08-26 13:35 — the first minutes of the US open:

    cycle-envelope write failed: TimeoutError()      x21,731 in ~20 minutes
    alpaca GET /v2/positions: ConnectionTimeoutError — retrying (1/2)
    MOMENTUM-002 risk|source refresh failed: TimeoutError:
    MOMENTUM-002 state|session outcome — TRADING: NO DECISION — bar coverage 32% below the 80% floor

Those four lines are ONE failure. `_publish_trades` runs on the event-driven fold, and it spawned an
unbounded `run_coroutine_threadsafe` upsert for EVERY projected cycle on EVERY tick — 31 cycles per
event. At the open the event rate spiked, the 30 pool slots (20 + 10 overflow, sized in #542) filled,
and every further write sat 30s waiting for a connection that never came. The engine's DB pool and its
event loop were saturated by writes that had nothing new to say, so MOMENTUM-002's source refresh timed
out five seconds before its decision, fell back to a stale 107-name pool, found bars for 34 of them and
correctly refused to rank a universe it could not see. Coverage had been 95-98% on 64-65 names.

#542 raised the pool ceiling and its own comment said "RAISING IT IS HALF A FIX". This is the other
half. The ceiling was never the problem — the write AMPLIFICATION was.

THE INVARIANT, aimed at the class and not at this incident: the number of envelope writes the engine
issues is bounded by the number of envelope CHANGES, not by ticks x cycles. `CycleEnvelopeStore`'s own
docstring already said "call `upsert` on transitions"; the caller wrote on every tick. Two derivations
of one contract, disagreeing, with only the venue to notice.

`EnvelopeRow` is a frozen dataclass whose `last_event_ts` comes from the projection's fold state and
NOT from the wall clock, so an unchanged cycle re-derives byte-identically. That is what makes equality
a sound coalescing key, and `test_an_unchanged_cycle_re_derives_EQUAL` pins it — if that ever stops
being true this whole optimisation silently reverts to writing every tick.
"""

from __future__ import annotations

import threading

from nautilus_trader.model.identifiers import ClientId, StrategyId

from api import engine_node
from api.cycle_store import EnvelopeRow, envelope_from_dto, envelope_key
from api.engine_node import UiFeedStrategy
from api.models import TradeDTO
from api.trade_cycle import TradeCycleProjection


class _Log:
    def __init__(self):
        self.errors: list[str] = []

    def error(self, m):
        self.errors.append(str(m))

    def warning(self, m):
        pass


class _Clock:
    @staticmethod
    def timestamp_ns():
        return 1_786_000_000_000_000_000


class _Store:
    """Bound to the REAL coroutine signature. `upsert` must BE a coroutine function, because production
    hands its return value to `run_coroutine_threadsafe`, which rejects anything else — a plain `def`
    double would accept a caller that production cannot run."""

    async def upsert(self, row: EnvelopeRow) -> None:  # pragma: no cover - never awaited here
        return None


class _Fut:
    """`run_coroutine_threadsafe` returns a concurrent.futures.Future; production only calls
    `add_done_callback` on it. Fire the callback immediately with a SUCCESSFUL result, which is what a
    healthy store does — the failure direction is pinned separately below."""

    def __init__(self, ok=True):
        self._ok = ok

    def add_done_callback(self, cb):
        cb(self)

    def result(self):
        if not self._ok:
            raise TimeoutError()


class _Proj:
    """Re-emits like the real fold: identical CONTENT, but `last_event_ts` strictly advancing every call.

    THE FIRST VERSION OF THIS DOUBLE RETURNED THE SAME DTOs EVERY TIME, and that is the whole reason the
    first version of the #564 fix passed its tests while being a no-op in production. `trade_cycle`
    advances the fold clock on every emit by design, so an "unchanged" book still yields a different
    `EnvelopeRow` each tick — and the fix coalesced on whole-row equality, which therefore never matched.

    A double that cannot re-emit the way production re-emits cannot test a re-emission optimisation.
    `test_the_real_projection_advances_last_event_ts_every_emit` holds this honest.
    """

    def __init__(self, dtos):
        self.dtos = dtos
        self._emit = 0

    def project(self, cache, now_ns=0):
        self._emit += 1
        return [d.model_copy(update={"last_event_ts": d.last_event_ts + self._emit})
                for d in self.dtos]


class _Fake:
    def __init__(self, dtos):
        self.clock = _Clock()
        self.log = _Log()
        self.cache = object()
        self._cycles_seeded = True
        self._trade_cycles = {"MANUAL-001": _Proj(dtos)}
        self._cycle_store = _Store()
        self._loop = object()
        self.published: list = []
        self._last_good_trades: list = []
        self._broker_stop_prices: dict = {}
        self._realized_periods = None
        #: The closed-leg registry and its seed status (#846) — set in production's `__init__`, present
        #: here for the same reason as `_realized_periods` above: the double must represent production.
        self._closed_legs: dict = {}
        self._legs_restored = 0
        self._legs_seed_stopped_at = None
        self._legs_seeded = False
        self._envelope_written: dict = {}
        self._envelope_pending: dict = {}
        self._attempt_seq: int = 0
        self._envelope_lock = engine_node._new_envelope_lock()
        #: `_publish_trades` projects UNDER THIS LOCK (#568) — `project()` mutates, so it is a
        #: writer. Built the way production builds it.
        self._projection_lock = threading.RLock()

    def _publish(self, key, payload):
        self.published.append((key, payload))

    def _mark_cycle_financials(self, dtos):
        pass

    def _mark_broker_stop_prices(self, dtos):
        pass

    _session_realized = UiFeedStrategy._session_realized
    _realized_windows = UiFeedStrategy._realized_windows
    _lane_flows = UiFeedStrategy._lane_flows  # the frame carries lane flows too (#699 a)
    _realized_legs_status = UiFeedStrategy._realized_legs_status
    _on_envelope_write = UiFeedStrategy._on_envelope_write


class _RealishCache:
    """Enough native cache for the REAL `TradeCycleProjection` to fold one open position.

    Only the five methods `trade_cycle` actually calls. The position exposes the ten attributes it reads,
    with the SHAPES Nautilus uses — `side`/`quantity` as objects with the attributes the projection reads,
    not bare strings — so the projection exercises its real paths rather than a simplified one.
    """

    class _Pos:
        def __init__(self):
            from types import SimpleNamespace
            self.instrument_id = "AAPL.XNAS"
            self.strategy_id = "MANUAL-001"
            self.id = "AAPL.XNAS-MANUAL-001"        # CycleLeg.from_position reads it (#846)
            self.is_open = True
            self.side = SimpleNamespace(name="LONG")
            self.quantity = 10
            self.avg_px_open = 100.0
            self.realized_pnl = None
            self.ts_opened = 500
            self.ts_closed = None
            self.ts_last = 500

    class _Acct:
        id = "SIM-001"

    def __init__(self):
        self._pos = self._Pos()

    def accounts(self):
        return [self._Acct()]

    def positions_open(self):
        return [self._pos]

    def orders_open(self):
        return []

    def position(self, pos_id):
        return self._pos

    def position_snapshots(self, pos_id):
        return []


def _dto(sym: str, *, ts: int = 1000, state: str = "HELD") -> TradeDTO:
    """Every REQUIRED field supplied explicitly. `TradeDTO` rejects a partial dict, which is the point:
    a double built from the fields this test happens to care about would not be a thing production can
    project, and four defects on 2026-08-14 hid behind exactly that."""
    return TradeDTO(
        cycle_id=f"cyc-{sym}", account_id="ACC", client_id="ALPACA",
        instrument_id=f"{sym}.XNAS", strategy_id="MANUAL-001",
        opened_ts=1, closed_ts=None, state=state, last_event_ts=ts,
        side="LONG", quantity="10", is_capital_deployed=True, is_engaged=True,
        realized_pnl="0.0", leg_count=1,
    )


def _spy(monkeypatch, *, ok=True):
    """Count writes at the exact call production makes, closing each coroutine so an unawaited-coroutine
    warning cannot mask a miscount."""
    seen: list = []

    def _rcts(coro, loop):
        coro.close()
        seen.append(coro)
        return _Fut(ok=ok)

    monkeypatch.setattr(engine_node.asyncio, "run_coroutine_threadsafe", _rcts)
    return seen


# ==================================================================================================
# The fixture's own property FIRST. Without this, every "writes nothing" assertion below would pass
# just as happily against a harness that can never write at all.
# ==================================================================================================


def test_the_fixture_can_actually_produce_writes(monkeypatch):
    seen = _spy(monkeypatch)
    fake = _Fake([_dto("AAPL"), _dto("MSFT"), _dto("NVDA")])
    UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 3, (
        f"the first tick wrote {len(seen)} envelopes for 3 new cycles — the harness cannot reach the "
        f"write path, so nothing else in this file means anything")


def test_the_real_projection_advances_last_event_ts_every_emit():
    """THE PREMISE THE WHOLE FIX RESTS ON, read off the REAL projection and not off a double.

    `trade_cycle._build_dto` does `cycle.last_event_ts = max(now_ns, native_ts, cycle.last_event_ts + 1)`
    on every emit, deliberately, so each transition strictly out-orders the previous durable write. So an
    unchanged book NEVER re-derives an equal `EnvelopeRow`, and coalescing on row equality can never fire.

    The first version of this fix did exactly that and shipped a no-op. This test is what makes that
    impossible to do again: if someone "simplifies" `envelope_key` back to row equality, the content-key
    assertion below fails and says why.
    """
    proj = TradeCycleProjection(ClientId("SIM"), StrategyId("MANUAL-001"))
    cache = _RealishCache()
    first = proj.project(cache, now_ns=1_000)
    second = proj.project(cache, now_ns=1_000)   # SAME clock, SAME cache — nothing happened in between
    assert first and second, "the cache double produced no cycle; this test would prove nothing"
    a, b = envelope_from_dto(first[0]), envelope_from_dto(second[0])
    assert a != b, (
        "the real projection re-derived an EQUAL envelope for an unchanged book — if this is now true, "
        "`envelope_key` can be simplified to row equality; until then it must not be")
    assert a.last_event_ts != b.last_event_ts, "the fold clock did not advance"
    assert envelope_key(a) == envelope_key(b), (
        "two emits of an unchanged cycle produced different CONTENT keys — coalescing cannot work and "
        "the engine is back to writing every cycle every tick")


def test_the_content_key_still_separates_a_REAL_transition():
    """The discriminating half. A key that collapses everything would suppress every write and the
    envelope would never advance at all — a quieter outage than the one being fixed."""
    r = envelope_from_dto(_dto("AAPL"))
    assert envelope_key(r) != envelope_key(envelope_from_dto(_dto("AAPL", state="CLOSED")))


# ==================================================================================================
# THE DEFECT
# ==================================================================================================


def test_an_unchanged_book_writes_NOTHING_on_the_next_tick(monkeypatch):
    seen = _spy(monkeypatch)
    fake = _Fake([_dto("AAPL"), _dto("MSFT"), _dto("NVDA")])
    UiFeedStrategy._publish_trades(fake)
    seen.clear()
    UiFeedStrategy._publish_trades(fake)
    assert seen == [], (
        f"{len(seen)} envelope writes for a book in which nothing changed — this is the amplification "
        f"that exhausted the pool at the open")


def test_only_the_CHANGED_cycle_is_written(monkeypatch):
    """A detector that suppresses everything is not a detector. The changed cycle must still reach the
    store, or the envelope diverges from the projection and the restart seed restores a stale boundary."""
    seen = _spy(monkeypatch)
    fake = _Fake([_dto("AAPL"), _dto("MSFT"), _dto("NVDA")])
    UiFeedStrategy._publish_trades(fake)
    seen.clear()
    fake._trade_cycles["MANUAL-001"].dtos = [
        _dto("AAPL"), _dto("MSFT", ts=2000, state="CLOSED"), _dto("NVDA")]
    UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 1, f"expected exactly the one changed cycle to be written, got {len(seen)}"


def test_writes_are_bounded_by_CHANGES_not_by_TICKS_TIMES_CYCLES(monkeypatch):
    """The class-level assertion, at the incident's own shape: 31 cycles, 50 fold ticks, one real
    transition. The bug issues 1,550 writes into a 30-slot pool. The fix issues 32."""
    dtos = [_dto(f"SYM{i}") for i in range(31)]
    seen = _spy(monkeypatch)
    fake = _Fake(dtos)
    for tick in range(50):
        if tick == 25:
            # A REAL transition — a state change. Bumping only `last_event_ts` is NOT one: the fold does
            # that on every emit, and treating it as news is the bug this file exists to prevent.
            fake._trade_cycles["MANUAL-001"].dtos = (
                [_dto("SYM0", state="CLOSED")] + dtos[1:])
        UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 32, (
        f"{len(seen)} writes for 31 cycles + 1 transition over 50 ticks — the engine's DB pool has 30 "
        f"slots and a 30s acquire timeout, so this number IS the outage")


def test_a_FAILED_write_is_RETRIED_on_the_next_tick(monkeypatch):
    """The dangerous direction. Caching what we TRIED to write rather than what the store ACCEPTED
    would make a failed write permanent: the engine would look durable while the envelope silently
    diverged, which is the exact hazard the done-callback was added to prevent."""
    seen = _spy(monkeypatch, ok=False)
    fake = _Fake([_dto("AAPL")])
    UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 1
    assert fake.log.errors, "a failing store wrote no error — the failure is invisible"
    UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 2, (
        "the write failed and was never retried — the coalescing cache recorded an attempt as if it "
        "were a durable write")


def test_the_cache_does_not_grow_without_bound(monkeypatch):
    """It is keyed by cycle_id and the engine runs for weeks. Cycles that leave the book must leave the
    cache, or this is a slow leak replacing a fast one."""
    _spy(monkeypatch)
    fake = _Fake([_dto(f"SYM{i}") for i in range(20)])
    UiFeedStrategy._publish_trades(fake)
    fake._trade_cycles["MANUAL-001"].dtos = [_dto("SYM0")]
    UiFeedStrategy._publish_trades(fake)
    assert set(fake._envelope_written) == {"cyc-SYM0"}, (
        f"cache holds {len(fake._envelope_written)} entries for a 1-cycle book: {sorted(fake._envelope_written)}")


# ==================================================================================================
# THE FAILURE PATH — all four raised by codex against the first version of this fix, which had none
# of them. Coalescing alone makes the healthy case cheap; these are what stop the SICK case from
# re-creating the outage, which is the condition the fix exists to survive.
# ==================================================================================================


class _PendingFut:
    """A write that has been issued and has not come back — a slow or wedged database. Production only
    calls `add_done_callback`, so holding the callback is exactly what a pending future looks like."""

    def __init__(self):
        self.cb = None
        #: Set before firing `cb`. A double that could only come back FAILED cannot express a late write
        #: that SUCCEEDED — which is the only case that can regress the cache, i.e. the one worth testing.
        self.ok = True

    def add_done_callback(self, cb):
        self.cb = cb

    def result(self):
        if not self.ok:
            raise TimeoutError()

    def land(self):
        """Complete the write and run production's callback, as the loop thread would."""
        self.cb(self)


def _pending_spy(monkeypatch):
    futs: list[_PendingFut] = []

    def _rcts(coro, loop):
        coro.close()
        f = _PendingFut()
        futs.append(f)
        return f

    monkeypatch.setattr(engine_node.asyncio, "run_coroutine_threadsafe", _rcts)
    return futs


def test_one_in_flight_write_per_cycle_even_while_the_database_hangs(monkeypatch):
    """THE BOUND. Without `_pending`, a database that stops answering means every tick re-spawns every
    dirty row — 31 more coroutines every tick, into a pool with 30 slots. That is the original outage
    reconstructed by the fix meant to prevent it, and coalescing on content alone does not stop it:
    nothing has landed, so nothing is recorded as written, so everything looks dirty forever."""
    futs = _pending_spy(monkeypatch)
    fake = _Fake([_dto("AAPL"), _dto("MSFT")])
    for _ in range(50):
        UiFeedStrategy._publish_trades(fake)
    assert len(futs) == 2, (
        f"{len(futs)} writes in flight for 2 cycles over 50 ticks against a hung database — the pool has "
        f"30 slots, so this is how the storm comes back")


def test_a_failed_write_is_retried_on_the_NEXT_tick_but_not_before(monkeypatch):
    """The other side of the bound: suppression must last exactly as long as the write is in flight.
    A cycle whose write FAILED is dirty again immediately — one retry per round-trip, not zero."""
    futs = _pending_spy(monkeypatch)
    fake = _Fake([_dto("AAPL")])
    UiFeedStrategy._publish_trades(fake)
    assert len(futs) == 1
    UiFeedStrategy._publish_trades(fake)
    assert len(futs) == 1, "retried while the first write was still in flight"
    futs[0].ok = False
    futs[0].land()                           # the write comes back FAILED
    assert fake.log.errors, "a failed write logged nothing"
    UiFeedStrategy._publish_trades(fake)
    assert len(futs) == 2, "the failed write was never retried — the envelope silently diverges"


def test_a_cycle_that_CHANGES_mid_flight_waits_rather_than_stacking(monkeypatch):
    """With a true in-flight bound, a cycle that transitions while its write is out does NOT get a second
    write queued behind the first. It waits one round-trip. Nothing is lost: the projection re-emits every
    tick, so the tick after the write returns sees the newer content and writes it.

    Suppressing only on an EQUAL key would instead enqueue a future per tick for the one cycle that is
    moving — which is the storm again, on exactly the cycle that matters most."""
    futs = _pending_spy(monkeypatch)
    fake = _Fake([_dto("AAPL")])
    UiFeedStrategy._publish_trades(fake)                      # write #1, HELD, in flight
    fake._trade_cycles["MANUAL-001"].dtos = [_dto("AAPL", state="CLOSED")]
    for _ in range(10):
        UiFeedStrategy._publish_trades(fake)                  # transitions, but #1 is still out
    assert len(futs) == 1, f"{len(futs)} writes stacked on one cycle while its write was in flight"
    futs[0].land()                                            # #1 comes back
    UiFeedStrategy._publish_trades(fake)
    assert len(futs) == 2, "the newer content was never written after the slot freed"


def test_a_STALE_ATTEMPT_cannot_satisfy_a_NEWER_write_that_shares_its_key(monkeypatch):
    """THE REASON THE GUARD COUNTS ATTEMPTS INSTEAD OF COMPARING KEYS.

    Keys repeat — a cycle returns to a state it already held. Here AAPL's first write is still in flight
    when the cycle leaves the book (pruned), then comes back with the SAME content, so a second write is
    issued carrying an IDENTICAL key. The first future then lands.

    Against a key-based guard the stale callback matches by coincidence, marks the key durable and frees
    the slot while the real write is still out. If that one then fails, `_envelope_written` already claims
    it landed and nothing ever retries it — the envelope diverges permanently, silently.
    """
    futs = _pending_spy(monkeypatch)
    fake = _Fake([_dto("AAPL")])
    UiFeedStrategy._publish_trades(fake)                      # attempt 1, in flight
    fake._trade_cycles["MANUAL-001"].dtos = []                # AAPL leaves the book -> pruned
    UiFeedStrategy._publish_trades(fake)
    fake._trade_cycles["MANUAL-001"].dtos = [_dto("AAPL")]    # returns with the SAME content
    UiFeedStrategy._publish_trades(fake)                      # attempt 2, same key
    assert len(futs) == 2
    futs[0].land()                                            # the STALE attempt lands, successfully
    assert "cyc-AAPL" not in fake._envelope_written, (
        "a stale attempt marked a newer in-flight write as durable, because it happened to carry the "
        "same key — if that write now fails it will never be retried")
    assert "cyc-AAPL" in fake._envelope_pending, "the stale callback freed the live write's slot"
    futs[1].land()                                            # the REAL write lands
    assert "cyc-AAPL" in fake._envelope_written


def test_a_callback_for_a_cycle_that_LEFT_the_book_does_not_resurrect_it(monkeypatch):
    """The prune and an in-flight write race by construction. A callback landing after its cycle left the
    book must not put it back, or the cache accumulates dead cycles that no later prune is looking for."""
    futs = _pending_spy(monkeypatch)
    fake = _Fake([_dto("AAPL"), _dto("MSFT")])
    UiFeedStrategy._publish_trades(fake)
    fake._trade_cycles["MANUAL-001"].dtos = [_dto("MSFT")]    # AAPL leaves the book
    UiFeedStrategy._publish_trades(fake)
    futs[0].land()                                            # AAPL's write lands anyway, SUCCEEDING
    assert "cyc-AAPL" not in fake._envelope_written, "a departed cycle was resurrected by its own callback"
    assert "cyc-AAPL" not in fake._envelope_pending


def test_an_ALREADY_COMPLETED_future_does_not_deadlock(monkeypatch):
    """MEASURED, not assumed: `add_done_callback` on a future that is already done runs the callback
    SYNCHRONOUSLY on the calling thread — inside the block that holds the lock. A `threading.Lock` here
    deadlocks the fold thread and the engine stops publishing entirely; `RLock` is load-bearing.

    `_spy` returns a future that fires its callback immediately, which is that case exactly. If this
    hangs rather than fails, that is the bug.
    """
    # Built by PRODUCTION's factory, so swapping RLock->Lock in engine_node reaches this test.
    seen = _spy(monkeypatch)
    fake = _Fake([_dto("AAPL")])
    UiFeedStrategy._publish_trades(fake)
    assert len(seen) == 1
    assert fake._envelope_written["cyc-AAPL"], "the synchronous callback did not record the write"
