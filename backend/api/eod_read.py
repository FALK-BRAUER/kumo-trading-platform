"""Reading the observation table into a per-lane window delta (#699 read path, #738).

WHAT THIS IS FOR, and it is the thing originally asked for: the strategy tile showing the DELTA for
the selected period. The tile shows `realized(W)` today; the missing half is `Δunrealized(W)` per
lane, which needs a per-lane mark at the window's START. The broker publishes only account-level
curves, which is why #734 built the table. This reads it.

THE SUBTRACTION ITSELF LIVES IN TYPESCRIPT (`lib/framework/windowBase.windowDelta`), not here. The
"now" term is on the live frame, so the tile is where the two ends meet — and a Python
`window_delta_unrealized` existed here with its None-propagation rules pinned by tests and NO
production caller. Two implementations of one rule is the drift this whole chain is about, so it was
deleted in the same commit that ported the rules across: one commit of overlap, then one owner.

THE IDENTITY. `NET(W) = realized(W) + [unrealized_now − unrealized_at_window_start]`, and it holds
per LANE because the table records per-lane unrealized per position per day. A position closed during
the window contributed its unrealized at the start and its result now sits in `realized(W)`, so the
two terms do not double-count.

EVERYTHING HERE RETURNS None RATHER THAN ZERO WHEN IT CANNOT TELL. That is the entire reason the
table exists: the tile currently renders an em dash for these windows, and replacing an honest em
dash with a confident wrong number is the only outcome worse than the status quo. `now - 0` for a
lane with no start row would report months of accumulated drift as this window's performance —
exactly what #336 refused and #699 declined to ship.
"""

from __future__ import annotations

import math
import re

#: A version we can ORDER. `v1`, `v2`, `v10`. Anything else is refused rather than sorted.
_VERSION = re.compile(r"^v(\d+)$")

#: Kinds that are window BASES. Mirrors `eod_observation_store.BASE_KINDS`; an intraday probe is not
#: something a period return may subtract from.
_BASE_KINDS = frozenset({"close", "reconstructed-eod"})


def select_version(rows) -> str | None:
    """WHICH derivation to read. ONE function, and #738 says it must exist before any reader does.

    `method_version` is in the base unique key deliberately, so a corrected re-derivation lands
    BESIDE the original rather than overwriting an audit trail. The cost of that choice is that
    "exactly one base row per lane-instrument-day" stopped being a database guarantee — so every
    reader must choose, and two readers choosing differently is the drift these tickets are about.

    NEWEST WINS, because the only reason a second version exists is that the first was corrected.

    ORDERED NUMERICALLY, NOT LEXICALLY. A string sort puts `v10` before `v9` and would then read the
    older derivation forever, silently, from the tenth correction onward.

    A version that cannot be ordered is REFUSED, naming it. Guessing would report a number derived
    from whichever string happened to sort highest, which is a stored conclusion nobody can audit.

    None means no rows at all — unknown, not a default.
    """
    versions = {str(r.get("method_version") or "") for r in rows or ()}
    if not versions:
        return None
    bad = sorted(v for v in versions if not _VERSION.match(v))
    if bad:
        raise ValueError(
            f"method_version(s) {bad} cannot be ordered against {sorted(versions - set(bad))}. "
            f"Refusing rather than sorting them as strings: a reader that guesses which derivation "
            f"is newest reports a number nobody can trace back to a rule."
        )
    return max(versions, key=lambda v: int(_VERSION.match(v).group(1)))


