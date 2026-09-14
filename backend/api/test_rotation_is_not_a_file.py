"""The rotation read must come from the engine, not off disk (#384).

Operator, 2026-08-21: "it should not feed from a file!!!!"

WHAT IT USED TO BE. A sidecar container shelled out to `fintrack/tools/rotation_read.py` on a host
mount every five minutes, wrote `/data/rotation/rotation.json`, and `GET /market/rotation` read it back.

THREE THINGS WERE WRONG WITH THAT, and only the third is plumbing:

  * A SECOND DATA SOURCE. The tool pulls daily OHLC from Yahoo. So the cockpit graded rotations off one
    feed while trading off Alpaca bars, and the two can disagree about the same session — the
    two-derivations-of-one-fact trap this repo keeps measuring.
  * NO RELATIONSHIP TO ENGINE LIVENESS. Measured, not theorised: on 2026-08-21 the sidecar's bind mount
    went stale, every refresh failed with `FileNotFoundError: /data/rotation/.rotation.tmp`, and the
    route served a four-hour-old payload with no indication anything was wrong — a file that is present
    and parseable looks healthy.
  * A FILE IS NOT A PLANE. trades, account, equity_curve and session are all published by the engine
    and carried on the bus. The rotation was the one fact that travelled by disk.

NOTHING IS REIMPLEMENTED. fintrack's tool still computes the whole read — `ratio_bars`, `ichi`,
`weekly`, `adx_di`, `window_stat`, `verdict`, `read_pair`. The adapter injects a bars-backed `fetch`
into it. A second Ichimoku here would be the very thing the file version got right.
"""

from __future__ import annotations

import inspect


def _src(fn) -> str:
    """Source with comments and docstrings stripped.

    Three assertions elsewhere in this repo passed against a DOCSTRING that merely discussed the code
    they were meant to pin. These files talk about `rotation.json` at length on purpose, so a raw scan
    for it would match the prose explaining why it is gone.
    """
    import re

    s = inspect.getsource(fn)
    s = re.sub(r'"""[\s\S]*?"""', " ", s)
    s = re.sub(r"#[^\n]*", " ", s)
    return s


def test_the_route_does_not_open_a_file():
    """The whole of the operator's complaint, as an assertion."""
    from api import app as app_module

    src = _src(app_module.get_rotation)
    assert "open(" not in src, "the route is reading a file again"
    assert "rotation.json" not in src
    assert "KUMO_ROTATION_PATH" not in src, "the env var that pointed at the file is gone"


def test_the_route_reads_the_PUBLISHED_plane():
    from api import app as app_module

    src = _src(app_module.get_rotation)
    assert "node.rotation()" in src


def test_the_consumer_stores_the_published_frame():
    """A route reading `node.rotation()` proves nothing if nothing ever fills it — the seam, not the
    unit. This is the shape that has shipped four times here (#233/#322/#336/#400): one end publishes,
    the other never declares it, and everything compiles."""
    from api import consumer as consumer_module

    src = inspect.getsource(consumer_module)
    assert 'elif kind == "rotation"' in src
    assert "def rotation(self)" in src


def test_NOTHING_PUBLISHED_is_an_error_not_an_empty_market():
    """"There is no rotation right now" and "nobody has looked" are different claims.

    Rendering the first for the second turns a dead feed into a calm market — the same rule as an
    unpriced symbol that must not render as flat (#356), and the reason the file version reported a
    missing file rather than an empty axes list.
    """
    import asyncio

    from api import app as app_module

    class _Node:
        def rotation(self):
            return {}

    original = getattr(app_module.app.state, "node", None)
    app_module.app.state.node = _Node()
    try:
        out = asyncio.run(app_module.get_rotation())
    finally:
        if original is not None:
            app_module.app.state.node = original

    assert out["axes"] == []
    assert out["generated"] is None
    assert out["error"], "an unpublished rotation must report WHY, not render as a flat market"


def test_a_PUBLISHED_payload_passes_through_unchanged():
    """The contract belongs to the tool. Reshaping here would put a second schema between generator and
    tile, and the tile would end up pinned to whichever of the two drifted last."""
    import asyncio

    from api import app as app_module

    payload = {
        "generated": "2026-08-21T01:00:00+00:00",
        "source": "engine:alpaca-daily",
        "axes": [{"pair": "XLE/SPY", "verdict": "🟢 ON"}],
        "errors": [{"pair": "GDX/GLD", "err": "thin history"}],
    }

    class _Node:
        def rotation(self):
            return dict(payload)

    original = getattr(app_module.app.state, "node", None)
    app_module.app.state.node = _Node()
    try:
        out = asyncio.run(app_module.get_rotation())
    finally:
        if original is not None:
            app_module.app.state.node = original

    assert out["axes"] == payload["axes"]
    assert out["errors"] == payload["errors"]
    assert out["source"] == "engine:alpaca-daily"
    assert out["error"] is None


