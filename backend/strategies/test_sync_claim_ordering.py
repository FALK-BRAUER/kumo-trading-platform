"""#845 — N terminal events for one order race each other in `sync_claim`.

Measured on paper, BCTROT-004 GMAB, 2026-09-09 19:40:19Z. Nautilus's position reconciler pulled three
missing fills for one SELL 59 and applied them 22 ms apart (35, 4, 20 → net 24, 20, 0). Each fill
reached `_record_terminal`, which captured `_lane_quantity()` on the event thread and scheduled
`sync_claim(GMAB, qty, px)` on the loop. Three coroutines, three transactions, commit order not
scheduling order: the DELETE from the third fill committed at .342 and the INSERT from the first
committed at .422, so the ledger ended on a fresh row — `qty=24 quality=adopted opened_at=19:40:19.422
sessions_held=0` — for a position the broker had already closed. The original row (59, live, six
sessions held, opened 2026-09-01) was gone.

`test_sync_claim.py` drives one call at a time against a list-of-statements double. It cannot hold two
transactions open, so it cannot express this. The double here can, and the first test proves that it
does BEFORE anything is asserted about the gateway — a double that cannot resurrect a row would make
every test below vacuous.

THE PROPERTY, not the instance (codex, step 3): for one `(strategy_id, symbol)` the ledger must apply
writes in EVENT order, whatever order Postgres would otherwise commit them in. Every seam test asserts
the applied-operation log, not only the final row.

WHAT THIS FILE PROVES, PRECISELY (review, mutations A/B). The double inverts BOTH seams — the order
lock requests ARRIVE at Postgres and the order commits land — so only production's in-process lock
can make the ordering tests pass; and `advisory_sent` pins that the advisory statement is actually
issued. Before the arrival inversion the suite proved "at least one of the two mechanisms": deleting
the in-process lock left all 30 tests green.

THE DOUBLE IS ADVERSARIAL BUT CANNOT DEADLOCK A CORRECT FIX (codex, step 3): it releases whichever
commits are pending, LAST-BEGUN FIRST, and repeats until `n` have applied. Against the unserialised code
three are pending at once and it inverts them; against a serialised fix only one is ever pending and
it releases that one. A double that waited for all `n` to be pending would hang the fix it exists to
prove.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.sql.dml import Delete, Insert
from sqlalchemy.sql.elements import TextClause

from strategies.momentum import SessionGateway as Momentum
from strategies.qc345 import QC345SessionGateway as QC345

LANE = "BCTROT-004"
SYM = "GMAB"
KEY = (LANE, SYM)
PX = 33.14
T = 5.0     # every await in these tests is bounded — a hang is a failure, not a timeout page

#: The row as it stood before the exit — the backup table's copy of it (kumo-strategies#124).
LIVE_ROW = {"qty": 59.0, "entry": 33.37, "peak": 34.32, "quality": "live",
            "sessions_held": 6, "sessions_since_high": 1, "opened_at": 0}


class _Ledger:
    """`exec_position_state` with Postgres's two properties that matter here: a transaction is applied
    at COMMIT, and commits are ordered by whoever commits first, not by whoever began first.

    Interprets exactly the two statements `kumo_strategies.runtime.executor.store` emits, per that
    module's own contract: a DELETE keyed on both columns, and an INSERT whose conflict branch moves
    `qty` (and `updated_at`) alone. `opened_at` is a commit sequence number, because in production it
    is `DEFAULT now()` — it moves only on a real INSERT, which is how the live row gave itself away.

    `fail_commit_no` makes the N-th transaction to reach commit raise instead — the shape a lost
    Postgres connection has, and the way a lock that leaks on exception is caught.
    """

    def __init__(self, rows: dict | None = None, *, fail_commit_no: int | None = None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.applied: list[tuple[str, tuple, float | None]] = []     # (kind, key, qty)
        self._seq = 0
        self._pending: list[asyncio.Event] = []
        self._advisory: dict[str, asyncio.Lock] = {}
        self._arrival_no = 0
        self.advisory_sent = 0          # mutation B: the advisory statement must actually be sent
        self._begun = 0
        self.hold_commits = False
        self._fail_commit_no = fail_commit_no

    # -- the sessionmaker contract `record_claim`/`drop_claim` use -------------------------------
    def sessionmaker(self):
        ledger = self

        class _Session:
            def __init__(self):
                self.buffer, self.held = [], []

            @asynccontextmanager
            async def begin(self):
                try:
                    yield
                    ledger._begun += 1
                    if ledger._fail_commit_no == ledger._begun:
                        raise ConnectionError("postgres went away at commit")
                    if ledger.hold_commits:
                        gate = asyncio.Event()
                        ledger._pending.append(gate)
                        await gate.wait()
                    for stmt in self.buffer:
                        ledger._apply(stmt)
                finally:
                    for lock in self.held:          # the advisory lock ends with the transaction
                        lock.release()

            async def execute(self, stmt, params=None):
                # `pg_advisory_xact_lock` (kumo-strategies#124): modelled as a per-key wait until the
                # holder's transaction ends — Postgres's semantics, nothing more. The in-process
                # ordering lock is production's own code and is deliberately NOT simulated here.
                if isinstance(stmt, TextClause) and "pg_advisory_xact_lock" in str(stmt):
                    # ARRIVAL IS NOT SUBMISSION ORDER (review, mutation A): with one connection per
                    # transaction the lock request reaches Postgres after a connect of varying
                    # latency. Without this, every coroutine ran uninterrupted to `acquire()` and
                    # the advisory model alone ordered the double — the in-process lock could be
                    # deleted with all 30 tests green. Each arrival now yields a DESCENDING number
                    # of times, so the last-submitted request arrives first unless something
                    # upstream serialised the callers.
                    ledger._arrival_no += 1
                    for _ in range(max(0, 8 - 2 * ledger._arrival_no)):
                        await asyncio.sleep(0)
                    ledger.advisory_sent += 1
                    key = " ".join(str(v) for v in stmt.compile().params.values())
                    lock = ledger._advisory.setdefault(key, asyncio.Lock())
                    await lock.acquire()
                    self.held.append(lock)
                    return None
                self.buffer.append(stmt)

        @asynccontextmanager
        async def _cm():
            yield _Session()

        return _cm()

    async def release_adversarially(self, n: int) -> None:
        """Until `n` commits have applied: wait for at least one to be pending, release the one that
        began LAST. Inverts whatever is concurrently in flight; passes a serialised writer through."""
        released = 0
        while released < n:
            while not self._pending:
                await asyncio.sleep(0)
            gate = self._pending.pop()
            gate.set()
            released += 1
            await asyncio.sleep(0)

    # -- Postgres semantics for the two statements -----------------------------------------------
    def _apply(self, stmt) -> None:
        self._seq += 1
        if isinstance(stmt, Delete):
            # `drop_claim_stmt` keys the WHERE on both columns; a DELETE missing either would clear
            # another lane's claim, so the double reads the clause rather than assuming the key.
            where = {c.left.name: c.right.value for c in stmt.whereclause.clauses}
            key = (where["strategy_id"], where["symbol"])
            self.rows.pop(key, None)
            self.applied.append(("delete", key, None))
            return
        assert isinstance(stmt, Insert), type(stmt)
        params = stmt.compile().params
        key = (params["strategy_id"], params["symbol"])
        if key in self.rows:
            # ON CONFLICT DO UPDATE — the columns the statement names, nothing else.
            moved = {(c if isinstance(c, str) else c.name)
                     for c, _ in stmt._post_values_clause.update_values_to_set}
            for col in moved:
                if col in params:
                    self.rows[key][col] = params[col]
            self.applied.append(("update", key, params["qty"]))
            return
        self.rows[key] = {c: params[c] for c in
                          ("qty", "entry", "peak", "quality", "sessions_held", "sessions_since_high")}
        self.rows[key]["opened_at"] = self._seq
        self.applied.append(("insert", key, params["qty"]))

    def ops(self, key=KEY) -> list[tuple[str, float | None]]:
        return [(kind, qty) for kind, k, qty in self.applied if k == key]


def _gateway(cls, ledger, lane=LANE):
    g = object.__new__(cls)
    g._journal = ledger
    g._strategy_id = lane
    return g


async def _from_event_thread(loop, calls) -> list:
    """Schedule each `coro` the way production does: `run_coroutine_threadsafe` from a thread that is
    not the loop's, one call per terminal event, in event order. Returns the futures WITHOUT awaiting
    them, after the thread has submitted every one — so a releaser created afterwards is queued
    behind all of the events' first steps and sees everything that can be in flight, deterministically."""
    futures, errors = [], []

    def _thread():
        try:
            for coro in calls:
                futures.append(asyncio.run_coroutine_threadsafe(coro, loop))
        except BaseException as exc:                                    # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_thread)
    t.start()
    await loop.run_in_executor(None, t.join)
    assert not errors, errors
    return [asyncio.wrap_future(f) for f in futures]


