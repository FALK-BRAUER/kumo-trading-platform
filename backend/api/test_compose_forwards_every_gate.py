"""Every KUMO_ gate the code READS must be forwarded by compose (#574, #581, #734).

THE DEFECT CLASS. `KUMO_DATA` was declared in both instance files, listed in the instances README
under "providers", exported by the Makefile — and read by nothing. Fixing that was a no-op, because
compose forwarded neither `KUMO_DATA` nor `KUMO_EXEC` into any container: twenty other `KUMO_*` vars
were present and those two were not. Both hid because the two sources AGREED at the default.

`deploy/compose.paper.yml` has NO `env_file` directive — the engine's environment IS its
`environment:` block — so a variable absent from that block reaches nothing, whatever instance.env
says. A gate that cannot be turned on looks exactly like a gate nobody turned on.

ENUMERATING THE SITES BY HAND IS WHAT KEEPS FAILING. This scans instead: every `KUMO_*` name the
backend reads must appear in the compose file. It is the same move as the #737 predicate rule.

THIS FILE ASKS "IS IT IN THE COMPOSE FILE", WHICH IS NOT THE WHOLE QUESTION (#814). The file has one
`environment:` block per SERVICE, and a var in the engine's block reaches nothing in the api. That is
how `KUMO_ORDERS_ARMED` passed this test for its whole life while the api process — the one that
actually reads it, in `app.py::_inert_contradictions` — never received it, leaving the inert detector
unable to fire on any instance. `test_api_process_env_is_forwarded.py` asks the per-service question
for the api. Neither test subsumes the other and both must stay: this one is broad and covers every
name; that one is narrow and covers the process boundary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE = _ROOT.parent / "deploy" / "compose.paper.yml"

#: PRE-EXISTING AND UNAUDITED, as of 2026-08-30. Every one of these is read by the backend and
#: forwarded by nothing, so it cannot be set on a running instance — the #574/#581 condition, fifteen
#: times over. They are NOT blessed here: this is a BASELINE that must not grow, and the ticket says
#: what still has to be decided about each.
#:
#: They are baselined rather than exempted-with-reasons because writing fifteen reasons I have not
#: verified would be the prose-that-reads-as-safety defect — the most dangerous kind of wrong,
#: because it survives review by sounding careful. Most look like tuning knobs whose defaults are
#: fine; at least two (`KUMO_SESSION_STRATEGY_ID`, `KUMO_IBKR_SYMBOL_MIC`) change BEHAVIOUR and
#: deserve a real answer.
#: Every entry names the ticket that owns it: #740 is the wiring list; #972 and #973 are the two that
#: need a DECISION, not a forward. An entry without a ticket is debt nobody owns, indistinguishable
#: from a decision.
_BASELINE_UNFORWARDED = frozenset({
    "KUMO_BIND_PORT",                 # #740
    "KUMO_CANCEL_CONFIRM_TIMEOUT_S",  # #740
    "KUMO_DB_POOL_MAX_OVERFLOW",      # #740
    "KUMO_DB_POOL_SIZE",              # #740
    "KUMO_EXECQUALITY_CSV",           # #740
    "KUMO_IBKR_HIST_REQ_PER_MIN",     # #740
    "KUMO_IBKR_SYMBOL_MIC",           # #973 — DECISION: instrument identity; feed.toml or forward
    "KUMO_REDIS_URL",                 # #740
    "KUMO_SESSION_STRATEGY_ID",       # #972 — DECISION: the session frame names one lane of four
    "KUMO_SHARES_FREE_TIMEOUT_S",     # #740
    "KUMO_FLIP_CONFIRM_TIMEOUT_S",    # #740 — ADDED in #898 (7a6bebc) while the comment above said "must not grow";
                                      # nothing could object, which is why `test_the_BASELINE_cannot_GROW` exists
    "KUMO_SOURCE_INTERVAL_S",         # #740
    "KUMO_SOURCE_TIMEOUT_S",          # #740
    # KUMO_REDIS_PORT left this set in #814: it is now forwarded to both api and engine.
    # KUMO_RECONCILIATION_TIMEOUT_S left in #954: forwarded into every engine service.
    # KUMO_DISCONNECTION_TIMEOUT_S left in #954: the READ was deleted — it is a constant pinned to
    # compose's literal stop_grace_period, not a knob (a runtime value would reintroduce #381).
})

#: THE SET AS RATIFIED ON 2026-09-11 (#954), spelled out — NOT derived from the set above. The first
#: draft was `frozenset(_BASELINE_UNFORWARDED)`, a copy of the thing it guards: a name added to the
#: baseline is added to the copy too, so `live - copy` is empty BY CONSTRUCTION and the guard can
#: never fire. Two derivations that are one derivation detect nothing. Bitten for real by appending
#: a name (line-number edit, presence grepped): `test_the_BASELINE_cannot_GROW` names it and fails.
#: "Must not grow" was PROSE from 2026-08-30 to 2026-09-11 — and it grew (#898). Adding a knob means
#: ratifying a defect; do it by editing THIS literal and saying so in the commit. A legitimate SHRINK
#: edits both sets in the same commit; the date in the name is when the set was last ratified, and
#: it moves with it.
_BASELINE_RATIFIED_2026_09_11 = frozenset({
    "KUMO_BIND_PORT", "KUMO_CANCEL_CONFIRM_TIMEOUT_S", "KUMO_DB_POOL_MAX_OVERFLOW", "KUMO_DB_POOL_SIZE",
    "KUMO_EXECQUALITY_CSV", "KUMO_IBKR_HIST_REQ_PER_MIN", "KUMO_IBKR_SYMBOL_MIC", "KUMO_REDIS_URL",
    "KUMO_SESSION_STRATEGY_ID", "KUMO_SHARES_FREE_TIMEOUT_S", "KUMO_FLIP_CONFIRM_TIMEOUT_S",
    "KUMO_SOURCE_INTERVAL_S", "KUMO_SOURCE_TIMEOUT_S",
})


def _forwarded_names(compose: str) -> set[str]:
    """The names actually ASSIGNED in an `environment:` block — not names that merely APPEAR.

    A substring check over the file text was the first version, and it did not bite: deleting the
    `KUMO_EOD_CAPTURE:` line left the test green, because the COMMENT I wrote above it says
    "not bare `${KUMO_EOD_CAPTURE}`" and the substring survived. That is the same evasion review
    already found once — a stub replaced by a comment passing a substring check — reproduced here by
    the person who wrote it down. Match an assignment.
    """
    return set(re.findall(r'^\s*(KUMO_[A-Z0-9_]+)\s*:', compose, re.MULTILINE))


def _read_names() -> set[str]:
    """`KUMO_*` names the backend actually reads from the environment."""
    names: set[str] = set()
    pattern = re.compile(r'["\'](KUMO_[A-Z0-9_]+)["\']')
    for path in _ROOT.rglob("*.py"):
        if ".venv" in str(path) or path.name.startswith("test_"):
            continue
        body = path.read_text()
        if "environ" not in body and "getenv" not in body and "_GATE" not in body:
            continue
        names |= set(pattern.findall(body))
    return names


@pytest.mark.skipif(not _COMPOSE.exists(), reason="compose file not present in this checkout")
def test_every_gate_the_code_reads_is_forwarded_into_the_container():
    compose = _COMPOSE.read_text()
    names = _read_names()

    # FIXTURE PROPERTY FIRST, TWICE. A scan that found no names would pass forever; and one that
    # found names but could not see the compose file's contents would too. Both are asserted against
    # a variable known to be present, so a change to either side's format fails loudly here rather
    # than silently disarming the check.
    assert names, "the scan found no KUMO_* names at all — it is no longer looking at anything"
    assert "KUMO_ORDERS_ARMED" in names, "the scan no longer sees a name it certainly should"
    assert "KUMO_ORDERS_ARMED" in _forwarded_names(compose), (
        "the compose file no longer looks the way this test reads it — an assignment it certainly has is no longer being matched"
    )

    forwarded = _forwarded_names(compose)
    missing = sorted(n for n in names if n not in forwarded and n not in _BASELINE_UNFORWARDED)
    assert not missing, (
        f"these are read by the backend and forwarded by NOTHING, so they cannot be set on a running "
        f"instance whatever instance.env says: {missing}. compose.paper.yml has no `env_file`, so the "
        f"`environment:` block is the entire environment. Add them, or exempt them in "
        f"_NOT_CONTAINERISED with the reason."
    )


@pytest.mark.skipif(not _COMPOSE.exists(), reason="compose file not present in this checkout")
def test_forwarded_gates_use_a_DEFAULT_rather_than_a_bare_interpolation():
    """`${KUMO_X}` with the variable unset interpolates to the EMPTY STRING, and
    `os.environ.get(k, default)` then returns "" instead of firing the default. That turned the
    forwarding fix for #574 into a fresh defect on the sibling knob — the same rule, one hop over."""
    bare = re.findall(r'^\s*(KUMO_[A-Z0-9_]+):\s*"\$\{(KUMO_[A-Z0-9_]+)\}"',
                      _COMPOSE.read_text(), re.MULTILINE)
    assert not bare, (
        f"these interpolate to the empty string when unset, which defeats the reader's default: "
        f"{[b[0] for b in bare]}. Use ${{NAME:-value}}."
    )


