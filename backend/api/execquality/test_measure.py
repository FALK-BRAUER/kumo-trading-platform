"""Execution-quality measurement. The auction fixtures are real 2026-08-06/10 records.

The first test is the one that matters: it pins the methodology error that produced the original
study's nonsense numbers.
"""

from __future__ import annotations

from api.execquality.measure import Leg, append_csv, compare, fill_vwap, official_open, summarise

T0 = "2026-08-06T13:30:01.272404425Z"

# Real SU 2026-08-06 record. NYSE traded 31,812 shares at the open; Arca traded 100 and Nasdaq 100.
SU_DAY = {
    "d": "2026-08-06",
    "o": [
        {"c": "Q", "p": 63.20, "s": 100, "x": "P", "t": "2026-08-06T13:30:01.271385129Z"},
        {"c": "O", "p": 63.38, "s": 31812, "x": "N", "t": "2026-08-06T13:30:01.272404425Z"},
        {"c": "Q", "p": 63.38, "s": 31812, "x": "N", "t": "2026-08-06T13:30:01.272407383Z"},
        {"c": "Q", "p": 63.11, "s": 100, "x": "T", "t": "2026-08-06T13:30:02.897458497Z"},
    ],
}


def test_the_official_print_is_taken_not_the_first_record():
    """THE trap. The endpoint returns one record per price/exchange/condition triplet, so the first
    entry can be a 100-share print from a secondary venue. Taking it gave SU −316 bps in the original
    study — a methodology error reported as a finding until the raw records were inspected."""
    px, exchange, size, _ts = official_open(SU_DAY)
    assert (px, exchange, size) == (63.38, "N", 31812)
    assert px != 63.20, "took Arca's 100-share print instead of the NYSE official open"


def test_the_largest_print_wins_when_no_official_condition_is_present():
    """A venue that traded 30,000 shares at the open estimates the open better than one that did 100."""
    day = {"d": "2026-08-06", "o": [{"c": "Q", "p": 10.0, "s": 100, "x": "P", "t": T0},
                                    {"c": "Q", "p": 10.5, "s": 9000, "x": "N", "t": T0}]}
    assert official_open(day)[0] == 10.5


def test_a_day_with_no_opening_record_yields_nothing():
    assert official_open({"d": "2026-08-06", "o": []}) is None
    assert official_open({}) is None


# -- partial fills ------------------------------------------------------------------------------------
def test_partial_fills_fold_into_one_quantity_weighted_leg():
    """AFL filled in five pieces on 6 Aug for one intended sell. Comparing each piece to a single
    auction print would count one decision five times."""
    acts = [
        {"transaction_time": "2026-08-06T13:35:24.9Z", "symbol": "AFL", "side": "sell",
         "qty": "45", "price": "126.34"},
        {"transaction_time": "2026-08-06T13:35:12.5Z", "symbol": "AFL", "side": "sell",
         "qty": "16", "price": "126.32"},
        {"transaction_time": "2026-08-06T13:35:53.9Z", "symbol": "AFL", "side": "sell",
         "qty": "14", "price": "126.35"},
    ]
    (qty, vwap, first), = fill_vwap(acts).values()
    assert qty == 75
    assert 126.32 < vwap < 126.36
    assert first == "2026-08-06T13:35:12.5Z", "earliest fill should anchor the timestamp"


def test_malformed_activity_rows_are_skipped_not_fatal():
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "AFL", "side": "sell"},
            {"transaction_time": "2026-08-06T13:35:00Z", "symbol": "X", "side": "buy",
             "qty": "1", "price": "2"}]
    assert list(fill_vwap(acts)) == [("2026-08-06", "X", "buy")]


# -- sign convention ------------------------------------------------------------------------------------
def _auctions(price: float, sym: str = "SU", day: str = "2026-08-06") -> dict:
    return {"auctions": {sym: [{"d": day, "o": [{"c": "O", "p": price, "s": 5000, "x": "N",
                                                 "t": f"{day}T13:30:01.100316737Z"}]}]}}


