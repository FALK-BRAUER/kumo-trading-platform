"""The lane must size off the LEDGER, and the distribution must run BEFORE the read (#537, #373)."""

from __future__ import annotations

import asyncio

from strategies import session_budget


class _Sleeve:
    def __init__(self, actual, target):
        self.actual, self.target = actual, target


class _Book:
    def __init__(self, sleeves):
        self.sleeves = sleeves


run_ids: list = []


def _wire(monkeypatch, *, sleeves, on_distribute=None, load_raises=False, order=None):
    """Doubles keyed the way `Book.sleeves` really is — a DICT. Shaped as a list, `.get()` would raise
    into the resolver's own except and every assertion below would pass over a fallback."""
    import api.budget_store as bs
    import api.db.engine as eng

    run_ids.clear()

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self):
            if order is not None: order.append("commit")
        async def rollback(self):
            if order is not None: order.append("rollback")

    monkeypatch.setattr(eng, "session_factory", lambda: _Session(), raising=False)

    async def _dist(_s, **kw):
        if order is not None: order.append("distribute")
        run_ids.append(kw.get("run_id"))
        return on_distribute() if on_distribute else []

    async def _load(_s):
        if order is not None: order.append("load")
        if load_raises: raise RuntimeError("ledger unreadable")
        return _Book(sleeves)

    monkeypatch.setattr(bs, "distribute_unallocated", _dist, raising=False)
    monkeypatch.setattr(bs, "load_book", _load, raising=False)


def test_the_fixture_reaches_the_resolver(monkeypatch):
    """FIXTURE FIRST. The resolver returns `fallback` on ANY failure, so a broken double is
    indistinguishable from a working resolver that found nothing."""
    _wire(monkeypatch, sleeves={"A-001": _Sleeve(10_000.0, 20_000.0)})
    assert asyncio.run(session_budget.resolve("A-001", fallback=20_000.0)) == 10_000.0, (
        "got the fallback — the doubles are not reaching the resolver")


def test_it_sizes_off_ACTUAL_not_the_settings_target(monkeypatch):
    """THE DEFECT. QC345-003 live: settings 20,000, ledger 10,000, deployed 15,860."""
    _wire(monkeypatch, sleeves={"QC345-003": _Sleeve(10_000.0, 20_000.0)})
    assert asyncio.run(session_budget.resolve("QC345-003", fallback=20_000.0)) == 10_000.0


def test_TARGET_still_caps_a_sleeve_that_holds_more_than_it_was_granted(monkeypatch):
    """`min(actual, target)` — the rule the order gate already applies. Not a third definition."""
    _wire(monkeypatch, sleeves={"B-002": _Sleeve(50_000.0, 20_000.0)})
    assert asyncio.run(session_budget.resolve("B-002", fallback=20_000.0)) == 20_000.0


def test_DISTRIBUTION_RUNS_BEFORE_THE_READ(monkeypatch):
    """THE ORDERING, and it is the whole point.

    Distributing after the read funds a sleeve the lane has already sized against — one session too
    late, and it would look like it worked because the ledger moved.
    """
    order: list[str] = []
    _wire(monkeypatch, sleeves={"C-003": _Sleeve(20_000.0, 20_000.0)}, order=order)
    asyncio.run(session_budget.resolve("C-003", fallback=1.0))

    assert order.index("distribute") < order.index("load"), f"wrong order: {order}"
    assert "commit" in order, (
        "the distribution was never committed. `distribute_unallocated` only FLUSHES and "
        "`AsyncSession.__aexit__` rolls back — the /sleeves/distribute route answered 200 with an "
        "untouched database until it learned this")
    assert order.index("commit") < order.index("load"), (
        "committed after reading, so the read saw the pre-distribution book")


def test_a_FAILED_distribution_still_lets_the_session_size(monkeypatch):
    """A distribution failure must not stop a lane trading — it sizes off the book as it stands."""
    def _boom():
        raise RuntimeError("conservation check failed")

    order: list[str] = []
    _wire(monkeypatch, sleeves={"D-004": _Sleeve(9_000.0, 20_000.0)},
          on_distribute=_boom, order=order)

    assert asyncio.run(session_budget.resolve("D-004", fallback=20_000.0)) == 9_000.0
    assert "rollback" in order, "a failed distribution left the transaction open"


def test_an_UNREADABLE_ledger_keeps_the_previous_basis(monkeypatch):
    """#377: never take a session down. An unreadable ledger must not CHANGE how a live session
    sizes — it keeps what it had, which is the old behaviour rather than a new one."""
    _wire(monkeypatch, sleeves={}, load_raises=True)
    assert asyncio.run(session_budget.resolve("E-005", fallback=20_000.0)) == 20_000.0


def test_a_lane_with_no_sleeve_keeps_its_fallback(monkeypatch):
    _wire(monkeypatch, sleeves={})
    assert asyncio.run(session_budget.resolve("NEW-009", fallback=20_000.0)) == 20_000.0


def test_a_ZERO_sleeve_reaches_the_lane_AS_ZERO(monkeypatch):
    """A lane wound down to nothing must size on nothing, not fall back to its target.

    `0.0` is falsy, and `or fallback` would fuse "allocated zero" with "unreadable" — the exact
    defect kumo-strategies fixed in its own runners this week.
    """
    _wire(monkeypatch, sleeves={"F-006": _Sleeve(0.0, 20_000.0)})
    assert asyncio.run(session_budget.resolve("F-006", fallback=20_000.0)) == 0.0


