"""The claims ledger may not claim more than the account holds (#437, issue 66).

MEASURED ON A LIVE PAPER BOOK, 2026-08-22. Three of eight symbols breach RIGHT NOW:

    symbol  claimed  held   by
    BETA        156    79   +77   BCTROT-004=79 MOMENTUM-002=77
    WHD          56     0   +56   BCTROT-004=28 MOMENTUM-002=28
    XLV          22     0   +22   BCTROT-004=11 MOMENTUM-002=11

WHY THIS IS THE CHECK THAT MATTERS. It fires the session BEFORE a bad sell, not twelve hours after.
MOMENTUM's exit sizing is `min(account, claim, attribution)` — genuinely scoped — but attribution only
narrows where it REPORTS the symbol, and a position reconciled in after a restart comes back with no
strategy id. Then sizing falls back to `min(account, claim)`. On WHD that was `min(28, 28)` and
MOMENTUM sold BCTROT's 28 shares.

BETA is the one to look at for Monday: 79 held, MOMENTUM claiming 77 of shares it does not own. WHD
happened to be flat so the damage was a phantom short. BETA is not flat.

The predicate is kumo-trading-strategies' (`over_claimed`, c2358d1) and is deliberately NOT reimplemented here
— two derivations of one fact is the defect class this whole ticket is about. Cockpit owns the
CONSUMER: it has the account book and the claims store, and the predicate had none until now.
"""

from __future__ import annotations

from api.claims_invariant import claim_breaches, format_breach

#: The live 2026-08-22 ledger, exactly as `exec_position_state` and the broker report it.
#: KEYED BY STRATEGY, THEN BY SYMBOL — and getting that inverted is why this fixture is annotated.
#: The first version of this consumer passed symbol-keyed dicts. `over_claimed` did not raise: it
#: returned {'A': (9, 0), 'B': (9, 0)}, folding STRATEGY IDS into the symbol slot and reporting a
#: perfectly consistent ledger as two breaches. A wrong-shaped argument produced confident nonsense,
#: which is the failure mode a type-free dict boundary always has.
_LIVE_CLAIMS = {
    "BCTROT-004": {"AEM": 9, "BETA": 79, "WHD": 28, "XLV": 11},
    "MOMENTUM-002": {"AEM": 9, "BETA": 77, "WHD": 28, "XLV": 11},
}
_LIVE_ACCOUNT = {"AEM": 18, "BETA": 79}  # WHD and XLV are absent — the account holds none


def test_the_fixture_contains_BOTH_a_consistent_symbol_and_a_breach():
    """Fixture property first. AEM claims 18 against 18 held and must stay silent; if every symbol in
    the fixture breached, an implementation that flagged everything would pass. kumo-trading-strategies'
    truncation test passed twice with the bug reintroduced for exactly this reason."""
    def total(sym):
        return sum(c.get(sym, 0) for c in _LIVE_CLAIMS.values())

    assert total("AEM") == _LIVE_ACCOUNT["AEM"], "no consistent symbol — a flag-everything impl passes"
    assert total("BETA") > _LIVE_ACCOUNT["BETA"], "no breaching symbol — the assertion is vacuous"


def test_the_three_live_breaches_are_reported_and_the_consistent_symbol_is_not():
    b = claim_breaches(_LIVE_ACCOUNT, _LIVE_CLAIMS)
    assert sorted(b) == ["BETA", "WHD", "XLV"]
    assert b["BETA"] == (156, 79)
    assert "AEM" not in b


def test_the_argument_is_keyed_by_STRATEGY_not_by_symbol():
    """Pinned because I got it wrong and nothing objected. Passing symbol-keyed claims returns a
    confident, wrong answer — strategy ids appear where symbols belong and a consistent ledger reads
    as breached. There is no type at this boundary, so the shape is asserted instead."""
    symbol_keyed = {"AEM": {"BCTROT-004": 9, "MOMENTUM-002": 9}}
    wrong = claim_breaches({"AEM": 18}, symbol_keyed)
    assert set(wrong) == {"BCTROT-004", "MOMENTUM-002"}, (
        "symbol-keyed input no longer produces the strategy-id-as-symbol confusion; if the predicate "
        "gained validation, assert THAT instead and delete this"
    )
    strategy_keyed = {"BCTROT-004": {"AEM": 9}, "MOMENTUM-002": {"AEM": 9}}
    assert claim_breaches({"AEM": 18}, strategy_keyed) == {}


def test_a_symbol_ABSENT_from_the_account_is_a_breach_not_a_skip():
    """WHD and XLV are not in the account dict at all. Treating a missing key as "unknown, skip" is the
    silencing direction and would drop two of tonight's three breaches — including the one that already
    caused a phantom short."""
    assert claim_breaches({}, {"STRAT-A": {"WHD": 1}}) == {"WHD": (1, 0)}


def test_a_consistent_ledger_is_silent():
    assert claim_breaches({"AEM": 18}, {"A": {"AEM": 9}, "B": {"AEM": 9}}) == {}


def test_under_claiming_is_NOT_reported():
    """The account holding MORE than anyone claims is external or manual activity, which is a different
    question with its own handler. Reporting it here would bury the breaches under noise on every
    account the operator trades by hand — and he trades this one by hand."""
    assert claim_breaches({"AEM": 100}, {"A": {"AEM": 9}}) == {}


def test_the_message_names_WHO_claims_what_not_just_the_total():
    """An alert saying "BETA over-claimed by 77" tells an operator nothing actionable. Which strategy
    holds the stale claim is the entire content of the alarm."""
    holders = {s: c["BETA"] for s, c in _LIVE_CLAIMS.items()}
    msg = format_breach("BETA", (156, 79), holders)
    assert "BETA" in msg and "156" in msg and "79" in msg
    assert "MOMENTUM-002=77" in msg and "BCTROT-004=79" in msg


def test_the_predicate_is_KUMO_STRATEGIES_and_not_a_second_implementation():
    """Two derivations of one fact is the defect class this ticket exists to close. If cockpit ever
    grows its own copy, this fails and says why."""
    import inspect

    import api.claims_invariant as mod

    src = inspect.getsource(mod)
    assert "from kumo_strategies" in src, (
        "cockpit has reimplemented the claims invariant instead of importing kumo-trading-strategies' "
        "`over_claimed` — two implementations of one property will disagree, which is the bug"
    )
