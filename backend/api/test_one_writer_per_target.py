"""`strategy_sleeve.target` has zero readers and one writer that nothing calls (#463).

MEASURED on the live paper database, 2026-08-23:

    strategy_id     target        actual
    BCTROT-004      0.0000        10000.0000
    MANUAL-001      0.0000        10000.0000
    MOMENTUM-002    0.0000        20000.0000
    QC345-003       0.0000        10000.0000

Every target is ZERO, because nothing has ever written one — `set_target` has no production caller.
Meanwhile the settings domain says 20,000 for all of them, and that is the number the book serves and
sizing uses:

    GET /strategies  ->  QC345-003 target=20000.0 actual=10000.0 headroom=10000.0

So the column is genuinely vestigial and `load_book` is right not to read it. What is left is a public,
exported, input-VALIDATING function that writes a column nobody reads. Calling it sets a target that
has no effect and raises no error — and it validates carefully, which makes it look even more like it
worked. #463 calls it "a loaded gun for whoever calls it next" and that is exactly right.

ONE WRITER EACH is the design and it is a good one: settings owns target (config, typed by a person,
schema-validated, renders in the existing UI for free), the sleeve table owns actual (runtime state,
produced by fills). A second writer for target is not a feature that is missing — it is the two-
derivations-of-one-fact defect with a docstring.

So it is DELETED rather than made to work. Making it write settings would give target two writers and
recreate the disagreement this split exists to prevent.

THE TRIPWIRE THAT GUARDED THIS WAS ITSELF INERT. `test_setting_a_target_moves_no_capital` carried an
`xfail(strict=True)` saying "whoever fixes it must delete this marker and read why" — the same device
that caught QC345's sizing defect four hours ago and worked perfectly there. This one is marked
`needs_services`, and those tests cannot run in this environment: they error on collection and are
deselected, so the strict marker could never fire. A tripwire in a test that never runs is a note.
"""

from __future__ import annotations

import inspect


def test_the_sleeve_target_column_has_NO_writer() -> None:
    """The invariant, aimed at the CLASS rather than at `set_target`.

    The question is not "is that one function gone" but "can anything write this column again". A
    replacement under a different name would satisfy a name-based test and reintroduce the defect.
    """
    from api import budget_store

    src = inspect.getsource(budget_store)
    writes = [ln.strip() for ln in src.splitlines()
              if ".target" in ln and "=" in ln.split(".target")[1][:3]]
    assert writes == [], f"something writes the vestigial target column again: {writes}"


def test_set_target_is_gone_from_the_public_surface() -> None:
    """It was exported in `__all__`, which is what made it findable and therefore dangerous."""
    from api import budget_store

    assert not hasattr(budget_store, "set_target")
    assert "set_target" not in getattr(budget_store, "__all__", ())


def test_targets_still_come_from_SETTINGS_and_still_reach_the_book() -> None:
    """Deleting the writer must not disturb the reader. `_targets()` is the single source and
    `load_book` must still consult it — otherwise every sleeve reads target 0 and every lane looks
    fully deployed, which would refuse every entry on Monday."""
    from api import budget_store

    assert "_targets()" in inspect.getsource(budget_store.load_book), (
        "load_book no longer reads the settings targets — every sleeve would report target 0"
    )
    doc = inspect.getdoc(budget_store._targets) or ""
    assert "ONE WRITER EACH" in doc, "the single-writer rule is no longer stated where it is enforced"
