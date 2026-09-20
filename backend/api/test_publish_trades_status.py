"""#298 — the trades frame must say WHICH kind of empty it is.

On 2026-08-14 a NameError in the projection made every tick raise. The engine survived (correct), logged
(correct), and published NOTHING — so the UI rendered an empty book while eight positions worth $68k were
held at the broker, and said nothing was wrong.

An empty book is indistinguishable from a LIQUIDATED book, and those demand opposite reactions. There is
also a legitimate empty window at startup while reconciliation runs. Three states, one rendering.

The real `_publish_trades` is bound to a double here rather than reimplemented — five defects on
2026-08-14 hid behind hand-written stand-ins for the code under test.
"""

from __future__ import annotations

import threading

from api.engine_node import UiFeedStrategy


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


class _Fake:
    """Only what `_publish_trades` touches."""

    def __init__(self, *, seeded=True, projections=None):
        self.clock = _Clock()
        self.log = _Log()
        self.cache = object()   # production reads self.cache and hands it to project()
        self._cycles_seeded = seeded
        self._trade_cycles = projections if projections is not None else {}
        self._cycle_store = None
        self._loop = None
        self.published: list[tuple[str, dict]] = []
        self._last_good_trades: list = []
        self._broker_stop_prices: dict = {}
        # Set in production's `__init__` (#322). Present here because the double must be able to
        # represent production — the alternative, a `getattr` default in the frame, would make a
        # genuinely missing attribute look like "not swept yet" forever.
        self._realized_periods = None
        #: The closed-leg registry and its seed status (#846) — set in production's `__init__`, present
        #: here for the same reason as `_realized_periods` above: the double must represent production.
        self._closed_legs: dict = {}
        self._legs_restored = 0
        self._legs_seed_stopped_at = None
        self._legs_seeded = False
        #: `_publish_trades` projects UNDER THIS LOCK (#568) — `project()` mutates, so it is a
        #: writer. Built the way production builds it.
        self._projection_lock = threading.RLock()

    def _publish(self, key, payload):
        self.published.append((key, payload))

    def _mark_cycle_financials(self, dtos):
        pass

    # The REAL method, bound to the double (#233). Production calls it from `_publish_trades`, and a
    # stand-in would hide two things at once: that the frame carries the field at all, and that the
    # method degrades gracefully when `self.cache` cannot answer — which is exactly this double, whose
    # cache is a bare `object()`. A healthy projection must stay "ok" through that.
    _session_realized = UiFeedStrategy._session_realized
    _realized_windows = UiFeedStrategy._realized_windows
    _lane_flows = UiFeedStrategy._lane_flows  # the frame carries lane flows too (#699 a)
    _realized_legs_status = UiFeedStrategy._realized_legs_status

    def _mark_broker_stop_prices(self, dtos):
        pass


class _Proj:
    def __init__(self, dtos=None, raises=None):
        self._dtos = dtos or []
        self._raises = raises

    def project(self, cache, now_ns=0):
        if self._raises:
            raise self._raises
        return self._dtos


def _run(fake):
    UiFeedStrategy._publish_trades(fake)


def _frame(fake):
    assert fake.published, "nothing was published — the UI has no way to tell empty from broken"
    return fake.published[-1][1]


def test_a_healthy_projection_reports_ok():
    fake = _Fake(projections={"MANUAL-001": _Proj(dtos=[])})
    _run(fake)
    assert _frame(fake)["status"] == "ok"


def test_still_seeding_says_SEEDING_not_an_empty_book():
    """The legitimate empty window. After a restart, reconciliation takes ~30s before positions land, and
    `held: 0` in that window is lag, not liquidation. That rule lived in a handoff document; the operator should
    not have to know it to read his own screen."""
    fake = _Fake(seeded=False, projections={"MANUAL-001": _Proj()})
    _run(fake)
    frame = _frame(fake)
    assert frame["status"] == "seeding"
    assert frame["trades"] == []


def test_a_FAILED_projection_says_so_and_carries_the_reason():
    """The 2026-08-14 case. The engine must survive — that part was right — but the failure has to reach
    the surface, or the display is confidently wrong."""
    fake = _Fake(projections={"MANUAL-001": _Proj(raises=NameError("name 'cache' is not defined"))})
    _run(fake)
    frame = _frame(fake)
    assert frame["status"] == "failed"
    assert "cache" in frame["error"]
    # And the engine still logged it, as before.
    assert fake.log.errors


def test_a_failed_projection_keeps_the_LAST_GOOD_trades_rather_than_blanking_the_book():
    """Showing the last known 8 positions marked stale is strictly better than showing none: it preserves
    the operator's mental model instead of destroying it. Blanking is what made this alarming."""
    good = [{"instrument_id": "AEM.XNYS"}]
    fake = _Fake(projections={"MANUAL-001": _Proj(dtos=[])})
    fake._last_good_trades = good

    fake._trade_cycles = {"MANUAL-001": _Proj(raises=RuntimeError("boom"))}
    _run(fake)

    frame = _frame(fake)
    assert frame["status"] == "failed"
    assert frame["trades"] == good, "the book was blanked on failure"


def test_the_engine_still_survives_a_projection_crash():
    """The quarantine is not being loosened — a display defect must never take down the trading loop."""
    fake = _Fake(projections={"MANUAL-001": _Proj(raises=RuntimeError("boom"))})
    _run(fake)  # must not raise
    assert fake.log.errors
