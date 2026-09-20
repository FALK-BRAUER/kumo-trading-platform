"""The pool is swept for symbols the venue does not list, wherever they were written (#723).

#663 put the check on cockpit's `POST /pool/source/{name}`. That is not the door the symbols come
through. The refresh that actually runs on schedule runs INSIDE the engine —
`pgjobs.py:128 -> PgSymbolPool.refresh_source(...)` — with no HTTP hop and therefore no validation.
Measured on an Alpaca paper instance 2026-08-29: `ledger_book` was refreshed at 10:32, an hour after #663 deployed, and
still carried BLLLN, IQVIA, JEPO and OVVI.

So the guard covers the MANUAL seeding path and nothing else. This sweep reads the pool that is
actually composed and reports what the venue does not list — it therefore covers the engine door,
the manual door, the operator-PIN door (GTLAB is an `exec_pool_override` row, no feed involved) and
any door nobody has found yet, because it asks about the RESULT rather than about a writer.

IT REPORTS AND DOES NOT MUTATE. Excluding a symbol or deleting a pool row is a trading-data change:
the pool decides what can be bought, and a sweep that edits it on a bad catalog read would take
names out of the market over a REST failure. Naming them is the whole job.

THREE STATES, NEVER TWO (`api/pool_validation.py`, and the rule this repo keeps relearning):
catalog-unavailable is not a clean pool. Staging is an IBKR instance with NO Alpaca catalog at all,
so "no oracle" is its permanent normal state and it must stay silent — but silent because it was
never told, which is a different fact from "nothing is wrong" and is logged as such.

WHAT THE SWEEP ACTUALLY SEES ON PAPER, run against the live 14,260-symbol catalog on 2026-08-29:

    5 of 115    BLLLN -> BLLN · GTLAB -> GTLB · IQVIA -> IQV · JEPO (none) · OVVI (none)

FIVE, not the nine of #663. Three carry a correction and two deliberately do not: `_likely_twin`
names a twin only when the catalog holds EXACTLY ONE candidate, and live JEPO has four (JEPI, JEPQ,
CEPO, JPO) while OVVI has two (OVV, WVVI). #663's text calls JEPO->JEPQ and OVVI->OVV "airtight
without any external lookup"; the shipped validator disagrees, on purpose, because a confident
wrong suggestion invites an operator to pin a symbol nobody checked.

TWO NAMES FROM #663'S NINE ARE DELIBERATELY ABSENT. BOX, DBX, GEN and NSIT are all `listed: True`
in the same catalog this sweep consults (#720) — the lanes fail to resolve them for a reason that is
NOT ticker validity, and a sweep whose only oracle says they are fine must not claim them. FTNR is
genuinely unlisted but already carries an operator EXCLUDE row, so the composed pool never offers it
and naming it would be the noise this sweep exists to remove.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from kumo_strategies.runtime.executor.pgpool import PoolEntry
from kumo_strategies.runtime.executor.store import PoolOverride, PoolSource

from api.alerts import AlertsService
from api.notify import Alert, Notifier

ON = {"enabled": True}

#: The live paper reading, trimmed to what makes the point. BLLN/BLLLN and IQV/IQVIA travel together
#: — the pair signature that makes a misread diagnosable from inside the data.
LIVE_CATALOG = {"AAPL", "BLLN", "IQV", "JEPQ", "OVV", "GTLB", "BOX", "DBX", "GEN", "NSIT"}


class _Catalog:
    """The in-process instrument catalog, as `AlertsService` sees it.

    `known_symbols()` is the real method's exact contract: `set[str]`, or `None` when the catalog
    has never loaded. Returning `None` is the third state and must NOT read as "the venue lists
    nothing" — see `api/instrument_search.py:164`.
    """

    def __init__(self, symbols):
        self._symbols = None if symbols is None else {s.upper() for s in symbols}

    def known_symbols(self):
        return self._symbols


class _Pool:
    """`PgSymbolPool` as this sweep uses it: `effective()` -> {symbol: PoolEntry}.

    Built from the REAL `PoolEntry`, not a bare object, so a change to what the composed pool
    carries breaks here rather than in production. `effective()` is the composed answer — source
    rows PLUS pins MINUS excludes (`pgpool.py:250`) — which is why the pin door is covered for free
    and why the already-excluded FTNR is not reported.
    """

    def __init__(self, *rounds):
        self._rounds, self._i = list(rounds), 0

    async def effective(self):
        syms = self._rounds[min(self._i, len(self._rounds) - 1)]
        self._i += 1
        return {s: PoolEntry(s, ("ledger_book",), False, {}) for s in sorted(syms)}


class _Node:
    def health(self):
        return {"reconcile_drift": []}

    def positions(self):
        return []

    def account(self):
        return {"equity": 1.0}


class _Tx:
    def __init__(self):
        self.sent = []

    async def send(self, alert: Alert, *, silent: bool = False) -> bool:
        self.sent.append(alert)
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Silence every OTHER check in the poll so what lands in `_Tx` is this sweep's doing.

    Each of these is guarded and tested on its own; leaving them live here would mean asserting on
    an empty list for reasons unrelated to the subject.
    """
    async def _probe(_observed):
        return []

    async def _noop():
        return None

    monkeypatch.setattr("api.app._probe_subsystems", _probe)
    yield _noop


