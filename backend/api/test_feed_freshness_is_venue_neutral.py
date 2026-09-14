"""Feed freshness must mean "market data arrived", not "a TRADE TICK arrived" (#608).

THE DEFECT, measured 2026-08-27 with both tenants on the same commit:

    paper   (Alpaca)  trade ticks flowing     last_tick_ts = live (0.2 min)  banner: feed live
    staging (IBKR)    1,663 bars in 10 min    last_tick_ts = 0               banner: NO FEED
                      0 trade ticks

`on_trade_tick` was the ONLY writer of `_last_tick_ts`, and the UI's "Market-data feed stale" banner
reads that field. IBKR delivers BARS, not trade ticks — tick-by-tick is capped at the venue (#578,
73 hits in one session) — so the signal is structurally unreachable there.

staging has therefore NEVER had a non-zero `last_tick_ts`. The banner has been wrong for the whole
life of that instance, and it was read as evidence repeatedly, including by me, while the engine sat
there with a perfectly live feed.

This is #608's class: a platform-level signal that only one broker's data shape can satisfy.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from api.engine_node import UiFeedStrategy

#: Every handler through which market data can reach the engine. Freshness must be written from ALL
#: of them, because which one a venue uses is the venue's choice, not ours.
_DATA_HANDLERS = ("on_trade_tick", "on_quote_tick", "on_bar")


def _body(name: str) -> str:
    """Source with COMMENTS AND DOCSTRINGS STRIPPED.

    The first version of this file grepped the raw source, and the explanatory comment beside the
    fix contains the very identifier being asserted — so deleting the ASSIGNMENT left the test green.
    A rule its own explanation can satisfy pins nothing; the mutant passed 5/5 and I nearly shipped
    it. `periodPnlSource.test.ts` strips comments for exactly this reason and says so.
    """
    src = inspect.getsource(getattr(UiFeedStrategy, name))
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant) and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]                      # drop the docstring
    return ast.unparse(fn) if fn.body else ""


def test_the_handlers_this_pins_actually_exist():
    """Fixture property first. A typo'd handler name would make every assertion below pass against a
    method that is never called — pinning the absence of a mechanism rather than its presence."""
    for name in _DATA_HANDLERS:
        assert callable(getattr(UiFeedStrategy, name, None)), f"{name} is not a handler any more"


def test_a_COMMENT_mentioning_the_field_does_not_satisfy_this_file():
    """THE TRAP THIS FILE FELL INTO. Grepping raw source let the fix's own explanatory comment keep
    the test green with the assignment deleted — the mutant passed 5 of 5.

    Asserted directly so the stripping cannot be quietly removed later.
    """
    src = "def f(self):\n    # self._last_tick_ts is mentioned only in this comment\n    return 1\n"
    tree = ast.parse(src)
    assert "_last_tick_ts" not in ast.unparse(tree.body[0]), "comments are reaching the assertions"


def test_trade_ticks_still_mark_the_feed_fresh():
    """The original path must survive. Alpaca's stream is what works today and this change must not
    trade one venue's correctness for another's."""
    assert "_last_tick_ts" in _body("on_trade_tick")


def test_BARS_mark_the_feed_fresh():
    """THE DEFECT. IBKR delivers bars and no trade ticks, so a bar is the only evidence of life that
    venue produces — and it was not counted."""
    assert "_last_tick_ts" in _body("on_bar"), (
        "a bar does not mark the feed fresh, so an IBKR tenant receiving 1,663 bars in ten minutes "
        "still reports 'no feed' (#608)"
    )


def test_QUOTES_mark_the_feed_fresh():
    """A venue that streams quotes but not trades is the same defect wearing a third shape. Pinning
    only the two we happen to run today leaves the class alive."""
    assert "_last_tick_ts" in _body("on_quote_tick")


def test_EVERY_data_handler_writes_it_so_the_next_venue_is_covered():
    """AIMED AT THE CLASS, not the instance. The question is not 'does IBKR work now' but 'what would
    have caught this AND its siblings' — a fourth data path added later must not silently reintroduce
    a liveness signal that one venue cannot reach."""
    missing = [n for n in _DATA_HANDLERS if "_last_tick_ts" not in _body(n)]
    assert not missing, f"these data handlers do not mark the feed fresh: {missing}"


def test_freshness_uses_ARRIVAL_time_not_the_datas_own_timestamp():
    """MEASURED IN PRODUCTION, one deploy after the first version of this fix.

    `ts_event` is the DATA's timestamp — for a daily bar, the session it covers. Using it made
    staging's freshness read 39 HOURS OLD the instant the fix went live: non-zero, plausible, and
    still stale, because a daily bar delivered today carries yesterday's close.

    `ts_init` is the stamp the ADAPTER put on the object — arrival on IB's live path, the frame's own
    time on Alpaca (its parsers write one value into both fields) — never `ts_event`, the datum's own
    session time. What makes any of those safe is the monotonic `max()` at the write (#917); the full
    account is the comment in `on_bar`, and `test_feed_freshness_is_monotonic.py` pins it. On a daily
    bar the two stamps are a day apart on Alpaca's live channel and equal on a request response.
    """
    for name in ("on_bar", "on_quote_tick"):
        body = _body(name)
        assert "ts_init" in body, f"{name} marks freshness with the data's own timestamp, not arrival"
        assert "_last_tick_ts = " in body
