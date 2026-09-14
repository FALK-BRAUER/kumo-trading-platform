"""`make up` RE-SEEDS settings, so a runtime settings change does not survive a deploy (#759 family).

MEASURED 2026-09-01. Arming lanes for a test slot failed three times, each looking like success:

  1. PUT the slots via the API, then `make up` -> the engine booted reading `_SLOTS: []`. The deploy
     re-seeded settings from the instance directory and discarded the write.
  2. PUT then `docker restart` immediately -> the engine read the OLD value; the write had not
     reached the settings volume the engine mounts.
  3. Same again, with the API reporting the NEW value while the engine read the old one — two views
     of the same domain disagreeing.

This is not a bug in `seed`; seeding from the instance repo is the point, and CLAUDE.md is explicit
that `kumo-cockpit-instances` is the authority on what an instance IS. The defect is that the
overwrite is INVISIBLE: nothing warns that a runtime change is about to be discarded, and nothing in
the engine says which schedule it ended up with (fixed separately in `resolve_slots`).

So this pins the PROPERTY an operator has to know, in the place they will look for it.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2].parent / "kumo-cockpit-instances"


def _makefile() -> str:
    mk = ROOT / "Makefile"
    if not mk.exists():
        pytest.skip("kumo-cockpit-instances is not checked out beside this repo")
    return mk.read_text()


def test_the_fixture_can_find_the_instances_makefile():
    """Vacuity guard: without it every assertion below skips or passes on an empty string."""
    assert "seed:" in _makefile()


def test_UP_DEPENDS_ON_SEED_so_a_deploy_reseeds_settings():
    """The property that surprised me. `up: stage precheck seed` — so any runtime settings change made
    through the API before a deploy is overwritten by the instance's own files."""
    mk = _makefile()
    up = [l for l in mk.splitlines() if l.startswith("up:")]
    assert up, "no `up:` target found"
    assert "seed" in up[0], (
        f"`up` no longer depends on `seed` ({up[0]!r}). If that is deliberate the comment in this "
        f"test is now wrong and a runtime settings change WOULD survive a deploy — which changes the "
        f"operating procedure and should be a decision, not a drift."
    )


def test_SEED_REFUSES_rather_than_seeding_an_instance_with_no_settings():
    """The safe half, worth pinning so it is not lost: an instance without a settings directory is a
    refusal, not an empty seed that silently clears every domain."""
    assert "REFUSING" in _makefile()


def test_the_ORDER_OF_OPERATIONS_is_written_down_where_it_is_needed():
    """The procedure that works, recorded next to the thing that breaks it.

    Deploy FIRST, then write settings, then `docker restart` — and gate the restart on the ENGINE's
    own view of the setting, not the API's, because those two disagreed during the third failure.
    """
    doc = pathlib.Path(__file__).read_text()
    assert "docker restart" in doc and "re-seed" in doc.lower()
