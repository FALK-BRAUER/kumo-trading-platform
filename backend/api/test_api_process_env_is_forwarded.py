"""Every `KUMO_*` the API PROCESS reads must actually reach the API CONTAINER (#814).

MEASURED ON ibkr-paper, 2026-09-09. `instance.env` declares `KUMO_ORDERS_ARMED=false`. The engine
container has it. The api container does not:

    $ docker exec kumo-ibkr-paper-engine-1 printenv KUMO_ORDERS_ARMED   -> false
    $ docker exec kumo-ibkr-paper-api-1    printenv KUMO_ORDERS_ARMED   -> exit 1

`compose.paper.yml` forwards it to the `engine` service only. And the code that reads it —
`app.py::_inert_contradictions` — runs in the API process:

    armed_env = os.environ.get("KUMO_ORDERS_ARMED", "").strip().lower()
    orders_armed = armed_env in {...} if armed_env else None

Unset means `""` means `orders_armed = None`, and `inert.py` documents that `None` yields nothing:
"a surface that cannot tell 'unknown' from 'wrong' either cries wolf or passes vacuously". So the
ORDERS_ARMED half of the inert detector has never been able to fire on any instance — the half that
`api/inert.py`'s own docstring credits with catching the 2026-08-24 incident, where `/health` said
`ok` for a stack that would have placed nothing all day.

`KUMO_CORS_ORIGINS` had the same shape and got away with it: the api is the process that serves CORS,
the var never reached it, and the code's `"*"` default happened to equal what `instance.env` asked
for. Two sources agreeing is exactly when a severed wire is invisible.

THIS IS THE THIRD TIME. #574: `KUMO_DATA` declared, exported, documented, read by nothing. #581:
fixing that was a no-op because compose forwarded neither `KUMO_DATA` nor `KUMO_EXEC` into any
container. Both times the fix was a value; the class stayed open because nothing enforced the link.

SO THE RULE IS ENFORCED AT THE READ, NOT AT A LIST. This scans `app.py` for what it actually reads
and requires each name to be resolvable in the api container. Adding a new `os.environ.get("KUMO_…")`
to the api and forgetting the compose line fails here instead of on a stack, silently, months later.

WHY `test_compose_forwards_every_gate.py` DID NOT CATCH THIS, AND WHY BOTH FILES STAY. That test asks
whether a name appears ANYWHERE in the compose file. `KUMO_ORDERS_ARMED` does — in the `engine`
block — so it passed for the var's whole life while the process that reads it got nothing. This file
asks the per-SERVICE question, which is the one the process boundary actually poses. Broad-and-any
does not subsume narrow-and-per-service, and deleting either leaves half the class open.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from api.test_not_shipped import require_shipped

_BACKEND = pathlib.Path(__file__).parent.parent
_DEPLOY = _BACKEND.parent / "deploy"
_APP = _BACKEND / "api" / "app.py"

#: Env the IMAGE carries, so compose need not repeat it. Baked from build args in Dockerfile.backend
#: (`ENV KUMO_GIT_SHA=... KUMO_STRATEGIES_SHA=...`), which is why `/health` can report provenance on a
#: container whose compose block names neither.
_BAKED_BY_DOCKERFILE = frozenset({"KUMO_GIT_SHA", "KUMO_STRATEGIES_SHA"})


def _env_names_read_by(path: pathlib.Path) -> set[str]:
    """`KUMO_*` names this module passes to `os.environ.get` / `os.getenv`, from the AST.

    AST rather than grep: a regex over the source matches the name inside a comment or a docstring,
    and this file's whole job is to distinguish a real read from a mention of one.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        target = None
        if isinstance(fn, ast.Attribute) and fn.attr in ("get", "getenv"):
            target = fn.attr
        if target is None:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith("KUMO_"):
                names.add(first.value)
    return names


