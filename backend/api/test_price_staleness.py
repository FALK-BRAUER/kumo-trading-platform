"""A price used to size an order must carry when it was observed.

WHY THIS FILE EXISTS
--------------------
`_last_price_for` (`engine_node.py:3091`) returns a bare `float`. Both sources it reads from carry an
observation time — a `TradeTick` has `ts_event`, a `Bar` has `ts_event` — and both are discarded at the
boundary. Nothing downstream can tell a price from this second from one from last Tuesday.

That is not theoretical here. On 2026-08-18 the Alpaca data websocket was disconnected for a 27-minute
window and flapping either side of it; the engine kept serving the last cached trade throughout, and no
component could have known. A stale price sizes a protective stop at the wrong width and sizes an entry
at the wrong quantity, and neither failure announces itself.

WHY THE GUARD ELSEWHERE DOES NOT COVER THIS
-------------------------------------------
kumo-trading-strategies has its own staleness guards on its own bar path. Both are dead — the entry guard has
fired 8 times in the system's life, all of them before the commit that introduced a silent stale-bar
fallback, and the exit guard has fired zero times in 1013 rows. Neither guard sits on cockpit's price
accessor, which is a different path with different callers: protective stop sizing, the PYRAMID driver
check, mark price, and the manager re-entry price.

This matters more under ADR 0002, not less: if the platform ever actuates on its own, a stale-price entry
becomes a platform action rather than a strategy one. Build-spec item 4, and a prerequisite for anything
automatic.

WHAT THIS FILE ASSERTS
----------------------
Not "the guard fires" — a guard nothing consults is how both existing guards died. It asserts the
**observation time survives the accessor**, which is the fact every guard downstream would need and which
no caller can currently obtain.
"""

from __future__ import annotations

import ast
import pathlib

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _accessor_source() -> str:
    tree = ast.parse(_ENGINE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_last_price_for":
            return ast.get_source_segment(_ENGINE.read_text(), node) or ""
    raise AssertionError("_last_price_for no longer exists — this test is blind and must be rewritten")


class _Tick:
    def __init__(self, price, ts_event):
        self.price, self.ts_event = price, ts_event


class _Bar:
    def __init__(self, close, ts_event):
        self.close, self.ts_event = close, ts_event


class _Cache:
    def __init__(self, tick, bars=()):
        self._tick, self._bars = tick, list(bars)

    def trade_tick(self, _iid):
        return self._tick

    def bars(self, _bt):
        return self._bars


class _Clock:
    def __init__(self, now_ns):
        self._now = now_ns

    def timestamp_ns(self):
        return self._now


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)


class _Host:
    """A duck-typed `self` carrying exactly what the accessor reads.

    NOT `UiFeedStrategy.__new__(...)`: `cache` is a read-only attribute on Nautilus's Cython `Actor`
    (`AttributeError: attribute 'cache' … is not writable`), so a stub built that way cannot exist. The
    house rule is to fix the double rather than loosen production — so this calls the REAL unbound
    function with a host that provides the three things it touches, and nothing else. If the accessor
    ever reaches for a fourth, this raises rather than silently passing.
    """

    def __init__(self, tick, now_ns, bars=()):
        self.cache = _Cache(tick, bars)
        self.clock = _Clock(now_ns)
        self.log = _Log()


def _bind(tick, now_ns, bars=()):
    """Bind the real function to a host that can represent what production gives it."""
    from api.engine_node import UiFeedStrategy

    host = _Host(tick, now_ns, bars)
    host._last_price_for = lambda *a, **k: UiFeedStrategy._last_price_for(host, *a, **k)
    host._last_price_and_ts_for = lambda *a, **k: UiFeedStrategy._last_price_and_ts_for(host, *a, **k)
    return host


_NOW = 1_800_000_000_000_000_000


def test_a_stale_price_is_refused_when_a_bound_is_given():
    """Behavioural, not a source scan — and this test's own history is why.

    Two earlier drafts asserted on the accessor's SOURCE. The first matched `allocated_equity`-style
    strings; the second matched `ts_event` inside the docstring that explains the fix, and stayed green
    when the timestamp plumbing was deleted. Matching prose instead of code is a defect class this repo
    has catalogued, and it took three attempts here to stop repeating it.
    """
    obj = _bind(_Tick(100.0, _NOW - 3_600_000_000_000), _NOW)  # observed an hour ago
    assert obj._last_price_for("AAPL.XNAS", max_age_ns=60_000_000_000) is None, (
        "an hour-old price was accepted against a 60s bound — a price cached before a feed outage is "
        "being used to size stops and entries"
    )
    assert obj.log.warnings, "the refusal was silent; a stale price must say so, not just vanish"


def test_a_fresh_price_is_returned_under_the_same_bound():
    """The fixture must be able to go the other way, or the test above proves only that it returns None."""
    obj = _bind(_Tick(100.0, _NOW - 1_000_000_000), _NOW)  # one second old
    assert obj._last_price_for("AAPL.XNAS", max_age_ns=60_000_000_000) == 100.0


def test_no_bound_keeps_the_previous_behaviour_exactly():
    """Opt-in per caller. Refusing a stale price is right for sizing and WRONG for protection, where
    returning nothing leaves a position uncovered — that decision belongs to each caller, not to this
    plumbing change."""
    obj = _bind(_Tick(100.0, _NOW - 86_400_000_000_000), _NOW)  # a day old
    assert obj._last_price_for("AAPL.XNAS") == 100.0


def test_the_fixture_can_see_the_thing_it_judges():
    """An assertion over a function that has been renamed passes for the wrong reason."""
    assert _accessor_source().strip(), "the accessor source came back empty — the guarantees are vacuous"


def test_every_sizing_caller_goes_through_the_guarded_accessor():
    """Aimed at the CLASS: a second, unguarded price read would reopen this the day it is added.

    `_mark_px` and `_last_price_for` are the two price surfaces. If a third appears that reads
    `cache.trade_tick` or `cache.bars` directly for sizing, the bound above protects nothing.
    """
    source = _ENGINE.read_text()
    tree = ast.parse(source)
    direct: list[int] = []
    for node in ast.walk(tree):
        # AsyncFunctionDef too: engine_node.py is largely async, so skipping it let a direct
        # cache read inside any coroutine pass this guard silently (review finding 6).
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in {"_last_price_for", "_last_price_and_ts_for", "_mark_px", "_on_bar", "_on_trade_tick"}:
            continue
        body = ast.get_source_segment(source, node) or ""
        if "cache.trade_tick(" in body:
            direct.append(node.lineno)
    assert not direct, (
        f"a price is read straight from the cache outside the guarded accessor at lines {direct} — "
        f"staleness bounds only hold if every sizing read goes through one door"
    )


def test_the_bar_fallback_carries_its_timestamp_too():
    """The branch that actually matters in the incident this file cites, and it had no test.

    During the 2026-08-18 websocket outage TRADES stopped arriving while cached daily bars remained — so
    the bar path is precisely what served the stale price. Code review found `_Cache.bars()` returned an
    empty list unconditionally, meaning `int(bars[0].ts_event)` could be deleted and the whole file
    stayed green.
    """
    obj = _bind(None, _NOW, bars=[_Bar(97.0, _NOW - 7_200_000_000_000)])  # two hours old
    assert obj._last_price_for("AAPL.XNAS", max_age_ns=60_000_000_000) is None, (
        "a two-hour-old BAR was accepted against a 60s bound — this is the source that keeps serving "
        "during a trade-feed outage, which is exactly when staleness matters"
    )
    assert obj._last_price_for("AAPL.XNAS") == 97.0, "no bound must still return the bar, as before"