async def _race(ledger, calls, n) -> None:
    ledger.hold_commits = True
    futures = await _from_event_thread(asyncio.get_running_loop(), calls)
    releaser = asyncio.create_task(ledger.release_adversarially(n))
    await asyncio.wait_for(asyncio.gather(*futures), T)
    await asyncio.wait_for(releaser, T)


# -- fixture property: the double can express the defect ---------------------------------------
def test_the_ledger_double_CAN_resurrect_a_row_when_commits_land_out_of_order():
    """The real store statements, committed last-begun-first, must leave the row GMAB=24, adopted,
    `opened_at` moved — the exact live row. If this cannot happen here it cannot happen to the gateway
    below either, and the tests after this one would pass against any implementation at all."""
    from kumo_strategies.runtime.executor.store import claim_upsert, drop_claim_stmt

    async def bare(ledger, stmt):
        # The real statements in a bare transaction — NOT `record_claim`/`drop_claim`, which order
        # their writes since kumo-strategies#124 and would make this fixture test pass vacuously.
        async with ledger.sessionmaker() as s, s.begin():
            await s.execute(stmt)

    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        ledger.hold_commits = True
        releaser = asyncio.create_task(ledger.release_adversarially(3))
        await asyncio.wait_for(asyncio.gather(
            bare(ledger, claim_upsert(LANE, SYM, 24, PX)),
            bare(ledger, claim_upsert(LANE, SYM, 20, PX)),
            bare(ledger, drop_claim_stmt(LANE, SYM)),
        ), T)
        await asyncio.wait_for(releaser, T)
        return ledger

    ledger = asyncio.run(run())
    kinds = [k for k, _ in ledger.ops()]
    assert kinds[0] == "delete" and "insert" in kinds[1:], ledger.ops()
    assert ledger.rows[KEY]["qty"] == 24 and ledger.rows[KEY]["quality"] == "adopted"
    assert ledger.rows[KEY]["opened_at"] != LIVE_ROW["opened_at"], "a conflict-update, not a resurrection"
    assert ledger.rows[KEY]["sessions_held"] == 0, "the trail's memory must be lost for the fixture to be honest"