def _svc(pool, catalog, tx, monkeypatch, noop):
    import api.app

    monkeypatch.setattr(api.app, "pool", pool)
    monkeypatch.setattr(api.app.app.state, "search", catalog, raising=False)
    svc = AlertsService(_Node(),
                        notifier=Notifier(transport=tx, dedupe_backend="memory", settings=ON),
                        poll_seconds=0.01)
    svc._announce_slots = noop
    svc._announce_never_ran = noop
    svc._announce_external_positions = noop
    svc._announce_split_divergence = noop
    # Reads Postgres since #843 (it used to read two attributes no node has, and `_checked` erased
    # the failure it recorded — so a missing database was INVISIBLE here). Stubbed like its siblings:
    # this file isolates the pool sweep, and a real "stranded_claims check is broken" page would be
    # counted as the sweep's.
    svc._announce_stranded_claims = noop
    svc._announce_lanes_bleeding = noop               # reads the journal in Postgres (#1098), same rule
    svc._digests = lambda *a, **k: noop()
    return svc


def _run_polls(svc, seconds: float = 0.06):
    """Drive the REAL entry point — `run()` — for a few polls, then cancel.

    Not the announce method and not a helper: every defect this file's siblings were written for was
    a correct unit that nothing called. The question is whether the poll loop asks.
    """
    async def drive():
        task = asyncio.create_task(svc.run())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())


class _FakeDb:
    """A sessionmaker standing in for Postgres, and NOTHING above it.

    Everything between `api.app.pool` and the database stays real here: the module attribute lookup,
    the module-level `effective()`, `_pool()` building a real `PgSymbolPool`, and that class's real
    `effective()` composing sources with pins and excludes. Only `execute` is answered from memory.

    Dispatches on the statement's own entity rather than on call order, so the double cannot quietly
    keep passing if `effective()` reorders or adds a query.
    """

    def __init__(self, sources, pins=(), excludes=()):
        self._rows = {
            PoolSource: [PoolSource(source="ledger_book", symbol=s, meta={}) for s in sources],
            PoolOverride: ([PoolOverride(symbol=s, kind="pin") for s in pins]
                           + [PoolOverride(symbol=s, kind="exclude") for s in excludes]),
        }

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        rows = self._rows[entity]        # KeyError on an unexpected query, never a silent []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))


