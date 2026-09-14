"""Every core runtime dependency must reach the IMAGE, not just `pyproject.toml`.

`deploy/Dockerfile.backend` hardcodes its own `pip install` list. Nothing derives it from
`pyproject.toml`, so a dependency added to one and not the other produces a container that imports
fine on a developer's machine and crash-loops — or worse, degrades silently — in production.

WHAT THIS CAUGHT, 2026-08-25. `tzdata` was in neither. Python's `zoneinfo` falls back to the PyPI
`tzdata` package when the system IANA database is absent, and the slim base image ships
`/usr/share/zoneinfo` as an EMPTY DIRECTORY — so the path exists and the data does not:

    ZoneInfo('US/Eastern') -> ZoneInfoNotFoundError('No time zone found with key US/Eastern')

IB stamps every execution report in exchange-local time (`Time: 20260824 11:29:28 US/Eastern`), so
`process_exec_details` and `process_commission_report` BOTH raised on every fill. Orders reached IB,
were ACCEPTED with a real venue id, filled — and Nautilus never saw the execution, so it marked them
`ORDER_NOT_FOUND_AT_VENUE` and REJECTED. A flatten actually executed at the broker
(`SLD 27 @ 92.87, RealizedPnL 640.63`) while the cockpit showed nothing.

WHY THE OBVIOUS TEST WOULD BE A LIE. Asserting `ZoneInfo("US/Eastern")` resolves passes on macOS and
in CI, where the OS ships the tz database — it would be green while production stayed broken. That is
a double that cannot represent production, which this repo treats as the bug itself. So the assertion
is over the DECLARATION, which is the thing that actually differs between the two environments.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "backend" / "pyproject.toml"
_DOCKERFILE = _ROOT / "deploy" / "Dockerfile.backend"


def _name(spec: str) -> str:
    """`"sqlalchemy[asyncio]>=2.0"` -> `sqlalchemy`."""
    return re.split(r"[\[<>=!;]", spec.strip().strip('"').strip("'"), 1)[0].strip().lower()


def _core_dependencies() -> set[str]:
    return {_name(d) for d in tomllib.loads(_PYPROJECT.read_text())["project"]["dependencies"]}


def _image_installs() -> set[str]:
    return {_name(tok) for tok in re.findall(r'[\w\[\]".<>=!-]+', _DOCKERFILE.read_text())}


def test_every_core_dependency_is_installed_in_the_image():
    missing = sorted(_core_dependencies() - _image_installs())
    assert not missing, (
        f"declared in backend/pyproject.toml [project.dependencies] but never installed by "
        f"deploy/Dockerfile.backend: {missing}. The container will import these from nowhere.")


def test_tzdata_is_declared_because_the_slim_image_has_no_tz_DATABASE():
    """Named explicitly, not left to the set check above, because the reason is not obvious from a
    dependency list: `/usr/share/zoneinfo` EXISTS in the image and is empty, so every naive check for
    "do we have timezones" says yes."""
    assert "tzdata" in _core_dependencies(), (
        "tzdata is not a core dependency — IB stamps execution reports in exchange-local time and "
        "zoneinfo has no IANA database in the slim image, so every fill handler raises")
    assert "tzdata" in _image_installs(), "tzdata is declared but the Dockerfile never installs it"


def test_the_fixture_can_actually_read_both_files():
    """A test that cannot fail carries no information: if either parse silently yielded an empty set,
    the subtraction above would be empty and pass over nothing."""
    assert len(_core_dependencies()) >= 3, _core_dependencies()
    assert "nautilus_trader" in _image_installs(), "the Dockerfile parse found no known package"