def lane_unrealized_at(rows, session_date: str, lane: str, version: str,
                       *, captured: bool = False) -> float | None:
    """That lane's total unrealized on that day, or None when it cannot be told.

    NONE IN TWO CASES, AND THEY ARE BOTH UNKNOWN RATHER THAN ZERO:

    - NO ROWS for that lane and day. A lane nobody captured and a lane that held nothing must not
      read the same — that distinction is the whole reason the manifest exists beside this table.
    - ANY position with no mark. One unpriced leg makes the LANE total unknown, not smaller. The
      same rule `observe_lanes` and `_standing_unrealized_total` already follow, and presenting a
      partial sum as a total is #596.
    """
    same_lane_day = [
        r for r in rows or ()
        if str(r.get("session_date")) == session_date
        and str(r.get("strategy_id")) == lane
        and str(r.get("capture_kind")) in _BASE_KINDS
    ]
    mine = [r for r in same_lane_day if str(r.get("method_version")) == version]
    if not mine:
        # ZERO, NOT UNKNOWN — but only because the CALLER established that this day's capture
        # SUCCEEDED. A lane absent from a successfully captured day genuinely held nothing; a lane
        # absent from a day nobody captured is unknown, and the manifest is what tells them apart.
        # `base_rows_on_or_before` resolves through the manifest for exactly this reason.
        return 0.0 if captured else None
    # A PARTIAL RE-DERIVATION IS UNKNOWN, NOT A SMALLER TOTAL. A corrected version may legitimately
    # cover only SOME instruments — `reconstruct_lanes` skips symbols it cannot price, and the
    # backfill runner's own comment anticipates "a re-run after a method_version change that only
    # some rows carry". Reading the newer version alone would then return a confident total missing
    # whatever it did not re-derive: review constructed 11.0 where the truth was ~31.
    #
    # Dormant while only v1 exists, and it arms the moment that stops being true — which is exactly
    # when nobody will be looking for it.
    covered = {str(r.get("instrument_id")) for r in mine}
    everything = {str(r.get("instrument_id")) for r in same_lane_day}
    if covered != everything:
        return None
    total = 0.0
    for r in mine:
        # COMPUTED AT READ TIME, from what the row actually stores. There is no `unrealized_derived`
        # column and there was never meant to be — the model says `qty x (mark - basis)` is computed
        # here and the stored engine/venue figures are what it is CHECKED AGAINST (#370).
        #
        # The first version asked for a `unrealized_derived` key and fell back to `unrealized_engine`
        # when it was absent, which it always was. That worked for a live capture and was EMPTY for a
        # reconstructed day, where `unrealized_engine` is deliberately NULL because Nautilus has no
        # reading for a past day — so every backfilled row would have read back UNKNOWN and the
        # backfill would have produced nothing usable. A phantom key, silently None.
        #
        # NO FALLBACK TO THE ENGINE FIGURE. It is a cross-check, not a substitute; mixing the two
        # inside one total is how two derivations that exist precisely so they can DISAGREE get
        # silently averaged.
        qty, basis, mark = r.get("qty"), r.get("avg_px_engine"), r.get("mark_px")
        if qty is None or basis is None or mark is None:
            return None
        leg = float(qty) * (float(mark) - float(basis))
        # NaN SURVIVES EVERY COMPARISON WRITTEN FOR NUMBERS, and `json.dumps` emits a literal `NaN`
        # that `JSON.parse` rejects — so one poisoned row would break the whole payload rather than
        # one cell. Unknown, not propagated. Same family as the non-finite mark guard in
        # `reconstruct_lanes`.
        if not math.isfinite(leg):
            return None
        total += leg
    return total


def lane_instruments_at(rows, session_date: str, lane: str, version: str) -> list[str]:
    """The instrument ids that lane held on that day under that version (#1072: what a kernel fill
    must touch to make the lane's window unvouchable). Empty for a lane with no rows."""
    return sorted({
        str(r.get("instrument_id")) for r in rows or ()
        if str(r.get("session_date")) == session_date
        and str(r.get("strategy_id")) == lane
        and str(r.get("capture_kind")) in _BASE_KINDS
        and str(r.get("method_version")) == version
    })


def lane_qty_at(rows, session_date: str, lane: str, version: str) -> dict[str, float]:
    """`{instrument_id: signed qty}` that lane held on that day under that version (#1072 b: the
    base end of the qty invariant). Empty for a lane with no rows."""
    out: dict[str, float] = {}
    for r in rows or ():
        if (str(r.get("session_date")) == session_date and str(r.get("strategy_id")) == lane
                and str(r.get("capture_kind")) in _BASE_KINDS and str(r.get("method_version")) == version):
            iid = str(r.get("instrument_id"))
            out[iid] = out.get(iid, 0.0) + float(r.get("qty") or 0.0)
    return out


def lane_market_value_at(rows, session_date: str, lane: str, version: str) -> float | None:
    """That lane's MARKET VALUE (Σ signed qty × mark) on that day, or None when it cannot be told.

    THE OTHER END OF `net(W) = ΔMV − invested(W)` (#699 a). Same three-state rules as
    `lane_unrealized_at`, which this mirrors on purpose — minus its `captured` parameter, which no
    caller passes (review): a lane with NO rows on the day is UNKNOWN here, and "captured and flat →
    zero" is decided by the caller from the lane's ABSENCE in a captured day's row set. One unpriced
    leg → the LANE is unknown, not smaller; a partial re-derivation → unknown. Signed: a short's
    market value is negative, so a lane's ΔMV carries the short's move with the right sign.
    """
    same_lane_day = [
        r for r in rows or ()
        if str(r.get("session_date")) == session_date
        and str(r.get("strategy_id")) == lane
        and str(r.get("capture_kind")) in _BASE_KINDS
    ]
    mine = [r for r in same_lane_day if str(r.get("method_version")) == version]
    if not mine:
        return None
    covered = {str(r.get("instrument_id")) for r in mine}
    everything = {str(r.get("instrument_id")) for r in same_lane_day}
    if covered != everything:
        return None
    total = 0.0
    for r in mine:
        qty, mark = r.get("qty"), r.get("mark_px")
        if qty is None or mark is None:
            return None
        leg = float(qty) * float(mark)
        if not math.isfinite(leg):
            return None
        total += leg
    return total
