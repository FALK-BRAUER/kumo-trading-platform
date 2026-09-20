"""The one caller that fetches once and hands the SAME objects to gate and backfill (#734, #738).

WHY THIS EXISTS AT ALL. `verify_at_now` and `backfill_sessions` each take activities and a resolver,
and review's finding was that nothing bound the two calls: gate against a full fetch, backfill against
one truncated by a lookback window, and `agrees` is HONESTLY true while every row is wrong. The
activities half is now bound by a fingerprint. The resolver half cannot be — it is a function, and
hashing one checks identity rather than behaviour.

So the binding is STRUCTURAL: one function builds each input exactly once and passes the same object
to both. That is the fix #738 describes, and it is why this is a module rather than two call sites.
"""

from __future__ import annotations

import asyncio

import pytest

from api.eod_backfill_job import instrument_map, run_backfill, strategy_map


class _Iid:
    """Nautilus's InstrumentId, and it splits on the LAST dot — measured against the installed
    package, not assumed:

        AEM.XNYS     -> symbol='AEM'     venue='XNYS'
        BRK.B.XNYS   -> symbol='BRK.B'   venue='XNYS'

    THE FIRST VERSION OF THIS DOUBLE SPLIT ON THE FIRST DOT and turned `BRK.B.XNYS` into `BRK`. It
    failed the dotted-symbol test, and the failure was the DOUBLE being wrong rather than the code —
    which is the interesting direction, because a double that cannot represent production would have
    made me "fix" working code to match a broken fixture. Split on the last dot, the way the real
    class does.
    """

    def __init__(self, text):
        self._t = text
        self.symbol = text.rsplit(".", 1)[0] if "." in text else text

    def __str__(self):
        return self._t


class _Order:
    def __init__(self, venue_order_id, strategy_id):
        self.venue_order_id = venue_order_id
        self.strategy_id = strategy_id


class _Px:
    def __init__(self, v):
        self._v = float(v)

    def as_double(self):
        return self._v


class _Position:
    def __init__(self, lane, instrument, qty, avg_px=100.0):
        self.strategy_id = lane
        self.instrument_id = instrument
        self.signed_qty = qty
        self.avg_px_open = _Px(avg_px)

    def unrealized_pnl(self, price):
        if not hasattr(price, "as_double"):
            raise TypeError("takes a Price, not a float")
        return _Px(10.0)


class _Cache:
    def __init__(self, positions=(), orders=(), instruments=()):
        self._p, self._o, self._i = list(positions), list(orders), list(instruments)

    def positions_open(self):
        return list(self._p)

    def orders(self):
        return list(self._o)

    def instrument_ids(self):
        return list(self._i)

    def price(self, instrument_id, price_type):
        return _Px(120.0)


class _Store:
    def __init__(self):
        self.calls = []

    async def write(self, rows, manifests):
        from api.eod_observation_store import WriteResult
        self.calls.append((rows, manifests))
        return WriteResult(len(rows), 0, len(manifests), len(manifests))


class _Http:
    """Counts fetches, because "fetch once" is the entire point of this module."""

    def __init__(self, activities):
        self._a = activities
        self.fetches = 0

    async def list_activities(self, activity_type=None):
        self.fetches += 1
        return list(self._a)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)

    def warning(self, m):
        self.lines.append(m)

    def error(self, m):
        self.lines.append(m)


class _Actor:
    def __init__(self, cache, http):
        self.cache = cache
        self._http = http
        self.log = _Log()


FILL = {"activity_type": "FILL", "symbol": "AEM", "side": "buy", "qty": "10",
        "price": "100", "transaction_time": "2026-08-20T13:00:00Z", "order_id": "v-1"}
NOT_A_FILL = {"activity_type": "PTP", "symbol": "AEM", "net_amount": "-990.46",
              "transaction_time": "2026-08-20T13:00:00Z"}


def _actor():
    return _Actor(
        _Cache(positions=[_Position("MOMENTUM-002", "AEM.XNYS", 10.0)],
               orders=[_Order("v-1", "MOMENTUM-002")],
               instruments=[_Iid("AEM.XNYS"), _Iid("WPM.XNYS")]),
        _Http([FILL, NOT_A_FILL]),
    )


# ==================================================================================================
# THE MAPS COME FROM THE ENGINE'S OWN CACHE
# ==================================================================================================
def test_the_instrument_map_comes_from_the_ENGINES_cache_not_a_string_split():
    """The cache is the authority on what instruments exist — it is what `observe_lanes` keys on, so
    using anything else guarantees the two sides disagree.

    And it is built from `InstrumentId.symbol`, NEVER from `str(iid).split(".")[0]`: the live database
    holds both `BRK.B` and `BRKB`, so that parse turns `BRK.B.XNYS` into `BRK` — a different company.
    """
    cache = _Cache(instruments=[_Iid("AEM.XNYS"), _Iid("BRK.B.XNYS")])
    m = instrument_map(cache)
    assert m("AEM") == "AEM.XNYS"
    assert m("BRK.B") == "BRK.B.XNYS", "a dotted symbol must survive"
    assert m("NEVER-TRADED") is None, "an unknown symbol resolves to None, never to itself"


