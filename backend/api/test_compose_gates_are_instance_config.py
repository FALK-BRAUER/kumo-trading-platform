"""Every automation gate in the compose file must come from the INSTANCE, defaulting off.

Ibkr-paper-retired was defined with `KUMO_ORDERS_ARMED=false` and `KUMO_MOMENTUM_ENABLED=false` in its
`instance.env`, and came up with BOTH set to `true` against a live IB login (2026-08-24). Compose's
`environment:` block held them as literals, and a literal WINS over `env_file` — so the instance
definition was read, ignored, and never reported. Nothing traded only because the engine crash-looped
on an unrelated empty pool; a crash was the safety mechanism.

The check is derived from the NAME, not from a list of today's gates. A list would have to be edited
by the same person adding the next gate, which is exactly who is not thinking about this file.
"""
from __future__ import annotations

import re
from pathlib import Path

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.paper.yml"

# `NAME: value`, with a trailing `# comment` stripped. Quotes are compose noise, not meaning.
_ENV_LINE = re.compile(r"^\s{4,}([A-Z][A-Z0-9_]*):\s*(.*?)\s*(?:#.*)?$")


def _env_assignments(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _is_a_gate(name: str) -> bool:
    """A gate switches automation on. Named, not listed, so the NEXT one is covered too."""
    return name.startswith("KUMO_") and name.endswith(("_ARMED", "_ENABLED"))


def test_the_parser_actually_sees_a_hardcoded_literal():
    """The fixture must be able to REPRESENT the bug, or the assertions below prove nothing.

    Pinned first because an invariance test over a parser that silently matches nothing passes for
    the wrong reason — the failure mode that let three guarded migrations still ship broken.
    """
    found = _env_assignments('    environment:\n      KUMO_ORDERS_ARMED: "true"\n')
    assert found == {"KUMO_ORDERS_ARMED": "true"}
    assert _is_a_gate("KUMO_ORDERS_ARMED")
    assert _is_a_gate("KUMO_SOME_FUTURE_GATE_ENABLED"), "must catch a gate that does not exist yet"
    assert not _is_a_gate("KUMO_REDIS_HOST")


def test_the_compose_file_still_declares_gates_at_all():
    """Guards the guard: if the env block stops parsing, every assertion below vacuously passes."""
    gates = [n for n in _env_assignments(_COMPOSE.read_text()) if _is_a_gate(n)]
    assert gates, f"no gates found in {_COMPOSE} — the parser has drifted from the file"
    assert "KUMO_ORDERS_ARMED" in gates, "the gate that decides whether orders leave the process"


def test_no_gate_is_a_hardcoded_literal():
    """An instance that sets a gate must get the value it set. A literal silently overrides it."""
    offenders = {
        name: value
        for name, value in _env_assignments(_COMPOSE.read_text()).items()
        if _is_a_gate(name) and "${" not in value
    }
    assert not offenders, (
        f"hardcoded in {_COMPOSE.name}: {offenders}. compose `environment:` beats `env_file`, so an "
        "instance declaring these is read and ignored. Use ${NAME:-false}."
    )


def test_every_gate_defaults_to_off():
    """CLAUDE.md: all automation gates default False, opt-in only.

    `${NAME}` alone is not enough — an unset variable makes compose substitute an empty string,
    which is falsey here but leaves no record of the intent, and `${NAME:-true}` would arm an
    instance that never asked to be armed.
    """
    wrong = {
        name: value
        for name, value in _env_assignments(_COMPOSE.read_text()).items()
        if _is_a_gate(name) and not re.fullmatch(r"\$\{%s:-(false|0)\}" % re.escape(name), value)
    }
    assert not wrong, f"gates must read ${{NAME:-false}}, got: {wrong}"