def test_BOTH_lanes_ask_the_platform_AND_USE_THE_ANSWER_before_they_size():
    """THE SEAM. A lane that calls the resolver and discards the result is the defect unchanged.

    AN EARLIER VERSION OF THIS TEST ONLY CHECKED THE CALL EXISTED, and a mutation proved it hollow:
    deleting the assignment in qc27 — so the lane resolved a budget and kept sizing off settings —
    left all nine tests green. Asking is not using, and "it calls the function" is exactly the kind
    of adjacent fact this whole issue is about.

    Source-level because the alternative is constructing two full session runners; the behavioural
    assertions above cover the resolver itself.
    """
    import pathlib as _p
    import re

    here = _p.Path(__file__).parent
    for lane in ("qc27.py", "qc345.py"):
        src = (here / lane).read_text()
        assert "session_budget import" in src, f"{lane} does not import the platform budget"

        call = re.search(r"(\w*budget\w*)\s*=\s*await\s+_?resolve\(", src)
        assert call, f"{lane} never awaits the resolver into a variable"
        var = call.group(1)

        # THE ANSWER MUST REACH `allocated_equity`. Anything less and the lane sizes off settings.
        assert re.search(rf"allocated_equity\s*=\s*{re.escape(var)}\b", src), (
            f"{lane} resolves a budget into `{var}` and never assigns it to `allocated_equity` — it "
            f"asks the platform and keeps sizing off the settings target, which is #537 unchanged"
        )

        # BEFORE it sizes, not after — probing afterwards describes the NEXT session.
        sizes = min([i for i in (src.find("await self._submit("), src.find("await super().run("))
                     if i > 0])
        assert call.start() < sizes, f"{lane} resolves AFTER it sizes"


def test_the_run_id_VARIES_PER_SESSION_or_distribution_happens_exactly_once_ever(monkeypatch):
    """THE BUG I SHIPPED AND CAUGHT REVIEWING MY OWN DIFF, fourteen minutes after deploying it.

    `distribute_unallocated` derives `fill_id = f"{run_id}-{recipient}"` and `SleeveTransfer.fill_id`
    is UNIQUE — that uniqueness is the replay guard and it is load-bearing. The first version of
    `resolve` passed `run_id=f"session-{strategy_id}"`, a CONSTANT. Consequence: the first session
    inserts, and every session afterwards collides, logs "already applied — skipping duplicate", and
    allocates NOTHING. Distribution would have worked once per lane for the life of the deployment
    and then stopped silently, with the ledger looking untouched and no error anywhere.

    The nine tests already here all passed over it, because none of them ran the resolver TWICE.
    """
    _wire(monkeypatch, sleeves={"A-001": _Sleeve(10_000.0, 20_000.0)})

    asyncio.run(session_budget.resolve("A-001", fallback=1.0, session="2026-08-25"))
    asyncio.run(session_budget.resolve("A-001", fallback=1.0, session="2026-08-26"))

    assert len(run_ids) == 2, f"the distribution did not run twice: {run_ids}"
    assert run_ids[0] != run_ids[1], (
        f"both sessions used run_id={run_ids[0]!r}. `fill_id` is unique, so the second distribution "
        f"collides and allocates nothing — forever, and silently"
    )
    assert all(r and "A-001" in r for r in run_ids), f"run_id lost the strategy: {run_ids}"


def test_a_RETRY_within_one_session_keeps_the_same_run_id(monkeypatch):
    """The other half, and why the key is the SESSION rather than a timestamp: a retried session must
    land once. A clock-derived id would re-distribute on every retry, funding sleeves twice — which
    is precisely what the unique constraint exists to stop."""
    _wire(monkeypatch, sleeves={"A-001": _Sleeve(10_000.0, 20_000.0)})

    asyncio.run(session_budget.resolve("A-001", fallback=1.0, session="2026-08-25"))
    asyncio.run(session_budget.resolve("A-001", fallback=1.0, session="2026-08-25"))

    assert run_ids[0] == run_ids[1], (
        f"a retry of the SAME session produced a different run_id ({run_ids}), so the replay guard "
        f"cannot fire and the sleeves would be funded twice")


def test_a_WOUND_DOWN_lane_sizes_ZERO_not_its_full_holding(monkeypatch):
    """#648. The two derivations differed EXACTLY at target=0 — the schema default, the wound-down
    case, and what the settings schema itself defines: "Zero means wind down."

        session_budget:  min(actual, target) if target else actual   -> 50,000
        Sleeve.deployable:  min(actual, target)                      ->      0

    `if target` reads a REAL zero as "unset" — the falsy-`or` class that sized QC345 off the whole
    account (#568). On Alpaca the order gate's is_reducing catches the entries this would size; on
    IBKR there is NO venue-side gate, so this number goes straight to order size. A lane the
    operator wound down must size 0, by the SAME predicate the gate applies — which is what this
    module's own header already claims.
    """
    _wire(monkeypatch, sleeves={"C-003": _Sleeve(actual=50_000.0, target=0.0)})
    assert asyncio.run(session_budget.resolve("C-003", fallback=20_000.0)) == 0.0, (
        "a wound-down lane (target=0) sized off its full actual — the #648 disagreement"
    )
