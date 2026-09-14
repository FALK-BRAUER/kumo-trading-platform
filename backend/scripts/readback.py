"""Post-deploy readback that asserts COMPLETENESS against the running api's own schema (#887).

WHY. On 2026-09-10 paper carried 55 naked shares for five hours while every readback said clean:
the only `/health` field that knew (`failed_requests`) was one no hand-written list read — 12 of the
32 declared fields were read, 16 were not. A hand-list is the failure mode this family keeps
repeating (#233, #322, #336, #546, #618: five fields died at that seam). So the field list here is
DERIVED from `/openapi.json` as served by the api under test — the deployed schema, never the
checkout's model — and:

  - every schema property must be PRESENT in the payload, or the run FAILS naming it;
  - every payload key must be IN the schema, or the run FAILS naming it (a new engine field cannot
    be silently unread);
  - every container is THREE-STATED (#859): `null` = the engine did not say (a FAIL while the
    bridge is up), empty = asked and clean, non-empty = a finding that must be EXPLAINED by an
    explicit, dated allow-entry or the run FAILS — and an allowed finding is still printed;
  - the frame is judged FIRST: no `bridge_ok`, engine subsystem not ok, or lanes count None →
    REFUSED before any list is read (the #859 rule);
  - scalars the deploy expects (`cockpit_sha`, `strategies_sha`, …) are asserted by name.

Usage: `python -m scripts.readback --api http://localhost:8000 --expect strategies_sha=4d28488
--allow observations="2026-09-11 event-driven observers never_ran right after boot"`.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

#: Container fields whose EMPTY state is not a dict/list but a nested structure with its own
#: "nothing wrong" shape. Each is judged by the rule beside it rather than by emptiness.
_STRUCTURED = ("observations", "book_truth", "split_divergence", "subscriptions", "realized_legs", "shortable", "log_compaction")


@dataclass(frozen=True)
class Readback:
    verdict: str                       # "OK" | "FAIL" | "REFUSED"
    findings: list[str] = field(default_factory=list)


def _get(api: str, path: str) -> dict:
    with urllib.request.urlopen(f"{api}{path}", timeout=10) as r:      # noqa: S310 — operator tool, local api
        return json.load(r)


def _structured_finding(key: str, v: dict) -> str | None:
    if key == "observations":
        if v.get("failing") or v.get("never_ran"):
            return f"observations: failing={v.get('failing')} never_ran={v.get('never_ran_names') or v.get('never_ran')}"
        return None
    if key == "book_truth":
        bad = {k: v[k] for k in ("net_disagrees", "split_disagrees", "venue_unreadable", "abandoned") if v.get(k)}
        return f"book_truth: {bad}" if bad else None
    if key == "split_divergence":
        return f"split_divergence: {v}" if v.get("pairs") or v.get("error") else None
    if key == "subscriptions":
        return f"subscriptions.silent: {v.get('silent_subjects')}" if v.get("silent") else None
    if key == "realized_legs":
        return f"realized_legs: {v}" if v.get("error") or v.get("state") not in (None, "complete") else None
    return None                                             # shortable / log_compaction: informational


def readback(*, fetch: Callable[[str], dict], expect: dict[str, str], allow: dict[str, str] | None = None) -> Readback:
    allow = allow or {}
    schema = fetch("/openapi.json")
    props = set(schema["components"]["schemas"]["HealthResponse"]["properties"])
    h = fetch("/health")
    findings: list[str] = []

    # 1. THE FRAME, before any list (#859). Refuse loudly; read nothing else from this payload.
    engine = next((s for s in (h.get("subsystems") or []) if s.get("name") == "engine"), None)
    if h.get("bridge_ok") is False or (engine is not None and engine.get("ok") is not True) \
            or h.get("automated_lanes_running") is None:
        return Readback("REFUSED", [
            f"REFUSED — no engine frame: status={h.get('status')!r} bridge_ok={h.get('bridge_ok')!r} "
            f"engine={(engine or {}).get('ok')!r} automated_lanes_running={h.get('automated_lanes_running')!r}; "
            f"every list in this payload is unknown, not clean"])

    # 2. COMPLETENESS, both directions.
    for k in sorted(props - set(h)):
        findings.append(f"MISSING: schema property {k!r} is not in the payload")
    for k in sorted(set(h) - props):
        findings.append(f"UNKNOWN KEY: payload carries {k!r} which the served schema does not declare — nobody reads it")

    # 3. EXPECTED SCALARS.
    for k, want in expect.items():
        got = h.get(k)
        if str(got) != str(want):
            findings.append(f"EXPECTED {k}={want!r} but the stack reports {got!r}")

    # 4. EVERY CONTAINER, THREE-STATED.
    for k in sorted(props & set(h)):
        v = h[k]
        if v is None:
            if k in ("log_compaction", "shortable", "market_data_type"):
                findings.append(f"INFO: {k} is null (not asked / not on this venue)")
                continue
            findings.append(f"NULL while the bridge is up: {k} — the engine did not say; not clean")
            continue
        if not isinstance(v, (list, dict)):
            continue
        if k == "subsystems":
            down = [s for s in v if s.get("ok") is not True]
            text = f"subsystems not ok: {down}" if down else None
        elif k == "armed_lanes":
            # Non-empty is the HEALTHY shape here; a lane reading False is the finding.
            #
            # BOTH VALUE SHAPES, because this script runs AT THE BOOT and the api and the engine
            # recreate seconds apart. #997 turned each value from a bare bool into a ROW, so a
            # readback taken in that window sees the OLD bool from a lagging engine and the NEW row
            # once it catches up. Reading only the row would break this in the other direction.
            #
            # Without this, every value is a dict, no dict `is True`, and the readback reports EVERY
            # LANE not armed at the one moment the script exists for. It fails loud rather than
            # silently only because the test below is `is not True` and not a truthiness test — a
            # non-empty dict is always truthy, and that version would have reported every lane
            # healthy instead, which is the same defect pointing the other way.
            #
            # `is not True` is kept deliberately: `armed: None` means the lane COULD NOT BE ASKED and
            # must stay a finding, never collapse into "armed".
            unarmed = {lane: on for lane, on in v.items()
                       if (on.get("armed") if isinstance(on, dict) else on) is not True}
            text = f"armed_lanes not armed: {unarmed}" if unarmed else None
        elif k == "next_fire_ns":
            text = None                                     # informational: a schedule, never a defect
        elif k in _STRUCTURED:
            text = _structured_finding(k, v) if isinstance(v, dict) else None
        else:
            text = f"{k}: {json.dumps(v)[:300]}" if v else None
        if text is None:
            continue
        if k in allow:
            findings.append(f"ALLOWED ({allow[k]}): {text}")
        else:
            findings.append(f"FINDING: {text}")

    verdict = "FAIL" if any(f.split(":")[0] in ("MISSING", "UNKNOWN KEY", "FINDING") or f.startswith(("EXPECTED", "NULL")) for f in findings) else "OK"
    return Readback(verdict, findings)


def main() -> int:
    ap = argparse.ArgumentParser(description="Post-deploy readback: complete, three-stated, schema-derived (#887)")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--expect", action="append", default=[], help="key=value scalar the deploy expects, e.g. strategies_sha=4d28488")
    ap.add_argument("--allow", action="append", default=[], help='field="dated reason" for a non-empty container that is explained')
    a = ap.parse_args()
    expect = dict(e.split("=", 1) for e in a.expect)
    allow = dict(x.split("=", 1) for x in a.allow)
    r = readback(fetch=lambda p: _get(a.api, p), expect=expect, allow=allow)
    print(f"READBACK {r.verdict}")
    for f in r.findings:
        print("  " + f)
    if not r.findings:
        print("  every declared field present; every container asked and clean")
    return 0 if r.verdict == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
