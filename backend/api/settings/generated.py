"""Strategy settings domains, GENERATED from the kumo-strategies config dataclasses (#318/#320).

Every other domain here is a `*.schema.json` file someone wrote. A strategy's parameters must not be,
and upstream's own reasoning is the argument (kumo-strategies#32): `MomentumRotationConfig` gained
nine fields and lost two in a single day. A hand-kept list in another repository does not survive that
rate, and its failure mode is silent — cockpit offers a parameter that no longer exists, or omits one
that does, and the operator's model of what is configured stops matching what is running.

So the dataclasses are the single source of truth, upstream walks them (`schema_for`), and this
converts the result into the JSON Schema cockpit's settings machinery already speaks. A new parameter
appears here because it was added there, or it does not appear at all. There is no third state.

WHAT THE CONVERSION MUST PRESERVE
---------------------------------
`live_supported`, per exit field. A settings screen that lets an operator set a rule the live runner
cannot honour is worse than one that omits it: the rule typechecks, backtests, deploys, and is then
silently ignored by the thing holding real positions. Nothing fails; the exit simply never fires. The
flag is carried through as an annotation so the UI can grey those out, rather than being re-derived
here — re-deriving it is how the two answers drift apart.

`enum`, for the `Literal` fields. These are the ideal case: the valid set is already declared, so the
UI renders a select and an invalid value cannot be typed at all. `momentum_price_field` as free text
is exactly how a value typechecks, deploys, and silently changes what the strategy ranks on.

PROMOTED DEFAULTS ARE A DEPLOYMENT CHOICE
-----------------------------------------
The dataclass defaults are the STRATEGY's defaults; the promoted candidate is what we choose to run.
They are deliberately different objects, and the override lives here rather than upstream because
upstream should not have to know what one deployment decided. `PROMOTED` is asserted to actually
differ from the dataclass — an override that matches is decoration implying a decision nobody made.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

#: domain -> (import path of the config dataclass, promoted default overrides).
#:
#: Overrides are DOTTED for nested groups (`exits.stall_days`), matching how the brief expresses them
#: and how an operator reads them.
_STRATEGY_DOMAINS: dict[str, tuple[str, str, dict[str, Any]]] = {
    # MOMENTUM IS DELIBERATELY ABSENT, and this comment is the reason.
    #
    # It was added here, and that was a mistake caught by the kumo-strategies peer before it reached
    # an operator: MOMENTUM-002's live config is HARDCODED in `strategies/momentum.py::live_config()`
    # — `n_hold=8, buffer=5`, `give_back_frac=0.5` — and nothing reads settings. So the generated
    # domain would have shown an operator `give_back_frac: null` and `n_hold: 5`, the DATACLASS
    # defaults, while the strategy holding real positions ran 0.5 and 8.
    #
    # A form that cannot change anything and displays the wrong values is worse than no form: it reads
    # as configuration. And the peer's sharper point — saving it would turn every unset rule into a
    # written-down value, so the moment anything DOES consume this domain, an operator who opened the
    # screen once has silently rewritten a live strategy's config.
    #
    # MOMENTUM gets a domain when `live_config()` READS it. Until then, absent is the honest state.
    # Tracked on kumo-cockpit#323.
    "qc345": (
        "kumo_strategies.strategies.qc345_rotation.config",
        "QC345RotationConfig",
        {
            # The live-feasible candidate promoted by kumo-strategies PR #36. Measured over the
            # available 2025/2026 window: 115.478% return / Sharpe 1.395 / -26.392% max DD, against a
            # raw-close baseline of 97.166% / 1.137 / -36.199%.
            #
            # `close` + a 252-session corporate-action window is the LIVE-FEASIBLE pair: adjusted
            # prices do not exist live (adjustment is a retroactive rewrite of history, and a live bar
            # is a raw print), and the engine refuses raw close unless the split window covers the
            # whole momentum lookback. See #319.
            "momentum_price_field": "close",
            "corporate_action_window": 252,
            "exits.stall_days": 12,
        },
    ),
}


def strategy_domains() -> list[str]:
    return sorted(_STRATEGY_DOMAINS)


def _to_json_schema(node: dict, *, path: str, overrides: dict[str, Any]) -> dict:
    """One upstream group -> a JSON Schema object. Recurses into nested groups.

    `additionalProperties: false` mirrors the file-backed domains, whose loader strips unknown keys
    for the same reason: a stale or hand-edited values file must not smuggle configuration that no
    longer exists.
    """
    properties: dict[str, Any] = {}

    for name, spec in (node.get("fields") or {}).items():
        dotted = f"{path}{name}"
        entry: dict[str, Any] = {
            "title": name,
            "description": spec.get("description") or "",
        }
        # A nullable field must accept null as well as its type, or clearing an optional rule in the UI
        # fails validation — and every exit rule defaults to None precisely to mean "off".
        declared = spec.get("type", "string")
        entry["type"] = [declared, "null"] if spec.get("nullable") else declared
        if "enum" in spec:
            enum = list(spec["enum"])
            if spec.get("nullable"):
                enum.append(None)
            entry["enum"] = enum
        entry["default"] = overrides.get(dotted, spec.get("default"))
        if "live_supported" in spec:
            # Carried, not re-derived. A UI with no flag offers rules live cannot honour.
            entry["x-live-supported"] = spec["live_supported"]
            if not spec["live_supported"]:
                entry["description"] = (
                    (entry["description"] + " ").lstrip()
                    + "NOT SUPPORTED BY THE LIVE RUNNER — setting it changes nothing in production."
                ).strip()
        properties[name] = entry

    for group_name, group in (node.get("groups") or {}).items():
        properties[group_name] = _to_json_schema(
            group, path=f"{path}{group_name}.", overrides=overrides
        )
        # Upstream (kumo-strategies cd35d6e) emits not-operator-settable groups itself, read-only,
        # with its reason. Carried, not re-derived — and the reason lands in the description so a
        # reader of the schema alone sees why the field cannot be edited.
        if group.get(X_OPERATOR_SETTABLE) is False:
            properties[group_name][X_OPERATOR_SETTABLE] = False
            reason = str(group.get("x-not-settable-reason") or "").strip()
            properties[group_name]["description"] = (
                (reason or "Declared by the strategy in code — shown, not editable.") + " "
                + (properties[group_name].get("description") or "")).strip()

    out = {
        "type": "object",
        "additionalProperties": False,
        "title": node.get("title") or path.rstrip("."),
        "description": node.get("description") or "",
        "properties": properties,
    }
    if path:
        # A nested group needs a DEFAULT OF ITS OWN, or its fields never reach `resolve()`: the
        # defaulting validator fills properties at each level it VISITS, and only visits an object
        # already present in the instance. With `exits` absent from a fresh values file it never
        # descended, so the group vanished from the resolved values — `stall_days = 12` was in the
        # schema, would have rendered, and reached no consumer.
        #
        # Upstream now emits a group `default` itself (kumo-strategies 1209cac) after this was
        # reported, so PREFER theirs and fall back to composing one. Preferring it matters: theirs is
        # built from the dataclass, so a field added upstream appears in the default without cockpit
        # being touched, whereas the fallback only knows what this converter happened to walk.
        #
        # PROMOTED OVERRIDES STILL APPLY ON TOP. Upstream's default is the DATACLASS default and
        # cannot know that this deployment runs `stall_days = 12`.
        upstream_default = node.get("default")
        composed = {name: spec.get("default") for name, spec in properties.items()}
        out["default"] = {**(upstream_default or {}), **composed}
    return out


def load_strategy_schema(domain: str) -> dict:
    """JSON Schema for a strategy domain, generated from its config dataclass.

    Raises KeyError for an unknown domain, so callers can treat it exactly like a missing file.
    """
    try:
        module_path, class_name, overrides = _STRATEGY_DOMAINS[domain]
    except KeyError:
        raise KeyError(domain) from None

    import importlib

    from kumo_strategies.strategies.momentum_rotation.settings import schema_for

    config_cls = getattr(importlib.import_module(module_path), class_name)
    described = schema_for(config_cls)
    schema = _to_json_schema(described, path="", overrides=overrides)
    _offer_read_only_groups(schema, config_cls, schema_for=schema_for)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


#: Marks a group the strategy declares IN CODE: shown so an operator can see what runs, never edited.
X_OPERATOR_SETTABLE = "x-operator-settable"
#: Present when upstream's walker could not describe the group; carries the error text.
X_UNWALKABLE = "x-unwalkable"


def _offer_read_only_groups(schema: dict, config_cls: type, *, schema_for) -> None:
    """A nested config upstream declares NOT operator-settable is still OFFERED — read-only (#873).

    On the strategies versions cockpit deploys today (4d28488, 0fbcfff) upstream's `schema_for` SKIPPED
    `MarketViewConfig` (`_not_operator_settable`): the market view is the strategy's own declaration,
    measured per lane, and an operator must not be able to flip a lane from EXIT_ONLY to LIQUIDATE
    from a settings page. Right — and still a field the dataclass declares that cockpit did not
    offer, which is the pydantic-DTO-drops-published-fields shape (#233/#322/#336): a lane could run a
    view the operator cannot SEE. So every such group is walked by upstream's OWN walker on the class
    (one derivation, not a hand-list), placed in the schema with `x-operator-settable: false`, and its
    description says the value comes from code. `resolve()` fills its defaults like any group; nothing
    reads them — the running lane's view comes from its config in code, which is what the flag says.

    Since kumo-strategies cd35d6e upstream EMITS the group itself, read-only, with its own reason —
    then it is already in `schema["properties"]` (carried by `_to_json_schema`) and this function
    offers nothing for it. On that version this is a no-op in the safe direction; on the deployed
    pins it is the only reason the field is offered at all. `test_generated.py` branches on the
    measured capability and skips BY NAME on the branch that does not apply.
    """
    import typing
    from dataclasses import fields, is_dataclass
    try:
        hints = typing.get_type_hints(config_cls)
    except Exception as exc:  # noqa: BLE001 — a hint that cannot resolve must not take /settings down
        _log.warning("settings %s: type hints unresolvable (%r) — read-only groups not offered", config_cls.__name__, exc)
        return
    for f in fields(config_cls):
        sub = hints[f.name]
        if not is_dataclass(sub) or f.name in schema["properties"]:
            continue
        try:
            group = _to_json_schema(schema_for(sub), path=f"{f.name}.", overrides={})
        except TypeError as exc:
            # Upstream's walker cannot describe this class today (MarketViewConfig: "no JSON mapping
            # for <enum 'MarketSignal'>"). NOT a reason to drop the field, and NOT a reason to take
            # the editable schema down with it: the group is offered as a NAMED unwalkable stub — the
            # operator sees that the lane declares one and that cockpit cannot render it yet. The
            # mapping landed upstream in cd35d6e; older pins still take this path, and
            # `test_generated.py` asserts whichever branch the installed version makes reachable.
            group = {"type": "object", "additionalProperties": False, "title": sub.__name__,
                     "properties": {}, "default": {}, X_UNWALKABLE: f"{type(exc).__name__}: {exc}"}
        group[X_OPERATOR_SETTABLE] = False
        group["description"] = (
            "Declared by the strategy in code — shown so the running view is visible, not editable. "
            + (group.get("description") or "")).strip()
        schema["properties"][f.name] = group
