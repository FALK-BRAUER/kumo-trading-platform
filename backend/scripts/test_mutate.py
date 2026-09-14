"""The harness must refuse the mutations that lie (2026-08-22).

Every test here is a failure mode that reported SURVIVED while probing nothing. That is the dangerous
direction: a real guard gets deleted because the sweep said it was untested.
"""

from __future__ import annotations

import pytest

from scripts.mutate import apply_mutation


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def f(x):\n    if x < 0:\n        return 0\n    return x\n")
    return p


def test_a_normal_mutation_applies(src):
    out = apply_mutation(src, "if x < 0:", "if x <= 0:")
    assert "if x <= 0:" in out
    assert src.read_text() != out, "the fixture must not already contain the mutation"


def test_an_AMBIGUOUS_anchor_is_refused(src):
    """THE ONE THAT ACTUALLY BIT kumo-strategies. `replace(a, b, 1)` takes the FIRST occurrence, so a
    substring appearing twice mutates a site other than the one under test — the intended line stays
    untouched and the sweep reports SURVIVED for a guard it never probed."""
    src.write_text("a = 1\nb = 1\n")
    with pytest.raises(ValueError, match="appears 2 times"):
        apply_mutation(src, "= 1", "= 2")


def test_a_UNIQUE_anchor_of_the_same_shape_is_allowed(src):
    """Discriminating half: the rule is ambiguity, not substring-ness. Refusing every short anchor
    would make the harness useless and it would stop being used."""
    src.write_text("a = 1\nb = 2\n")
    assert "a = 9" in apply_mutation(src, "a = 1", "a = 9")


def test_an_IDENTICAL_replacement_is_refused(src):
    with pytest.raises(ValueError, match="IDENTICAL"):
        apply_mutation(src, "if x < 0:", "if x < 0:")


def test_a_SEMANTIC_no_op_is_APPLIED_because_no_string_harness_can_see_it(src):
    """The limit, asserted so nobody mistakes this tool for protection it does not give.

    `"" or X` evaluates to `X` — the second kumo-strategies case. The FILE changes and the BEHAVIOUR
    does not, so the mutation reports SURVIVED while probing nothing. This harness applies it happily
    and cannot do otherwise: the defect is semantic.

    A first draft raised on `mutated == source` and looked like it covered this. It was DEAD CODE —
    with an identical replacement already refused and the anchor present, `str.replace` always changes
    the text — and the test that appeared to cover it was actually passing an identical string, so it
    exercised the OTHER check. Both are gone. When a mutation reports SURVIVED, read the mutation."""
    src.write_text('v = "" or X\n')
    out = apply_mutation(src, '"" or X', 'X')
    assert "v = X" in out, "the harness must not pretend to catch a semantic no-op"


def test_a_MISSING_anchor_is_refused_rather_than_silently_doing_nothing(src):
    """The code moved. Silently applying nothing would report the whole sweep as clean."""
    with pytest.raises(ValueError, match="not found"):
        apply_mutation(src, "if y < 0:", "if y <= 0:")


def test_a_refused_mutation_writes_NOTHING(src, tmp_path):
    """A harness that half-applies leaves the tree in a state the next sweep inherits — and the next
    result is then about two mutations, not one."""
    before = src.read_text()
    with pytest.raises(ValueError):
        apply_mutation(src, "= 1", "= 2")
    assert src.read_text() == before
