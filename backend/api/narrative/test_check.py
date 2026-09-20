"""Every test is a way a narrative could mislead about a session that really happened.

The bundle is the real 2026-08-10 journal, trimmed. It is used because it carries the trap by
accident: the decision says `enter 8` and names eight symbols, two were cap-blocked, and six buys
were submitted. A narrator reading the decision instead of the orders reports eight.

Several tests below exist because an adversarial review broke the first version of this checker.
Those are marked.
"""

from __future__ import annotations

import pytest

from api.narrative.check import Narrative, actionable, check, render, submitted

BUNDLE = {
    "session": "2026-08-10",
    "lifecycle": {"state": "TRADING", "reason": "operator: start paper trading"},
    "decision": {
        "summary": "TRADING: hold 8 · enter 8 · exit 2",
        "reasons": {
            "CGAU": "entered: rank 1", "VCTR": "entered: rank 2", "VFLO": "entered: rank 3",
            "LNC": "entered: rank 4", "AEM": "entered: rank 5", "WPM": "entered: rank 6",
            "CHEF": "entered: rank 7", "BRK.B": "entered: rank 8",
            "MET": "left the ranking",
            "PRU": "gave back all of a 0.7% peak and is 1.5% below entry",
        },
        "equity": 96634.77,
    },
    "risk": [
        "bars missing for 3/65 pool names (95% coverage)",
        "1 account positions are not this strategy's — leaving them alone",
        "3 pool names are not subscribed in this node — excluded from ranking until it restarts",
        "skipped CHEF: position cap",
        "skipped BRK.B: position cap",
    ],
    "orders": [
        "SELL 105 MET: submitted via nautilus (initialized)",
        "SELL 81 PRU: submitted via nautilus (initialized)",
        "BUY 459 CGAU @~21.01: submitted via nautilus (initialized)",
        "BUY 88 VCTR @~109.81: submitted via nautilus (initialized)",
        "BUY 186 VFLO @~51.77: submitted via nautilus (initialized)",
        "BUY 212 LNC @~45.46: submitted via nautilus (initialized)",
        "BUY 54 AEM @~176.04: submitted via nautilus (initialized)",
        "BUY 73 WPM @~131.49: submitted via nautilus (initialized)",
    ],
    "errors": [],
    "trail": [{"sym": "MET", "entry": 94.85, "peak": 99.95, "held": 1, "quality": "reconstructed"}],
}

ATTENTION = [
    "bars missing for 3/65 pool names (95% coverage)",
    "1 account positions are not this strategy's — leaving them alone",
    "3 pool names are not subscribed in this node — excluded from ranking until it restarts",
]

TRUE = Narrative(
    headline="MOMENTUM-002 rotated positions out and in.",
    exits=["MET", "PRU"],
    entries_submitted=["CGAU", "VCTR", "VFLO", "LNC", "AEM", "WPM"],
    entries_blocked=["CHEF", "BRK.B"],
    needs_attention=ATTENTION,
)


def rules(n, bundle=BUNDLE) -> set[str]:
    return {v.rule for v in check(n, bundle)}


def swap(**kw) -> Narrative:
    return Narrative(**{**TRUE.__dict__, **kw})


# -- the truth passes -------------------------------------------------------------------------------
def test_an_accurate_narrative_raises_nothing():
    assert check(TRUE, BUNDLE) == []


def test_orders_are_read_not_the_decision_summary():
    """The summary says `enter 8`. Six buys were submitted. The checker sides with the orders."""
    assert submitted(BUNDLE, "BUY") == ["CGAU", "VCTR", "VFLO", "LNC", "AEM", "WPM"]
    assert submitted(BUNDLE, "SELL") == ["MET", "PRU"]


# -- the failure this module exists for --------------------------------------------------------------
def test_reporting_intent_as_outcome_is_caught():
    """Reading `enter 8` and reporting eight entries when the cap stopped two. The digest already
    made this in the other direction — 'entered 6' on a day that entered 0."""
    assert "entries_mismatch" in rules(swap(entries_submitted=[*TRUE.entries_submitted,
                                                               "CHEF", "BRK.B"]))


def test_a_symbol_cannot_be_both_submitted_and_blocked():
    assert "blocked_but_submitted" in rules(swap(entries_blocked=["CGAU"]))


def test_a_blocked_claim_needs_a_risk_row_to_back_it():
    """REVIEW: previously any symbol appearing anywhere could be called blocked, so
    'FSM — blocked by the cap' passed on a symbol that merely ranked."""
    assert "block_not_grounded" in rules(swap(entries_blocked=["FSM"]))


# -- invention ----------------------------------------------------------------------------------------
def test_an_invented_ticker_is_caught():
    assert rules(swap(entries_submitted=[*TRUE.entries_submitted[:-1], "NVDA"])) & {
        "ungrounded_symbol", "entries_mismatch"}


def test_a_ticker_invented_in_the_headline_is_caught():
    """REVIEW: free-text symbols were never validated, so a wrong ticker rode along in prose."""
    assert "ungrounded_symbol" in rules(swap(headline="NVDA led the book today."))


def test_the_model_may_not_state_numbers_at_all():
    """REVIEW: numbers were checked for presence anywhere in the bundle, so 'bought CGAU at 109.81'
    passed using VCTR's fill price. Counts and prices are rendered by us instead."""
    assert "number_in_free_text" in rules(swap(headline="Bought CGAU at 109.81."))
    assert "number_in_free_text" in rules(swap(headline="Submitted all 8 intended buys."))


