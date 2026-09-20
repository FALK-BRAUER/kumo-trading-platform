"""A quiet feed is not a dead feed unless the market was open (#757)."""

from __future__ import annotations

from api.feed_staleness import STALE_AFTER_TRADING_MINUTES, feed_is_stale

_MIN = 60_000_000_000


class _Cal:
    def __init__(self, minutes):
        self._m = minutes

    def trading_minutes_between(self, a, b):
        return self._m


def test_a_WEEKEND_of_silence_is_NOT_stale():
    """The live case that fooled me: 58.9 hours old, zero of them with the market open."""
    assert feed_is_stale(1, 1 + 59 * 60 * _MIN, _Cal(0)) is False


def test_OPEN_MARKET_silence_IS_stale():
    """The failure the weekend case must not hide: same age, market open throughout."""
    assert feed_is_stale(1, 1 + 59 * 60 * _MIN, _Cal(59 * 60)) is True


def test_a_SHORT_open_gap_is_not_stale():
    """A thin symbol goes minutes between prints; this is a whole-feed verdict, not per-symbol."""
    assert feed_is_stale(1, 1 + 5 * _MIN, _Cal(5)) is False
    assert feed_is_stale(1, 1 + 99 * _MIN, _Cal(STALE_AFTER_TRADING_MINUTES + 1)) is True


def test_NO_CALENDAR_is_UNKNOWN_not_healthy():
    """A calendar that cannot say has not told us the market was shut. Treating its silence as
    "closed, therefore fine" reads a refusal as an all-clear — the defect this file exists for."""
    assert feed_is_stale(1, 1 + 99 * 60 * _MIN, None) is None


def test_a_CALENDAR_THAT_RAISES_is_UNKNOWN_not_healthy():
    class _Angry:
        def trading_minutes_between(self, a, b):
            raise RuntimeError("no calendar for that range")

    assert feed_is_stale(1, 1 + 99 * 60 * _MIN, _Angry()) is None


def test_NEVER_HAVING_TICKED_is_UNKNOWN_not_stale():
    """A boot before the open has no tick yet. Reporting stale would page every morning, which is
    how a real alarm gets muted."""
    assert feed_is_stale(0, 1 + 99 * 60 * _MIN, _Cal(600)) is None
