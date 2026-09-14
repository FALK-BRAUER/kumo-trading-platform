"""Realized P&L must reconcile against the broker's own arithmetic, or say that it does not (#345 item 1).

THE DEFECT, MEASURED. On 2026-08-20 the panel reported all-time realized as **+$3,574.04 with
`unmatched: 0`** — asserting every sale matched an opening buy inside the fetched record. The broker's
own books implied **+$2,574.95** (equity 103,211.42 − baseline 100,000 − unrealized 636.47). Two
derivations of one fact, $999.09 apart, with the completeness flag reading clean.

WHAT IT WAS NOT. Every hypothesis in #345 was measured and every one was wrong:

  * NOT a sweep truncated at the new end. 323 fills, spanning 2026-07-13T17:42Z to 2026-08-20T16:07Z —
    four minutes before the probe ran — terminating on a genuinely short 4th page.
  * NOT the short-page-is-terminal assumption. The short page was real, not a cap.
  * NOT dividends. The account has no DIV, INT, JNLC or JNLS activity at all.
  * NOT the matcher. `cashflow-over-fills + cost_basis of open positions` = 3,574.04, to the cent —
    a derivation sharing no code with FIFO matching, agreeing exactly.

WHAT IT WAS. Cash that moved with no fill behind it, which the fill sweep cannot see:

    WH / SLWH   "PTP Withholding", symbol PAA, 2026-08-06     -990.46
    FEE x38     CAT / TAF / REG                                  -8.63
                                                              ---------
                                                                -999.09

exactly the disagreement. PAA is a publicly traded partnership; Alpaca withholds on PTP proceeds.

WHY `unmatched` COULD NOT HAVE CAUGHT IT — and this is the part worth keeping. The flag counts sales
whose opening buy is missing. A withholding is not a sale and a fee is not a sale, so it read 0, and
reading 0 is what made the number trustworthy. #345 already said the flag is structurally blind to a
window short at either end; it is also blind to every dollar that never was a trade. A completeness
claim a derivation makes about ITSELF is worth nothing — which is why the fix is a second derivation,
not a better flag.
"""

from __future__ import annotations

import asyncio
import inspect

from api.realized_broker import cash_adjustments, realized_by_period, reconcile

#: The account's real non-FILL activity, 2026-08-20. Shapes are Alpaca's, verbatim from
#: `/v2/account/activities` — a double that invented its own field names could not represent the payload
#: the sweep actually meets (`net_amount` as a STRING, `date` separate from `created_at`).
_WITHHOLDING = {
    "id": "20260806000000000::1317f9f5", "activity_type": "WH", "activity_sub_type": "SLWH",
    "date": "2026-08-06", "created_at": "2026-08-06T14:11:39.796202Z",
    "net_amount": "-990.46", "description": "PTP Withholding", "symbol": "PAA", "status": "executed",
}
_FEE = {
    "id": "20260819000000000::b1ce1d81", "activity_type": "FEE", "activity_sub_type": "REG",
    "date": "2026-08-19", "created_at": "2026-08-20T00:16:40.704375Z",
    "net_amount": "-2.26", "description": "REG fee", "status": "executed",
}
_FILL = {
    "id": "f1", "activity_type": "FILL", "symbol": "AEM", "side": "buy", "qty": "10",
    "price": "100.00", "transaction_time": "2026-08-19T14:00:00Z",
}

# The measured account totals, so the assertions below carry the reasoning and not just a number.
_REPORTED = 3574.04      # our matcher over 323 fills, unmatched 0
_ADJUSTMENTS = -999.09   # -990.46 withholding + -8.63 fees
_EQUITY = 103211.42
_BASELINE = 100000.0
_UNREALIZED = 636.47
_IMPLIED = 2574.95       # equity - baseline - unrealized


def test_the_fixture_actually_contains_the_gap():
    """The fixture's own property first. If the payload had no non-FILL rows, everything below would
    pass with the fix deleted — the kumo-strategies truncation test failed exactly this way, twice."""
    assert any(a["activity_type"] != "FILL" for a in (_WITHHOLDING, _FEE, _FILL))
    assert float(_WITHHOLDING["net_amount"]) != 0.0


def test_cash_adjustments_finds_the_money_the_fill_sweep_cannot_see():
    total, by_type = cash_adjustments([_FILL, _WITHHOLDING, _FEE])

    assert round(total, 2) == -992.72
    assert by_type == {"WH": -990.46, "FEE": -2.26}
    assert "FILL" not in by_type, "a fill is not an adjustment — it would be double-counted"


def test_the_withholding_is_the_bulk_of_it_and_is_NOT_a_fee():
    """Pinned by type because the instinct is to reach for a fee allowlist.

    `WH` is not a fee, was not on any list of expected types, and is 99% of the gap. That is why
    `cash_adjustments` is a NEGATIVE definition — everything that is not a FILL — rather than an
    enumeration: an allowlist would have missed exactly this row, and missing it is the bug.
    """
    total, by_type = cash_adjustments([_WITHHOLDING])
    assert by_type == {"WH": -990.46}
    assert round(total, 2) == -990.46


def test_the_reconciliation_CATCHES_the_real_disagreement():
    """The measured 2026-08-20 numbers, with the adjustments left out — i.e. the shipped behaviour."""
    r = reconcile(reported_realized=_REPORTED, adjustments=0.0,
                  equity=_EQUITY, baseline=_BASELINE, unrealized=_UNREALIZED)

    assert not r["reconciled"], "the $999.09 disagreement must not pass"
    assert round(r["residual"], 2) == 999.09
    assert round(r["implied"], 2) == _IMPLIED


