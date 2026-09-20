"""The built-in decision schedule per lane — ONE declaration (#378).

An empty `*_SLOTS` override means "keep the built-in schedule" to every lane (momentum's
`decision_slots_from_settings`: `if not raw: return default`; kumo-trading-strategies' rotation does the
same). It meant "nothing is due" to the never-ran detector, which read the raw settings value. So
four lanes decided on their built-ins while the detector expected nothing from any of them, and
armed-and-silent read exactly like healthy — the outage this ticket was filed for, with the
detector built, wired and green.

The defaults now live HERE, and both the builders and the detector resolve through them, so the
two cannot disagree about what a lane is going to do. A lane cockpit has no default for declares
NOTHING rather than a guess: alarming on every strategy we do not understand is how a channel gets
muted.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("kumo.decision_slots")

#: Lanes whose schedule COCKPIT declares: the builders pass `decision_slots=BUILTIN_SLOTS[id]`
#: into the rotation strategy, which honours an explicit argument first
#: (momentum_rotation.py:211), so for these the map IS the authority and
#: `test_the_BUILDERS_use_the_SHARED_defaults_not_literals` is what keeps it so.
COCKPIT_DECLARED = ("MOMENTUM-002", "BCTROT-004")


def lane_declared_slots(strategy_id: str) -> tuple[str, ...] | None:
    """What the lane's OWN module declares, for the lanes cockpit does not tell.

    THE MAP WAS TYPED BY HAND AND WAS WRONG (review, 2026-08-29): it said TECHIVOL-005 decides at
    open+5m while `qc27_runner.DECISION_SLOT` is built from OPEN_OFFSET_MINUTES = 150 — a false
    CRITICAL page every 12h on a healthy lane, and NO expectation at the slot the lane actually
    runs, so a real miss stayed invisible. That is #378's own outage reproduced by #378's fix, one
    lane over. kumo-trading-strategies' own docstring records the same literal drifting once before.

    Two categories, and conflating them is what produced the bug:
      * COCKPIT_DECLARED — cockpit passes the schedule in, so the map is the authority. BCTROT is
        additionally cross-checked against the strategy's own default because both exist.
      * everything else — the lane owns its schedule and the map may only MIRROR it. Pinned equal
        by `test_every_builtin_matches_what_the_LANE_actually_declares`.

    Imports are local and guarded: this can run on the alert path, and an import failure must
    degrade to UNKNOWN — which declares nothing — rather than to a guess.
    """
    try:
        if strategy_id == "TECHIVOL-005":
            from kumo_strategies.runtime.executor.qc27_runner import DECISION_SLOT

            return (str(DECISION_SLOT),)
        if strategy_id == "QC345-003":
            from strategies.qc345 import DECISION_SLOT as QC345_SLOT

            return (str(QC345_SLOT),)
        if strategy_id == "BCTROT-004":
            from kumo_strategies.runtime.nautilus.bctrot_rotation import DECISION_SLOTS

            return tuple(str(x) for x in DECISION_SLOTS)
        if strategy_id == "CRSISHORT-006":
            import inspect

            from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy

            default = inspect.signature(CrsiShortStrategy.__init__).parameters["decision_slots"]
            return tuple(str(x) for x in default.default)
        if strategy_id == "SMHGLD-007":
            # Read off the upstream adapter's SIGNATURE (issue 177), never typed: the
            # default `decision_slots` is the lane's own declaration. Absent on an older pin → None.
            import inspect
            from kumo_strategies.runtime.nautilus.smhgld_sleeve import SmhGldSleeveStrategy
            default = inspect.signature(SmhGldSleeveStrategy.__init__).parameters["decision_slots"].default
            return tuple(str(s) for s in default)
        if strategy_id == "MOMENTUM-002":
            # Cockpit-declared and the strategy exposes no module-level default to compare against
            # (its fallback is computed from an open offset), so the map is the only declaration.
            return None
    except Exception:  # noqa: BLE001 — see the docstring: unknown, never a guess
        return None
    return None


#: strategy_id -> the schedule the lane runs when settings declare no override.
#: A CACHE of `lane_declared_slots`, not a second declaration — the equality is pinned by a test,
#: because this map being hand-typed is exactly how #378's fix shipped a false alarm.
BUILTIN_SLOTS: dict[str, tuple[str, ...]] = {
    "MOMENTUM-002": ("open+5m",),
    # OPEN SLOT ADDED 2026-09-04 (the operator). Must move in lockstep with
    # `bctrot_rotation.DECISION_SLOTS` — BCTROT is COCKPIT_DECLARED *and* cross-checked against the
    # strategy's own default, because both exist and a hand-typed map already drifted once
    # (TECHIVOL-005, a false CRITICAL every 12h on a healthy lane).
    "BCTROT-004": ("open+5m", "open+150m", "close-20m"),
    "QC345-003": ("open+5m",),
    "TECHIVOL-005": ("open+150m",),
    # Mirrors `CrsiShortStrategy.__init__`'s `decision_slots` default (#858).
    "CRSISHORT-006": ("open+5m",),
    # SMHGLD-007 (issue 177): decide at close-20m, fill market-on-close (platform issue 965).
    "SMHGLD-007": ("close-20m",),
}


def effective_expected_slots(cfg: dict, defaults: dict | None = None,
                            registered=None) -> dict:
    """{strategy_id: slots} the lanes will ACTUALLY run, from the settings domain.

    Resolves each lane through the same function the lane resolves through, so an empty override
    yields the built-in, a valid override wins, and a malformed one falls back exactly as the lane
    falls back (a typo must not move the detector's expectation somewhere the lane never goes).
    """
    from strategies.momentum import resolve_slots

    known = BUILTIN_SLOTS if defaults is None else defaults
    # ONLY LANES THIS STACK ACTUALLY RUNS (review, 2026-08-29). The settings schema gives every
    # `*_SLOTS` key a `[]` default, so `resolve()` materialises all four on every stack — staging
    # registers BCTROT-004 alone and would have been paged daily for three lanes that cannot
    # decide there. `registered=None` means "caller cannot enumerate": fall back to the known
    # lanes rather than to zero, so an un-enumerable caller is no worse off than before.
    live = None if registered is None else {str(x) for x in registered}
    out: dict[str, tuple[str, ...]] = {}
    for key, raw in (cfg or {}).items():
        if not key.endswith("_SLOTS"):
            continue
        sid = key[: -len("_SLOTS")]
        if live is not None and sid not in live:
            continue
        # THE LANE'S OWN DECLARATION WINS AT RUNTIME, not just in a test (review, 2026-08-29).
        # The map is a cache; if a lane's module moves its schedule, the derivation follows it and
        # the detector cannot page for a slot the lane stopped running. Falls back to the cached
        # value only when the declaring module cannot be imported — and to nothing when neither is
        # available, because a lane cockpit cannot describe must declare NOTHING rather than a guess.
        declared = None if defaults is not None else lane_declared_slots(sid)
        default = declared or known.get(sid)
        if not default:
            # UNKNOWN, not "due at the open". Three states.
            continue
        if declared and known.get(sid) and tuple(declared) != tuple(known[sid]):
            _log.warning(
                "%s: the cached built-in schedule %s disagrees with what the lane declares (%s) — "
                "using the lane's. Update BUILTIN_SLOTS; a stale cache is how #378's fix shipped a "
                "false CRITICAL page on a healthy lane.",
                sid, tuple(known[sid]), tuple(declared))
        out[sid] = resolve_slots(raw, tuple(default), strategy_id=sid)
    return out