def test_buying_above_the_auction_is_positive_cost():
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "SU", "side": "buy",
             "qty": "100", "price": "101.0"}]
    leg, = compare(acts, _auctions(100.0))
    assert leg.drift_bps == 100.0
    assert leg.cost_usd == 100.0


def test_selling_below_the_auction_is_also_positive_cost():
    """Both sides must sign the same way or the aggregate is meaningless."""
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "SU", "side": "sell",
             "qty": "100", "price": "99.0"}]
    leg, = compare(acts, _auctions(100.0))
    assert leg.drift_bps == 100.0
    assert leg.cost_usd == 100.0


def test_beating_the_auction_is_negative():
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "SU", "side": "buy",
             "qty": "10", "price": "99.0"}]
    assert compare(acts, _auctions(100.0))[0].drift_bps == -100.0


def test_a_leg_with_no_auction_record_is_dropped_not_guessed():
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "ZZZZ", "side": "buy",
             "qty": "10", "price": "99.0"}]
    assert compare(acts, _auctions(100.0)) == []


def test_a_midday_trade_is_not_measured_against_the_morning_auction():
    """The confound that topped the original study's table: CVS and PENG filled around 13:09 ET and
    were compared to that morning's open, producing a huge meaningless number. Lag comes from the
    auction record's own timestamp, so no timezone reasoning is involved."""
    midday = [{"transaction_time": "2026-08-06T17:10:00Z", "symbol": "SU", "side": "buy",
               "qty": "10", "price": "99.0"}]
    assert compare(midday, _auctions(100.0)) == [], "a 3.5-hour-late fill was measured as slippage"


def test_a_fill_at_the_open_records_its_lag():
    acts = [{"transaction_time": "2026-08-06T13:35:30Z", "symbol": "SU", "side": "buy",
             "qty": "10", "price": "99.0"}]
    leg, = compare(acts, _auctions(100.0))
    assert 5.0 < leg.lag_minutes < 5.6, f"expected ~5.5 min after the print, got {leg.lag_minutes}"


def test_named_legs_can_still_be_excluded():
    acts = [{"transaction_time": "2026-08-06T13:35:00Z", "symbol": "SU", "side": "sell",
             "qty": "10", "price": "99.0"}]
    assert compare(acts, _auctions(100.0), exclude={("2026-08-06", "SU", "sell")}) == []


# -- accumulation ---------------------------------------------------------------------------------------
def _leg(session="2026-08-06", symbol="SU", bps=10.0) -> Leg:
    return Leg(session, symbol, "buy", 10, 100.0, 100.0, "N", 5000, f"{session}T13:35:00Z", 5.0,
               bps, 1.0)


def test_appending_the_same_session_twice_does_not_duplicate(tmp_path):
    """The point is accumulation over months; a re-run must be safe."""
    path = tmp_path / "execquality.csv"
    assert append_csv(path, [_leg(), _leg(symbol="AFL")]) == 2
    assert append_csv(path, [_leg(), _leg(symbol="AFL")]) == 0
    assert append_csv(path, [_leg(symbol="MET")]) == 1
    assert len(path.read_text().strip().splitlines()) == 4  # header + 3


def test_summary_reports_sessions_beside_legs():
    """Legs cluster by morning, so n=legs overstates the sample. Reporting only the leg count is
    exactly how the first pass overstated its confidence."""
    legs = [_leg(bps=10), _leg(symbol="AFL", bps=20), _leg(session="2026-08-10", bps=300)]
    s = summarise(legs)
    assert s["legs"] == 3
    assert s["sessions"] == 2, "must surface that 3 legs came from only 2 mornings"
    assert s["worse_than_100bps"] == 1


def test_summary_of_nothing_is_not_a_crash():
    assert summarise([])["legs"] == 0


def test_a_fill_before_the_opening_print_is_not_measured():
    """Seen live: FIG on 6 Aug filled at 13:30:36 while its NYSE opening print published a minute
    later — a delayed open. Comparing a fill to a print that did not exist yet measures the delay,
    not the execution."""
    early = [{"transaction_time": "2026-08-06T13:29:00Z", "symbol": "SU", "side": "sell",
              "qty": "10", "price": "99.0"}]
    assert compare(early, _auctions(100.0)) == []