def test_the_strategy_map_joins_on_the_VENUE_order_id_the_way_the_realized_sweep_does():
    """One join, already proven at 100% coverage from 2026-08-17. A second implementation of it would
    drift from the number the realized panel reports."""
    cache = _Cache(orders=[_Order("v-1", "MOMENTUM-002"), _Order("v-2", "BCTROT-004")])
    s = strategy_map(cache)
    assert s({"order_id": "v-1"}) == "MOMENTUM-002"
    assert s({"order_id": "v-2"}) == "BCTROT-004"
    assert s({"order_id": "v-unknown"}) is None, "an unjoinable fill is UNCLAIMED, never guessed"
    assert s({}) is None


# ==================================================================================================
# FETCH ONCE — the structural binding
# ==================================================================================================
def test_the_ledger_is_FETCHED_EXACTLY_ONCE_and_the_SAME_list_reaches_gate_and_backfill():
    """THE POINT OF THE MODULE. Two fetches is two ledgers, and a gate certified against one of them
    says nothing about the other — honestly, with nothing in the call looking wrong. The fingerprint
    would catch it; fetching once means it never has to."""
    actor, store = _actor(), _Store()
    report = asyncio.run(run_backfill(
        actor, store, sessions=["2026-08-20"], ledger_start="2026-08-01",
        marks_for=lambda d: {"AEM": 110.0}, currency="USD", snapshot_ts_for=lambda d: 1,
        gate_as_of="2026-08-20",
    ))
    assert actor._http.fetches == 1, "the ledger must be fetched once, not once per consumer"
    assert report.gate.activities_fingerprint is not None
    assert report.refusal is None, report.refusal


def test_only_FILL_activities_reach_the_matcher():
    """The ledger carries fees, withholdings and other cash movements. `_match` counts sales against
    their opening buys, and a PTP withholding is not a sale — feeding it one is how a residual becomes
    meaningless."""
    actor, store = _actor(), _Store()
    asyncio.run(run_backfill(
        actor, store, sessions=["2026-08-20"], ledger_start="2026-08-01",
        marks_for=lambda d: {"AEM": 110.0}, currency="USD", snapshot_ts_for=lambda d: 1,
        gate_as_of="2026-08-20",
    ))
    rows, _ = store.calls[0]
    assert len(rows) == 1 and rows[0]["qty"] == 10.0, "the non-FILL row must not have moved the book"


def test_rows_land_in_the_ENGINES_namespace():
    actor, store = _actor(), _Store()
    asyncio.run(run_backfill(
        actor, store, sessions=["2026-08-20"], ledger_start="2026-08-01",
        marks_for=lambda d: {"AEM": 110.0}, currency="USD", snapshot_ts_for=lambda d: 1,
        gate_as_of="2026-08-20",
    ))
    rows, _ = store.calls[0]
    assert rows[0]["instrument_id"] == "AEM.XNYS"


def test_a_FAILING_gate_writes_nothing_and_the_reason_reaches_the_caller():
    """End to end through the real gate: the engine holds 10 AEM and the ledger is emptied, so the
    two derivations genuinely disagree. Nothing may be written."""
    actor = _Actor(
        _Cache(positions=[_Position("MOMENTUM-002", "AEM.XNYS", 10.0)],
               orders=[_Order("v-1", "MOMENTUM-002")], instruments=[_Iid("AEM.XNYS")]),
        _Http([]),                                   # the engine holds a position no fill explains
    )
    store = _Store()
    report = asyncio.run(run_backfill(
        actor, store, sessions=["2026-08-20"], ledger_start="2026-08-01",
        marks_for=lambda d: {}, currency="USD", snapshot_ts_for=lambda d: 1,
        gate_as_of="2026-08-20",
    ))
    assert store.calls == []
    assert not report.gate.agrees
    assert "held by only one derivation" in str(report.gate.disagreements)


