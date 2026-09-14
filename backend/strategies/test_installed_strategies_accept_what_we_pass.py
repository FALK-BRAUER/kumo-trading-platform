"""The INSTALLED kumo-strategies must accept every kwarg cockpit's builders pass it.

WHY THIS EXISTS, AND WHY THE EXISTING TEST COULD NOT CATCH IT.

`test_slot_rereader_wiring.py` asserts, via `inspect.getsource`, that each builder MENTIONS
`read_open_offset` / `read_slots`. Every one of those assertions is true. The kwarg still never
reached the strategy, because `slot_reader._live_reread_kwargs` inspects the installed class's
`__init__` signature and SILENTLY DROPS anything it does not accept — deliberately, so a revision
mismatch degrades to the pre-#514 behaviour instead of taking the whole node down at build (#377).

Fail-open plus a source-only test is invisible by construction. Measured on 2026-08-25: the cockpit
backend venv carried a kumo-strategies where NEITHER `QC27RotationStrategy` nor
`QC345RotationStrategy` accepted `read_open_offset`, so:

    - `_live_reread_kwargs` dropped it on every build,
    - both lanes kept a slot captured at BUILD — the exact state #514 was filed to end,
    - the whole local suite ran against a revision production does not run,
    - and `test_slot_rereader_wiring.py` was green throughout.

`agreement-is-not-connection`, one level out: the builder's source and the fix agreed, while the
wire between the builder and the installed class was cut.

WHAT THIS ASSERTS. Not that a particular revision is pinned — that belongs to
`kumo-cockpit-instances/versions.lock`. This asserts the narrower, local property: the strategies
package THIS venv resolves can accept what THIS repo's builders send it. When it cannot, the suite
is measuring something production does not do, and that must be loud rather than a log line.

RED HERE MEANS: `uv pip install -e ../../kumo-strategies` (or align the venv to the deployed pin)
before trusting any result from this suite.
"""

from __future__ import annotations

import inspect

import pytest

# (label, import path, the kwargs cockpit's builder passes through `_live_reread_kwargs`)
INSTALLED = [
    ("QC27RotationStrategy",
     "kumo_strategies.runtime.nautilus.qc27_rotation", "QC27RotationStrategy",
     ["read_open_offset"]),
    ("QC345RotationStrategy",
     "kumo_strategies.runtime.nautilus.qc345_rotation", "QC345RotationStrategy",
     ["read_open_offset"]),
    # THE MOMENTUM PAIR TAKES `read_slots`, NOT `read_open_offset` — different kwarg, same seam, and
    # this file covered only the two above until the kumo-strategies peer pointed out the gap
    # (2026-08-25). BCTROT is the one that matters most here: `cb5ee79`'s own commit message says
    # forwarding `read_slots` through `**kwargs` left the lane invisible to cockpit (#514). A test
    # that pins two of four lanes is the drift it was written to catch, one lane over.
    ("BCTRotationStrategy",
     "kumo_strategies.runtime.nautilus.bctrot_rotation", "BCTRotationStrategy",
     ["read_slots"]),
    ("MomentumRotationStrategy",
     "kumo_strategies.runtime.nautilus.momentum_rotation", "MomentumRotationStrategy",
     ["read_slots"]),
    # CRSISHORT (#858) takes `read_slots` like the momentum pair. Skipped, loudly elsewhere, when
    # the installed revision has no adapter (test_installed_strategies_carry_crsishort.py).
    ("CrsiShortStrategy",
     "kumo_strategies.runtime.nautilus.crsi_short", "CrsiShortStrategy",
     ["read_slots"]),
]