# -- the seam: terminal events for one key, scheduled the way `fire_and_report` schedules them ------
@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_three_fills_for_one_exit_apply_in_EVENT_order_and_leave_the_ledger_FLAT(cls):
    """`(24, 20, 0)` is what `_lane_quantity()` read on the event thread for the three GMAB fills."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, q, PX) for q in (24, 20, 0)], 3)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("update", 24.0), ("update", 20.0), ("delete", None)], ledger.ops()
    assert KEY not in ledger.rows, ledger.rows.get(KEY)


@pytest.mark.parametrize("cls", [Momentum, QC345])
@pytest.mark.parametrize("fills", [(50, 40, 30, 20, 10, 0), (24, 20)])
def test_ANY_number_of_fills_applies_in_event_order(cls, fills):
    """Six fills, and a partial exit that leaves the lane holding 20. The property is the same: the
    ledger's operation order is the event order, and a partial exit moves `qty` on the ORIGINAL row —
    never delete-and-reinsert, which would zero `sessions_held`."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, q, PX) for q in fills], len(fills))
        return ledger

    ledger = asyncio.run(run())
    expected = [("delete", None) if q == 0 else ("update", float(q)) for q in fills]
    assert ledger.ops() == expected, ledger.ops()
    if fills[-1] > 0:
        assert ledger.rows[KEY]["qty"] == fills[-1]
        assert ledger.rows[KEY]["opened_at"] == LIVE_ROW["opened_at"], "the row was re-created"
        assert ledger.rows[KEY]["sessions_held"] == 6 and ledger.rows[KEY]["quality"] == "live"
    else:
        assert KEY not in ledger.rows


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_rejection_between_two_fills_does_not_reorder_them(cls):
    """`px=None` is the rejection/denial/cancel shape: the lane still holds some, the claim is left
    alone. It must not let the fills on either side of it swap."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, 24, PX), g.sync_claim(SYM, 24, None),
                             g.sync_claim(SYM, 0, PX)], 2)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("update", 24.0), ("delete", None)], ledger.ops()


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_NEW_position_after_a_full_exit_is_a_real_insert(cls):
    """The one legitimate INSERT-after-DELETE: flat, then a fresh entry fills. Ordering must not turn
    into "never insert after a delete"."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, 0, PX), g.sync_claim(SYM, 10, 35.0)], 2)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("delete", None), ("insert", 10.0)], ledger.ops()
    assert ledger.rows[KEY]["qty"] == 10 and ledger.rows[KEY]["quality"] == "adopted"


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_two_gateway_instances_for_ONE_lane_still_apply_in_event_order(cls):
    """The key is `(strategy_id, symbol)`, not the object. A lock hung on the instance would pass the
    tests above and race here (codex, step 3)."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g1, g2 = _gateway(cls, ledger), _gateway(cls, ledger)
        await _race(ledger, [g1.sync_claim(SYM, 24, PX), g2.sync_claim(SYM, 20, PX),
                             g1.sync_claim(SYM, 0, PX)], 3)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("update", 24.0), ("update", 20.0), ("delete", None)], ledger.ops()


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_a_commit_that_RAISES_does_not_wedge_the_key(cls):
    """`sync_claim` never raises into the event handler; a serialisation that leaked its lock on that
    swallowed exception would leave every later write for the key waiting forever. The second write
    must still land, within the bound."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW}, fail_commit_no=1)
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, 24, PX), g.sync_claim(SYM, 0, PX)], 1)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("delete", None)], ledger.ops()
    assert KEY not in ledger.rows


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_different_keys_are_NOT_serialised_against_each_other(cls):
    """Two symbols on one lane, and one symbol on two lanes: a held commit on one key must not stop
    the other key's commit. A lane-wide or global lock would pass every ordering test and stall the
    book on the first slow write."""
    other_sym, other_lane = (LANE, "AEM"), ("MOMENTUM-002", SYM)

    async def run():
        ledger = _Ledger({KEY: LIVE_ROW, other_sym: LIVE_ROW, other_lane: LIVE_ROW})
        g, g_other = _gateway(cls, ledger), _gateway(cls, ledger, lane="MOMENTUM-002")
        ledger.hold_commits = True
        first = asyncio.create_task(g.sync_claim(SYM, 24, PX))
        while not ledger._pending:                      # its commit is now held
            await asyncio.sleep(0)
        held = ledger._pending.pop(0)                    # ours to release, not the releaser's
        await asyncio.wait_for(asyncio.gather(
            g.sync_claim("AEM", 12, 40.0), g_other.sync_claim(SYM, 59, PX),
            asyncio.create_task(ledger.release_adversarially(2)),
        ), T)
        assert ledger.ops(other_sym) == [("update", 12.0)] and ledger.ops(other_lane) == [("update", 59.0)]
        assert ledger.ops() == [], "the held key's commit was released by another key's traffic"
        held.set()
        await asyncio.wait_for(first, T)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("update", 24.0)]


