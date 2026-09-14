"""Strategy settings domains generated from the kumo-strategies dataclasses (#318/#320, upstream #32/#37).

The point of generating rather than writing is that the two repos cannot drift. These tests are what
make that true rather than aspirational.
"""

from __future__ import annotations

import json

import pytest

from api.settings import store
from api.settings.generated import load_strategy_schema, strategy_domains


def test_qc345_is_a_registered_domain():
    assert "qc345" in store.domains()
    assert "qc345" in strategy_domains()


def test_EVERY_upstream_field_is_offered():
    """The drift guard, and the reason this is generated at all.

    Upstream's config gained nine fields and lost two in one day. A field it declares and cockpit does
    not offer is a parameter the operator cannot set and does not know exists — silent, and the
    operator's model of what is configured stops matching what is running.
    """
    from dataclasses import fields as dc_fields

    from kumo_strategies.strategies.momentum_rotation.config import ExitConfig
    from kumo_strategies.strategies.qc345_rotation.config import QC345RotationConfig

    schema = load_strategy_schema("qc345")
    offered = set(schema["properties"])
    declared = {f.name for f in dc_fields(QC345RotationConfig)}
    assert declared <= offered, f"upstream fields not offered: {declared - offered}"

    exits = set(schema["properties"]["exits"]["properties"])
    declared_exits = {f.name for f in dc_fields(ExitConfig)}
    assert declared_exits <= exits, f"exit rules not offered: {declared_exits - exits}"


def test_the_promoted_candidate_is_what_an_operator_gets_by_default():
    """The deployment choice, not the strategy's own defaults."""
    schema = load_strategy_schema("qc345")
    assert schema["properties"]["momentum_price_field"]["default"] == "close"
    assert schema["properties"]["corporate_action_window"]["default"] == 252
    assert schema["properties"]["exits"]["properties"]["stall_days"]["default"] == 12


def test_the_promoted_defaults_actually_OVERRIDE_something():
    """An override that matches the dataclass is decoration implying a decision nobody made.

    If upstream ever adopts these as its own defaults, this fails and the override should be deleted
    rather than left behind asserting a choice that is no longer being expressed here.
    """
    from kumo_strategies.strategies.qc345_rotation.config import QC345RotationConfig

    cfg = QC345RotationConfig()
    assert cfg.momentum_price_field != "close"
    assert cfg.corporate_action_window != 252
    assert cfg.exits.stall_days != 12


def test_literal_fields_render_as_enums_not_free_text():
    """`momentum_price_field` as a text box is how a value typechecks, deploys, and silently changes
    what the strategy ranks on — the #319 failure in miniature. The valid set is already declared, so
    an invalid one must be untypeable."""
    schema = load_strategy_schema("qc345")
    for name in ("asset_universe_mode", "market_cap_mode", "momentum_price_field"):
        spec = schema["properties"][name]
        assert "enum" in spec, f"{name} renders as free text"
        assert len(spec["enum"]) >= 2
    assert "close_split_dividend" in schema["properties"]["momentum_price_field"]["enum"]


def test_live_supported_survives_the_conversion():
    """Carried from upstream, never re-derived here — two answers to one question drift.

    A UI that offers a rule the live runner ignores is worse than one that omits it: it typechecks,
    backtests, deploys, and then silently never fires.
    """
    exits = load_strategy_schema("qc345")["properties"]["exits"]["properties"]
    assert exits["stall_days"]["x-live-supported"] is True
    assert all("x-live-supported" in spec for spec in exits.values()), (
        "an exit rule reached the UI with no live-support flag"
    )


def test_a_nullable_field_accepts_null_so_a_rule_can_be_turned_off():
    """Every exit rule defaults to None to mean OFF. A schema that refused null would make clearing a
    rule in the UI fail validation."""
    spec = load_strategy_schema("qc345")["properties"]["exits"]["properties"]["max_hold_days"]
    assert "null" in spec["type"], f"cannot be cleared: {spec['type']}"


def test_the_generated_schema_is_json_serialisable():
    """It crosses the REST boundary. A dataclass or Literal leaking through would 500 the settings
    screen rather than render."""
    json.dumps(load_strategy_schema("qc345"))


def test_resolve_round_trips_through_the_normal_settings_machinery():
    """Requirement 3 of the activation brief: through the NORMAL mechanism, not a parallel path."""
    values = store.resolve("qc345")
    assert values["momentum_price_field"] == "close"
    assert values["corporate_action_window"] == 252
    assert values["lookback_sessions"] == 252, "a non-overridden default was lost"