def _service_env(compose: pathlib.Path, service: str) -> set[str]:
    """The `KUMO_*` keys declared under one service's `environment:` block."""
    src = compose.read_text()
    m = re.search(rf"^  {re.escape(service)}:$", src, re.M)
    assert m, f"{compose.name} has no `{service}` service"
    rest = src[m.end():]
    nxt = re.search(r"^  [a-z0-9-]+:$", rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    return set(re.findall(r"^\s{6}(KUMO_[A-Z0-9_]+):", block, re.M))


# -------------------------------------------------------------------------------------------------
# VACUITY GUARDS. A scan that finds nothing passes forever; assert it reaches its own subject first.
# -------------------------------------------------------------------------------------------------

def test_the_scan_actually_finds_the_read_that_started_this():
    """If `_env_names_read_by` returns an empty set — a moved file, a renamed helper, a refactor into
    a constant — every assertion below passes while enforcing nothing. Anchor it on the exact read
    whose absence made the inert detector inert."""
    names = _env_names_read_by(_APP)
    assert names, "the AST scan found no KUMO_* reads in app.py — it is not reaching its subject"
    assert "KUMO_ORDERS_ARMED" in names, (
        "app.py no longer reads KUMO_ORDERS_ARMED by that literal name; re-anchor this test rather "
        "than deleting it — the class it guards is still open"
    )


def test_the_scan_ignores_a_name_that_only_appears_in_prose():
    """The AST is what makes this a read rather than a mention. Proven against a fixture, because a
    regex-based version of this test would pass while enforcing something weaker than it claims."""
    import tempfile
    src = (
        '"""A docstring naming KUMO_NOT_A_REAL_READ."""\n'
        "import os\n"
        "# comment about KUMO_ALSO_NOT_REAL\n"
        'X = "KUMO_STRING_LITERAL_ONLY"\n'
        'Y = os.environ.get("KUMO_GENUINELY_READ")\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
        p = pathlib.Path(f.name)
    try:
        assert _env_names_read_by(p) == {"KUMO_GENUINELY_READ"}
    finally:
        p.unlink()


def test_the_compose_parser_actually_finds_the_api_block():
    """Same hazard on the other side: a parser returning an empty set makes every var look missing,
    or — with the assertion written the other way — makes every var look present."""
    keys = _service_env(_DEPLOY / "compose.paper.yml", "api")
    assert len(keys) >= 5, f"the api service block parsed to {keys} — the parser is not reaching it"
    assert "KUMO_DATABASE_URL" in keys, "the parser missed a var the api provably needs"


# -------------------------------------------------------------------------------------------------
# THE RULE.
# -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("compose_name", ["compose.paper.yml", "compose.prod.yml"])
def test_every_KUMO_var_the_api_reads_reaches_the_api_container(compose_name):
    """THE DEFECT, AND ITS SIBLINGS. Not "KUMO_ORDERS_ARMED is forwarded" — that pins one value and
    leaves the class alive, which is how this arrived for the third time."""
    require_shipped(f"deploy/{compose_name}")
    compose = _DEPLOY / compose_name
    read = _env_names_read_by(_APP)
    declared = _service_env(compose, "api") | _BAKED_BY_DOCKERFILE
    missing = sorted(read - declared)
    assert not missing, (
        f"{compose_name}: the api process reads {missing} but the api service never receives "
        f"them. `os.environ.get` returns the default, the operator's declared value is silently "
        f"discarded, and nothing reports it — #574/#581 for the third time."
    )


@pytest.mark.parametrize("compose_name", ["compose.paper.yml", "compose.prod.yml"])
def test_every_interpolated_KUMO_forward_supplies_a_DEFAULT(compose_name):
    """FORWARDING A VAR WITHOUT `:-` CREATES THE DEFECT IT WAS MEANT TO FIX (#581, measured).

    Compose interpolates an UNSET variable to the EMPTY STRING. `os.environ.get(k, default)` then
    returns `""` — a set-but-empty value — and the code's default never fires. So `${KUMO_X}` is
    strictly worse than not forwarding at all: before, the default applied; after, an empty string
    silently wins. Every `${KUMO_…}` forward must be `${KUMO_…:-something}`.

    This is the sibling case that made #581's fix create a new defect on the neighbouring knob, which
    is why it is enforced here rather than remembered.
    """
    require_shipped(f"deploy/{compose_name}")
    src = (_DEPLOY / compose_name).read_text()
    bare = re.findall(r"^\s+KUMO_[A-Z0-9_]+:\s*\"?\$\{(KUMO_[A-Z0-9_]+)\}", src, re.M)
    assert not bare, (
        f"{compose_name}: {sorted(set(bare))} are interpolated with no `:-default`. An unset "
        f"variable becomes the empty string, which defeats the default in os.environ.get (#581)."
    )


def test_the_engine_still_receives_ORDERS_ARMED_too():
    """The gate itself lives in the engine (`engine_node.py:855`), and the fix must ADD a forward
    rather than move one. Removing it there would disarm the check that actually blocks orders while
    making the detector look healthier."""
    assert "KUMO_ORDERS_ARMED" in _service_env(_DEPLOY / "compose.paper.yml", "engine"), (
        "the engine no longer receives KUMO_ORDERS_ARMED — the gate that blocks live order flow"
    )


def test_ORDERS_ARMED_defaults_to_FALSE_in_compose_on_BOTH_services():
    """Both forwards must carry the same default, and it must be the safe one. Two derivations of one
    rule drift: an api that defaults `true` while the engine defaults `false` reports a stack as armed
    that cannot place, which is the inert detector lying in the other direction."""
    src = (_DEPLOY / "compose.paper.yml").read_text()
    forwards = re.findall(r"KUMO_ORDERS_ARMED:\s*\"?\$\{KUMO_ORDERS_ARMED:-([a-z]+)\}", src)
    assert len(forwards) == 2, f"expected one forward per service, found {forwards}"
    assert set(forwards) == {"false"}, (
        f"KUMO_ORDERS_ARMED does not default to false everywhere: {forwards} — all new automation "
        f"gates default False, and the two services must not disagree about it"
    )
