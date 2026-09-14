"""`/claims/breaches` — the ledger check, in the repo rather than in someone's terminal (#437).

`over_claimed` is kumo-strategies' predicate and `claims_invariant` is cockpit's consumer, but until
now the only thing CALLING them was an ad-hoc monitor command in a chat session. That is not a
mechanism; it dies when the session does, and it is the same "built, never executed" category as the
detector that had no caller and the flag nothing checked.

The endpoint exists because the two halves live in different places: claims are rows in Postgres
(`exec_position_state`) and the account is the broker's. Only the API process holds both.
"""

from __future__ import annotations

import pytest

from api.claims_endpoint import account_from_positions, build_breaches


class _Row:
    """Shaped like the SQLAlchemy Row the claims query returns — `_mapping`, not a dict. `dict(row)`
    on a real Row raises, so a dict double would accept code that fails in production."""

    def __init__(self, sid, sym, qty):
        self._mapping = {"strategy_id": sid, "symbol": sym, "qty": qty}


_LIVE_ROWS = [
    _Row("BCTROT-004", "AEM", 9), _Row("MOMENTUM-002", "AEM", 9),
    _Row("BCTROT-004", "BETA", 79), _Row("MOMENTUM-002", "BETA", 77),
    _Row("BCTROT-004", "WHD", 28), _Row("MOMENTUM-002", "WHD", 28),
]
_LIVE_ACCOUNT = {"AEM": 18.0, "BETA": 79.0}  # WHD absent — the account holds none


def test_the_double_rejects_what_a_real_Row_rejects():
    with pytest.raises(TypeError):
        dict(_Row("A", "B", 1))


def test_the_fixture_holds_a_clean_symbol_AND_a_breach():
    """Fixture property first: AEM is consistent and must stay silent, BETA is not. Without both, an
    implementation that flags everything passes."""
    assert sum(r._mapping["qty"] for r in _LIVE_ROWS if r._mapping["symbol"] == "AEM") == 18
    assert sum(r._mapping["qty"] for r in _LIVE_ROWS if r._mapping["symbol"] == "BETA") > 79


def test_the_live_ledger_reports_exactly_the_two_breaches():
    b = build_breaches(_LIVE_ROWS, _LIVE_ACCOUNT)
    assert b.status == "ok"
    assert sorted(b.breaches) == ["BETA", "WHD"]
    assert b.breaches["BETA"]["claimed"] == 156
    assert b.breaches["BETA"]["held"] == 79
    assert b.breaches["BETA"]["holders"] == {"BCTROT-004": 79, "MOMENTUM-002": 77}


def test_the_holders_are_named_not_just_the_total():
    """"BETA over-claimed by 77" is not actionable. WHICH strategy carries the stale claim is the
    entire content of the alarm — it is what tells you whose exit will size against shares it does not
    own."""
    b = build_breaches(_LIVE_ROWS, _LIVE_ACCOUNT)
    assert b.breaches["WHD"]["holders"] == {"BCTROT-004": 28, "MOMENTUM-002": 28}


def test_a_clean_ledger_is_status_ok_with_no_breaches():
    b = build_breaches([_Row("A", "AEM", 9), _Row("B", "AEM", 9)], {"AEM": 18.0})
    assert (b.status, b.breaches) == ("ok", {})


def test_an_UNREADABLE_account_is_status_error_and_NOT_an_empty_clean_result():
    """The silencing direction, and the one that matters. `{"status": "ok", "breaches": {}}` for a book
    that could not be read is indistinguishable from a healthy ledger — every consumer would report
    green. `status` exists so a reader can tell "no breaches" from "no answer", the same reason the
    trades plane carries one (#298)."""
    b = build_breaches(_LIVE_ROWS, None)
    assert b.status != "ok"
    assert b.breaches == {}


def test_a_reader_can_distinguish_the_two_states_by_status_ALONE():
    """Aimed at the class: any consumer that checks only `breaches` treats an outage as health. This
    pins that the two are distinguishable without looking at anything else."""
    clean = build_breaches([_Row("A", "AEM", 9)], {"AEM": 18.0})
    broken = build_breaches(_LIVE_ROWS, None)
    assert clean.breaches == broken.breaches == {}
    assert clean.status != broken.status


# ==================================================================================================
# NETTING THE ENGINE'S LEGS INTO AN ACCOUNT BOOK. The claims check compares against the ACCOUNT, and
# the engine serves PER-STRATEGY legs — two legs per symbol for every shared holding.
# ==================================================================================================
class _Pos:
    def __init__(self, iid, side, qty):
        self.instrument_id, self.side, self.quantity = iid, side, qty


def test_the_fixture_has_an_unsigned_quantity_on_the_SHORT_leg():
    # Fixture property first, and it is the whole hazard: the DTO reports 28 for a short, not -28.
    assert _Pos("WHD.XNYS", "SHORT", 28.0).quantity == 28.0


def test_opposing_legs_net_to_ZERO_and_not_to_double():
    """The live WHD book. Summing unsigned quantities gives 56 held — which would make the claims of
    56 look perfectly covered and delete the breach that matters."""
    assert account_from_positions([
        _Pos("WHD.XNYS", "LONG", 28.0), _Pos("WHD.XNYS", "SHORT", 28.0)]) == {"WHD": 0.0}


def test_same_side_legs_ADD():
    """Discriminating half — netting must not turn into cancelling. BCTROT 86 + MOMENTUM 88 of CGAU is
    174 real shares, and the broker agrees."""
    assert account_from_positions([
        _Pos("CGAU.XNYS", "LONG", 86.0), _Pos("CGAU.XNYS", "LONG", 88.0)]) == {"CGAU": 174.0}


def test_the_venue_suffix_is_stripped_because_claims_are_keyed_on_BARE_symbols():
    """`exec_position_state.symbol` holds `WHD`; the engine holds `WHD.XNYS`. Comparing them unstripped
    means every symbol looks unclaimed and the check reports a permanently clean ledger."""
    assert account_from_positions([_Pos("AEM.XNYS", "LONG", 9.0)]) == {"AEM": 9.0}


def test_an_UNKNOWN_side_is_skipped_rather_than_counted_as_long():
    """Fail closed on the silencing direction: treating an unrecognised side as LONG inflates the
    account book, and an inflated account makes over-claiming disappear."""
    assert account_from_positions([_Pos("X.XNYS", "WAT", 5.0)]) == {}