# -- the module boundary the doubles cannot see --------------------------------------------------
def test_the_sweep_reaches_the_pool_THROUGH_THE_REAL_MODULE(monkeypatch, _isolate):
    """The attribute lookup itself, which every other test in this file patches away.

    `api.app` binds the `api.pool` MODULE (`from api import pool`, app.py:21) — not a `PgSymbolPool`.
    The first version of this sweep called `pool.effective()` believing otherwise; the module has
    `_pool`, `list_pool`, `refresh_source`, `set_override`, `clear_override`, `source_health` and no
    `effective`, so production raised AttributeError on EVERY poll. It raised BEFORE the catalog
    third-state check, so staging's by-design silence never ran and every instance would have paged
    a permanent critical "the pool_unlisted check is broken" after two polls. The sweep would have
    validated nothing, ever.

    All 14 sibling tests were green because `_svc` monkeypatches `api.app.pool` with a double that
    HAS `.effective()` — a double that cannot represent production, which is this repo's named worst
    class and the very thing this PR's description argued for.

    So this test patches ONLY BENEATH the module: the session factory. `api.app.pool` stays the real
    module, `effective()` stays the real function, and `PgSymbolPool.effective()` really composes.
    """
    import api.app
    import api.pool

    # FIXTURE PROPERTY: the real module is in place and un-doubled. If a future refactor rebinds
    # `api.app.pool` to an instance, this assertion — not a silent pass — is what says so.
    assert api.app.pool is api.pool, "api.app.pool is not the real module; this test proves nothing"
    assert not hasattr(api.pool, "_Pool"), "a double leaked into the module under test"

    monkeypatch.setattr(api.pool, "session_factory",
                        _FakeDb(sources=["AAPL", "BLLLN", "FTNR"], pins=["GTLAB"],
                                excludes=["FTNR"]))
    tx = _Tx()
    monkeypatch.setattr(api.app.app.state, "search", _Catalog(LIVE_CATALOG), raising=False)
    svc = AlertsService(_Node(),
                        notifier=Notifier(transport=tx, dedupe_backend="memory", settings=ON),
                        poll_seconds=0.01)
    svc._announce_slots = _isolate
    svc._announce_never_ran = _isolate
    svc._announce_external_positions = _isolate
    svc._announce_split_divergence = _isolate
    svc._announce_stranded_claims = _isolate          # see `_svc` — reads Postgres since #843
    svc._announce_lanes_bleeding = _isolate           # see `_svc` — reads the journal (#1098)
    svc._digests = lambda *a, **k: _isolate()
    _run_polls(svc)

    titles = [a.title for a in tx.sent]
    assert not any("broken" in t for t in titles), (
        f"the sweep raised through the real module instead of sweeping: {titles}"
    )
    bodies = " ".join(f"{a.title} {a.body}" for a in tx.sent)
    assert "BLLLN" in bodies, f"the real path swept nothing: {titles}"
    # Composition proven through the REAL `effective()`, not asserted about it: the pin arrives even
    # though no source carries it, and the excluded name is gone even though a source does.
    assert "GTLAB" in bodies, "a pin did not reach the sweep through the real composition"
    assert "FTNR" not in bodies, "an operator-excluded symbol was swept anyway"


# -- fixture properties --------------------------------------------------------------------------
def test_the_fixture_pool_holds_a_symbol_the_fixture_catalog_refuses():
    """FIXTURE PROPERTY FIRST. Every assertion below is about a refusal; if the fixture cannot
    produce one, "no alert" and "the sweep is dead" are the same green.

    Asserted in BOTH directions: the misread absent AND its twin present, because a catalog that
    happens to be empty would refuse everything and prove nothing.
    """
    assert "BLLLN" not in LIVE_CATALOG, "the fixture cannot express an unlisted symbol"
    assert "BLLN" in LIVE_CATALOG, "no twin in the catalog — no correction is reachable"
    assert {"BOX", "DBX", "GEN", "NSIT"} <= LIVE_CATALOG, (
        "#720: these four ARE listed in the same catalog this sweep consults — a fixture that "
        "refuses them would let the sweep claim four symbols it has no evidence against"
    )


# -- the sweep -----------------------------------------------------------------------------------
def test_the_sweep_names_a_symbol_the_engine_refresh_wrote_that_the_venue_does_not_list(
        monkeypatch, _isolate):
    """The #723 subject: `ledger_book` refreshed at 10:32 through the engine, no HTTP hop, no
    validation — and BLLLN sat in the pool where the ranking can select it and then silently not
    trade it."""
    tx = _Tx()
    pool = _Pool({"AAPL", "BLLLN", "BOX"})
    svc = _svc(pool, _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc)

    bodies = " ".join(f"{a.title} {a.body}" for a in tx.sent)
    assert tx.sent, "the poll loop never swept the pool — the check is not wired into run()"
    assert "BLLLN" in bodies, f"the sweep did not name the unlisted symbol: {bodies!r}"
    assert "BLLN" in bodies.replace("BLLLN", ""), (
        "the correction is what turns a refusal into a fix — the twin was not named"
    )
    assert "BOX" not in bodies, "#720: BOX is listed by this catalog and must not be claimed"


