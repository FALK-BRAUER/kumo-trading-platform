"""A lane that already holds must CLAIM what it holds (#540).

Measured on an Alpaca paper instance 2026-08-29: QC345-003 held 5 positions (AMAT 6, DELL 7, INTC 37, LRCX 10,
MRVL 14) and wrote ZERO rows to exec_position_state. `_claim` fires on ACCEPT, so only positions
opened after the wiring landed are claimed; the standing book predates it and nothing back-fills.

Consequences measured the same week: DELL is held by QC345 (7) and TECHIVOL (2) while only
TECHIVOL claims its side (#541), and an unclaimed holding is invisible to every ceiling that sizes
off claims — the same ledger whose disagreement with the cache refused the BDX exit (#692).

THE ENTRY PRICE IS THE BROKER'S, NOT TODAY'S. `position_entries()` exists for this
(kumo-trading-platform issue 197 B1): seeding an adopted position from the current price asserts a peak that never
happened and mis-states every give-back rule downstream.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import strategies.claims_backfill as mod


class _Broker:
    """What production's NautilusBroker actually returns: quantities by symbol, entries by symbol,
    and entries ONLY for positions Nautilus attributes to this strategy."""

    def __init__(self, positions, entries, account=None):
        self._p, self._e = positions, entries
        # The ACCOUNT book production anchors on. Defaults to the attributed quantities, i.e. a
        # cache that agrees with the broker — the healthy case; the cap tests override it.
        self._a = dict(positions) if account is None else account

    def positions(self):
        return dict(self._a)

    def strategy_positions(self):
        return dict(self._p)

    def position_entries(self):
        return dict(self._e)


def _run(broker, claims_now, *, monkeypatch):
    recorded = []

    async def _record(journal, strategy_id, symbol, qty, entry_px, **_kw):
        recorded.append((strategy_id, symbol, qty, entry_px))
        return qty

    monkeypatch.setattr(mod, "_record_claim", _record, raising=True)
    monkeypatch.setattr(mod, "_existing_claims", lambda _j, _s: _fake_existing(claims_now),
                        raising=True)
    asyncio.run(mod.backfill_claims(broker, SimpleNamespace(), "QC345-003"))
    return recorded


def _fake_existing(claims):
    async def _coro():
        return dict(claims)
    return _coro()


def test_the_fixture_reproduces_the_live_shape(monkeypatch):
    """FIXTURE PROPERTY, and the first version of THIS test was vacuous (review, 2026-08-29):
    `assert broker.strategy_positions() and not {}` — `not {}` is unconditionally true and the
    claims table it claimed to be about was never referenced. It promised to catch a double that
    pre-claims and could not.

    What it must actually establish: the lane HOLDS something, the ledger claims NOTHING of it, and
    the sweep therefore has work to do. Driving the sweep is what proves it."""
    broker = _Broker({"AMAT": 6, "DELL": 7}, {"AMAT": 145.5, "DELL": 122.25})
    recorded = _run(broker, {}, monkeypatch=monkeypatch)
    assert len(recorded) == 2, "the fixture leaves the sweep nothing to do — it cannot fail"


def test_a_standing_position_with_no_claim_is_adopted(monkeypatch):
    broker = _Broker({"AMAT": 6, "DELL": 7, "INTC": 37}, {"AMAT": 145.5, "DELL": 122.25, "INTC": 21.4})
    recorded = _run(broker, {}, monkeypatch=monkeypatch)
    assert {r[1] for r in recorded} == {"AMAT", "DELL", "INTC"}, (
        "standing positions were not adopted — the lane keeps holding shares no ledger knows about"
    )


def test_the_entry_price_is_the_BROKERS_not_todays(monkeypatch):
    """#197 B1: seeding from today's price asserts a peak that never happened."""
    broker = _Broker({"AMAT": 6}, {"AMAT": 145.5})
    recorded = _run(broker, {}, monkeypatch=monkeypatch)
    assert recorded == [("QC345-003", "AMAT", 6, 145.5)]


def test_an_ALREADY_CLAIMED_symbol_is_not_touched(monkeypatch):
    """Idempotent, and it must not overwrite a live claim's entry with the broker's average — a
    lane that added to a position has a claim entry that means something."""
    broker = _Broker({"AMAT": 6, "DELL": 7}, {"AMAT": 145.5, "DELL": 122.25})
    recorded = _run(broker, {"AMAT": 6}, monkeypatch=monkeypatch)
    assert [r[1] for r in recorded] == ["DELL"]


