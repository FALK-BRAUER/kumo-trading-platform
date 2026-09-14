"""A RELATIVE host bind mount silently mounts the wrong directory under staged execution.

WHAT HAPPENED (kumo-cockpit#524). `compose.paper.yml` mounted fintrack's rotation tooling as

    - ../../fintrack/tools:/fintrack/tools:ro

Relative paths in compose resolve from the COMPOSE FILE'S directory. Run from the platform repo that
is `~/projects/fintrack/tools` and correct. But `kumo-cockpit-instances` stages each pinned
ref into `.stage/<instance>/cockpit/`, so the same line resolves to
`.stage/<instance>/fintrack/tools` — which does not exist, so Docker CREATES IT EMPTY and mounts that.

Measured 2026-08-24 on the running engine:

    docker inspect ... /fintrack/tools ->
        /host_mnt~/projects/kumo-cockpit-instances/.stage/test-alpaca/fintrack/tools
    contents: 0 files

The Market tab was therefore permanently empty — `No module named 'rotation_read'` on every refresh,
at WARN, with the "keep last known" fallback preserving a payload that had always been None. The API
reported "no rotation published yet — the engine computes it on a 5-minute timer", which reads as
still-warming-up rather than never-worked.

THIS IS A KNOWN CLASS, ALREADY PAID FOR ONCE. The instances Makefile documents the identical trap for
`additional_contexts: strategies: ../../kumo-strategies`, which failed loudly with
`failed to get build context strategies: stat .../kumo-strategies: no such file`. That one failed
BEFORE the build so nothing was recreated. A bind mount fails SILENTLY — Docker creates the directory
and the container starts happily with nothing in it.

So the test is over EVERY host bind mount, not over the one that broke.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_COMPOSE = sorted((Path(__file__).resolve().parents[2] / "deploy").glob("compose*.yml"))


def _host_binds(text: str) -> list[tuple[int, str]]:
    """`- <host>:<container>[:mode]` lines whose host side is a PATH rather than a named volume."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        spec = line[2:].split("#", 1)[0].strip()
        # A `${VAR:?message}` host side is a bind mount whose path the INSTANCE supplies (#1045: the
        # ledger mount left this file for instance.env). The `:?` form carries colons of its own, so
        # the host side ends at the closing brace, not at the first colon. It is a host path by
        # construction and counts as "seen" — or the fixture assertion below would report that the
        # parser stopped looking the day the literal path left.
        if spec.startswith("${"):
            host = spec[: spec.index("}") + 1] if "}" in spec else spec
        else:
            host = spec.split(":", 1)[0]
        if "/" in host or host.startswith(".") or host.startswith("${"):
            out.append((n, host))
    return out


@pytest.mark.parametrize("path", _COMPOSE, ids=lambda p: p.name)
def test_no_compose_bind_mount_uses_a_RELATIVE_host_path(path: Path):
    """Relative host paths resolve from the compose file's directory, and the instances repo runs
    compose from a STAGED COPY. The failure is silent: Docker creates the missing directory and
    mounts it empty."""
    # `${VAR:?}` is absolute by contract: the instance declares it and compose refuses an empty one.
    offenders = [(n, h) for n, h in _host_binds(path.read_text()) if not (h.startswith("/") or h.startswith("${"))]
    assert not offenders, (
        f"{path.name} has relative host bind mounts. Staged from kumo-cockpit-instances these "
        f"resolve under .stage/<instance>/cockpit/deploy/ and Docker mounts an EMPTY directory "
        f"instead of failing:\n  " + "\n  ".join(f"line {n}: {h}" for n, h in offenders))


def test_the_fixture_can_actually_see_bind_mounts():
    """A test that cannot fail carries no information. If the parser stopped recognising binds, the
    check above would pass on any file at all — so prove it finds the absolute ones that exist."""
    assert _COMPOSE, "no compose files found — the check above is asserting over nothing"
    found = [h for p in _COMPOSE for _, h in _host_binds(p.read_text())]
    assert found, "the parser found no host bind mounts in any compose file — it is not looking"
    assert any(h.startswith("/") or h.startswith("${") for h in found), found