def test_the_store_upper_cases_the_symbol_so_two_casings_are_ONE_key():
    """`store.claim_upsert`/`drop_claim_stmt` upper-case the symbol, so the double sees one ROW key for
    `gmab` and `GMAB`. Two SEQUENTIAL calls: this pins the DML's canonical symbol only. The lock key's
    casing is pinned where the lock lives — kumo-strategies `test_claim_writes_are_serialised.py`
    (`test_the_lock_key_is_case_normalised_like_the_row_key_on_EVERY_writer`)."""
    from kumo_strategies.runtime.executor.store import drop_claim, record_claim

    async def run():
        ledger = _Ledger()
        await record_claim(ledger, LANE, "gmab", 10, PX)
        await drop_claim(ledger, LANE, "GMAB")
        return ledger

    ledger = asyncio.run(run())
    assert ledger.ops() == [("insert", 10.0), ("delete", None)] and KEY not in ledger.rows


@pytest.mark.parametrize("cls", [Momentum, QC345])
def test_the_advisory_lock_statement_is_actually_SENT_for_every_write(cls):
    """Mutation B (review): with the advisory statement never sent, every ordering test here still
    passed — the in-process lock alone orders one process. The statement is the half that holds
    across processes, and nothing in this repo pinned that it is issued. Now it is."""
    async def run():
        ledger = _Ledger({KEY: LIVE_ROW})
        g = _gateway(cls, ledger)
        await _race(ledger, [g.sync_claim(SYM, q, PX) for q in (24, 0)], 2)
        return ledger

    ledger = asyncio.run(run())
    assert ledger.advisory_sent == 2, ledger.advisory_sent