def test_an_unknown_strategy_domain_raises_KeyError_like_a_missing_file():
    with pytest.raises(KeyError):
        load_strategy_schema("not-a-strategy")


def test_a_NESTED_group_reaches_resolve_not_just_the_schema():
    """The gap that shipped for one deploy.

    The defaulting validator fills properties at each level it VISITS, and it only visits a nested
    object already present in the instance. With `exits` absent from a fresh values file it never
    descended, so the entire group was missing from `resolve()` — `stall_days = 12` was in the schema,
    would have rendered in the form, and reached no consumer.

    Every test here asserted the SCHEMA's defaults, and all of them passed. Caught by reading the
    deployed `/settings/qc345` response. So this asserts the RESOLVED values, which is what anything
    downstream actually consumes.
    """
    values = store.resolve("qc345")
    assert "exits" in values, "the nested exits group never reached resolve()"
    assert values["exits"]["stall_days"] == 12
    assert values["exits"]["max_hold_days"] is None, "a rule that is OFF must resolve to None, not vanish"


def test_a_promoted_override_survives_upstreams_own_group_default():
    """Upstream now emits a group `default` built from the dataclass (kumo-strategies 1209cac).

    Preferring theirs is right — a field added upstream then appears in the default without cockpit
    being touched. But theirs cannot know what THIS deployment runs, so the promoted override has to
    win on top. If it did not, `stall_days` would silently revert to the dataclass's None and the
    strategy would run with the stall rule off.
    """
    values = store.resolve("qc345")
    assert values["exits"]["stall_days"] == 12, "upstream's dataclass default overwrote the promoted one"
    assert values["exits"]["off_peak_pct"] is None, "a rule with no override should stay at its default"


def test_setting_descriptions_are_short_and_impersonal():
    """Operator, 2026-08-16: "shortent the setting text. They also sometimes are specifically for me.
    others will run this app too."

    Two rules, both checkable:

      LENGTH — a settings form is read on a phone. Several of these ran past 400 characters and one
      hit 669, which is an essay where a caption belongs. The reasoning still exists; it lives in the
      code and the commit history, which is where a reader looking for WHY goes.

      NO SECOND PERSON — "your own trades", "a strategy you are growing" reads as though the app has
      one operator. It does not, and text written for its author is text a second operator has to
      translate.

    Applied to the schemas cockpit OWNS. Generated strategy domains carry upstream's field docstrings
    and are excluded — rewriting those belongs in kumo-strategies.
    """
    import json
    import re
    from pathlib import Path

    limit = 260
    problems: list[str] = []
    for path in sorted((Path(__file__).parent.parent.parent / "config" / "settings").glob("*.schema.json")):
        schema = json.loads(path.read_text())

        def walk(node, where: str) -> None:
            if not isinstance(node, dict):
                return
            text = node.get("description")
            if isinstance(text, str):
                if len(text) > limit:
                    problems.append(f"{path.name}:{where} is {len(text)} chars (limit {limit})")
                if re.search(r"\b(you|your|yourself)\b", text, re.IGNORECASE):
                    problems.append(f"{path.name}:{where} addresses the reader directly")
            for name, child in (node.get("properties") or {}).items():
                walk(child, f"{where}.{name}" if where else name)

        walk(schema, "")
    assert problems == [], "\n".join(problems)


# -- #873: a group upstream declares NOT operator-settable is offered READ-ONLY, never dropped ------------

def _upstream_market_view_node():
    from kumo_strategies.strategies.momentum_rotation.settings import schema_for
    from kumo_strategies.strategies.qc345_rotation.config import QC345RotationConfig
    return (schema_for(QC345RotationConfig).get("groups") or {}).get("market_view")


def test_FIXTURE_upstream_never_emits_the_market_view_as_EDITABLE():
    """Three upstream generations, none editable: (a) excluded from the node (the deployed pins),
    (b) emitted read-only with `x-operator-settable: False` (cd35d6e). An editable emission would be
    the defect — an operator able to flip a lane's action from a settings page."""
    node = _upstream_market_view_node()
    assert node is None or node.get("x-operator-settable") is False, node and {k: node[k] for k in node if k != "fields"}


def _config_declares_market_view() -> bool:
    """The deployed pins (4d28488, 0fbcfff) predate `QC345RotationConfig.market_view` entirely — there
    is nothing to offer there, and the tests below skip BY NAME rather than pass for the wrong reason."""
    from dataclasses import fields as dc_fields
    from kumo_strategies.strategies.qc345_rotation.config import QC345RotationConfig
    return any(f.name == "market_view" for f in dc_fields(QC345RotationConfig))


