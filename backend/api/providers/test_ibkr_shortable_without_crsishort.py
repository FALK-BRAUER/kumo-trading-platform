"""The shortable plane must attach on a kumo-strategies pin that has NO CRSISHORT module.

Measured on the codex merged-result review of main 87b173f (2026-09-10 20:05Z), then read: staging2
pins kumo-strategies 0fbcfff, which predates `kumo_strategies.strategies.crsi_short`. IBKR always
declares the plane, `build_node` always attaches it, `attach_shortable` immediately calls
`plane.borrow_rates()`, and `borrow_rates_provider` imported `LOCATABLE` from the absent module at
BUILD time — a ModuleNotFoundError inside `build_node`, i.e. an engine crash-loop at boot, for a lane
that is not enabled and cannot exist on that pin. The tests that would have caught it skip themselves
on exactly that pin (test_shortable_wiring.py, test_ibkr_shortable.py), so this file does not skip.

THE CONTRACT ON A PIN WITHOUT THE MODULE: the plane attaches, `/health.shortable` says the contract is
absent, and the provider REFUSES BY NAME if anything calls it. Never a silent {sym: None} — that is a
"no locate" answer, and an answer is not what a missing contract has.
"""

from __future__ import annotations

import sys

import pytest

from api.providers.ibkr_shortable import IBShortablePlane, ShortableRegistry


def _plane():
    return IBShortablePlane(ShortableRegistry(publish=lambda *a: None), client=None, now_ns=lambda: 1_000)


class _Spec:
    """The connector's declaration: a factory that yields the real plane (as providers/ibkr.py does)."""

    def __init__(self, plane):
        self.shortable_plane = lambda publish, now_ns: plane


class _Feed:
    """Only what `_attach_shortable_plane` and `attach_shortable` touch — the REAL method bound."""

    def __init__(self):
        from types import SimpleNamespace
        self.clock = SimpleNamespace(timestamp_ns=lambda: 1_000)
        self.shortable_provider = None
        self._shortable_plane = None

    def publish_data(self, *a):
        pass

    def attach_shortable(self, plane):
        from api.engine_node import UiFeedStrategy
        return UiFeedStrategy.attach_shortable(self, plane)


@pytest.fixture
def without_crsishort(monkeypatch):
    """`None` in sys.modules makes `import` raise ImportError — the pin's shape, not a mock."""
    monkeypatch.setitem(sys.modules, "kumo_strategies.strategies.crsi_short", None)
    with pytest.raises(ImportError):
        import kumo_strategies.strategies.crsi_short  # noqa: F401
    return True


def test_fixture_property_the_module_IS_importable_here_so_absence_is_what_the_fixture_makes():
    """On the dev venv the module exists; without this the absence fixture could be vacuous."""
    import kumo_strategies.strategies.crsi_short as m
    assert hasattr(m, "LOCATABLE")


def test_attaching_the_plane_on_a_pin_without_CRSISHORT_does_not_crash_the_boot(without_crsishort):
    from api.engine_node import _attach_shortable_plane
    feed, plane = _Feed(), _plane()
    _attach_shortable_plane(feed, _Spec(plane))          # the build_node seam — raised before the fix
    assert feed._shortable_plane is plane
    assert feed.shortable_provider is not None


def test_on_that_pin_the_provider_REFUSES_by_name_rather_than_answering_no_locate(without_crsishort):
    from api.engine_node import _attach_shortable_plane
    feed = _Feed()
    _attach_shortable_plane(feed, _Spec(_plane()))
    with pytest.raises(RuntimeError, match="crsi_short"):
        feed.shortable_provider(["NVDA"])


def test_on_that_pin_health_says_the_contract_is_ABSENT_not_ok(without_crsishort):
    plane = _plane()
    plane.borrow_rates()
    h = plane.health()
    assert h.get("contract") == "absent", h


def test_with_the_module_present_the_provider_answers_and_health_says_present():
    plane = _plane()
    provider = plane.borrow_rates()
    assert provider(["NVDA"]) == {"NVDA": None}       # registry empty → no locate, an ANSWER
    assert plane.health().get("contract") == "present"
