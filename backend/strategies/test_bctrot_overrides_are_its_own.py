"""A `MOMENTUM_*` override must not move BCTROT-004 (#794).

`live_config()` calls `_live_overrides()`, which reads `MOMENTUM_N_HOLD`, `MOMENTUM_BUFFER` and
`MOMENTUM_GIVE_BACK_FRAC` from the `strategies` settings domain. `bctrot_config()` is
`replace(live_config(), ...)`, overriding only `score` and `min_abs_gap_pct`. So all three
MOMENTUM-named keys reached BCTROT unchanged — live, on the next config re-read, with no redeploy and
nothing saying so.

WHY IT IS A DEFECT NOW AND WAS NOT BEFORE. The two lanes deliberately shared one config so their
live divergence measured one variable. As of 2026-09-04 (#787/#114) they differ by schedule, ranking
AND an entry filter, so a variable named for one lane silently steering the other is no longer a
harmless alias — it is a control whose name lies about its blast radius. An operator narrowing
MOMENTUM's book during an incident would narrow BCTROT's too.

AIMED AT THE CLASS, NOT AT THE THREE KEYS. `_live_overrides` grew from two keys to three once
already; a fourth would arrive untested under a per-key test. These assert the PROPERTY — no key
named for MOMENTUM changes anything BCTROT reads — by sweeping the domain rather than by listing.
"""

from __future__ import annotations

import dataclasses

import pytest


def _domain(**over):
    """A `strategies` domain stub carrying exactly the overrides a test names."""
    return dict(over)


def _configs(monkeypatch, domain):
    """`(momentum, bctrot)` built against a settings domain, with nothing else patched."""
    import api.settings as settings
    import strategies.momentum as m

    monkeypatch.setattr(settings, "resolve", lambda name: domain if name == "strategies" else {})
    return m.live_config(), m.bctrot_config()


def _diff(a, b) -> dict:
    """Field-by-field difference between two configs, one level into the dataclass tree."""
    out = {}
    for f in dataclasses.fields(a):
        va, vb = getattr(a, f.name), getattr(b, f.name)
        if va != vb:
            out[f.name] = (va, vb)
    return out


def test_the_fixture_can_express_the_bug__a_MOMENTUM_override_really_does_reach_live_config(monkeypatch):
    """FIXTURE PROPERTY. If the override did not move MOMENTUM either, every assertion below would be
    about a knob that does nothing — the vacuity this repo has paid for repeatedly."""
    base, _ = _configs(monkeypatch, _domain())
    moved, _ = _configs(monkeypatch, _domain(MOMENTUM_N_HOLD=3))
    assert base.portfolio.n_hold != moved.portfolio.n_hold, (
        "MOMENTUM_N_HOLD did not change MOMENTUM's own config — the override path is dead and these "
        "tests would pass over any behaviour at all"
    )
    assert moved.portfolio.n_hold == 3


@pytest.mark.parametrize("key,value", [
    ("MOMENTUM_N_HOLD", 3),
    ("MOMENTUM_BUFFER", 9),
    ("MOMENTUM_GIVE_BACK_FRAC", 0.9),
])
def test_no_MOMENTUM_key_changes_ANY_field_BCTROT_reads(monkeypatch, key, value):
    """THE PROPERTY, per key, over the WHOLE config rather than the field the key is named for.

    Asserting only `n_hold` would miss an override that reached BCTROT through some other field, and
    the point is that the blast radius is zero — not that one field is safe.
    """
    _, clean = _configs(monkeypatch, _domain())
    _, dirty = _configs(monkeypatch, _domain(**{key: value}))
    assert _diff(clean, dirty) == {}, (
        f"{key} moved BCTROT-004: {_diff(clean, dirty)}. It is named for MOMENTUM-002 and the two "
        f"lanes have differed since 2026-09-04, so this is an operator changing a lane they did not "
        f"name."
    )


def test_the_sweep_is_over_EVERY_MOMENTUM_KEY_the_code_reads__not_a_list_kept_by_hand(monkeypatch):
    """THE CLASS GUARD. `_live_overrides` went from two keys to three once; a fourth added without a
    test would be silently shared again.

    Reads the keys out of the FUNCTION'S OWN SOURCE, so a key added there is covered the day it is
    added rather than the day someone remembers this file.
    """
    import ast
    import inspect

    import strategies.momentum as m

    # DERIVED FROM THE SUFFIXES, because the keys are now built as f"{prefix}_N_HOLD" and no literal
    # "MOMENTUM_*" survives in the source. The first version of this guard scanned for that literal
    # and went blind the moment the fix landed — it failed loudly rather than passing, which is the
    # only reason it is not now certifying nothing.
    src = inspect.getsource(m._live_overrides)
    suffixes = {n.value for n in ast.walk(ast.parse(src.strip()))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.startswith("_") and n.value.isupper()}
    assert suffixes, "found no override key suffixes in _live_overrides — this guard has gone blind"
    keys = {f"MOMENTUM{sfx}" for sfx in suffixes}

    _, clean = _configs(monkeypatch, _domain())
    for key in sorted(keys):
        # A value every cast accepts, so the override is APPLIED rather than skipped as malformed —
        # a malformed value is dropped and would make this pass without exercising anything.
        _, dirty = _configs(monkeypatch, _domain(**{key: 2}))
        assert _diff(clean, dirty) == {}, f"{key} reaches BCTROT-004: {_diff(clean, dirty)}"


def test_BCTROT_has_its_OWN_overrides__the_lane_is_steerable_by_its_own_name(monkeypatch):
    """The other half. Removing the shared path must not leave BCTROT unsteerable: an operator has to
    be able to narrow THIS lane during an incident, under a name that says which lane it is."""
    _, base = _configs(monkeypatch, _domain())
    _, moved = _configs(monkeypatch, _domain(BCTROT_N_HOLD=3))
    assert moved.portfolio.n_hold == 3, "BCTROT_N_HOLD does not steer BCTROT-004"
    assert base.portfolio.n_hold != 3, "the fixture's default already equals the override value"

    # And it must not leak the other way either.
    momentum, _ = _configs(monkeypatch, _domain(BCTROT_N_HOLD=3))
    assert momentum.portfolio.n_hold != 3, "BCTROT_N_HOLD moved MOMENTUM-002"
