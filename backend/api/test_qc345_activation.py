"""QC345 activation readiness (#318/#320, kumo-strategies#34 and #37).

QC345 is NOT activatable yet, and this file is the honest record of exactly why. Two dependencies are
missing upstream, both small, both isolated. Cockpit does not fake activation with local logic — a
strategy that appears to run while its decision code lives somewhere else is worse than one that
plainly does not run.

What these tests do is make the boundary MEASURED rather than remembered: each blocker is asserted, so
the day it lands upstream the assertion fails and says what to do next. A blocker recorded only in a
ticket is a blocker nobody notices has been fixed.
"""

from __future__ import annotations

#: The promoted live-feasible candidate (kumo-strategies PR #36). NOT the strategy's own defaults —
#: those remain `close_split_dividend` / 41 / None. This is a DEPLOYMENT choice, which is why it lives
#: in cockpit's settings rather than upstream in the dataclass.
#:
#: Measured over the available 2025/2026 window:
#:   baseline   97.166% return, Sharpe 1.137, max DD -36.199%
#:   candidate 115.478% return, Sharpe 1.395, max DD -26.392%
PROMOTED = {
    "momentum_price_field": "close",
    "corporate_action_window": 252,
    "exits.stall_days": 12,
}


def test_qc345_is_registered_with_the_right_identity():
    """`QC345-004`, and the tag is not cosmetic.

    It was briefly 003, assigned by taking the next free number without checking what kumo-strategies
    had already named. kumo-strategies#32 is titled "BCTROT-003", so 003 was spoken for — and two
    strategies sharing an `order_id_tag` does not degrade, it stops the node booting at
    `Trader.add_strategy`.

    Free to correct only because QC345 had never registered or filled. After one position the id is
    permanent: the NETTING position id is {instrument}-{strategy_id}, so changing it re-homes
    everything it owns.
    """
    from api.strategy_registry import by_id, validate

    validate()
    entry = by_id("QC345-003")
    assert entry is not None, "QC345 is not in the registry"
    assert (entry.name, entry.tag) == ("QC345", "003")
    assert by_id("QC345-004") is None, "the interim tag is still registered"


def test_qc345_has_a_budget_field_so_it_can_be_funded():
    """Without one it renders no control and can never be allocated capital — and it would look like a
    strategy deliberately set to zero."""
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).parent.parent / "config" / "settings" / "strategies.schema.json").read_text()
    )
    assert "QC345-003" in schema["properties"]
    assert "QC345-004" not in schema["properties"], "a budget field for the interim id lingers"


def test_the_promoted_candidate_uses_only_LIVE_SUPPORTED_exit_rules():
    """The trap this catches: a settings UI that lets an operator set a rule the live runner cannot
    honour is worse than one that omits it — the rule typechecks, backtests, deploys, and is then
    silently ignored by the thing holding real positions.

    `stall_days` is in the promoted candidate, so it MUST be live-supported. Verified against upstream's
    own `LIVE_SUPPORTED_EXITS` rather than assumed.
    """
    from kumo_strategies.strategies.momentum_rotation.config import LIVE_SUPPORTED_EXITS

    assert "stall_days" in LIVE_SUPPORTED_EXITS, (
        "the promoted candidate sets stall_days, which the live runner would ignore"
    )


def test_the_promoted_candidate_differs_from_the_strategys_own_defaults():
    """Pins that these are a DEPLOYMENT choice, not a restatement of upstream defaults.

    If they ever coincide, the override is doing nothing and should be deleted rather than left as
    decoration that implies a decision was made.
    """
    from kumo_strategies.strategies.qc345_rotation.config import QC345RotationConfig

    cfg = QC345RotationConfig()
    assert cfg.momentum_price_field != PROMOTED["momentum_price_field"]
    assert cfg.corporate_action_window != PROMOTED["corporate_action_window"]
    assert cfg.exits.stall_days != PROMOTED["exits.stall_days"]


def test_the_schema_blocker_is_RESOLVED_and_the_domain_is_generated():
    """kumo-strategies#37, fixed upstream in 261710d — and this test is the record of how it was found.

    The previous version asserted the blocker EXISTED, so it failed the moment upstream fixed it and
    said what to do next. That is the whole point of pinning a blocker as a test rather than a ticket:
    a ticket has to be re-read to notice it is stale.

    What replaced it: `schema_for` now maps `Literal` to an enum and recurses into nested dataclasses,
    so cockpit generates the `qc345` domain from the config dataclass instead of hand-keeping a copy.
    Covered in detail by `api/settings/test_generated.py`; asserted here so the activation file states
    the current boundary honestly.
    """
    from api.settings import store

    assert "qc345" in store.domains(), "the generated qc345 settings domain is gone"