def test_an_operator_PIN_the_venue_does_not_list_is_swept_too(monkeypatch, _isolate):
    """GTLAB is an `exec_pool_override` pin from 2026-08-11, `created_by='cockpit'`, no expiry — no
    feed wrote it and no source validation can ever remove it. It reaches the composed pool through
    `effective()`, which is why sweeping the RESULT covers a door that guarding writers does not."""
    tx = _Tx()
    svc = _svc(_Pool({"AAPL", "GTLAB"}), _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc)

    bodies = " ".join(f"{a.title} {a.body}" for a in tx.sent)
    assert "GTLAB" in bodies, f"the pin door is unswept: {bodies!r}"
    assert "GTLB" in bodies.replace("GTLAB", ""), "the pin's correction was not named"


def test_a_pool_the_venue_lists_entirely_says_nothing(monkeypatch, _isolate):
    """The always-on trap. An alert that fires on a healthy pool is wallpaper by the third day."""
    tx = _Tx()
    svc = _svc(_Pool({"AAPL", "BOX", "DBX"}), _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc)

    assert tx.sent == [], f"a clean pool alarmed: {[a.title for a in tx.sent]}"


def test_no_catalog_alarms_nothing_because_an_IBKR_INSTANCE_HAS_NONE(monkeypatch, _isolate):
    """Staging runs IBKR and carries no Alpaca credential at all, so `app.state.search` is None for
    the life of the process. Refusing to answer is its permanent normal state.

    THE FIXTURE MAKES THE SILENCE MEAN SOMETHING: the pool it is given holds BLLLN, IQVIA and JEPO,
    so a sweep that treated "no oracle" as "no matches" would have three names to shout. Silence
    here is a decision about the third state, not an empty input.
    """
    tx = _Tx()
    # `None`, not `_Catalog(None)`: on an IBKR instance `app.state.search` IS None — no catalog
    # object is ever built. A double that answers `known_symbols() -> None` is a DIFFERENT state
    # (configured, never loaded), and using it here would have left staging's actual shape untested.
    svc = _svc(_Pool({"AAPL", "BLLLN", "IQVIA", "JEPO"}), None, tx, monkeypatch, _isolate)
    _run_polls(svc)

    assert tx.sent == [], (
        f"an instance with no catalog alarmed on symbols nothing could verify: "
        f"{[a.title for a in tx.sent]}"
    )


def test_a_catalog_that_loaded_zero_rows_is_the_same_third_state(monkeypatch, _isolate):
    """A broken read, not a venue that lists nothing. Empty IS the failure mode, so it must not be
    the evidence — refusing the whole pool on it would take every lane out of the market."""
    tx = _Tx()
    svc = _svc(_Pool({"AAPL", "BLLLN"}), _Catalog(set()), tx, monkeypatch, _isolate)
    _run_polls(svc)

    assert tx.sent == [], f"an empty catalog refused a whole pool: {[a.title for a in tx.sent]}"


def test_an_AMBIGUOUS_misread_is_not_reported_as_having_no_near_spelling():
    """The wording has to survive the two symbols it prints most often.

    `_likely_twin` names a correction only when the catalog holds EXACTLY ONE candidate — a wrong
    suggestion invites an operator to pin a symbol nobody checked, and its own docstring records
    OVVI -> WVVI as the false answer a ranked rule produced. Measured against the live 14,260-symbol
    catalog on 2026-08-29, JEPO has FOUR near neighbours (JEPI, JEPQ, CEPO, JPO) and OVVI has two
    (OVV, WVVI), so both arrive here with no suggestion.

    Saying "no near spelling exists, so this is a delisting" of JEPO would be the exact inversion
    this repo keeps finding: prose that reads as careful and states the opposite of the truth.
    """
    from api.pool_sweep import pool_sweep_alerts
    from api.pool_validation import validate_pool_symbols

    catalog = _Catalog({"JEPI", "JEPQ", "CEPO", "JPO"})
    verdict = validate_pool_symbols(["JEPO"], catalog)
    # FIXTURE PROPERTY: the refusal is real AND genuinely ambiguous — several near spellings, so
    # the validator declines. A fixture with one candidate would produce a twin and prove nothing.
    assert verdict.refused == ("JEPO",)
    assert verdict.suggestions == {}, "the fixture is not ambiguous — no silence to check"

    body = pool_sweep_alerts(verdict, total=1)["pool_unlisted:JEPO"].body
    assert "delisting" not in body.lower(), (
        f"the alert INFERS a delisting from a silence that means ambiguity — JEPO has four near "
        f"spellings in this catalog and JEPQ is listed and tradable: {body!r}"
    )
    assert "several" in body.lower(), (
        f"the alert does not say WHY no correction is offered, so an operator reads the silence as "
        f"'nothing close exists' and goes looking for a delisting: {body!r}"
    )


