"""Settings framework (#49) — per-domain config as a JSON Schema (the contract, in the repo) + a JSON
values file (the overrides, on a writable volume). One schema per domain; both the api and the engine read
the same schema so validation can't drift, and the UI renders forms from it.

Two validation modes, per the design:
  - SAVE (api): validate strictly against the schema → raise on any error (the caller maps to HTTP 422),
    never persist garbage.
  - RESOLVE/load (engine + api reads): coerce, never crash — strip unknown keys, apply schema defaults,
    drop values that fail validation, so a hand-edited or schema-stale file degrades to defaults.

Secrets never live here (API keys stay in the keychain/env). Values are written atomically (temp+rename)
by the api ONLY; the engine reads.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators

_log = logging.getLogger("kumo.settings")

# Schemas ship in the repo (git-versioned). Values live on a writable volume (mounted; env override).
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "config" / "settings"
_VALUES_DIR = Path(os.environ.get("KUMO_SETTINGS_DIR", "/data/settings"))


class SettingsError(ValueError):
    """Invalid settings on save — carries the per-field validation errors (api → HTTP 422)."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("settings validation failed")
        self.errors = errors


def _extend_with_default(validator_class):
    """jsonschema does not apply `default`s — extend the validator to fill missing properties (used on the
    coerce/load path only, so a partial file resolves to full defaults)."""
    base = validator_class.VALIDATORS["properties"]

    def set_defaults(validator, properties, instance, schema):
        if isinstance(instance, dict):
            for prop, subschema in properties.items():
                if isinstance(subschema, dict) and "default" in subschema and prop not in instance:
                    instance[prop] = deepcopy(subschema["default"])
        yield from base(validator, properties, instance, schema)

    return validators.extend(validator_class, {"properties": set_defaults})


_DefaultingValidator = _extend_with_default(Draft202012Validator)


def _strip_additional(instance: Any, schema: dict) -> Any:
    """Drop keys not declared in the schema (a hand-edited/stale file must not smuggle unknown config)."""
    if not isinstance(instance, dict) or schema.get("type") != "object":
        return instance
    props = schema.get("properties", {})
    return {k: _strip_additional(v, props[k]) for k, v in instance.items() if k in props}