def test_the_GATE_is_run_against_TODAY_not_against_the_last_BACKFILLED_session():
    """THE as_of FOOT-GUN. `verify_at_now` compares the reconstruction against the LIVE cache, which
    is today's book — so its `as_of` must be today. Gating at `sorted(sessions)[-1]` truncated the
    ledger at a PAST date and compared that to the present book: any fill since makes the gate
    disagree spuriously, and when it does pass for a past date it passes by coincidence, because no
    fills happened since. Every test on the branch fixtured `cache == book(last session)`, so the
    difference was invisible.

    It fails CLOSED, so no wrong row could land — but a gate that refuses for a reason unrelated to
    the data is a gate people learn to re-run until it agrees.
    """
    actor = _Actor(
        _Cache(positions=[_Position("MOMENTUM-002", "AEM.XNYS", 15.0)],
               orders=[_Order("v-1", "MOMENTUM-002"), _Order("v-2", "MOMENTUM-002")],
               instruments=[_Iid("AEM.XNYS")]),
        _Http([FILL, {**FILL, "order_id": "v-2", "qty": "5",
                      "transaction_time": "2026-08-27T13:00:00Z"}]),
    )
    store = _Store()
    report = asyncio.run(run_backfill(
        actor, store, sessions=["2026-08-20"], ledger_start="2026-08-01",
        marks_for=lambda d: {"AEM": 110.0}, currency="USD", snapshot_ts_for=lambda d: 1,
        gate_as_of="2026-08-28",
    ))
    # FIXTURE PROPERTY FIRST: the engine holds 15 while the requested SESSION's book holds only 10,
    # so gating at the session date would disagree and gating at today agrees.
    rows, _ = store.calls[0]
    assert rows[0]["qty"] == 10.0, "the written row is the SESSION's book"
    assert report.gate.agrees, (
        f"the gate compares against the live cache, which holds 15: {report.gate.verdict}"
    )


def test_gate_as_of_is_REQUIRED_rather_than_defaulted_to_the_last_session():
    """The default was the whole bug: it produced a plausible date that made the mistake invisible.
    A caller must state which instant it is gating."""
    with pytest.raises(TypeError, match="gate_as_of"):
        asyncio.run(run_backfill(
            _actor(), _Store(), sessions=["2026-08-20"], ledger_start="2026-08-01",
            marks_for=lambda d: {}, currency="USD", snapshot_ts_for=lambda d: 1,
        ))


# ==================================================================================================
# PROVIDER NEUTRALITY — the capture works on both venues, the backfill does not
# ==================================================================================================
def test_a_venue_with_NO_ACTIVITY_LEDGER_is_REFUSED_by_name_rather_than_crashing():
    """THE FEATURE MUST WORK ON ALPACA AND IBKR, and half of it cannot — so the half that cannot must
    say so instead of raising `AttributeError` on `None`.

    `list_activities` exists ONLY on the Alpaca client; an IBKR node has no `_http` at all, which is
    why `_refresh_realized_periods` already degrades with "supplies no activity ledger this engine
    can read". The BACKFILL reconstructs history from that same ledger, so it cannot run there.

    WHAT STILL WORKS ON BOTH, and the split is not obvious: the forward CAPTURE reads only
    `cache.strategy_ids`, `cache.price` and the Position's own fields — all Nautilus-native, no venue
    client — and the READ PATH reads the table. Only the historical reconstruction is Alpaca-only.

    The consequence for an IBKR instance is that 1W/1M/3M stay dark until enough closes accumulate.
    That is a real limitation, and it must be REPORTED rather than discovered as a stack trace at
    04:20 SGT."""
    class _NoLedger:
        cache = _Cache(instruments=[_Iid("AEM.XNYS")])
        _http = None
        log = _Log()

    store = _Store()
    with pytest.raises(RuntimeError, match="no activity ledger"):
        asyncio.run(run_backfill(
            _NoLedger(), store, sessions=["2026-08-25"], ledger_start="2026-08-01",
            gate_as_of="2026-08-25", marks_for=lambda d: {}, currency="USD",
            snapshot_ts_for=lambda d: 1,
        ))
    assert store.calls == [], "nothing may be written when the history cannot be read"


def test_the_engine_wires_a_marks_source_that_CAN_REFUSE():
    """I WROTE THE REFUSAL AND THEN WIRED PAST IT.

    `backfill_sessions` distinguishes "no bar for that symbol" — an empty dict, a real answer about
    prices, recorded per position as an unknown valuation — from "we do not know whether bars exist",
    which RAISES and refuses the session. The engine passed `lambda d: marks.get(d, {})`, which
    CANNOT RAISE, so the refusal branch was unreachable in production.

    The cost of that would have been permanent: a session with no cached bars writes rows with
    `mark_px` NULL, base rows are append-only, and at one `method_version` those days are unfixable
    without a version bump. One unpriced symbol at the 1W base makes that lane's headline window an
    em dash for the life of the row — and reconstruction prices from bars held in PROCESS MEMORY,
    seeded per symbol, so missing coverage is likely rather than exotic.

    Refusing is recoverable; writing is not."""
    from api.engine_node import _marks_or_raise

    source = _marks_or_raise({"2026-08-24": {"AEM": 110.0}})

    # A day it HAS is answered normally.
    assert source("2026-08-24") == {"AEM": 110.0}

    # FIXTURE PROPERTY FIRST: the absent day must genuinely not be in the map, or the raise below
    # proves nothing about which branch fired.
    assert "2026-08-25" not in {"2026-08-24": {}}
    with pytest.raises(LookupError, match="no cached daily bars"):
        source("2026-08-25")

    # AND AN EMPTY DAY IS STILL AN ANSWER, not a refusal — "no bar for that symbol" is a real fact
    # about prices and must not be confused with "we could not ask".
    assert _marks_or_raise({"2026-08-24": {}})("2026-08-24") == {}