def test_an_unconsulted_catalog_yields_NO_ALERTS_EVEN_IF_THE_VERDICT_NAMES_REFUSALS():
    """The two silence tests above cannot fail, and this is the one that can.

    `validate_pool_symbols` builds a `catalog_unavailable` verdict with `refused=()`, so through the
    real seam the third state and a clean pool are the SAME EMPTY LIST — a sweep with no third-state
    guard at all would pass both of them. That is the shape this repo names "a detector that
    recognises its subject by a property the defect destroys".

    So the fixture violates the invariant directly: a verdict that says the catalog could not be
    consulted AND names two refusals. Only a guard reading `status` can answer {} to that.
    """
    from api.pool_sweep import pool_sweep_alerts
    from api.pool_validation import PoolSymbolVerdict

    impossible = PoolSymbolVerdict(status="catalog_unavailable", refused=("BLLLN", "IQVIA"))
    assert impossible.refused, "FIXTURE PROPERTY: nothing to suppress, nothing proven"
    assert pool_sweep_alerts(impossible, total=3) == {}, (
        "an unconsulted catalog produced alarms — 'never told us' was read as 'known bad'"
    )


def test_a_catalog_that_BLINKS_does_not_re_page_the_standing_set(monkeypatch, _isolate):
    """The catalog is TTL-refreshed over REST, so it can go unavailable for a poll and come back.

    If the alarmed set were pruned against an unavailable verdict — whose `refused` is empty — every
    standing symbol would be forgotten and re-announced the moment the catalog returned. A channel
    that re-pages the same five names after every transient REST failure is a muted channel, and
    then the tenth symbol is invisible again, which is the whole point of the sweep.
    """
    class _Blinking:
        """Loaded, then unavailable, then loaded again — the TTL reload failing once."""

        def __init__(self):
            self._n = 0

        def known_symbols(self):
            self._n += 1
            return None if self._n == 2 else {s.upper() for s in LIVE_CATALOG}

    tx = _Tx()
    svc = _svc(_Pool({"AAPL", "BLLLN"}), _Blinking(), tx, monkeypatch, _isolate)
    _run_polls(svc, seconds=0.1)

    paged = [a for a in tx.sent if "BLLLN" in f"{a.title} {a.body}"]
    assert len(paged) == 1, (
        f"the standing symbol paged {len(paged)} times across a catalog blink: "
        f"{[a.title for a in tx.sent]}"
    )


def test_the_two_kinds_of_MISSING_CATALOG_are_told_apart(monkeypatch, _isolate, caplog):
    """Both alarm nothing; they are not the same fact and must not read as one.

    `search is None` is an IBKR instance, which has no Alpaca catalog and never will — permanent,
    by design, nothing to do. A catalog that IS configured and has not loaded is a broken oracle
    producing an identical silence, and the only place either is visible is this log line. Merging
    them makes the second unfindable, which is "absence of evidence is a timestamp, not a property"
    with both readings collapsed onto one string.
    """
    import logging

    def _sweep_with(catalog):
        caplog.clear()
        tx = _Tx()
        svc = _svc(_Pool({"AAPL", "BLLLN"}), catalog, tx, monkeypatch, _isolate)
        with caplog.at_level(logging.INFO, logger="api.alerts"):
            _run_polls(svc)
        assert tx.sent == [], "this test is about the silent path; something alarmed"
        return " ".join(r.getMessage() for r in caplog.records)

    absent = _sweep_with(None)                  # IBKR: no catalog object exists at all
    unloaded = _sweep_with(_Catalog(None))      # Alpaca: catalog built, warm never succeeded

    # FIXTURE PROPERTY: both paths actually logged something. Two empty strings are trivially
    # unequal to nothing and would pass a naive difference check.
    assert "UNCHECKED" in absent and "UNCHECKED" in unloaded, (
        f"one of the two silent paths logged nothing: {absent!r} / {unloaded!r}"
    )
    assert absent != unloaded, (
        f"a missing catalog and an unloaded one produced the SAME line, so the broken one is "
        f"invisible: {absent!r}"
    )
    assert "not configured" in absent or "no venue catalog is configured" in absent, absent
    assert "has not loaded" in unloaded, unloaded