def _cls(module: str, name: str):
    import importlib

    if name == "CrsiShortStrategy":
        from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

        if not crsishort_installed():
            pytest.skip("CRSISHORT adapter absent from the installed kumo-strategies — the pin "
                        "predates kumo-strategies#121 (loud guard is the strict xfail beside it)")
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize(
    "label,module,name,kwargs", INSTALLED, ids=[r[0] for r in INSTALLED]
)
def test_the_installed_class_ACCEPTS_the_live_slot_rereader(label, module, name, kwargs):
    """The seam `test_slot_rereader_wiring.py` cannot see: does the CLASS take the kwarg.

    A source grep proves the builder offers it. Only the signature proves anything receives it.
    """
    params = inspect.signature(_cls(module, name).__init__).parameters

    # NAMED, NOT SWALLOWED — and this distinction IS the bug, not a nicety.
    #
    # `cb5ee79`'s own commit message: "forwarding read_slots through **kwargs left the lane invisible
    # to cockpit (#514)". A `**kwargs` catch-all accepts the keyword at the call site and can drop it
    # on the floor, and `_live_reread_kwargs` only checks acceptance — it asks the signature, gets a
    # yes, and passes the reader to something that never reads it. An explicitly named parameter is
    # the only form that proves the class knows the kwarg exists.
    #
    # An earlier version of this guard rejected ANY class carrying `**kwargs`, which failed on
    # BCTRotationStrategy — a correct implementation that names `read_slots` AND takes `**kwargs` for
    # unrelated arguments. A test that is wrong about working code gets fixed rather than heeded.
    named = {
        n for n, prm in params.items()
        if prm.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    catch_all = [n for n, prm in params.items() if prm.kind is inspect.Parameter.VAR_KEYWORD]

    swallowed = [k for k in kwargs if k not in named and catch_all]
    assert not swallowed, (
        f"{label} accepts {swallowed} only through **{catch_all[0]}, not as a named parameter. That "
        f"is exactly the #514 defect `cb5ee79` fixed: the call binds, `_live_reread_kwargs` sees "
        f"acceptance and passes the reader, and the class drops it — the lane keeps a decision slot "
        f"captured at BUILD and nothing reports it"
    )

    missing = [k for k in kwargs if k not in named]
    assert not missing, (
        f"the installed {label} does not accept {missing}. `_live_reread_kwargs` drops unknown "
        f"kwargs by design (so a revision mismatch degrades rather than killing the node at build), "
        f"so this fails SILENTLY: the lane keeps a decision slot captured at BUILD — the pre-#514 "
        f"state — while `test_slot_rereader_wiring.py` stays green because it only greps the "
        f"builder's source.\n\n"
        f"This venv is not running what production runs. Align it to the deployed pin "
        f"(`uv pip install -e ../../kumo-strategies`) before trusting any result from this suite."
    )


def test_live_reread_kwargs_DROPS_what_the_class_cannot_take_and_says_so(caplog):
    """The fail-open half, pinned in both directions.

    Dropping is correct — a `TypeError` at build takes every other lane down with it (#377). Dropping
    SILENTLY is not: the warning is the only signal that a lane's settings knob is dead, so a change
    that keeps the drop and loses the log removes the one thing that reports it.
    """
    import logging

    from strategies.slot_reader import _live_reread_kwargs

    class _Narrow:
        def __init__(self, read_open_offset=None):
            pass

    class _Deaf:
        def __init__(self):
            pass

    reader = object()

    assert _live_reread_kwargs(_Narrow, read_open_offset=reader) == {"read_open_offset": reader}, (
        "a class that ACCEPTS the kwarg did not receive it — the re-reader is dead on every lane"
    )

    with caplog.at_level(logging.WARNING):
        assert _live_reread_kwargs(_Deaf, read_open_offset=reader) == {}, (
            "a kwarg the class cannot take was passed anyway — that is a TypeError at build, which "
            "takes down every lane, not just this one (#377)"
        )
    assert any("read_open_offset" in r.getMessage() for r in caplog.records), (
        "the kwarg was dropped with no warning. Fail-open plus silence is invisible by construction "
        "— that combination is why the whole suite ran for a day against a revision where the #514 "
        "fix was inert"
    )


# ---------------------------------------------------------------------------------------------
# ATTRIBUTES, not just kwargs. Same seam, same silence. (Log/static sweep, 2026-08-25.)
# ---------------------------------------------------------------------------------------------

#: Names cockpit reads off a kumo-strategies object with `getattr(..., default)`, so a rename
#: upstream returns the DEFAULT instead of raising.
#:
#: PINNED BY PROVENANCE, NOT EXISTENCE, AND THE FIRST VERSION OF THIS WAS VACUOUS BECAUSE OF IT.
#: `hasattr(QC27RotationStrategy, "is_running")` is True no matter what kumo-strategies does —
#: `is_running` is defined on Nautilus's own `Component`, four classes up the MRO. Renaming it in the
#: lane's module left all 19 tests green. A mutation that kills nothing is a test that measures
#: nothing.
#:
#: So each entry records WHICH CLASS defines the name. That is the fact that can actually change:
#: an attribute silently moving between kumo-strategies' mixin and Nautilus's base means cockpit is
#: reading something it did not think it was reading.
#:
#: (attribute, defining class, what the silent default makes cockpit believe)
GUESSED_ON_STRATEGY = [
    ("is_running", "Component",
     "engine_node.py:381 counts running lanes with `getattr(s, 'is_running', False)`. NAUTILUS owns "
     "this one, so it cannot go missing — but a lane SHADOWING it would change what the count means, "
     "and the #498 shape is a count that looks healthy while the lanes that matter are absent"),
    ("is_armed", "RegistrationMixin",
     "engine_node.py:365 reads the lane's own arming answer. kumo-strategies owns it, so a rename "
     "there makes every lane read as UNARMED — indistinguishable from a calendar that never resolved"),
    ("preflight", "RegistrationMixin",
     "preflight_runner.py:58. Absent means 'unprobeable', which the gate correctly refuses to call "
     "healthy — but a RENAME would report every lane that way and the alarm would be switched off"),
]

GUESSED_ON_RUNNER = [
    ("record_terminal",
     "the venue-outcome path (#383, #512). Cockpit reaches it as "
     "`getattr(self._runner, 'record_terminal', None)`; absent, terminal outcomes are silently never "
     "recorded and a rejected order stays unretryable with nothing saying so"),
]

RUNNERS = [
    ("PgSessionRunner", "kumo_strategies.runtime.executor.pgrunner", "PgSessionRunner"),
    ("QC27SessionRunner", "kumo_strategies.runtime.executor.qc27_runner", "QC27SessionRunner"),
]


@pytest.mark.parametrize("attr,owner,consequence", GUESSED_ON_STRATEGY,
                         ids=[a[0] for a in GUESSED_ON_STRATEGY])
@pytest.mark.parametrize("label,module,name,_kw", INSTALLED, ids=[r[0] for r in INSTALLED])
def test_every_attribute_cockpit_GUESSES_is_defined_WHERE_WE_THINK(
        label, module, name, _kw, attr, owner, consequence):
    """A `getattr(x, "name", default)` across the repo boundary cannot fail loudly. Pin the names AND
    where they come from — existence alone is unfalsifiable for anything Nautilus defines."""
    cls = _cls(module, name)
    defining = next((k.__name__ for k in cls.__mro__ if attr in k.__dict__), None)

    assert defining is not None, (
        f"{label} has no `{attr}` anywhere in its MRO. Cockpit reads it with a default, so this does "
        f"not raise — {consequence}"
    )
    assert defining == owner, (
        f"{label}.{attr} is now defined on `{defining}`, recorded as `{owner}`. The name still "
        f"resolves, so nothing raises and no test but this one can see it — {consequence}"
    )


@pytest.mark.parametrize("attr,consequence", GUESSED_ON_RUNNER, ids=[a[0] for a in GUESSED_ON_RUNNER])
@pytest.mark.parametrize("label,module,name", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_every_attribute_cockpit_GUESSES_on_a_runner_actually_exists(
        label, module, name, attr, consequence):
    """These ARE existence checks, legitimately: the runners are kumo-strategies' own classes with no
    third-party base that could supply the name by accident."""
    cls = _cls(module, name)
    assert any(attr in k.__dict__ for k in cls.__mro__), (
        f"{label} has no `{attr}`. Cockpit reaches it with a default, so this does not raise — "
        f"{consequence}"
    )