def test_the_reconciliation_PASSES_once_the_adjustments_are_counted():
    """The same numbers with the fix. Residual collapses to a cent — this is the whole claim."""
    r = reconcile(reported_realized=_REPORTED, adjustments=_ADJUSTMENTS,
                  equity=_EQUITY, baseline=_BASELINE, unrealized=_UNREALIZED)

    assert r["reconciled"]
    assert abs(r["residual"]) < 0.02, f"residual {r['residual']}"


def test_the_tolerance_is_far_below_the_bug_and_above_the_noise():
    """A $1 window. Fee rows post overnight, so an intraday read is legitimately a few cents out on the
    day's not-yet-booked CAT/TAF — but $999.09 is three orders of magnitude above that. A tolerance
    loose enough to swallow the defect would make the guard decorative, which is worse than absent."""
    near = reconcile(reported_realized=_REPORTED + 0.90, adjustments=_ADJUSTMENTS,
                     equity=_EQUITY, baseline=_BASELINE, unrealized=_UNREALIZED)
    far = reconcile(reported_realized=_REPORTED + 1.50, adjustments=_ADJUSTMENTS,
                    equity=_EQUITY, baseline=_BASELINE, unrealized=_UNREALIZED)
    assert near["reconciled"] and not far["reconciled"]


def test_adjustments_are_bucketed_into_THE_SAME_WINDOWS_as_realizations():
    """A fee is recognised on the day it posts, exactly as a sale's P&L is recognised at the sale.

    Bucketed by `date`, NOT `created_at`: the 2026-08-19 REG fee carries `created_at`
    2026-08-20T00:16Z, so a `created_at` bucket would file the fee for Tuesday's trades under Wednesday
    — and on a 1D window that is the difference between a fee appearing and not.
    """
    day = 86_400 * 1_000_000_000

    def ns_of(iso: str) -> int:
        # Day-resolution stand-in: "2026-08-19T…" -> day index.
        y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
        return (y * 372 + m * 31 + d) * day

    today = ns_of("2026-08-20T00:00:00Z")
    out = realized_by_period([], day_start_ns=today, ns_of=ns_of, adjustments=[_WITHHOLDING, _FEE],
                             floor_of=lambda d, days: d - days * 86_400_000_000_000)

    # 1D floors at TODAY's ET open (PERIOD_DAYS["1D"] == 0), so yesterday's fee is correctly outside it
    # even though it POSTED after midnight UTC today. That is the whole reason for bucketing on `date`:
    # bucketing on `created_at` would have pulled this row into 1D and inflated today by a fee that
    # belongs to yesterday's trades.
    assert out["1D"]["adjustments"] == 0.0
    # 1W reaches back seven days: the 08-19 fee is in, the 08-06 withholding is not.
    assert round(out["1W"]["adjustments"], 2) == -2.26
    assert round(out["all"]["adjustments"], 2) == -992.72
    assert round(out["all"]["net"], 2) == -992.72, "net = fills + adjustments, and fills are empty here"


def test_total_KEEPS_its_meaning_and_the_new_fact_gets_a_new_name():
    """#336 shipped three incomparable numbers under one label. Not again.

    `total` stays fills-only. `adjustments` and `net` are new keys. A consumer that reads `total` gets
    exactly what it got before this change — nothing silently becomes a different quantity.
    """
    out = realized_by_period([], day_start_ns=0, ns_of=lambda _v: 0, adjustments=[_WITHHOLDING],
                             floor_of=lambda d, days: d - days * 86_400_000_000_000)
    assert out["all"]["total"] == 0.0, "an adjustment is not a realization"
    assert out["all"]["adjustments"] == -990.46
    assert out["all"]["net"] == -990.46


def test_the_sweep_fetches_EVERY_activity_type_not_only_fills():
    """THE SEAM. `cash_adjustments` is correct and would have been dead on arrival: the sweep asked for
    `activity_type="FILL"`, so the withholding never reached it. A green unit test on the helper says
    nothing about whether anything calls it with the right data."""
    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._refresh_realized_periods)
    assert 'list_activities(activity_type="FILL")' not in src, "the sweep cannot see non-fill cash"
    assert "activity_type=None" in src
    assert "cash_adjustments" in src and "reconcile" in src


def test_list_activities_with_no_type_OMITS_the_filter():
    """Alpaca returns every type when `activity_types` is absent. Sending it empty is not the same
    request, and sending `activity_types=None` would send the literal string."""
    from api.providers.alpaca.http import AlpacaHttpClient

    class _Recorder(AlpacaHttpClient):
        def __init__(self):
            self.calls = []
            self._trading = "https://paper-api.example"

        async def _get(self, base, path, params=None):
            self.calls.append(dict(params or {}))
            return []

    http = _Recorder()
    asyncio.run(http.list_activities(activity_type=None))
    assert "activity_types" not in http.calls[0], http.calls[0]

    http2 = _Recorder()
    asyncio.run(http2.list_activities(activity_type="FILL"))
    assert http2.calls[0].get("activity_types") == "FILL", "the existing callers must be unchanged"


def test_a_MISSING_baseline_disables_the_check_rather_than_crying_wolf():
    """A wrong baseline makes two correct derivations disagree by a constant forever.

    This repo has paid repeatedly for alarms that fire on healthy state — an operator learns to scroll
    past them, and then scrolls past the real one. When the input is unknown the honest output is
    silence, not a confident residual.
    """
    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._refresh_realized_periods)
    assert "_ACCOUNT_BASELINE" in src
    assert "reconciliation disabled" in src, "an unset baseline must skip, not report a false residual"
