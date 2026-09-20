"""Every rotation adapter cockpit builds must be handed a LIVE slot re-reader (#514).

THE DEAD KNOB. `_slot_and_offset_from_settings()` (qc27.py, qc345.py) and
`decision_slots_from_settings()` (momentum.py) were read ONCE at build and captured into the adapter.
`set_time_alert(..., override=True)` then re-armed with the boot-time value, so editing `*_SLOTS` in
settings did nothing until somebody shipped a deploy. Same class as `ALLOCATED_EQUITY = 20_000.0`:
a settings key an operator can edit that silently has no effect.

Measured 2026-08-24: changing the slots and re-seeding changed nothing; only the recreate took effect.

kumo-trading-strategies db25413 added the seam — `read_slots` (momentum/BCTROT) and `read_open_offset`
(qc27/qc345), called at the top of every `_arm()`. Omitting it is byte-for-byte today's behaviour,
which is why this test is over EVERY builder rather than the one that gets wired first: a lane left
unwired is silently still dead, and looks identical from outside.

THE READER MUST BE SYNC AND CHEAP. `_arm` runs on the Nautilus clock callback; blocking I/O there is
the shape that took three lanes down on 2026-08-22 (#377). `settings.resolve` is a file read measured
at 1.22 ms on the engine, and `_cached_settings_read` collapses a burst of arms onto one.
"""
from __future__ import annotations

import inspect

import pytest

from strategies import momentum, qc27, qc345

#: (module, builder, the kwarg the adapter expects). Named explicitly rather than discovered — a scan
#: that silently stopped finding builders would pass over nothing.
BUILDERS = [
    ("qc27.build_qc27_strategy", qc27.build_qc27_strategy, "read_open_offset"),
    ("qc345.build_qc345_strategy", qc345.build_qc345_strategy, "read_open_offset"),
    ("momentum.build_momentum_strategy", momentum.build_momentum_strategy, "read_slots"),
    ("momentum.build_bctrot_strategy", momentum.build_bctrot_strategy, "read_slots"),
]


@pytest.mark.parametrize("name,fn,kwarg", BUILDERS, ids=[b[0] for b in BUILDERS])
def test_every_rotation_builder_passes_a_live_slot_rereader(name, fn, kwarg):
    src = inspect.getsource(fn)
    if f"{kwarg}=" in src:
        return                                     # passed directly as a keyword
    # The momentum family goes through `_build_rotation`, which splats an `identity` dict into the
    # constructor. Assert BOTH halves — the key being present is only a live wiring if the splat is
    # still there, and a test that checked one without the other would pass on a broken path.
    shared = inspect.getsource(momentum._build_rotation)
    assert f'"{kwarg}"' in src, (
        f"{name} does not pass `{kwarg}` — its decision slot is captured at build, so the settings "
        f"knob is dead for that lane until a redeploy")
    assert "_live_reread_kwargs" in shared, (
        f"{name} puts `{kwarg}` in `identity`, but `_build_rotation` no longer routes it into the "
        f"constructor — the key is carried and never delivered")


def test_the_readers_are_SYNC_because_arm_runs_on_the_clock_callback():
    """An async reader would be awaited nowhere and return a coroutine, which the adapter would reject
    as non-numeric and log — a dead knob again, wearing a fix."""
    for reader in (qc27._read_open_offset, qc345._read_open_offset, momentum._read_slots_for):
        assert not inspect.iscoroutinefunction(reader), reader


def test_a_reader_NEVER_raises_out_of_arm(monkeypatch):
    """A lane that stopped scheduling looks exactly like one that decided to hold. The adapter already
    guards, but a reader that raises would still cost the log line and the ambiguity — and cockpit is
    the side that knows the settings store degrades to defaults rather than raising."""
    def boom(_domain):
        raise RuntimeError("settings store gone")

    monkeypatch.setattr("api.settings.resolve", boom)
    assert qc27._read_open_offset() is None
    assert qc345._read_open_offset() is None
    assert momentum._read_slots_for("MOMENTUM-002", ("open+5m",)) is None


def test_the_reader_returns_what_SETTINGS_say_not_the_builtin(monkeypatch):
    """12345 rather than a plausible number: the built-ins are 150 and 5, so a test using either would
    pass whether the reader reads settings or ignores them. That coincidence is what hid the
    allocation constant for the lane's whole life."""
    monkeypatch.setattr("api.settings.resolve",
                        lambda _d: {"TECHIVOL-005_SLOTS": ["open+12345m"]})
    qc27._invalidate_settings_cache()
    assert qc27._read_open_offset() == 12345