def test_absence_BY_DESIGN_warns_once_while_a_BROKEN_ORACLE_warns_every_poll(
        monkeypatch, _isolate, caplog):
    """The two silent states differ in WORDING and in VOLUME, and the volume is the load-bearing half.

    On ibkr-paper-retired `search is None` is permanent and correct. A WARNING every 30s for that is ~2,880
    a day describing a healthy state, at the same level as `_check_failed`'s real ones — which is how
    a log earns a mute, and a muted log defeats the loud-degradation this whole branch argues for.
    A catalog that is configured and has not loaded is the opposite: a broken oracle wearing the same
    silence, which must keep saying so.

    Same remedy as `telegram.py`'s `_warned_unconfigured`, for the same reason.
    """
    import logging

    def _warnings_over_many_polls(catalog):
        caplog.clear()
        tx = _Tx()
        svc = _svc(_Pool({"AAPL", "BLLLN"}), catalog, tx, monkeypatch, _isolate)
        with caplog.at_level(logging.DEBUG, logger="api.alerts"):
            _run_polls(svc, seconds=0.12)
        polls = [r for r in caplog.records if "pool sweep" in r.getMessage()]
        warns = [r for r in polls if r.levelno >= logging.WARNING]
        return polls, warns

    absent_polls, absent_warns = _warnings_over_many_polls(None)
    unloaded_polls, unloaded_warns = _warnings_over_many_polls(_Catalog(None))

    # FIXTURE PROPERTY: the loop really did sweep many times in BOTH cases. Without this, "1 warning"
    # and "the sweep ran once" are the same green and the test proves nothing about volume.
    assert len(absent_polls) > 2, f"too few polls to tell once from every-poll: {len(absent_polls)}"
    assert len(unloaded_polls) > 2, f"too few polls in the unloaded case: {len(unloaded_polls)}"

    assert len(absent_warns) == 1, (
        f"a permanent, by-design absence warned {len(absent_warns)} times across "
        f"{len(absent_polls)} polls — on staging that is ~2,880 a day about a healthy state"
    )
    assert len(unloaded_warns) == len(unloaded_polls), (
        f"a configured-but-unloaded catalog warned {len(unloaded_warns)} of {len(unloaded_polls)} "
        f"polls — the state that wants attention must keep saying so"
    )


def test_the_standing_set_pages_once_but_a_NEW_arrival_pages_again(monkeypatch, _isolate):
    """Why the sweep exists at all, in one test.

    Nine permanent entries in the unresolved list are what makes the TENTH invisible (#663). A
    detector that re-pages the standing set every 30s is muted within a day and then the tenth is
    invisible again — so the standing set must page ONCE, and a new name must still be news.
    """
    tx = _Tx()
    pool = _Pool({"AAPL", "BLLLN"}, {"AAPL", "BLLLN"}, {"AAPL", "BLLLN", "IQVIA"})
    svc = _svc(pool, _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc, seconds=0.1)

    bodies = [f"{a.title} {a.body}" for a in tx.sent]
    standing = [b for b in bodies if "BLLLN" in b]
    arrival = [b for b in bodies if "IQVIA" in b]
    assert len(standing) == 1, f"the standing symbol paged {len(standing)} times: {bodies}"
    assert len(arrival) == 1, f"a NEW unlisted symbol was swallowed by the dedupe: {bodies}"