def test_cockpit_finds_the_adapter_by_EXTERNAL_id_not_by_its_internal_tag():
    """Operator: "why would you even look at the adapter ... it doesn't matter if you manage it at 006".

    Right, and my mistake was subtler than a wrong number: I read `STRATEGY_TAG = "003"` out of the
    shipped adapter and reconciled cockpit TO it. That makes the adapter the authority on an id cockpit
    owns, and turns any upstream rename into a cockpit migration.

    The internal id keys positions (`{instrument}-{strategy_id}` under NETTING) and must never move
    once anything has traded. The external id is only ever a lookup key, so upstream can rename itself
    without touching a single position.

    This asserts the LOOKUP works and deliberately does NOT assert the adapter's tag matches ours.
    """
    from api.strategy_registry import by_external_id

    entry = by_external_id("QC345")
    assert entry is not None and entry.strategy_id == "QC345-003"


def test_the_internal_tag_is_cockpits_and_need_not_match_the_adapters():
    """The property that makes the mapping worth having.

    Re-tagging QC345 to 006 must be a cockpit-local decision that changes nothing about how the adapter
    is found. If this ever requires touching upstream, the mapping is not doing its job.
    """
    from api.strategy_registry import StrategyEntry, by_external_id, validate

    retagged = (StrategyEntry("QC345", "006", "qc345", "QC345", "QC345 — retagged", "monthly", "trail"),)
    validate(retagged)
    assert by_external_id("QC345", retagged).strategy_id == "QC345-006"


def _unused_the_adapter_has_LANDED_and_carries_the_identity_cockpit_allocated():
    """kumo-strategies#34 is resolved — the blocker test fired and named this follow-up.

    Both blocker tests did their job today: each failed the moment upstream fixed the thing it
    described, rather than sitting stale in a ticket nobody re-reads.

    The adapter hardcodes `STRATEGY_TAG = "003"`, and cockpit's registry now agrees. It briefly did
    not: I had reserved 003 for BCTROT on the strength of kumo-strategies#32's title and moved QC345
    to 004. Cockpit owns allocation, and the right exercise of that is to give the tag to the strategy
    that EXISTS — disagreeing with the shipped adapter would mean the registry and the running code
    assert different ids for the same positions.
    """
    from kumo_strategies.runtime.nautilus import qc345_rotation as adapter

    from api.strategy_registry import by_id

    entry = by_id("QC345-003")
    assert (adapter.STRATEGY_NAME, adapter.STRATEGY_TAG) == (entry.name, entry.tag), (
        "the registry and the shipped adapter disagree about QC345's identity"
    )


def test_BOTH_shipped_adapters_satisfy_the_registration_contract():
    """kumo-strategies#39 implemented (33eee4c). The third blocker test to fire and be retired today.

    Each asserted a gap so it would FAIL the day upstream closed it, rather than sitting in a ticket
    nobody re-reads. All three worked: the schema generator, the missing adapter, and this.

    Asserted for MOMENTUM as well as QC345 — the request was joint precisely so cockpit does not grow
    a per-strategy registration path, and a contract only one strategy satisfies is not a contract.
    """
    from kumo_strategies.runtime.nautilus.momentum_rotation import MomentumRotationStrategy
    from kumo_strategies.runtime.nautilus.qc345_rotation import QC345RotationStrategy

    from api.strategy_contract import conforms

    for strategy in (QC345RotationStrategy, MomentumRotationStrategy):
        ok, missing = conforms(strategy)
        assert ok, f"{strategy.__name__} is missing {missing}"


def test_the_external_id_carries_no_allocation_number():
    """Upstream now rejects a trailing -NNN by test, and cockpit relies on it.

    An external id ending in a number is what made `BCTROT-003` look like a claim on tag 003 to me and
    a plain name to them, costing a 003 -> 004 -> 003 round trip. Digits INSIDE a name are fine —
    QC345's 345 identifies the QuantConnect strategy rather than claiming an allocation.
    """
    import re

    from kumo_strategies.runtime.nautilus.qc345_rotation import EXTERNAL_ID

    from api.strategy_registry import by_external_id

    # The MODULE CONSTANT, not the class attribute: `external_id` is a property, so reading it off the
    # class yields the property object rather than a string. `conforms()` is unaffected — it asks
    # hasattr, which a property satisfies — but anything wanting the VALUE needs the constant or an
    # instance, and an instance needs a live node.
    external = EXTERNAL_ID
    assert not re.search(r"-\d+$", external), f"{external} looks like an allocation"
    assert by_external_id(external) is not None, "cockpit cannot map the id the adapter publishes"


def test_cockpit_can_instantiate_QC345_at_a_tag_of_its_own_choosing():
    """The property that makes the mapping real rather than decorative.

    `order_id_tag` is a constructor parameter now, so cockpit can allocate 006 and the adapter honours
    it. Before, cockpit could map QC345 to 006 and then had no way to build it at 006.
    """
    import inspect

    from kumo_strategies.runtime.nautilus.qc345_rotation import QC345RotationStrategy

    assert "order_id_tag" in inspect.signature(QC345RotationStrategy.__init__).parameters, (
        "the adapter builds its own StrategyId again — cockpit cannot allocate"
    )
