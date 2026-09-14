"""A lane must SAY which schedule it is running, always — not only when settings override it.

MEASURED COST, 2026-09-01. Arming staging's lanes for a test slot failed THREE TIMES in a row and
each failure looked identical to success from outside:

  1. `make up` re-seeds settings from the instances repo, so an API PUT made before a deploy is
     silently discarded — the engine booted reading `_SLOTS: []`.
  2. A PUT immediately before `docker restart` had not reached the engine's view yet; it read the
     old value.
  3. Same again, with the API reporting the new value while the engine read the old one.

In every case the engine logged NOTHING about slots, because `resolve_slots` only speaks when it
OVERRIDES the built-in or when the value is malformed. An empty setting returns the default in
silence, so "no override is configured" and "the override never reached me" produce identical output.

Three states, never two: OVERRIDDEN, NO OVERRIDE CONFIGURED, UNUSABLE. The middle one was missing and
it is the one an operator needs when a schedule change appears not to have worked.
"""

from __future__ import annotations

import logging

import pytest

from strategies.momentum import resolve_slots

BUILT_IN = ("open+5m", "close-20m")


def test_the_fixture_can_express_the_bug(caplog):
    """Vacuity guard: the resolver must actually log something in the overridden case, or the
    assertions below cannot distinguish silence from a broken caplog."""
    with caplog.at_level(logging.INFO):
        resolve_slots(["open+90m"], BUILT_IN, strategy_id="MOMENTUM-002")
    assert caplog.text.strip(), "the resolver logged nothing at all — caplog is not wired"


def test_an_EMPTY_setting_SAYS_it_is_using_the_built_in(caplog):
    """THE ONE THAT WAS MISSING. This is what an unreached settings write looks like, and it used to
    be indistinguishable from a deliberate no-override."""
    with caplog.at_level(logging.INFO):
        out = resolve_slots([], BUILT_IN, strategy_id="MOMENTUM-002")
    assert out == BUILT_IN
    assert "MOMENTUM-002" in caplog.text
    assert "built-in" in caplog.text.lower(), (
        f"an empty slot setting resolved silently — an operator cannot tell 'no override configured' "
        f"from 'my settings change never arrived': {caplog.text!r}"
    )


def test_an_OVERRIDE_still_says_so(caplog):
    with caplog.at_level(logging.INFO):
        out = resolve_slots(["open+90m"], BUILT_IN, strategy_id="MOMENTUM-002")
    assert out == ("open+90m",)
    assert "OVERRIDDEN" in caplog.text


def test_a_MALFORMED_slot_still_names_what_was_wrong(caplog):
    """Unusable is its own state and must not collapse into either of the other two."""
    with caplog.at_level(logging.INFO):
        out = resolve_slots(["open+90minutes"], BUILT_IN, strategy_id="MOMENTUM-002")
    assert out == BUILT_IN
    assert "not usable" in caplog.text and "open+90minutes" in caplog.text


@pytest.mark.parametrize("raw", [[], None, ()])
def test_EVERY_shape_of_absent_is_reported(raw):
    """`None`, `[]` and `()` all mean 'nothing configured' and all used to be silent."""
    import logging as _l

    records = []
    handler = type("H", (_l.Handler,), {"emit": lambda self, r: records.append(r.getMessage())})()
    log = _l.getLogger("strategies.momentum")
    log.addHandler(handler); log.setLevel(_l.INFO)
    try:
        assert resolve_slots(raw, BUILT_IN, strategy_id="X") == BUILT_IN
        assert any("built-in" in m.lower() for m in records), f"silent for {raw!r}"
    finally:
        log.removeHandler(handler)
