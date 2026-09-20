"""The equity curve from Nautilus's own account history, not a vendor endpoint (#559).

`broker.equity_curve` had ONE publisher — the Alpaca exec client, hitting
`/v2/account/portfolio/history`. On an IBKR stack nothing ever wrote the topic, so the tile rendered
"No account history yet" over an account holding $1,000,000 with months of history.

`Account.events -> list[AccountState]` is the native, provider-agnostic source, and Nautilus persists
it. Measured 2026-08-26: 280,000 events on paper, 22,000 on staging. The IBKR curve was in the cache
the whole time; nobody read it.

THE DOUBLE HERE MIRRORS WHAT NAUTILUS ACTUALLY EMITS — `ts_event` in NANOSECONDS and a
`balances_total` mapping of currency to a Money-like with `.as_double()` — because a double that
cannot represent production is how the last four defects shipped green.
"""

from __future__ import annotations

from api.account_curve import build_curves


class _Money:
    def __init__(self, v: float, ccy: str = "USD"):
        self._v = float(v)
        self.currency = ccy

    def as_double(self) -> float:
        return self._v


class _State:
    """An AccountState as Nautilus emits one: ns timestamps, balances keyed by currency."""

    def __init__(self, ts_secs: float, total: float, ccy: str = "USD"):
        self.ts_event = int(ts_secs * 1_000_000_000)
        self.balances_total = {ccy: _Money(total, ccy)}


NOW = 1_800_000_000
NOW_NS = NOW * 1_000_000_000


def test_the_fixture_matches_what_nautilus_emits():
    """Asserted first. `ts_event` is NANOSECONDS — feeding seconds would put every point 50 years in
    the past and every window would come back empty, which looks exactly like "no history"."""
    s = _State(NOW, 100.0)
    assert s.ts_event == NOW * 1_000_000_000
    assert s.balances_total["USD"].as_double() == 100.0


def test_a_curve_is_built_per_period():
    events = [_State(NOW - i * 60, 100.0 + i) for i in range(600, 0, -1)]
    curves = build_curves(events, NOW_NS)
    assert "1D" in curves and "1W" in curves
    assert curves["1D"]["points"], "no points in the 1D window"


def test_points_are_ONE_PER_BUCKET_and_take_the_LAST_value():
    """An account emits many states per second; a chart wants each interval's closing value."""
    minute = NOW // 60 * 60
    events = [_State(minute + 5, 100.0), _State(minute + 30, 111.0),
              _State(minute + 60 + 5, 120.0), _State(minute + 60 + 40, 122.0)]
    pts = build_curves(events, NOW_NS + 120 * 1_000_000_000)["1D"]["points"]
    assert [p["equity"] for p in pts] == [111.0, 122.0], (
        f"expected the last value in each minute bucket, got {pts}")


def test_PNL_is_LAST_MINUS_BASE_not_a_running_sum():
    """Alpaca's own series taught this: its last 1M `profit_loss` was +1161.81 — that DAY's move —
    while the month was DOWN 2244.29, and reading the last element printed "+$1,161 this 1M" over a
    falling chart."""
    events = [_State(NOW - 3 * 60, 100.0), _State(NOW - 2 * 60, 130.0), _State(NOW - 60, 90.0)]
    c = build_curves(events, NOW_NS)["1D"]
    assert c["base_value"] == 100.0
    assert c["points"][-1]["pnl"] == -10.0, (
        f"period P&L must be last - base (90 - 100), got {c['points'][-1]['pnl']}")


def test_ZERO_equity_states_are_dropped():
    """A state before the account was funded plots a cliff from the axis up to the real balance, which
    reads as a catastrophic loss recovered."""
    events = [_State(NOW - 5 * 60, 0.0), _State(NOW - 4 * 60, 0.0),
              _State(NOW - 3 * 60, 100.0), _State(NOW - 2 * 60, 101.0)]
    c = build_curves(events, NOW_NS)["1D"]
    assert c["base_value"] == 100.0, f"a zero state became the baseline: {c['base_value']}"
    assert all(p["equity"] > 0 for p in c["points"])


def test_a_MULTI_CURRENCY_state_is_SKIPPED_not_summed():
    """Staging's account is SGD and paper's is USD. Adding them produces a number that is not money,
    and taking the first silently picks one — the shape that let a currency-basis defect hide on
    staging for a week."""
    # THE LEAKED VALUE MUST BE DISTINGUISHABLE. A first version gave the multi-currency state a USD
    # balance of 100.0 — the same as the valid states — so disabling the guard let `balances[0]`
    # through and every assertion still passed. A fixture that cannot express the defect proves
    # nothing; 999.0 can only come from the state that should have been skipped.
    # AND THEY MUST LAND IN DIFFERENT BUCKETS. A second version spaced them 60/30/10 seconds apart —
    # all inside one 60s bucket — so last-write-wins overwrote the leaked value and the test passed
    # with the guard disabled anyway. Two fixture faults in a row, both "the fixture cannot express
    # the defect", which is the same lesson as the tenkan mutation that flipped no verdict.
    s = _State(NOW - 300, 0.0)
    s.balances_total = {"USD": _Money(999.0, "USD"), "SGD": _Money(135.0, "SGD")}
    ok_a, ok_b = _State(NOW - 180, 100.0), _State(NOW - 60, 102.0)

    pts = build_curves([s, ok_a, ok_b], NOW_NS)["1D"]["points"]
    assert pts, "nothing survived — the fixture cannot see the hazard"
    assert all(p["equity"] != 999.0 for p in pts), (
        f"a multi-currency state leaked into the curve by taking its first balance: {pts}")
    assert all(p["equity"] in (100.0, 102.0) for p in pts), pts


def test_the_CURRENCY_is_carried_so_the_tile_does_not_guess():
    events = [_State(NOW - 120, 1000.0, "SGD"), _State(NOW - 60, 1001.0, "SGD")]
    assert build_curves(events, NOW_NS)["1D"]["currency"] == "SGD"


def test_a_SINGLE_point_is_not_a_curve():
    """One sample drawn as a line asserts a flat account. Better to omit the period."""
    assert "1D" not in build_curves([_State(NOW - 60, 100.0)], NOW_NS)


def test_NO_events_yields_NO_curves_rather_than_an_empty_chart():
    assert build_curves([], NOW_NS) == {}
    assert build_curves(None, NOW_NS) == {}
