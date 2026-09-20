"""`on_emergency_exit` — the callback target kumo-trading-strategies' emergency exit calls (#922, a8504dc).

THE LANE DECIDES; THE OWNER OF THE POSITIONS ACTS. `strategies/emergency.py` upstream calls
`await hook(lane=..., verdict=..., reasons=...)` and journals what came back beside its own decision, so
"called for liquidation, owner accepted N" is ONE durable fact. This module builds that hook over the
engine's `liquidate_lane` command (`UiFeedStrategy._handle_liquidate_lane_command`) and maps the
command's three states onto the interface's three:

    ok / held_nothing          -> ACCEPTED, positions = submitted (0 is a legitimate ACCEPTED)
    partial                    -> PARTIAL (acts=False), positions = submitted, reason "<n> of <m>
                                  failed: ...", failures + remainder in `detail` — a real state with
                                  its own row, never ACCEPTED with the failures buried in a list
    refused / duplicate / ?    -> REFUSED, reason = the handler's why (never empty)
    deferred_to_next_open      -> REFUSED, named: LIQUIDATING is written, nothing was sold — THE LANE
                                  MUST ESCALATE, NOT BELIEVE A SALE. Anything softer lets a lane
                                  record "handled" while the book is intact until an open that may
                                  be fourteen hours away (#165: a DAY order there writes no row).
    the engine raising         -> UNREACHABLE "<ExcType>: msg" — a refusal is a decision by a system
                                  that is working; an exception is a system that is not, and the lane
                                  escalates each to a different person. Never raised through, never
                                  None.
    verdict that does not act  -> REFUSED before the engine is touched (rule 4, a second derivation)

CALLABLE AND UNREFERENCED. Nothing upstream calls this yet and the trigger decision is the operator's
(#873); no synthetic caller exists in cockpit to make it look exercised. The outcome type lives
upstream and is imported AT CALL TIME (`outcome_type=None`), so this module imports on any pin and
fails by name, on the first call, on a pin that predates a8504dc — never silently.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

_log = logging.getLogger("kumo.emergency_exit")

ACCEPTED, PARTIAL, REFUSED, UNREACHABLE = "accepted", "partial", "refused", "unreachable"

_ACTED = ("ok",)
_DEFERRED = "deferred_to_next_open"


def _outcome_class(outcome_type):
    if outcome_type is not None:
        return outcome_type
    try:
        from kumo_strategies.strategies.emergency import EmergencyOutcome
    except ImportError as exc:  # the pin predates a8504dc — say so by name, on the call, never silently
        raise RuntimeError(
            "on_emergency_exit needs kumo_strategies.strategies.emergency.EmergencyOutcome (a8504dc, PR #169) "
            f"and the installed kumo-trading-strategies has none: {type(exc).__name__}: {exc}") from exc
    return EmergencyOutcome


def _default_read_book(target) -> Callable[[str], list[tuple[str, int]]]:
    def read(lane: str) -> list[tuple[str, int]]:
        cache = getattr(target, "cache", None)
        if cache is None:
            raise RuntimeError("no cache on the liquidate target — cannot read the lane's book for the leash")
        return [(str(p.instrument_id), int(abs(Decimal(str(p.quantity)))))
                for p in cache.positions_open(strategy_id=lane)]
    return read


def build_on_emergency_exit(target, *, outcome_type=None, read_book=None):
    """Build the hook over `target.liquidate_lane(cid, payload) -> (status, detail)`.

    `target` is the engine (or a double) exposing `liquidate_lane`; `read_book(lane)` supplies the
    leash from the LIVE book — `[(instrument_id, abs_qty), ...]` — so the handler can refuse a moved
    book the same way it refuses an operator's stale count.
    """
    read = read_book or _default_read_book(target)

    async def on_emergency_exit(*, lane: str, verdict, reasons: tuple[str, ...] = ()):
        Outcome = _outcome_class(outcome_type)
        reasons = tuple(reasons or getattr(verdict, "reasons", ()) or ())
        reason_text = " | ".join(str(r) for r in reasons) or f"{lane} called emergency_exit"
        if not getattr(verdict, "acts", False):
            return Outcome(status=REFUSED, positions=0,
                           reason=f"verdict does not act (state={getattr(verdict, 'state', '?')}) — nothing acts on unknown; {reason_text}")
        try:
            book = read(lane)
            payload = {
                "strategy_id": lane,
                "expected_positions": len(book),
                "expected_total_qty": sum(q for _, q in book),
                "invoked_by": f"hook:{lane}",
                "reason": reason_text,
            }
            status, detail = await target.liquidate_lane(uuid.uuid4().hex, payload)
        except Exception as exc:  # noqa: BLE001 — never raise through: the lane would record UNREACHABLE for a liquidation that may have started
            _log.exception("on_emergency_exit(%s): the engine raised", lane)
            return Outcome(status=UNREACHABLE, positions=0, reason=f"{type(exc).__name__}: {exc}", detail={"reasons": list(reasons)})
        detail = dict(detail or {})
        submitted = int(detail.get("submitted", 0) or 0)
        if status in _ACTED:
            return Outcome(status=ACCEPTED, positions=submitted, detail=detail)
        if status == "partial":
            failed = list(detail.get("failed") or []); remainder = list(detail.get("remainder") or [])
            held = int(detail.get("held", 0) or 0)
            what = ", ".join(f.get("symbol") or f.get("order") or "?" for f in failed) or "none"
            return Outcome(status=PARTIAL, positions=submitted, detail=detail,
                           reason=f"{len(failed)} of {held} failed: {what}"
                                  + (f"; {len(remainder)} remained open after the sweep" if remainder else ""))
        if status == _DEFERRED:
            return Outcome(status=REFUSED, positions=0, detail=detail,
                           reason="deferred_to_next_open: LIQUIDATING written, nothing submitted outside regular hours — "
                                  "the book keeps its resting stops; invoke again in hours")
        why = str(detail.get("why") or "no reason given")
        known = status in ("refused", "duplicate")
        return Outcome(status=REFUSED, positions=0, detail=detail,
                       reason=why if known else f"engine answered {status!r}: {why}")   # an unknown status is NAMED

    on_emergency_exit.__kumo_target__ = target  # type: ignore[attr-defined]
    return on_emergency_exit