def test_the_engine_REFRESHES_it_on_a_timer_and_publishes():
    from api.engine_node import UiFeedStrategy

    src = _src(UiFeedStrategy._refresh_rotation)
    assert "build_payload" in src
    assert '_publish("rotation"' in src
    # THE CALL, on the engine's own HTTP client. A first version asserted `"get_bars" in src` and
    # passed when the call was swapped for `self._noop_get_bars(` — the substring survived the rename.
    # REVERSED 2026-08-26. This required the fetch to go through `self._http.get_bars(` — an
    # `AlpacaHttpClient`, built only when `data_provider == "alpaca"`. "Our own bars" was the right
    # requirement; the mechanism was vendor-locked, so on any other provider the compass returned
    # silently and the Market tab looked like a quiet market. Nautilus is the one bar source every
    # adapter fills, and `request_bars` works on whichever one this node has.
    assert "request_bars" in src and "cache.bars(" in src, (
        "the deep history must come from OUR OWN bars, through Nautilus rather than a vendor client")


def test_the_refresh_is_OFF_THE_CONNECT_PATH():
    """The failure this ordering exists to avoid, and it is not hypothetical.

    An earlier attempt wired the ticker resolution into startup, which put a third concurrent
    `/v2/assets` call in a race with the Data and Exec clients' own `_connect`. The node came up
    RUNNING with both clients disconnected and had to be rolled back. This must stay on the timer, like
    the realized sweep — after the node is up, on the loop, over an HTTP client already in use.
    """
    from api.engine_node import UiFeedStrategy

    start = _src(UiFeedStrategy.on_start)
    assert "_ROTATION_TIMER" in start, "the refresh must be armed as a timer"
    assert "rotation_tickers" not in start, "resolving tickers during start is what broke the node"


def test_a_FAILED_refresh_keeps_the_last_payload():
    """A transient 429 must not turn a live read into an empty market. Same rule as the realized sweep:
    a failed refresh leaves the last good answer standing rather than blanking it."""
    from api.engine_node import UiFeedStrategy

    src = _src(UiFeedStrategy._refresh_rotation)
    assert "except Exception" in src
    assert "keeping last known" in inspect.getsource(UiFeedStrategy._refresh_rotation)


def test_the_bars_are_MEMOISED_per_session_not_refetched_every_tick():
    """The read grades DAILY candles. Refetching 20 tickers of three-year history every five minutes
    would be ~5,700 REST calls a day to compute a number that changes once."""
    from api.engine_node import UiFeedStrategy

    src = _src(UiFeedStrategy._refresh_rotation)
    # The per-session DATE stamp went with the per-session request set: both encoded "we already did
    # this today", and neither could tell "asked" from "have". The cache is the memo now.
    # THE GUARD ITSELF. A first version asserted `"continue" in src`, which survives `if False: continue`
    # — the statement stayed, the condition that made it do anything did not.
    # Same requirement, new mechanism: a ticker whose history was already REQUESTED this session must
    # not be requested again. The per-session dict of fetched rows became a per-session set of
    # requests, because Nautilus fills the cache and the cache is then the store.
    # REVERSED AGAIN, and this is the second time this assertion has changed mechanism. It first
    # required a per-session dict of fetched rows, then a per-session set of requests. Both encoded
    # "asked once" — and "asked" is not "have": a request that never landed was never retried, and
    # test-alpaca sat at 0 of 25 axes for fifteen minutes with ZERO RequestBars in that window.
    #
    # The requirement is unchanged: do not re-request 27 instruments' full history every tick. The
    # mechanism is now the cache itself — a ticker with enough bars is skipped, one without is
    # retried. Self-healing, and it cannot go stale the way a flag does.
    assert "_ROTATION_MIN_BARS" in src, (
        "nothing gates the request on what the cache HOLDS — either every tick re-requests, or a "
        "dropped request is never retried")


def test_rotation_is_served_as_a_PLANE_not_only_as_a_poll():
    """Operator, 2026-08-21: "It should work similar to trend lines, watch, portfolio etc."

    The first cut of #384 moved the SOURCE into the engine and left the TRANSPORT as a 60s REST poll, so
    the Market tile was still the only tile asking for its data rather than being told. `trades`,
    `session`, `account` and `equity_curve` all resolve through the websocket topic resolver; rotation
    must too, or a stopped publisher is indistinguishable from a quiet market — which is exactly how a
    dead sidecar served a four-hour-old payload that looked healthy.
    """
    import inspect

    from api import app as app_module

    src = inspect.getsource(app_module)
    assert 'topic.channel == "rotation"' in src, "the rotation plane has no websocket resolver"
    # The REST route stays, as it does for trades and positions — a bootstrap read is not the problem.
    assert "async def get_rotation" in src