def test_the_standing_set_STAYS_quiet_after_the_repeat_WINDOW_elapses(monkeypatch, _isolate):
    """The test above cannot see the sweep's own dedupe, and this one can.

    `Notifier`'s in-memory backend holds a successful send's mark for the life of the process, so in
    a test the notifier alone suppresses everything and `_pool_alarmed` looks load-bearing while
    being dead — the agreement-is-not-connection shape. PRODUCTION uses the Redis backend with a
    `repeat_after_hours` TTL (12h default), and when that expires the notifier lets the key through
    ON PURPOSE: "a condition still true tomorrow is worth repeating once".

    That default is wrong for THIS condition. The unlisted set is permanent until someone edits a
    feed — five names twice a day forever is how a channel gets muted, and a muted channel is how
    the tenth symbol goes back to being invisible. So the sweep keeps its own alarmed set on top.

    Modelled by making the dedupe window expire on every poll, which is exactly what an elapsed TTL
    does to `Notifier.send`.
    """
    tx = _Tx()
    svc = _svc(_Pool({"AAPL", "BLLLN"}), _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)

    async def _window_always_expired(_key, _ttl):
        return False

    monkeypatch.setattr(svc._n, "_already_announced", _window_always_expired)
    # FIXTURE PROPERTY: with the notifier's dedupe defeated, the transport WOULD take every send —
    # so a single page below is the sweep's doing and nothing else's.
    assert asyncio.run(svc._n._already_announced("pool_unlisted:BLLLN", 12)) is False

    _run_polls(svc, seconds=0.1)

    paged = [a for a in tx.sent if "BLLLN" in f"{a.title} {a.body}"]
    assert len(paged) == 1, (
        f"the standing set re-paged {len(paged)} times once the repeat window elapsed: "
        f"{[a.title for a in tx.sent]}"
    )


def test_a_symbol_its_GATE_refused_is_not_marked_as_though_it_had_paged(monkeypatch, _isolate):
    """Turning the switch on later must still page the standing set.

    `run()` re-reads settings every poll precisely so "enabling it in the UI takes effect without a
    restart". Marking a symbol alarmed before consulting `send()`'s answer breaks that contract for
    this alert only: with the master `enabled` on and `notify_pool_unlisted` off, `send()` refuses at
    `allowed()` and returns False, but the symbol is recorded as alarmed anyway — so flipping the
    flag on afterwards pages nothing, forever, and the switch looks broken.
    """
    import api.app

    settings = {"enabled": True, "notify_pool_unlisted": False}
    tx = _Tx()
    monkeypatch.setattr(api.app, "pool", _Pool({"AAPL", "BLLLN"}))
    monkeypatch.setattr(api.app.app.state, "search", _Catalog(LIVE_CATALOG), raising=False)
    svc = AlertsService(_Node(),
                        notifier=Notifier(transport=tx, dedupe_backend="memory", settings=settings),
                        poll_seconds=0.01)
    for name in ("_announce_slots", "_announce_never_ran", "_announce_external_positions",
                 "_announce_split_divergence", "_announce_stranded_claims", "_announce_lanes_bleeding"):
        setattr(svc, name, _isolate)
    svc._digests = lambda *a, **k: _isolate()

    _run_polls(svc)
    # FIXTURE PROPERTY: the gate really is what silenced it — the sweep ran and found BLLLN, the
    # switch refused it. Without this, an empty list here could just mean the sweep never ran.
    assert tx.sent == [], f"the gate did not silence the alert: {[a.title for a in tx.sent]}"
    assert svc._pool_alarmed == set(), (
        f"a symbol the gate REFUSED was recorded as alarmed: {svc._pool_alarmed}"
    )

    settings["notify_pool_unlisted"] = True
    _run_polls(svc)
    assert any("BLLLN" in f"{a.title} {a.body}" for a in tx.sent), (
        "turning the switch on never paged the standing set — the contract run() states in its own "
        "comment, broken for this alert alone"
    )


