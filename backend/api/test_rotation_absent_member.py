"""One never-arriving symbol must not hold the whole market compass dark (#606).

THE DEFECT, measured on staging-ibkr 2026-08-27: XLU/XLRE/XLP were not in the IBKR instrument
provider, so every `request_bars` for them was refused with "instrument not found" — and
`_refresh_rotation`'s seeding gate

    if short:
        self._publish("rotation", {..., "seeding": "... requested, not yet delivered"})
        return

returns on ANY short instrument. A symbol the venue can never serve keeps `short` non-empty for the
life of the process, `build_payload` is never called, and the compass publishes no axis, forever,
while claiming to be seeding. "Hasn't arrived yet" and "will never arrive" are one state.

THREE STATES, NEVER TWO (repo rule). Known-ready, known-short-but-expected, and never-told-us. The
third must become its own answer after a BOUNDED, STATED condition — here an EVENT COUNT, not a
wall clock: `_ROTATION_ABSENT_TICKS` consecutive refresh ticks in which the member gained no bars
while at least one sibling was fully satisfied. Event-count because the refresh runs off the
engine's timer, so backtests and slow feeds classify identically; sibling-satisfaction because a
cold cache after a restart has NO satisfied sibling and must stay seeding indefinitely (the branch's
own comment defends exactly that, and it is right).

DEGRADED LOUDLY. The published frame carries the missing members BY NAME (`payload["absent"]`) plus
the per-axis reasons `build_payload` already records in `errors` — a consumer can always tell a
23-axis degraded compass from a 25-axis full one. And absence is a timestamp, not a property: the
member keeps being re-requested every tick, and the moment its bars land it is graded again.

Driven at the REAL SEAM: the unbound `UiFeedStrategy._refresh_rotation` against a host double, the
same pattern as `test_rotation_resolves_in_one_fetch` — the real gating code, the real
`_rotation_ids_cached` cache-hit path, the real `bar_type`, the real `bars_to_tuples`, the real
`build_payload` and grading maths. Only the Nautilus cache, clock, log and publish plane are doubles.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from types import MethodType, SimpleNamespace

import pandas as pd

ABSENT = "XLU"  # in `rotation_tickers()`, and one of the three symbols measured missing on staging


class _Bar:
    """Shaped like what production emits: int-able `ts_event` (ns), float-able ohlcv.

    `bars_to_tuples` reads exactly these fields; a plain object exercises the same conversion the
    live cache bars do.
    """

    def __init__(self, ts_ns: int, px: float) -> None:
        self.ts_event = ts_ns
        self.open = px
        self.high = px * 1.01
        self.low = px * 0.99
        self.close = px
        self.volume = 1_000_000.0


def _series(seed: int, n: int) -> list[_Bar]:
    base = dt.datetime(2024, 1, 2, 16, tzinfo=dt.UTC)  # 16:00 UTC — one ET session date per bar
    return [
        _Bar(
            int((base + dt.timedelta(days=i)).timestamp() * 1e9),
            50 + seed + 0.02 * i + 2 * math.sin(i / 9 + seed),
        )
        for i in range(n)
    ]


class _Cache:
    def __init__(self) -> None:
        self.store: dict[str, list] = {}

    def bars(self, bt) -> list:
        return self.store.get(str(bt), [])


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg, *a, **k) -> None:
        self.lines.append(str(msg))

    warning = debug = error = info


def _host(missing: tuple[str, ...] = (ABSENT,), bars_per_sibling: int = 620):
    """A host double around the real methods, with `missing` symbols never delivering a bar."""
    from nautilus_trader.model.identifiers import InstrumentId

    from api.bar_spec import bar_type
    from api.engine_node import UiFeedStrategy, _ROTATION_MIN_BARS
    from strategies.rotation_from_cache import rotation_tickers

    wanted = rotation_tickers()
    iids = {t: InstrumentId.from_str(f"{t}.ARCX") for t in wanted}
    cache = _Cache()
    for j, t in enumerate(wanted):
        if t in missing:
            continue
        cache.store[str(bar_type(iids[t], "1d"))] = _series(j, bars_per_sibling)

    host = SimpleNamespace(
        cache=cache,
        clock=SimpleNamespace(utc_now=lambda: pd.Timestamp("2026-08-28", tz="UTC")),
        log=_Log(),
        _rotation=None,
        # Pre-seeded resolution cache + the REAL method, so the real cache-hit branch runs and no
        # worker thread is spawned. Every configured ticker resolves — the defect under test is
        # bars-never-arriving, not resolution (that is #622's seam).
        _rotation_iids=iids,
        _rotation_iids_key=tuple(wanted),
        # Production's __init__ always sets this (#616); None is the hand-built-double state the
        # payload stamps as 'undeclared'. Absent would be an AttributeError production cannot raise.
        _daily_bars_cover=None,
        requested=[],
        published=[],
    )
    host._rotation_ids_cached = MethodType(UiFeedStrategy._rotation_ids_cached, host)
    host._refresh_rotation = MethodType(UiFeedStrategy._refresh_rotation, host)
    host._request_bars_paced = lambda bt, **kw: host.requested.append(str(bt))
    host._publish = lambda channel, payload: host.published.append((channel, payload))
    host._min_bars = _ROTATION_MIN_BARS
    host._bar_type = bar_type
    return host


def _tick(host, n: int = 1) -> None:
    for _ in range(n):
        asyncio.run(host._refresh_rotation())


def _absent_ticks() -> int:
    """The stated bound, read from production. Defaulted generously when it does not exist yet so
    the DEFECT assertion below goes red on the behaviour, not on an import."""
    from api import engine_node

    return int(getattr(engine_node, "_ROTATION_ABSENT_TICKS", 3))


# ---------------------------------------------------------------------------------------------
# Fixture properties FIRST. If the siblings are not genuinely ready, "published anyway" and
# "blocked anyway" are indistinguishable; if the missing member could quietly arrive, "classified
# absent" and "delivered late" are indistinguishable.
# ---------------------------------------------------------------------------------------------

def test_fixture_siblings_are_ready_and_the_absent_member_never_arrives():
    host = _host()
    ready = [t for t, iid in host._rotation_iids.items() if t != ABSENT]
    for t in ready:
        n = len(host.cache.bars(host._bar_type(host._rotation_iids[t], "1d")))
        assert n >= host._min_bars, f"fixture: sibling {t} has {n} bars < {host._min_bars} — not ready"

    absent_bt = host._bar_type(host._rotation_iids[ABSENT], "1d")
    assert host.cache.bars(absent_bt) == [], "fixture: the absent member has bars"
    # NEVER-ARRIVING BY CONSTRUCTION: ticks do not fill it, because nothing in the double answers a
    # request. Re-read after driving the real refresh — still empty, while it WAS asked for.
    _tick(host, 3)
    assert host.cache.bars(absent_bt) == [], "fixture: the absent member arrived — it must never"
    assert str(absent_bt) in host.requested, (
        "fixture: the absent member was never even requested — 'filtered' and 'forgotten' would be "
        "indistinguishable"
    )


def test_fixture_grades_the_siblings_alone():
    """The 26 available members really do produce a non-empty graded payload — so if the compass
    publishes nothing, the gate is what blocked it, not the data."""
    from strategies.rotation_from_cache import build_payload, bars_to_tuples

    host = _host()

    def bars_for(t):
        return bars_to_tuples(host.cache.bars(host._bar_type(host._rotation_iids[t], "1d")))

    payload = build_payload(bars_for)
    assert len(payload["axes"]) >= 20, f"fixture cannot grade: {payload['errors']}"
    assert any(ABSENT in e["pair"] for e in payload["errors"]), (
        "fixture: no axis references the absent member, so its absence costs nothing and the test "
        "cannot distinguish 'skipped it' from 'never needed it'"
    )


# ---------------------------------------------------------------------------------------------
# THE DEFECT (#606).
# ---------------------------------------------------------------------------------------------

def test_one_never_arriving_symbol_does_not_hold_the_compass_dark():
    """Seen red 2026-08-29 before the fix: every publish was `seeding, 27 of 27` with `axes: []`,
    exactly the loop measured on staging — `short` never empties, `build_payload` never runs."""
    host = _host()
    _tick(host, _absent_ticks() + 3)

    assert host.published, f"nothing published at all; log: {host.log.lines[-3:]}"
    channel, payload = host.published[-1]
    assert channel == "rotation"
    assert payload.get("axes"), (
        f"the compass is still dark after {_absent_ticks() + 3} refreshes with 26 of 27 members "
        f"fully seeded — one never-arriving symbol blocks everything; last publish: "
        f"{ {k: v for k, v in payload.items() if k != 'axes'} }; log: {host.log.lines[-3:]}"
    )
    assert "seeding" not in payload, (
        "a compass graded from 26 ready members must not still claim 'requested, not yet delivered' "
        "about a member that will never deliver"
    )
    # DEGRADED LOUDLY, NEVER SILENTLY: the frame names the missing member, so a consumer can tell
    # this 23-axis read from a full 25-axis one without diffing axis lists.
    absent = payload.get("absent")
    assert absent and ABSENT in absent.get("symbols", []), (
        f"the degraded frame does not name its missing member — silent narrowing; got {absent!r}"
    )
    # And the last good payload is retained for the keep-last-known path.
    assert host._rotation is payload


def test_a_cold_cache_stays_seeding_forever():
    """The transient path is the thing the seeding branch defends, and it must survive the fix: with
    NO satisfied sibling there is no evidence of 'never', however many ticks pass — a wall-clock or
    a bare tick-counter would go red here."""
    host = _host(missing=tuple(host_wanted()) )
    _tick(host, _absent_ticks() + 10)
    for _channel, payload in host.published:
        assert "seeding" in payload, "a fully cold cache was classified instead of left seeding"
        assert not payload.get("axes"), "a fully cold cache produced axes from nothing"
        assert not payload.get("absent"), (
            "members were declared absent while NO sibling had arrived — after a restart that "
            "blacklists the whole universe during normal bootstrap"
        )


def host_wanted() -> list[str]:
    from strategies.rotation_from_cache import rotation_tickers

    return rotation_tickers()


def test_an_absent_member_that_finally_arrives_is_graded_again():
    """Absence is a timestamp, not a property (repo rule; codex on #606: a filter keying on one
    observation would have permanently blacklisted 21 instruments IBKR serves fine). The member is
    re-requested every tick, and when bars land it rejoins the compass."""
    host = _host()
    _tick(host, _absent_ticks() + 3)
    assert host.published[-1][1].get("absent"), "precondition: the member was not classified absent"

    requests_before = host.requested.count(str(host._bar_type(host._rotation_iids[ABSENT], "1d")))
    assert requests_before >= _absent_ticks() + 3, (
        f"the absent member stopped being requested ({requests_before} requests) — classification "
        f"became a permanent blacklist"
    )

    j = list(host._rotation_iids).index(ABSENT)
    host.cache.store[str(host._bar_type(host._rotation_iids[ABSENT], "1d"))] = _series(j, 620)
    _tick(host, 1)
    _channel, payload = host.published[-1]
    assert not payload.get("absent"), f"the member arrived but is still reported absent: {payload['absent']!r}"
    assert any(ABSENT in a.get("pair", "") for a in payload["axes"]), (
        "the arrived member's axes were not graded — late arrival healed the report but not the read"
    )