def test_a_position_with_NO_broker_entry_is_SKIPPED_and_named(monkeypatch, caplog):
    """Three states: claimed / adoptable / unknowable. A position whose entry the broker cannot
    supply must NOT be claimed at a guessed price — it is named and left, because a wrong entry is
    worse than a missing claim (it drives give-back and peak rules)."""
    broker = _Broker({"AMAT": 6, "GHOST": 3}, {"AMAT": 145.5})
    recorded = _run(broker, {}, monkeypatch=monkeypatch)
    assert [r[1] for r in recorded] == ["AMAT"]
    assert "GHOST" in caplog.text


def test_a_ZERO_quantity_is_not_a_holding(monkeypatch):
    broker = _Broker({"AMAT": 0}, {"AMAT": 145.5})
    assert _run(broker, {}, monkeypatch=monkeypatch) == []


def test_the_SESSION_actually_runs_the_backfill():
    """THE WIRING. A correct sweep nobody calls is the defect this repo ships most often (#644,
    #662, #683). Pinned on the session entry point, docstring-stripped so the fix's own prose
    cannot satisfy it."""
    import ast
    import inspect
    import textwrap

    from strategies.qc345 import QC345SessionGateway

    tree = ast.parse(textwrap.dedent(inspect.getsource(QC345SessionGateway.run)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    src = ast.unparse(fn)
    assert "backfill_claims(" in src, "the session never back-fills claims — #540 is unreached"
    # BEFORE the ranking reads the book: adopting after `held` is computed would leave the very
    # session that adopts sizing off an unclaimed book.
    assert src.index("backfill_claims(") < src.index("strategy_positions()"), (
        "the back-fill runs after the book is read — this session still sizes unclaimed"
    )


def test_an_adoption_is_CAPPED_BY_THE_ACCOUNT_NET(monkeypatch):
    """REVIEW FINDING (2026-08-29). `strategy_positions()` is the CACHE's per-strategy attribution,
    which ADR 0001 says the broker never verifies — only the net is a hard anchor. The live paper
    cache already carries legs whose per-lane attribution EXCEEDS the account net (CGAU: BCTROT
    LONG 168 beside MOMENTUM SHORT 88 against ~80 held). Adopting 168 there would write a claim
    larger than the account, and `own_ceiling(acct, mine, others)` would then collapse every other
    holder's ceiling toward zero — the BETA/WHD freeze, minted by the adoption sweep. Worse, that
    over-claim is unretirable: retire_claims keeps a positive claim on a positively-held symbol.

    So an adoption is capped by what the ACCOUNT holds, and a capped adoption is NAMED — a silently
    shrunk claim is a number nobody can explain later.
    """
    broker = _Broker({"CGAU": 168}, {"CGAU": 20.0})
    broker.positions = lambda: {"CGAU": 80}          # the account net, the only hard anchor
    recorded = _run(broker, {}, monkeypatch=monkeypatch)
    assert recorded == [("QC345-003", "CGAU", 80, 20.0)], (
        "the sweep adopted more than the account holds — every other lane's exit ceiling collapses"
    )


def test_the_cap_does_not_shrink_a_HEALTHY_adoption(monkeypatch):
    """The quiet direction: where attribution is inside the account net, nothing is capped."""
    broker = _Broker({"AMAT": 6}, {"AMAT": 145.5})
    broker.positions = lambda: {"AMAT": 9}
    assert _run(broker, {}, monkeypatch=monkeypatch) == [("QC345-003", "AMAT", 6, 145.5)]


def test_an_UNREADABLE_account_book_adopts_NOTHING(monkeypatch):
    """Three states again: the anchor is available, the anchor says less, or the anchor is unknown.
    Unknown must not fall through to the cache's word — that is the whole finding."""
    broker = _Broker({"AMAT": 6}, {"AMAT": 145.5})

    def _boom():
        raise RuntimeError("account read failed")

    broker.positions = _boom
    assert _run(broker, {}, monkeypatch=monkeypatch) == []


def test_a_claim_BELOW_what_we_hold_is_NOT_healed_up(monkeypatch):
    """THE BLOCKER (review, final pass) — verified against the live paper ledger.

        MOMENTUM-002 BDX  claim 45   cache attributes 55   net 55
        BCTROT-004   HALO claim 19   cache attributes 55
        BCTROT-004   CGAU claim 80   cache attributes 168

    A heal on the bare inequality "claim < min(attributed, net)" cannot tell a claim THIS SWEEP
    capped from one that is deliberately smaller — a partial exit (an accepted-but-unfilled sell
    does not move the position, and `run()` re-enters the sweep on resume), or simply the state
    above, which predates #540. Healing BDX 45 -> 55 makes claims 65 against a net of 55, so
    `own_ceiling(55, 10, 55)` = 0 and BCTROT's exit FREEZES: the #692 shape, minted by the sweep,
    and unretirable because retire_claims keeps a positive claim on a positively held symbol.

    Until something records WHY a claim was capped, a claim that exists is left exactly as it is.
    """
    broker = _Broker({"BDX": 55}, {"BDX": 240.0}, account={"BDX": 55})
    assert _run(broker, {"BDX": 45}, monkeypatch=monkeypatch) == [], (
        "an existing smaller claim was healed up — this is what freezes the other holder's exit"
    )


# ==================================================================================================
# THE DELETE-RACE (kumo-trading-platform issue 845, codex scope review): adoption must decide under the key's lock
# ==================================================================================================
def test_an_adoption_does_NOT_resurrect_a_claim_a_terminal_sync_dropped_between_read_and_write():
    """`backfill_claims` reads "held 6, no claim" up front and writes the adoption later. A fill's
    `sync_claim(..., 0, ...)` landing in between deletes the row — and the lane's cache position
    goes to 0 with it. `only_if_absent` cannot help: after the DELETE the row IS absent. The
    decision must be re-taken under `store.write_claim`'s key lock, from the lane's quantity as it
    is THEN. Red on the code that adopts from the up-front read."""
    from sqlalchemy.sql.dml import Delete, Insert
    from sqlalchemy.sql.elements import TextClause

    rows: dict[tuple[str, str], float] = {}
    applied: list[tuple[str, float | None]] = []

    class _Ledger:
        """The two SELECTs backfill runs, the key lock, and the two DML shapes — nothing else."""
        strategy_id = "QC345-003"

        def sessionmaker(self):
            from contextlib import asynccontextmanager

            class _S:
                @asynccontextmanager
                async def begin(self):
                    yield

                async def execute(self, stmt, params=None):
                    if isinstance(stmt, TextClause) and "pg_advisory_xact_lock" in str(stmt):
                        return None
                    if isinstance(stmt, TextClause):                     # the two SELECTs
                        prm = params or stmt.compile().params
                        sid, sym = prm.get("sid"), prm.get("sym")
                        got = [(s, q) for (l, s), q in rows.items() if l == sid and (sym is None or s == sym)]
                        return SimpleNamespace(all=lambda: got)
                    if isinstance(stmt, Delete):
                        w = {c.left.name: c.right.value for c in stmt.whereclause.clauses}
                        rows.pop((w["strategy_id"], w["symbol"]), None)
                        applied.append(("delete", None))
                        return None
                    assert isinstance(stmt, Insert), type(stmt)
                    p = stmt.compile().params
                    key = (p["strategy_id"], p["symbol"])
                    if key not in rows:
                        rows[key] = float(p["qty"])
                        applied.append(("insert", float(p["qty"])))
                    return None

            @asynccontextmanager
            async def _cm():
                yield _S()

            return _cm()

    class _RacingBroker(_Broker):
        """Attributes AMAT 6 on the sweep's up-front read; 0 on every read after — the fill and its
        `drop_claim` landed in between."""
        def __init__(self):
            super().__init__({"AMAT": 6}, {"AMAT": 145.5})
            self.reads = 0

        def strategy_positions(self):
            self.reads += 1
            return {"AMAT": 6} if self.reads == 1 else {"AMAT": 0}

        def positions(self):
            return {"AMAT": 6} if self.reads <= 1 else {"AMAT": 0}

    broker = _RacingBroker()
    out = asyncio.run(mod.backfill_claims(broker, _Ledger(), "QC345-003"))
    assert out["status"] == "ok", out
    assert broker.reads >= 2, "adoption never re-read the lane's quantity under the lock"
    assert ("QC345-003", "AMAT") not in rows, f"resurrected: {rows} applied={applied}"
    assert out["adopted"] == 0
    # NOT SILENT: the refusal is in the result the session journals, so this sweep cannot read as
    # "everything already claimed" (review, #845 — "0 of 0 is not 0 of 4").
    assert out["refused"] == ["AMAT(held 6 at read, none under the lock)"], out


def test_reread_is_REQUIRED_so_the_pre_845_call_cannot_come_back_quietly():
    """A default `reread=None` would be the old up-front adoption with no warning — the shape
    CLAUDE.md names: a default argument is what makes a missing argument invisible."""
    import inspect
    import pytest
    p = inspect.signature(mod._record_claim).parameters["reread"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        asyncio.run(mod._record_claim(SimpleNamespace(), "QC345-003", "AMAT", 6, 145.5))


def test_a_row_that_APPEARED_under_the_lock_is_not_counted_as_an_adoption():
    """A `sync_claim(qty>0)` landing between the sweep's read and its write leaves a row; the
    adoption's `ON CONFLICT DO NOTHING` writes nothing — and `adopted` must say so, not count a
    write that did not happen (review, #845)."""
    from sqlalchemy.sql.elements import TextClause

    class _J:
        strategy_id = "QC345-003"

        def sessionmaker(self):
            from contextlib import asynccontextmanager

            class _S:
                @asynccontextmanager
                async def begin(self):
                    yield

                async def execute(self, stmt, params=None):
                    if isinstance(stmt, TextClause) and "pg_advisory_xact_lock" in str(stmt):
                        return None
                    if isinstance(stmt, TextClause):
                        return SimpleNamespace(all=lambda: [("AMAT", 6.0)])      # a row is there now
                    raise AssertionError(f"wrote {type(stmt).__name__} over a row that exists")

            @asynccontextmanager
            async def _cm():
                yield _S()

            return _cm()

    got = asyncio.run(mod._record_claim(_J(), "QC345-003", "AMAT", 6, 145.5, reread=lambda: (6, 6)))
    assert not got and isinstance(got, mod.Refused) and "row stands" in got, got


def _nothing_to_do():
    return {"status": "ok", "adopted": 0, "capped": [], "unknowable": [], "refused": []}


def test_a_STALLED_claim_write_is_named_in_the_result_not_swallowed_into_a_warning(monkeypatch):
    """`store.write_claim` (kumo-trading-strategies f54f54d) raises `ClaimWriteStalled` after 30 s — a class
    the pre-#845 `record_claim` could not produce, so this PR CREATES the state. Caught by the
    sweep's `except Exception` and logged, a stalled sweep returned the SAME dict as a sweep with
    nothing to do: `status ok, adopted 0, refused []` — "everything already claimed"
    (review-848-final, measured byte-identical). Fixture property first: the stalled sweep must
    DISAGREE with the idle one. Then the failure is named, per symbol, with the class that raised.
    Any exception, not only the stall: a Postgres error on the write is the same unclaimed position."""
    from kumo_strategies.runtime.executor.store import ClaimWriteStalled

    for exc in (ClaimWriteStalled("QC345-003/AMAT: claim write exceeded 30s; rolled back"),
                RuntimeError("connection reset")):
        async def _raise(journal, strategy_id, symbol, qty, entry_px, **_kw):
            raise exc

        monkeypatch.setattr(mod, "_existing_claims", lambda _j, _s: _fake_existing({}), raising=True)
        monkeypatch.setattr(mod, "_record_claim", _raise, raising=True)
        out = asyncio.run(mod.backfill_claims(_Broker({"AMAT": 6}, {"AMAT": 145.5}),
                                              SimpleNamespace(), "QC345-003"))
        idle = {k: v for k, v in out.items() if k in _nothing_to_do()}
        assert idle != _nothing_to_do() or out.get("failed"), (
            f"a sweep whose write raised {type(exc).__name__} reads identical to one with nothing "
            f"to do: {out}")
        assert out["failed"] == [f"AMAT({type(exc).__name__}: {exc})"], out
        assert out["status"] == "ok" and out["adopted"] == 0 and out["refused"] == [], out


def test_a_row_that_APPEARED_under_the_lock_is_refused_for_ITS_reason_not_the_other_one():
    """`_build` refuses for two different reasons — the lane holds nothing now, or ANOTHER WRITER's
    row already stands — and the sweep labelled both "held N at read, none under the lock". For the
    second that is false: a row exists and the lane may well hold it. Two states, one string, the
    string picking the wrong one (review-848-final). Driven through `backfill_claims` so the label
    the session journals is the one asserted."""
    from sqlalchemy.sql.elements import TextClause

    class _J:
        strategy_id = "QC345-003"

        def sessionmaker(self):
            from contextlib import asynccontextmanager

            class _S:
                @asynccontextmanager
                async def begin(self):
                    yield

                async def execute(self, stmt, params=None):
                    if isinstance(stmt, TextClause) and "pg_advisory_xact_lock" in str(stmt):
                        return None
                    if isinstance(stmt, TextClause):
                        return SimpleNamespace(all=lambda: [("AMAT", 6.0)])      # a row is there now
                    raise AssertionError(f"wrote {type(stmt).__name__} over a row that exists")

            @asynccontextmanager
            async def _cm():
                yield _S()

            return _cm()

    import strategies.claims_backfill as m
    real_existing = m._existing_claims
    try:
        # The sweep's up-front read sees no claim (the row appears only under the lock).
        m._existing_claims = lambda _j, _s: _fake_existing({})
        out = asyncio.run(m.backfill_claims(_Broker({"AMAT": 6}, {"AMAT": 145.5}), _J(), "QC345-003"))
    finally:
        m._existing_claims = real_existing
    assert out["adopted"] == 0, out
    assert out["refused"] == ["AMAT(a row stands under the lock — another writer's, not an adoption)"], out
    assert not any("none under the lock" in r for r in out["refused"]), out
