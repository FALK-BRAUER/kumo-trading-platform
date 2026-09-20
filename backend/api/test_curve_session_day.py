"""Daily curve points must be labelled by the SESSION they close, not by UTC (#559 follow-up).

WHAT THE OPERATOR WAS ORIGINALLY LOOKING AT. The operator asked why the 1W read −$1,491.75. Part of that answer is that
the window's endpoints are indexed off this curve, and the daily buckets are floored in UTC on a book
that trades ET sessions.

`ts // 86400 * 86400` puts the boundary at UTC midnight = 20:00 ET. So every account state after
20:00 ET lands on the NEXT calendar label, and the engine publishes account states continuously —
overnight, at the weekend, whenever it is running. A Friday-evening equity is stamped Saturday, and a
window whose base indexes a labelled day gets the value from a different session than the label says.

HONEST LIMIT ON THIS FILE: the arithmetic below is proven, and the LIVE symptom is not — the curve
endpoint is not on the deployed paper build (`2637ae1`), so I could not read a real series back. What
is asserted here is the labelling rule, not a reproduction of a screen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.account_curve import _session_day_start

ET = timezone(timedelta(hours=-4))


def _at(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_an_evening_state_belongs_to_the_session_that_just_CLOSED():
    """20:30 ET on Friday is Friday's book, not Saturday's. Under UTC flooring it landed on Saturday,
    so a weekend point appeared carrying Friday evening's equity — a day that never traded, holding a
    number, labelled as its own."""
    friday_evening = _at(2026, 8, 28, 20, 30)
    saturday_label = _session_day_start(friday_evening) 
    friday_close = _session_day_start(_at(2026, 8, 28, 16, 0))
    assert saturday_label == friday_close, "the evening state must fold into its own session's day"


def test_the_boundary_is_the_ET_DAY_not_UTC_MIDNIGHT():
    """The whole defect in one assertion. UTC midnight is 20:00 ET, so flooring in UTC splits a
    session's own evening away from its afternoon."""
    afternoon = _session_day_start(_at(2026, 8, 28, 16, 0))
    late_night = _session_day_start(_at(2026, 8, 28, 23, 59))
    next_morning = _session_day_start(_at(2026, 8, 29, 9, 30))

    assert afternoon == late_night, "16:00 and 23:59 the same ET day must share a bucket"
    assert next_morning != afternoon, "a new ET day must start a new bucket"


def test_it_survives_the_DST_change_rather_than_drifting_an_hour():
    """A fixed −4 offset would put the boundary an hour out for half the year, which is how a
    once-a-year one-day shift becomes a bug nobody can reproduce in August. Uses the real zone."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    winter = int(datetime(2026, 1, 15, 23, 30, tzinfo=ny).timestamp())
    winter_afternoon = int(datetime(2026, 1, 15, 15, 0, tzinfo=ny).timestamp())
    assert _session_day_start(winter) == _session_day_start(winter_afternoon)


def test_the_bucket_is_STABLE_so_two_states_in_one_session_do_not_split():
    """The fold keeps the LAST write per bucket, so an unstable key would let an earlier state win
    or produce two points for one day."""
    keys = {_session_day_start(_at(2026, 8, 28, h)) for h in (10, 13, 16, 19, 22)}
    assert len(keys) == 1, f"one ET session must yield one bucket, got {keys}"


# ==================================================================================================
# THE SEAM — the tests above proved the helper, not the fold
# ==================================================================================================
class _Money:
    def __init__(self, v, c):
        self._v, self.currency = float(v), c

    def as_double(self):
        return self._v


class _State:
    """An AccountState as Nautilus emits one — ns timestamps, balances keyed by currency."""

    def __init__(self, ts_secs, total, ccy="USD"):
        self.ts_event = int(ts_secs * 1_000_000_000)
        self.balances_total = {ccy: _Money(total, ccy)}


def test_the_FOLD_puts_an_evening_state_on_its_OWN_session_day():
    """MUTATION SURVIVOR, AND IT IS THE SHAPE I HAVE BEEN CATCHING ALL SESSION. Reverting the fold to
    `ts // bucket * bucket` left ALL 2598 tests green, because every test above drives
    `_session_day_start` DIRECTLY — the unit, never the seam. A correct helper nothing calls is worth
    nothing, and that is the fifth time this exact gap has appeared today.

    This drives `build_curves` and reads the emitted point back.

    Friday 16:00 ET and Friday 20:30 ET are ONE session. Under UTC flooring the second lands on
    Saturday, producing a weekend point that carries Friday-evening equity — a day that never traded,
    holding a number, labelled as its own.
    """
    from datetime import datetime, timedelta, timezone

    from api.account_curve import build_curves

    ET = timezone(timedelta(hours=-4))
    close = int(datetime(2026, 8, 28, 16, 0, tzinfo=ET).timestamp())
    evening = int(datetime(2026, 8, 28, 20, 30, tzinfo=ET).timestamp())
    now_ns = int(datetime(2026, 8, 31, 12, 0, tzinfo=ET).timestamp()) * 1_000_000_000

    # FIXTURE PROPERTY FIRST: the two states must straddle UTC midnight, or there is nothing for the
    # boundary rule to get wrong and the assertion below is about nothing.
    assert (datetime.fromtimestamp(close, timezone.utc).date()
            != datetime.fromtimestamp(evening, timezone.utc).date()), \
        "the fixture must straddle UTC midnight, or the boundary rule has nothing to get wrong"

    curves = build_curves([_State(close, 100.0), _State(evening, 101.0)], now_ns)
    days = {p["t"] for p in curves["1M"]["points"]}
    assert len(days) == 1, (
        f"one ET session must produce ONE daily point; got {len(days)} — the evening state was "
        f"labelled a separate day"
    )
