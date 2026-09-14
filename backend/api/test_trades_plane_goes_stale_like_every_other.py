"""The trades plane served a 15-minute-old `seeding` frame while the engine was publishing a full book.

MEASURED 2026-08-26, both tenants, ~2.5 hours before the US open. The operator found it by opening the app;
`/health` said `status=ok` throughout.

    redis  ui:state:trades   TTL 5   -> {"trades": [AEM.XNYS BCTROT-004 HELD, ...31 cycles]}
    REST   /trades                   -> {"trades": [], "status": "seeding"}
    postgres trade_cycle             -> 31 rows with closed_ts IS NULL

The UI does `data.trades.filter(t => t.is_engaged)`, so an empty array empties the book. The Portfolio
tab showed ONE row — the unclaimed broker position — and every strategy-owned position was invisible.
Restarting the API alone restored 31 trades in under 15 seconds. The engine was never at fault.

THE DEFECT IS A MISSING STALENESS GUARD, AND THIS FILE IS ABOUT THE INCONSISTENCY.

`api/consumer.py` applies one policy to every other plane:

    "armed_lanes":      self._health.get("armed_lanes", {}) if bridge_ok else {},
    "next_fire_ns":     self._health.get("next_fire_ns", {}) if bridge_ok else {},
    "reconcile_drift":  self._health.get("reconcile_drift", []) if bridge_ok else [],

with its own reason recorded: "a lane that WAS armed when the engine died is not armed now, it is not
running at all, and reporting the last known value would show a dead lane as ready to trade."

`trades()` and `trades_health()` return `self._trades` and `self._trades_status` unconditionally. So
the plane carrying the BOOK — the one that matters most — is the only one with no relationship
between what it serves and whether anything is still publishing.

WHY `status` MAKES IT WORSE. A stale `ok` frame at least shows real positions. A stale `seeding` frame
shows an empty book AND tells the UI that emptiness is legitimate, so the tile renders "Reconciling
with the broker…" indefinitely. The false claim is more convincing than the missing data.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

CONSUMER = pathlib.Path(__file__).parent / "consumer.py"


def _method(name: str) -> str:
    src = CONSUMER.read_text()
    i = src.index(f"def {name}(self)")
    ends = [x for x in (src.find("\n    def ", i + 10), src.find("\n    async def ", i + 10)) if x > 0]
    return src[i:min(ends)]


def test_the_fixture_can_see_both_accessors():
    """Every assertion below reads these two. Renamed, and this file passes over nothing."""
    src = CONSUMER.read_text()
    assert "def trades(self)" in src and "def trades_health(self)" in src, (
        "the trades accessors moved — this file is blind")


def test_the_OTHER_planes_really_do_guard_on_bridge_ok():
    """THE FIXTURE'S OWN PROPERTY. The claim below is that trades is INCONSISTENT with its siblings —
    if the siblings turned out not to guard either, this file would be arguing for a rule that does
    not exist."""
    src = CONSUMER.read_text()
    guarded = [ln for ln in src.splitlines() if "if bridge_ok else" in ln]
    assert len(guarded) >= 4, (
        f"only {len(guarded)} planes guard on bridge_ok — the consistency argument below has no basis")


def test_TRADES_drops_to_EMPTY_when_the_bridge_is_stale():
    """THE DEFECT. `self._trades` was returned unconditionally, so a frame from boot is served forever."""
    body = _method("trades")
    assert "bridge_ok" in body or "_stale" in body, (
        "trades() returns the last frame with no staleness check — a book from fifteen minutes ago "
        "is indistinguishable from the book now, and an empty one from boot renders as 'no positions'")


def test_TRADES_STATUS_also_stops_claiming_when_the_bridge_is_stale():
    """The status is the worse half. A stale `seeding` does not just withhold the book — it TELLS the
    UI the emptiness is expected, so the tile shows 'Reconciling with the broker…' forever."""
    body = _method("trades_health")
    assert "bridge_ok" in body or "_stale" in body, (
        "trades_health() returns the last status unconditionally, so a stale 'seeding' keeps asserting "
        "that an empty book is legitimate")


def test_a_STALE_frame_is_not_reported_as_ok():
    """The direction that matters. Whatever the fix returns when stale, it must not be a status the UI
    reads as healthy — otherwise a dead bridge renders as a real, empty book, which is worse than
    saying nothing."""
    body = _method("trades_health")
    tree = ast.parse("class X:\n" + "\n".join("    " + ln for ln in body.splitlines()))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "ok" not in strings, (
        f"trades_health() can return a literal 'ok' — a stale bridge must never report healthy: {strings}")


def test_the_LIVE_path_is_unchanged():
    """The property the fix must not break: with a live bridge, the plane serves what it has. A guard
    that empties the book on a healthy stack is far worse than the bug."""
    from api.consumer import RedisConsumer

    assert hasattr(RedisConsumer, "trades") and hasattr(RedisConsumer, "trades_health")
    sig = inspect.signature(RedisConsumer.trades)
    assert list(sig.parameters) == ["self"], (
        "trades() grew a parameter — every caller in app.py passes none")
