"""The UI and the API must publish on the SAME interface, or the UI resolves an API nothing serves (#824).

WHY THE INTERFACE IS A PARAMETER AT ALL. A bare `8011:8000` publish binds 0.0.0.0, which INCLUDES the
tailnet address, so `tailscale serve --https=8011` cannot bind the same port. Measured on 2026-09-09:
two consecutive deploys of staging2 died with

    Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:8011
    -> 127.0.0.1:0: listen tcp 0.0.0.0:8011: bind: address already in use

and the only other listener was tailscaled. Binding 127.0.0.1 leaves the tailnet address free for
`tailscale serve`, and the two coexist ON THE SAME PORT NUMBER.

THE SAME PORT NUMBER IS NOT A PREFERENCE. `ui/src/lib/config.ts` derives the API base from the PAGE's
own hostname plus `NEXT_PUBLIC_API_PORT`:

    API_BASE = `${scheme}://${loc().hostname}:${apiPort}`

so publishing the UI and the API on different interfaces gives a page that loads and then cannot
reach any API — the failure this repo already recorded once, when a UI image baked
`NEXT_PUBLIC_API_PORT=8000` and staging-ibkr's UI read kumo-paper's API.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parent.parent.parent / "deploy"

#: `- "<host>:<port>:<container port>"`, host and port both possibly `${VAR:-default}`.
_PUBLISH = re.compile(r'^\s*-\s*"([^"]*?):(\$\{[A-Z0-9_]+:-\d+\}|\d+):(\d+)"', re.M)


def _publishes(compose: Path) -> list[tuple[str, str, str]]:
    return _PUBLISH.findall(compose.read_text())


def test_the_parser_finds_the_publishes_it_is_about():
    """Vacuity guard: an empty list makes every assertion below pass against any compose file."""
    got = _publishes(_DEPLOY / "compose.paper.yml")
    assert len(got) >= 2, f"expected the api and ui publishes, parsed {got}"
    assert any("8000" == c for _, _, c in got), "the api publish (container port 8000) was not parsed"
    assert any("3000" == c for _, _, c in got), "the ui publish (container port 3000) was not parsed"


def test_the_UI_and_the_API_publish_on_THE_SAME_interface():
    """One value, both services. `config.ts` resolves the API from the page's own hostname, so a UI
    reachable somewhere the API is not is a page that loads and then reaches nothing."""
    hosts = {h for h, _, c in _publishes(_DEPLOY / "compose.paper.yml") if c in ("8000", "3000")}
    assert len(hosts) == 1, (
        f"the api and ui publish on different interfaces {hosts} — the UI derives the API base from "
        f"its OWN hostname, so it would ask for an API that is not there"
    )


@pytest.mark.parametrize("compose_name", ["compose.paper.yml", "compose.prod.yml"])
def test_every_publish_host_carries_a_DEFAULT(compose_name):
    """`${KUMO_PUBLISH_HOST}` with no `:-` interpolates to the EMPTY STRING when unset, and
    `":8011:8000"` is a DIFFERENT and much worse binding than `0.0.0.0` — it is how a port silently
    stops being published at all. Same trap as #814's env forwarding, one layer down."""
    src = (_DEPLOY / compose_name).read_text()
    bare = re.findall(r'^\s*-\s*"\$\{([A-Z0-9_]+)\}:', src, re.M)
    assert not bare, (
        f"{compose_name}: {sorted(set(bare))} are interpolated into a port publish with no "
        f"`:-default` — unset becomes the empty string and changes the binding silently"
    )


def test_the_DEFAULT_is_the_PREVIOUS_behaviour():
    """This must be inert on every instance that does not set it. `0.0.0.0` is what a bare
    `8011:8000` bound, so a default of anything else would silently un-expose the paper stack."""
    src = (_DEPLOY / "compose.paper.yml").read_text()
    defaults = set(re.findall(r'\$\{KUMO_PUBLISH_HOST:-([^}]*)\}', src))
    assert defaults == {"0.0.0.0"}, (
        f"KUMO_PUBLISH_HOST defaults to {defaults} rather than 0.0.0.0 — instances that never set it "
        f"would change binding on the next deploy, which is not a change anybody asked for"
    )