def test_the_BASELINE_cannot_GROW():
    """A test NAMED for a property it did not enforce is worse than no test: `only_SHRINKS` below is
    the thing a reviewer greps for and finds, and it can only see a baselined name becoming forwarded
    or unread — never one being ADDED. #898 added `KUMO_FLIP_CONFIRM_TIMEOUT_S` with an inline reason
    and nothing objected, because nothing could. Membership is pinned as a SET, not a count: a swap
    (one removed, one added) keeps the count and is the same defect one level up."""
    added = sorted(_BASELINE_UNFORWARDED - _BASELINE_RATIFIED_2026_09_11)
    assert not added, (
        f"{added} was ADDED to the known-unforwarded baseline. A knob the container cannot read is the "
        f"#574/#581 defect; forward it (compose `environment:` of every service that reads it) or delete "
        f"the read. Ratifying it here means deleting this test and saying so in the commit.")
    assert _BASELINE_UNFORWARDED == _BASELINE_RATIFIED_2026_09_11, "the ratified copy must shrink with the set"
    assert len(_BASELINE_UNFORWARDED) == 13, "readable summary only — the set above is the assertion"


def test_the_BASELINE_only_SHRINKS():
    """A baseline that can grow is not a baseline. Anything in the set that has since been forwarded
    must be REMOVED from it, the same way a stale ALLOWED entry in the orphan guard must — an
    exemption that has quietly become unnecessary reads like a considered decision forever."""
    compose = _COMPOSE.read_text()
    forwarded = _forwarded_names(compose)
    names = _read_names()

    fixed = sorted(n for n in _BASELINE_UNFORWARDED if n in forwarded)
    assert not fixed, (
        f"these are now forwarded and their baseline entries are false: {fixed}. Delete them from "
        f"_BASELINE_UNFORWARDED — in the same commit that forwards them."
    )
    # SET EQUALITY, not membership. A baseline that only fails in the growing direction silently
    # OVERSTATES the debt once someone fixes an entry without deleting it, and an exemption that has
    # quietly become unnecessary reads like a considered decision forever — the same rule the orphan
    # guard's staleness half enforces.
    gone = sorted(n for n in _BASELINE_UNFORWARDED if n not in names)
    assert not gone, (
        f"these are no longer READ by the backend at all, so baselining them as unforwarded debt is "
        f"false: {gone}. Delete them from _BASELINE_UNFORWARDED."
    )