def test_a_TRANSPORT_FAILURE_on_the_first_page_does_not_mute_that_symbol_forever(
        monkeypatch, _isolate):
    """One Telegram blip must not silence a symbol for the life of the process.

    `Notifier` already handles this: a failed send shortens the dedupe mark to
    `_RETRY_AFTER_FAILURE_S` (15 min) so the condition is genuinely retried rather than held for the
    full 12-hour repeat window. Marking `_pool_alarmed` on ATTEMPT rather than on success overrides
    that deliberate retry from above — the sweep skips the symbol before the notifier is ever asked
    again, and the one condition it exists to report is the one condition it drops.

    The 15 minutes are modelled by setting the retry window to zero; nothing else is patched.
    """
    import api.app

    class _FlakyTx:
        """Fails the first delivery, succeeds after — one blip, exactly as Telegram produces."""

        def __init__(self):
            self.sent, self._n = [], 0

        async def send(self, alert: Alert, *, silent: bool = False) -> bool:
            self._n += 1
            if self._n == 1:
                return False
            self.sent.append(alert)
            return True

    tx = _FlakyTx()
    monkeypatch.setattr(api.app, "pool", _Pool({"AAPL", "BLLLN"}))
    monkeypatch.setattr(api.app.app.state, "search", _Catalog(LIVE_CATALOG), raising=False)
    notifier = Notifier(transport=tx, dedupe_backend="memory", settings=ON)
    monkeypatch.setattr(type(notifier), "_RETRY_AFTER_FAILURE_S", 0)
    svc = AlertsService(_Node(), notifier=notifier, poll_seconds=0.01)
    for name in ("_announce_slots", "_announce_never_ran", "_announce_external_positions",
                 "_announce_split_divergence", "_announce_stranded_claims", "_announce_lanes_bleeding"):
        setattr(svc, name, _isolate)
    svc._digests = lambda *a, **k: _isolate()

    _run_polls(svc, seconds=0.1)

    # FIXTURE PROPERTY: the transport really was asked more than once, so a single delivery below is
    # a retry that succeeded and not an attempt that never happened.
    assert tx._n > 1, "the transport was asked once — the fixture cannot express a retry"
    assert any("BLLLN" in f"{a.title} {a.body}" for a in tx.sent), (
        "one transport blip muted the symbol for the life of the process, defeating the notifier's "
        "own 15-minute failed-send retry"
    )


def test_a_symbol_that_is_FIXED_and_later_returns_pages_again(monkeypatch, _isolate):
    """The alarmed set must forget a symbol that left the pool, or a corrected feed that regresses
    a week later is silent — the once-and-never-again failure the drift clear exists for."""
    tx = _Tx()
    pool = _Pool({"AAPL", "BLLLN"}, {"AAPL"}, {"AAPL"}, {"AAPL", "BLLLN"})
    svc = _svc(pool, _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc, seconds=0.12)

    paged = [a for a in tx.sent if "BLLLN" in f"{a.title} {a.body}"]
    assert len(paged) == 2, (
        f"a symbol fixed and then reintroduced paged {len(paged)} times, expected 2: "
        f"{[a.title for a in tx.sent]}"
    )


def test_the_sweep_reports_and_never_mutates_the_pool(monkeypatch, _isolate):
    """REPORT, DO NOT MUTATE. An automatic exclude is a trading-data change made on the strength of
    a REST call that can fail, and `must_liquidate` turns an exclude into a SALE at the next
    session. The double has no writer at all, so any attempt to edit raises here."""
    tx = _Tx()
    pool = _Pool({"AAPL", "BLLLN"})
    svc = _svc(pool, _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc)

    assert not hasattr(pool, "set_override"), "the double must not offer a writer to reach for"
    assert tx.sent, "the sweep reported nothing, so 'it did not mutate' proves nothing"


def test_a_broken_sweep_reports_ITSELF(monkeypatch, _isolate):
    """A swallowed exception on every poll is indistinguishable from a clean poll. If the pool read
    raises, silence from this check must not read as a clean pool — it goes through `_checked` so
    BROKEN_CHECK_POLLS eventually says the check is broken."""
    class _Broken:
        async def effective(self):
            raise RuntimeError("pool read wedged")

    tx = _Tx()
    svc = _svc(_Broken(), _Catalog(LIVE_CATALOG), tx, monkeypatch, _isolate)
    _run_polls(svc, seconds=0.1)

    titles = [a.title for a in tx.sent]
    assert any("pool_unlisted" in t and "broken" in t for t in titles), (
        f"a wedged sweep stayed silent and silence read as a clean pool: {titles}"
    )