def test_the_strategy_id_is_not_a_number_or_a_ticker():
    assert check(swap(headline="MOMENTUM-002 rotated the book."), BUNDLE) == []


# -- reasons are rendered, not written -----------------------------------------------------------------
def test_exit_reasons_come_from_the_journal_verbatim():
    """REVIEW killed the paraphrase check: an empty reason passed because '' is in every string, and
    'did not leave the ranking' passed on word overlap while reversing the meaning. The model no
    longer writes reasons at all."""
    out = render(TRUE, BUNDLE)
    assert "MET — left the ranking" in out
    assert "PRU — gave back all of a 0.7% peak and is 1.5% below entry" in out
    assert "sold into strength" not in out


def test_an_exit_with_no_recorded_reason_is_refused():
    bundle = {**BUNDLE, "decision": {**BUNDLE["decision"],
                                     "reasons": {k: v for k, v in BUNDLE["decision"]["reasons"].items()
                                                 if k != "PRU"}}}
    assert "reason_missing" in rules(TRUE, bundle)


def test_render_states_counts_from_the_orders_not_the_model():
    out = render(TRUE, BUNDLE)
    assert "6 submitted:" in out
    assert "2 ranked and not bought:" in out


# -- attention items are journal rows -------------------------------------------------------------------
def test_a_paraphrased_attention_item_is_refused():
    """REVIEW: loose overlap let 'the feed looks unhealthy' ride on a real row. Verbatim or nothing."""
    assert "attention_not_verbatim" in rules(swap(needs_attention=["the data feed looks unhealthy"]))


def test_an_invented_attention_item_is_refused():
    assert "attention_not_verbatim" in rules(swap(needs_attention=["broker connection unstable"]))


# -- the most dangerous claim ----------------------------------------------------------------------------
def test_a_false_all_clear_is_caught_on_errors():
    bundle = {**BUNDLE, "errors": ["submit failed for AEM: insufficient buying power"]}
    assert "false_all_clear" in rules(swap(nothing_needed=True, needs_attention=[]), bundle)


def test_a_false_all_clear_is_caught_on_a_degraded_risk_row():
    """REVIEW's sharpest catch: the model could stay SILENT about a risk row and then declare
    all-clear, because rule 7 only looked at errors. Silence was the exploit."""
    assert "false_all_clear" in rules(swap(nothing_needed=True, needs_attention=[]))


def test_a_position_cap_alone_does_not_block_an_all_clear():
    """The cap doing its job is policy, not an incident — otherwise every capped day cries wolf."""
    quiet = {**BUNDLE, "risk": ["skipped CHEF: position cap"], "errors": []}
    n = Narrative(headline="Rotation complete.", exits=["MET", "PRU"],
                  entries_submitted=["CGAU", "VCTR", "VFLO", "LNC", "AEM", "WPM"],
                  entries_blocked=["CHEF"], nothing_needed=True)
    assert check(n, quiet) == []


def test_flagging_items_and_claiming_all_clear_is_a_contradiction():
    assert "contradiction" in rules(swap(nothing_needed=True))


def test_actionable_separates_incidents_from_policy():
    rows = actionable(BUNDLE)
    assert any("not subscribed" in r for r in rows)
    assert not any("position cap" in r for r in rows)


# -- tense and style ------------------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", ["CGAU should keep running", "AEM is likely to continue",
                                    "WPM will retest its high"])
def test_forward_looking_language_is_caught(phrase):
    assert "forward_looking" in rules(swap(headline=phrase))


def test_banned_jargon_is_caught():
    assert "banned_word" in rules(swap(headline="Risk-on regime, conviction intact."))


def test_a_word_merely_containing_a_banned_word_is_allowed():
    """REVIEW: substring matching rejected 'insignificant' for containing 'significant'."""
    assert "banned_word" not in rules(swap(headline="An insignificant amount of drift."))


# -- degenerate bundles ------------------------------------------------------------------------------
def test_a_session_that_never_decided_cannot_be_narrated_as_a_rotation():
    blocked = {"session": "2026-08-11", "decision": None, "orders": [],
               "risk": ["bar coverage 41% below the floor — refusing to rank"], "errors": []}
    assert {"entries_mismatch", "exits_mismatch"} <= rules(TRUE, blocked)


def test_a_quiet_session_narrates_cleanly():
    quiet = {"session": "2026-08-11", "decision": {"reasons": {}}, "orders": [], "risk": [],
             "errors": []}
    n = Narrative(headline="No rotation today.", nothing_needed=True)
    assert check(n, quiet) == []
    assert "Nothing needs you." in render(n, quiet)


def test_an_empty_bundle_does_not_crash_the_checker():
    assert isinstance(check(Narrative(), {}), list)


def test_duplicate_sells_of_one_symbol_are_not_collapsed():
    """A partial exit followed by a full exit is two orders. Collapsing them would let a narrative
    claim one exit where two happened."""
    bundle = {**BUNDLE, "orders": [*BUNDLE["orders"], "SELL 20 MET: submitted via nautilus"]}
    assert submitted(bundle, "SELL") == ["MET", "PRU", "MET"]
    assert "exits_mismatch" in rules(TRUE, bundle)
