"""#653: a young account's 3M window must SAY it is clamped, not render as a confident 3M delta."""

from __future__ import annotations

from api.equity_coverage import curve_coverage

_DAY = 86_400


def _ts(days: float, n: int = 10) -> list[int]:
    """n timestamps spanning `days` days, ending at an arbitrary epoch."""
    end = 1_790_000_000
    start = end - int(days * _DAY)
    return [start + int(i * (end - start) / (n - 1)) for i in range(n)]


def test_the_fixture_spans_what_it_claims():
    """FIXTURE PROPERTY: the synthetic series really spans 14 days — the assertions below are about
    the rule, not about a broken generator."""
    ts = _ts(14)
    assert abs((max(ts) - min(ts)) / _DAY - 14) < 0.01


def test_the_staging_shape_a_two_week_account_does_NOT_cover_3M():
    """THE DEFECT, verbatim from staging: NET·3M == NET·1M to the cent on a ~14-day-old account,
    with nothing on screen saying the base was clamped to inception."""
    cov = curve_coverage("3M", _ts(14))
    assert cov["covered"] is False
    assert 13 <= cov["covers_days"] <= 15


def test_a_window_the_series_spans_IS_covered_with_one_bar_slack():
    assert curve_coverage("1M", _ts(30))["covered"] is True
    # One day short still counts — the trend strip's one-bar slack, same reasoning (JNJ, #612):
    # a session-boundary shortfall must not flip a plainly-covered window.
    assert curve_coverage("1M", _ts(29))["covered"] is True
    # A week short does NOT — otherwise "covered" stops meaning anything.
    assert curve_coverage("1M", _ts(23))["covered"] is False


def test_all_is_inception_to_date_and_always_covered():
    cov = curve_coverage("all", _ts(3))
    assert cov["covered"] is True
    assert cov["covers_days"] is None


def test_a_single_point_spans_nothing():
    cov = curve_coverage("1W", [1_790_000_000])
    assert cov["covered"] is False
    assert cov["covers_days"] == 0


def test_the_publisher_attaches_coverage_to_every_curve():
    """THE WIRING — a correct helper nobody calls is tonight's fourth-favourite defect. The curve
    dict the exec client publishes must carry the coverage fields."""
    import inspect

    from api.providers.alpaca import exec_client as mod

    src = inspect.getsource(mod.AlpacaExecutionClient._report_equity_curve)
    assert "curve_coverage" in src, "the curve publisher never consults curve_coverage (#653)"
