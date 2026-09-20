

# ==================================================================================================
# `is_entry` IS A SECOND DERIVATION AND IS NO LONGER REQUIRED (2026-08-22, both sessions agreed).
#
# The contract required it with this rationale:
#
#   "the budget gate needs this and cannot infer it: a SELL is an exit on a long and an entry on a
#    short. THE STRATEGY KNOWS; THE PLATFORM MUST NOT GUESS."
#
# The platform does exactly that, and is right to:
#
#   exec_client.py:599  positions_open(strategy_id=order.strategy_id, instrument_id=...)  <- scoped net
#   exec_client.py:603  entry = _is_entry(net, order.side.name, qty)
#   budget_gate.py:89   delegates to kumo_strategies' pure `is_entry_order(net, side, qty)`
#
# The net is STRATEGY- AND INSTRUMENT-SCOPED, so the premise that the platform can only see "an order"
# is false — it sees the strategy's own book. Net plus side plus quantity determines entry-ness
# completely, including the flip cases the rationale cites, and kumo-trading-strategies' own tests are the
# evidence (a flip that grows exposure is an entry; one that shrinks it is an exit).
#
# So the rationale and the implementation contradicted each other, and the conformance check enforced
# the losing side: every lane had to implement a method nothing calls, to satisfy a rule whose stated
# reason is untrue. `budget_gate.is_entry`'s own docstring already says why that is a defect —
# "ONE RULE, ONE IMPLEMENTATION ... treat a second derivation as a defect rather than a convenience".
#
# Removed from REQUIRED here FIRST; kumo-trading-strategies drops the Protocol member and the mixin behind it,
# so no lane is momentarily non-conforming.
# ==================================================================================================
def test_is_entry_is_NOT_required_of_a_strategy():
    from api.strategy_contract import REQUIRED

    assert "is_entry" not in REQUIRED, (
        "requiring is_entry makes every lane implement a second derivation of a fact "
        "`is_entry_order(net, side, qty)` already derives, and which nothing calls"
    )


def test_the_members_the_platform_ACTUALLY_cannot_work_without_are_still_required():
    """The relaxation must not become a licence to drop the rest. These four have real consumers."""
    from api.strategy_contract import REQUIRED

    assert set(REQUIRED) == {"external_id", "label", "claimed_instruments", "warmup_bars"}


def test_the_platform_derives_entry_ness_from_a_STRATEGY_SCOPED_net():
    """The load-bearing fact behind the removal. If the net were ACCOUNT-level, one strategy's exit
    could read as an entry against another's position and the old rationale would have been right."""
    import inspect

    # FOLLOWS THE LOGIC. The rule moved to `budget_guard` in #782 so both exec clients share ONE
    # derivation; a test still pointed at the Alpaca class would pass while checking a delegation
    # stub — a detector aimed one level away from what it protects.
    from api import budget_guard as exec_client

    src = inspect.getsource(exec_client)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    idx = code.index("_is_entry(")
    window = code[max(0, idx - 600):idx]
    assert "positions_open(strategy_id=" in window, (
        "the net feeding is_entry is no longer strategy-scoped — entry-ness would then be inferred "
        "from the ACCOUNT book and this removal must be reconsidered"
    )