def test_a_not_operator_settable_group_is_OFFERED_read_only_even_when_upstream_cannot_walk_it():
    """Three states for the group: absent (the defect), unwalkable-but-offered (today: upstream's
    walker has no JSON mapping for its enums — named on the group), walked (once upstream maps Enum)."""
    if not _config_declares_market_view():
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view (the deployed pins) — nothing to offer")
    from api.settings.generated import X_OPERATOR_SETTABLE, X_UNWALKABLE
    group = load_strategy_schema("qc345")["properties"]["market_view"]
    assert group[X_OPERATOR_SETTABLE] is False and "Declared by the strategy in code" in group["description"]
    assert "default" in group, "a group without a default never reaches resolve()"
    if _upstream_can_walk_market_view():
        assert X_UNWALKABLE not in group and set(group["properties"]) >= {"signal", "action"}, "walked: no stub"
    else:
        assert "no JSON mapping" in group[X_UNWALKABLE], "unwalkable: the stub names the reason"


def _upstream_can_walk_market_view() -> bool:
    """CAPABILITY, measured: kumo-strategies cd35d6e added the Enum mapping; the deployed pins predate it.
    The tests below assert the branch that applies and NAME the other, so neither pin reads green
    for the wrong reason."""
    from kumo_strategies.strategies.momentum_rotation.settings import schema_for
    from kumo_strategies.strategies.market_view import MarketViewConfig
    try:
        schema_for(MarketViewConfig)
        return True
    except TypeError:
        return False


def test_a_not_operator_settable_group_is_walked_with_every_field_from_the_dataclass():
    if not _upstream_can_walk_market_view():
        pytest.skip("installed kumo-strategies predates cd35d6e (no Enum mapping): the group is offered as a "
                    "NAMED x-unwalkable stub — see test_a_not_operator_settable_group_is_OFFERED_read_only_even_when_upstream_cannot_walk_it")
    from dataclasses import fields as dc_fields
    from kumo_strategies.strategies.market_view import MarketViewConfig, MarketAction, MarketSignal
    from api.settings.generated import X_OPERATOR_SETTABLE
    group = load_strategy_schema("qc345")["properties"]["market_view"]
    assert group[X_OPERATOR_SETTABLE] is False
    assert group["description"].startswith("Declared by the strategy in code")
    assert set(group["properties"]) == {f.name for f in dc_fields(MarketViewConfig)}, "enumerated, not hand-listed"
    # Enumerated from the upstream enums too: a member added upstream appears here without cockpit changes.
    assert set(group["properties"]["signal"]["enum"]) >= {m.value for m in MarketSignal}
    assert set(group["properties"]["action"]["enum"]) >= {m.value for m in MarketAction}
    assert "default" in group, "a group without a default never reaches resolve()"


def test_upstreams_editable_node_wins_when_it_stops_excluding(monkeypatch):
    if not _upstream_can_walk_market_view():
        pytest.skip("installed kumo-strategies cannot walk MarketViewConfig: un-excluding it raises upstream")
    if _upstream_market_view_node() is not None:
        pytest.skip("installed kumo-strategies emits market_view itself (read-only, cd35d6e) — there is no "
                    "exclusion to lift; cockpit carries upstream's flag, pinned by the read-only test")
    """Prefer upstream's node: if `_not_operator_settable` no longer names the class, the group
    arrives editable from upstream and cockpit must not stamp it read-only."""
    from kumo_strategies.strategies.momentum_rotation import settings as upstream
    from api.settings.generated import X_OPERATOR_SETTABLE
    monkeypatch.setattr(upstream, "_not_operator_settable", lambda: frozenset())
    group = load_strategy_schema("qc345")["properties"]["market_view"]
    assert X_OPERATOR_SETTABLE not in group and "Declared by the strategy in code" not in group["description"]


def test_a_read_only_group_still_resolves_so_the_operator_sees_the_declared_defaults():
    if not _config_declares_market_view():
        pytest.skip("installed kumo-strategies QC345RotationConfig declares no market_view (the deployed pins) — nothing to resolve")
    from api.settings.store import resolve
    from api.settings.generated import X_UNWALKABLE
    values = resolve("qc345")
    assert "market_view" in values and isinstance(values["market_view"], dict)
    if X_UNWALKABLE not in load_strategy_schema("qc345")["properties"]["market_view"]:
        assert set(values["market_view"]) >= {"signal", "action"}
