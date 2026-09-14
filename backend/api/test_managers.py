"""Tests for the manager state reducer (#55 first slice). Pure — pinning all three leash levels even though
only AUTO has a real caller today, so the machine is validated ahead of #46/#47 needing CONFIRM (code review:
don't ship something that looks generic but is AUTO-only in disguise)."""

from __future__ import annotations

from api.managers import next_state

# --- AUTO: trigger fires -> straight through to APPLYING, no human gate ---------------------------------

def test_auto_attach_arms():
    assert next_state("ARMED", "ATTACHED", "AUTO") == "ARMED"


def test_auto_trigger_records_intent_and_moves_to_applying():
    assert next_state("ARMED", "INTENT_RECORDED", "AUTO") == "APPLYING"


def test_auto_never_stops_at_proposed():
    """AUTO's trigger firing IS the decision — the slide-to-flatten already happened; only timing deferred."""
    assert next_state("ARMED", "PROPOSED", "AUTO") == "ARMED"


def test_auto_apply_success_and_failure():
    assert next_state("APPLYING", "APPLIED", "AUTO") == "APPLIED"
    assert next_state("APPLYING", "FAILED", "AUTO") == "FAILED"


# --- CONFIRM: trigger fires -> PROPOSED -> a human command approves or rejects ---------------------------

def test_confirm_trigger_stops_at_proposed():
    assert next_state("ARMED", "PROPOSED", "CONFIRM") == "PROPOSED"


def test_confirm_approval_advances_then_applies():
    assert next_state("PROPOSED", "APPROVED", "CONFIRM") == "APPROVED"
    assert next_state("APPROVED", "INTENT_RECORDED", "CONFIRM") == "APPLYING"
    assert next_state("APPLYING", "APPLIED", "CONFIRM") == "APPLIED"


def test_confirm_rejection_cancels():
    assert next_state("PROPOSED", "REJECTED", "CONFIRM") == "CANCELLED"


# --- ALERT: trigger fires -> PROPOSED -> nothing ever applies it automatically ---------------------------

def test_alert_trigger_stops_at_proposed():
    assert next_state("ARMED", "PROPOSED", "ALERT") == "PROPOSED"


def test_alert_never_advances_past_proposed():
    """No APPROVED path for ALERT — it flags, it never acts. Any event once PROPOSED is a no-op."""
    assert next_state("PROPOSED", "INTENT_RECORDED", "ALERT") == "PROPOSED"
    assert next_state("PROPOSED", "APPLIED", "ALERT") == "PROPOSED"


# --- cross-cutting: CANCELLED is reachable from anywhere, ATTACHED always arms ----------------------------

def test_cancel_from_any_state():
    for leash in ("AUTO", "CONFIRM", "ALERT"):
        for state in ("ARMED", "PROPOSED", "APPROVED", "APPLYING"):
            assert next_state(state, "CANCELLED", leash) == "CANCELLED"


def test_illegal_transition_holds_rather_than_guesses():
    """No legal path from APPLIED forward — must stay put, not silently move."""
    assert next_state("APPLIED", "INTENT_RECORDED", "AUTO") == "APPLIED"


def test_failed_is_only_reachable_from_applying():
    """Pinning the transition that made a fix inert (#255). The leash guard in `_dispatch_managers_of_kind`
    records FAILED for a row this engine will never apply — but the reducer accepts FAILED only out of
    APPLYING, so recording it against an ARMED row leaves the row ARMED and looking healthy. The guard has
    to CLAIM first. If this assertion ever flips, that claim becomes unnecessary; until then it is load-
    bearing."""
    assert next_state("ARMED", "FAILED", "AUTO") == "ARMED"
    assert next_state("APPLYING", "FAILED", "AUTO") == "FAILED"


def test_max_trim_count_takes_the_highest_not_the_sum():
    """#266. `trim_max` is a budget for the POSITION — the operator's reasoning was transaction cost, "not tiny
    repeated nibbles" — but it was only compared against a chain-local `trim_count` that restarts at 0 on
    every fresh arm. OKTA spent its two trims (58 -> 6), was re-armed at 13:48:12, and spent two more
    within 34 seconds (6 -> 2) at a HIGHER price.

    Each successor records its predecessor's count PLUS ONE, so the running total is the maximum. Summing
    would double-count the chain.
    """
    from api.managers import max_trim_count

    # One spent chain: the arm recorded 0, its two successors 1 and 2.
    assert max_trim_count([{"trim_count": 0}, {"trim_count": 1}, {"trim_count": 2}]) == 2
    assert max_trim_count([]) == 0
    assert max_trim_count([{}]) == 0


def test_max_trim_count_survives_a_malformed_row():
    """A single unparsable params blob must not make PEAK unarmable for the whole cycle."""
    from api.managers import max_trim_count

    assert max_trim_count([{"trim_count": "two"}, {"trim_count": 3}, None, {"trim_count": None}]) == 3


def test_a_null_cycle_cannot_be_scoped_and_reports_zero():
    """Legacy rows predate cycle ids (#68). Reporting 0 keeps the old per-chain behaviour, which is the
    safe direction: it can only ever PERMIT trims, never invent extra ones."""
    import asyncio

    from api.managers import trims_spent_on_cycle

    assert asyncio.run(trims_spent_on_cycle(None, "peak_watch", "AEM.XNYS", "MANUAL-001", None)) == 0
