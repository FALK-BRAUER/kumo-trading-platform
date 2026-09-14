"""#309 — bars must not share a stream with quotes, because quotes evict them in minutes.

Measured on the live paper book 2026-08-14 while the watchlist sat on "loading…" with every 1M/1Y/5Y
sparkline showing "—":

    ui:stream          200,015 entries   (cap: stream_maxlen = 200,000)
    retention window   7.5 minutes
    last 300 entries   177 quote · 83 price · 40 today_range · ZERO bars

Quotes and prices for ~119 symbols push roughly 27,000 entries a minute. Historical bars are published
ONCE during backfill — the docstring on `_publish` puts that at ~127,000 frames — so they are evicted
within minutes and a browser connecting afterwards never sees them. The data was fetched, published, and
thrown away before anyone could read it.

The codebase already knew this hazard: `STATE_KINDS` exists so latest-state planes bypass the stream, and
its comment says "no stream flood, no bar eviction". Trades were moved off for exactly this reason. Quotes
and prices never were, and they are far higher volume.
"""

from __future__ import annotations

from api.feed_config import BAR_KINDS, STATE_KINDS


def test_bars_are_routed_to_their_own_stream():
    """A bar must never land in the stream quotes flood."""
    assert "bar" in BAR_KINDS
    assert not (BAR_KINDS & STATE_KINDS), "a kind cannot be both a latest-state key and a stream"


def test_the_high_rate_kinds_are_NOT_bar_kinds():
    """quote/price/today_range are what evicted the bars — they must stay on the busy stream."""
    for kind in ("quote", "price", "today_range"):
        assert kind not in BAR_KINDS


def test_the_engine_routes_by_kind_and_the_consumer_reads_BOTH_streams():
    """The seam. Splitting the producer without teaching the consumer to read the second stream would
    lose bars completely rather than merely evicting them — strictly worse than the bug being fixed.

    Six defects this session were 'unit right, wiring wrong'; this pins the wiring.
    """
    import inspect

    from api.consumer import RedisConsumer
    from api.engine_node import UiFeedStrategy

    producer = inspect.getsource(UiFeedStrategy)
    assert "_bar_stream_key" in producer, "the engine does not route bars to a separate stream"

    consumer = inspect.getsource(RedisConsumer)
    assert "_bar_stream_key" in consumer, "the consumer never reads the bar stream — bars would be LOST"

    # And it must be a genuinely DIFFERENT key. Asserting the name appears in the source passed while the
    # value was set to the quote stream — the same "checked the label, not the value" gap that let a
    # bracket-only identifier and an id()-based dedupe through earlier today.
    c = RedisConsumer()   # reads the real feed config, same as production
    assert c._bar_stream_key != c._stream_key
    assert c._last_bar_id == "0-0", "the bar stream needs its own cursor, or one skips the other's frames"