def _unknown_keys(instance: Any, schema: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every key in `instance` the schema does not declare, RECURSIVELY, as dotted paths.

    Recursive because `_strip_additional` is (#872). A typo one level down — `atrMultiplier` inside a
    lane's protection object — was dropped by the strip before the validator ever saw it, so a strict
    PUT answered 200 having configured nothing. `additionalProperties: false` on a nested object is
    only enforcement if something checks the nested level; top-level-only made the declaration
    decorative exactly where a per-lane policy lives.

    Every other domain is flat, so recursion changes nothing for them.
    """
    if not isinstance(instance, dict) or schema.get("type") != "object":
        return []
    props = schema.get("properties", {})
    out: list[tuple[str, ...]] = []
    for key, value in instance.items():
        if key not in props:
            out.append(prefix + (key,))
            continue
        out.extend(_unknown_keys(value, props[key], prefix + (key,)))
    return sorted(out)


def domains() -> list[str]:
    """Registered settings domains: the schema files present, PLUS the generated strategy domains.

    A strategy's parameters are generated from its kumo-trading-strategies config dataclass rather than kept
    as a file here — that repo's own #32 makes the dataclasses the single source of truth, and a
    hand-kept copy in this one is the drift it exists to prevent.
    """
    from api.settings.generated import strategy_domains

    files = (p.stem.removesuffix(".schema") for p in _SCHEMA_DIR.glob("*.schema.json"))
    return sorted(set(files) | set(strategy_domains()))


def load_schema(domain: str) -> dict:
    """A domain's JSON Schema. A FILE wins over a generated domain of the same name.

    That precedence is deliberate: it leaves an escape hatch if a generated schema is ever wrong in a
    way that blocks an operator, without needing a release of the other repository. It is an escape
    hatch, not a workflow — a file shadowing a generated domain reintroduces exactly the drift the
    generation removes, so anything using it should be short-lived.
    """
    path = _SCHEMA_DIR / f"{domain}.schema.json"
    if path.exists():
        return json.loads(path.read_text())

    from api.settings.generated import load_strategy_schema

    return load_strategy_schema(domain)          # raises KeyError for an unknown domain


def _values_path(domain: str) -> Path:
    return _VALUES_DIR / f"{domain}.json"


def _fill_defaults(schema: dict, instance: dict) -> None:
    """Apply schema defaults to `instance` in place WITHOUT raising — iter_errors triggers the defaulting
    validator's side effect (fills missing defaulted props) and we discard any residual errors (a required
    field with no default simply stays absent; the caller degrades rather than crashes)."""
    for _ in _DefaultingValidator(schema).iter_errors(instance):
        pass


def _coerced_file(domain: str, schema: dict) -> dict:
    """The values file for a domain, COERCED and never raising, WITHOUT filling defaults: unknown keys
    stripped; each top-level property that fails ITS subschema (flat OR nested) dropped. Returns only the
    values the user actually set (that are valid) — the basis for both `resolve` and `get_override`."""
    path = _values_path(domain)
    raw: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                # ANNOUNCE THE DROP. An INVALID key already warns below; an unknown one used to vanish
                # in silence, which is the more likely mistake — alpaca-paper's notifications file has
                # carried quiet_start_hour/quiet_end_hour with no such keys in the schema, so quiet
                # hours were configured, did not exist, and nothing said so.
                unknown = sorted(set(loaded) - set(schema.get("properties", {})))
                if unknown:
                    _log.warning(
                        "settings %s: ignoring unknown key(s) %s — not in this domain's schema, so "
                        "whatever they were meant to configure is NOT in effect",
                        domain, ", ".join(repr(k) for k in unknown))
                raw = _strip_additional(loaded, schema)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("settings %s unreadable, using defaults: %s", domain, exc)
    props = schema.get("properties", {})
    instance: dict = {}
    for key, value in raw.items():
        subschema = props.get(key)
        errors = list(Draft202012Validator(subschema).iter_errors(value)) if subschema else [True]
        if errors:
            _log.warning("settings %s: dropping invalid %r", domain, key)
        else:
            instance[key] = value
    return instance


def declared(domain: str) -> dict:
    """What the operator actually WROTE for a domain — the coerced file, WITHOUT schema defaults.

    `resolve` fills every defaulted property, so a resolved dict cannot say whether a value came from
    the file or from the schema. Where that distinction is load-bearing — a lane's protection stance,
    where an absent key means "the lane's own declaration" and a written one means "the operator's
    override" (#1029, #965) — read this beside `resolve`. Never raises; a missing file is `{}`."""
    return _coerced_file(domain, load_schema(domain))


def resolve(domain: str) -> dict:
    """The effective settings for a domain: the coerced file merged with schema defaults. Never raises — a
    hand-edited/stale/corrupt/nested-invalid file degrades to defaults + a warning."""
    schema = load_schema(domain)
    instance = _coerced_file(domain, schema)
    _fill_defaults(schema, instance)
    return instance


def get_override(domain: str, key: str) -> Any | None:
    """The user's EXPLICITLY-set value for one key (no schema default), or None — for layering settings over
    another config source (e.g. feed.toml) without the default clobbering it. Swallows all errors (missing
    schema, unreadable file) → None, so a consumer safely falls back to its own default."""
    try:
        return _coerced_file(domain, load_schema(domain)).get(key)
    except Exception as exc:  # noqa: BLE001 — best-effort override read; never break the consumer
        _log.warning("settings %s.%s override unavailable: %s", domain, key, exc)
        return None


def _to_bps(pct: float) -> int:
    """Percent -> basis points, rounding HALF AWAY FROM ZERO to match JavaScript's `Math.round`.

    Python's built-in `round` is banker's rounding (2.5 -> 2), JS's is not (2.5 -> 3). The UI converts
    with `Math.round` before sending arm params, so a validator using Python's rule would accept a pair
    the UI then collapses onto the same bps value — e.g. 2.506% / 2.505% passes here and arrives as
    251/251, which the engine rejects. Same rule on both sides, or the check is decorative.
    (codex review, High.)
    """
    return math.floor(pct * 100 + 0.5)


def _validate_peak(values: dict) -> list[dict]:
    """PEAK's cross-field rules, mirroring `_validate_peak_ranges` in the engine.

    Kept in step with it deliberately: the engine's copy is the real gate (the attach command takes
    arbitrary params, not just what this form can produce), and this one exists so the operator is told
    at SAVE time rather than discovering it when a position refuses to arm.
    """
    errors: list[dict] = []

    wide, tight = values.get("trailWidePct"), values.get("trailTightPct")
    if isinstance(wide, (int, float)) and isinstance(tight, (int, float)):
        # Compared in BASIS POINTS, because that is the unit the engine checks in and rounding can make
        # two distinct percents land on the same bps value.
        if _to_bps(tight) >= _to_bps(wide):
            errors.append({
                "loc": ["trailTightPct"],
                "msg": f"must be tighter than the wide trail ({wide}%) — otherwise a blow-off would "
                       "WIDEN the stop instead of tightening it",
            })
    return errors


def _validate_protection(values: dict) -> list[dict]:
    """The floor must sit below the ceiling (codex review, Medium).

    JSON Schema cannot compare two properties, and an inverted band is not merely odd — the clamp is an
    `if/elif`, so with `minTrailPct` above `maxTrailPct` the branch taken depends on the raw ATR and the
    resulting width can land outside the band entirely.
    """
    lo, hi = values.get("minTrailPct"), values.get("maxTrailPct")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo >= hi:
        return [{
            "loc": ["minTrailPct"],
            "msg": f"must be below the ceiling ({hi}%) — an inverted band makes the clamp order-dependent",
        }]
    return []


_CROSS_FIELD_VALIDATORS = {"peak": _validate_peak, "protection": _validate_protection}


def save(domain: str, incoming: dict, *, strict: bool = False) -> dict:
    """Validate `incoming` strictly against the schema and atomically write it. Raises SettingsError (→ 422)
    on any validation error — nothing invalid is ever persisted. Returns the resolved (defaulted) values.

    `strict` REFUSES top-level keys the schema does not declare, instead of dropping them. It is for
    callers asserting intent right now — the HTTP PUT — and not for loading a file, where dropping is
    what keeps startup alive when a key left the schema last month.

    Without it, `PUT /settings/alpaca {"values": {...}}` — the GET response shape, and an easy mistake
    — answered 200 OK and wrote nothing (2026-08-24). Because a PUT REPLACES the whole domain, the
    dangerous version is a payload that is partly right: 200, and every key the caller did not send
    silently reverts to its default.
    """
    schema = load_schema(domain)
    raw = incoming if isinstance(incoming, dict) else {}
    if strict:
        unknown = _unknown_keys(raw, schema)
        if unknown:
            # NAMED, not counted. "unexpected keys" sends the caller back to diff two documents by eye,
            # which is how the wrong-shape payload survived a 200 in the first place.
            raise SettingsError([
                {"loc": list(loc), "msg": f"unknown setting {'.'.join(loc)!r} for domain {domain!r} "
                                          "— not in its schema"}
                for loc in unknown
            ])
    instance = _strip_additional(raw, schema)
    errors = [
        {"loc": list(e.path), "msg": e.message}
        for e in sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    ]
    if errors:
        raise SettingsError(errors)
    cross = _CROSS_FIELD_VALIDATORS.get(domain)
    if cross is not None:
        # JSON Schema 2020-12 cannot compare one property against another, so a relationship like "the
        # tight trail must be tighter than the wide one" is unrepresentable in the schema. Without this
        # hook a perfectly schema-valid file can be saved that the ENGINE then refuses on every arm, and
        # the operator finds out later, at the toggle, with no idea which setting did it. Reject at save.
        # (codex review, High.)
        resolved = dict(instance)
        _fill_defaults(schema, resolved)
        cross_errors = cross(resolved)
        if cross_errors:
            raise SettingsError(cross_errors)
    _VALUES_DIR.mkdir(parents=True, exist_ok=True)
    path = _values_path(domain)
    # Unique temp per writer so two concurrent PUTs can't interleave into one temp before the atomic rename.
    tmp = path.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w") as f:
            json.dump(instance, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)  # atomic on POSIX — a reader sees the old or new file, never a partial
    finally:
        tmp.unlink(missing_ok=True)
    # Return the resolved (defaulted) values in-memory — no disk round-trip (the file was just validated).
    result = dict(instance)
    _fill_defaults(schema, result)
    return result
