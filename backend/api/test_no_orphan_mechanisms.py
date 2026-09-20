"""No public function may be added without a caller (2026-08-23).

NINE mechanisms were found in one session that were built, tested, documented with the very incident
they caught, and driven by nothing:

    #439  the slot-attribution detector          #345  open_lots_after (phantom lots)
    #462  a caller that never passed its argument #437  build_breaches — /claims was a 404
    #467  an acceptor no caller drove             #349  scan_slots / judge_slot
    #440  run_boot_gate — never executed          #463  set_target — a validating no-op API
    #378  the never-ran verdict

That is not nine accidents. A mechanism whose only witness is itself never gets called, because
nothing fails when it is not. Two of them had `xfail(strict=True)` tripwires aimed squarely at them;
one worked and one could never fire because its test was `needs_services` and never runs here.

So the check is mechanical instead. A public module-level function with no reference anywhere in
production is either dead or unwired, and both need to be a deliberate, written-down decision rather
than something that happens quietly.

WHY AN ALLOWLIST AND NOT A BAN. Some orphans are legitimate: #425 built three trading mechanisms as
CANDIDATES for an explicit build/remove decision the operator has not made yet. Those are not defects — they
are proposals. The allowlist is where that distinction gets stated, and the test fails in BOTH
directions: a new orphan appears, or an allowlisted one gets wired and the entry goes stale.
"""

from __future__ import annotations

import ast
import pathlib

ROOTS = ("api", "scripts", "strategies")

#: name -> why it has no caller. Every entry is a claim someone can check.
#:
#: SHRINKING IS THE POINT. An entry means "we know, and here is the reason"; it does not mean "fine".
ALLOWED = {
    "build_on_emergency_exit": "#922 — the callback target kumo-trading-strategies' emergency exit calls "
                               "(strategies/emergency.py, e600383). Built CALLABLE AND UNREFERENCED on "
                               "purpose: the trigger — whether a daily-loss breach liquidates or keeps "
                               "halting-and-holding — is the operator's risk decision (#873), and a synthetic "
                               "caller built to make this look exercised would be a mechanism never "
                               "driven by the thing that will actually use it. Wired the moment a lane "
                               "registers the hook; if this entry is still here after that, the wiring "
                               "was forgotten — which is exactly what this guard exists to say.",
    "secured_delta": "#787 — the mechanism is built and bitten; WHERE it goes on screen is the operator's "
                     "call (replace SECURED, which answers a different question against entry, or "
                     "sit beside Δ UNREALIZED as its secured portion). Listed rather than wired "
                     "because guessing the surface is how a cell ends up answering a question "
                     "nobody asked. If this entry is still here once the placement is decided, the "
                     "wiring was forgotten — which is exactly what this guard exists to say.",




    "oversize_by_lane": "#748 — lane-aware oversize. An oversize stop sells shares that are not "
                        "there and FLIPS the position; the existing correction measures per "
                        "INSTRUMENT and shrinks largest-first, which is right for one aggregate stop "
                        "per leg and wrong in two directions for one stop per LANE. It picks the "
                        "wrong victim (MANUAL flat with a 2-stop, MOMENTUM holding 54 with a "
                        "54-stop: excess 2, largest-first cuts MOMENTUM's CORRECT stop to 52 and "
                        "leaves the orphan resting) and, worse, it fires NOTHING when the instrument "
                        "nets out while a lane over-covers (MOMENTUM 44 held against its 54-stop "
                        "flips it short 10 on trigger, every instrument-level detector green).\n"
                        "REFUSES RATHER THAN GUESSING: if any resting protective order cannot be "
                        "attributed, no lane's coverage is known and the whole leg is reported "
                        "undecidable, because shrinking on unknown numbers is how a correctly-sized "
                        "stop gets cut.\n"
                        "NOT WIRED. Depends on `coverage_by_lane` and must land with it and the "
                        "cancel-path change — see the `split_by_lane` entry for why activating any "
                        "one alone is unsafe.",

    "split_by_lane": "#748 — the per-lane source. Joins the two planes the protective-stop split "
                     "needs and neither of which can do it alone: the BROKER anchors the total "
                     "(`broker_rows` sets `strategy_id: \"\"` because the broker does not know our "
                     "sleeves) and the CACHE supplies the split (the sweep plans from broker state "
                     "deliberately, #285, because a cache-based coverage check once answered "
                     "'nothing resting' and the next pass placed a duplicate stop). Where the two "
                     "disagree the instrument is NOT split — it falls back to one aggregate row and "
                     "reports the divergence, because splitting on numbers that do not describe the "
                     "shares the venue holds would attribute real shares to a lane that does not "
                     "hold them.\n"
                     "IT IS DELIBERATELY NOT WIRED, AND WIRING IT NOW WOULD BE UNSAFE. Review of "
                     "a764610 established that the old one-stop-per-leg rule was load-bearing for "
                     "three other mechanisms, and all three break the moment a real per-lane source "
                     "exists: `release_for_exit`'s cancel-confirm goes VACUOUS (the native pass "
                     "filters on MANUAL-001, the stop carries the lane, so "
                     "`_await_reducing_orders_clear` returns True having confirmed nothing — and on "
                     "a venue that does not reserve shares the second gate skips too); oversize is "
                     "instrument-level with largest-first victim selection, so it shrinks the wrong "
                     "lane's correctly-sized stop; and `unprotected_positions` credits coverage "
                     "PRO-RATA, so per-lane shortfalls are wrong at the source. This function is the "
                     "foundation those three fixes build on, and it lands first so they can be "
                     "written against something tested rather than imagined.\n"
                     "WIRE IT ONLY WITH THOSE THREE, NOT BEFORE. If #748 is abandoned, delete it "
                     "rather than leave it standing as coverage.",


    # --- #734: THE FIRST SLICE OF A MULTI-STEP BUILD. Wired by the next commit, not this one. -----

    # `should_capture`, `capture_enabled` and `failed_manifest_row` were here too, and went the same
    # way when the engine hook landed and actually CALLED them. This guard is now the wiring test for
    # `schedule_capture`: the repo's own note on `_schedule_bar_drain` says asserting that
    # `set_timer` appears somewhere in `on_start` did not bite when the registration was disabled,
    # because the text survives inside a dead branch. "Has a caller in production" is the check that
    # does bite, and it is general rather than a scan somebody has to keep correct.
    #
    # `observation_rows` and `manifest_rows` were here and are NOT any more: the backfill runner
    # calls both, so the guard's staleness half told me to delete these entries. That direction is
    # the one an allowlist normally gets wrong — an exemption that has quietly become false reads
    # like a considered decision forever.





    # --- #425: BUILT AS CANDIDATES, awaiting the operator's build/remove decision. Not defects. ---------
    "bedtime_trim": "#425 candidate — bank half a green position before the operator sleeps. "
                    "Measured, built, and NOT wired pending the build/remove decision.",
    "open_window_stop": "#425 candidate — widen the stop through the opening auction. 28 of 64 "
                        "stop-exits fired in the first 15 minutes and 89% reclaimed their own stop "
                        "price the same day. Pending the same decision.",
    "ratchet_stop": "#425 candidate — ATR-based stop distance replacing hand-set ones. Pending.",

    "seed_projection": "SUPERSEDED, not unbuilt (#568). The restart seed moved into "
                       "`UiFeedStrategy._load_seed` + `_apply_seed`, which the old helper could not "
                       "become: the load runs OFF the node loop (it waited 4m47s for a slot there), "
                       "against its own NullPool engine (the shared asyncpg pool cannot cross event "
                       "loops), with a deadline the blocking Redis scan actually honours, and the "
                       "apply happens under `_projection_lock` because `project()` mutates. Its tests "
                       "still pin per-strategy scoping, which is why it is kept rather than deleted "
                       "at 04:00. Delete it, and fold that scoping assertion into the new path.",

    # --- Genuinely unwired. Each is a surface nothing renders, not a mechanism nothing runs. -----
    "format_breach": "claims formatting — `/claims` (#473) returns structured rows and session_watch "
                     "formats its own line, so this duplicates both. Delete or adopt; do not leave.",
    "claim_conflicts": "multi-strategy claim detector — REDUNDANT with kumo-trading-strategies' "
                       "`over_claimed`, which cockpit imports and drives via `/claims`. Two "
                       "derivations of one fact; keep the imported one.",
    "over_budget_strategies": "'which strategy is refusing entries' for the operator surface. "
                              "Nothing renders it — the UI cannot say WHY an entry was denied.",
    "over_committed": "targets summing above the account. Nothing renders it.",
    "universe_age_days": "staleness of QC345's universe. Nothing renders it — AND it measures the "
                         "settings-file mtime, so ANY settings write resets it (writing a forced "
                         "rebalance date on 2026-08-23 made it read 1.0 day). Wrong quantity too.",
    "is_stamped": "whether an image can name its own commit. `make verify-stamp-paper` does this "
                  "from outside; nothing inside asks.",
    "assign_tag": "allocation entry point for a strategy NAME — upstream never adopted it.",
    "by_external_id": "registry lookup by external id — no caller yet.",

    # --- Superseded siblings. A dead twin beside a live one is the two-derivations trap waiting. ---
    "may_cancel": "SUPERSEDED by `may_cancel_order`, which is wired (#467) with 2 engine call sites. "
                  "Its own module's docstring says owner-alone is too wide. Delete it.",
    "realized_from_fills": "SUPERSEDED by `realized_by_period`, which the engine drives. Mentioned "
                           "only in a docstring beside `open_lots_after`.",
    "apply_transfer": "pure transfer application. `budget_store.on_sell_fill` is the live path.",

    # --- Built, never driven. Same category as the nine. --------------------------------------
    "conforms": "#438's contract check — 'does this candidate satisfy the strategy contract'. Nothing "
                "asks. #438 was closed because `run_boot_gate` got wired; this is the OTHER half and "
                "it is still unwired.",
    "cancel_if_cancelable": "the manager toggle's OFF path. PR #244 is an open DRAFT titled 'make the "
                            "manager toggle's OFF actually work' — this is why.",
    "check": "narrative check — does the journal's own words explain an exit. Research surface.",
    "render": "renders that narrative. Same.",
}


def _public_functions_and_refs():
    files = [p for r in ROOTS for p in pathlib.Path(r).rglob("*.py") if not p.name.startswith("test_")]
    defined: dict[str, pathlib.Path] = {}
    decorated: set[str] = set()
    trees: dict[pathlib.Path, ast.AST] = {}
    for p in files:
        try:
            trees[p] = ast.parse(p.read_text())
        except SyntaxError:                     # a file we cannot parse cannot be judged
            continue
        for node in trees[p].body:              # MODULE LEVEL only — a method has a class to find it by
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                defined.setdefault(node.name, p)
                if node.decorator_list:
                    # @app.get, @app.websocket, @contextmanager — the framework is the caller.
                    decorated.add(node.name)
    # REFERENCES BY AST, not by substring: `count("exit")` matches `broker.exit`, `_exits`, and the
    # word in a docstring, and a scan that counts prose finds every mechanism perfectly wired.
    refs: set[str] = set()
    for p, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs.add(node.id)
            elif isinstance(node, ast.Attribute):
                refs.add(node.attr)
            elif isinstance(node, ast.alias):
                # BOTH names. `from api.protection import is_resting as _is_resting` is a real use of
                # `is_resting`; recording only the asname reported a wired function as an orphan, and
                # a detector with false positives is one whose failures get waved through.
                refs.add(node.name.rpartition(".")[2])
                if node.asname:
                    refs.add(node.asname)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                refs.add(node.value)            # `__all__` entries and getattr-by-name
    # A `def` produces no `ast.Name` for its own name, so nothing has to be subtracted here — a
    # function referenced ONLY by its own definition is exactly what `refs` will not contain.
    return defined, decorated, refs


def _orphans():
    defined, decorated, refs = _public_functions_and_refs()
    return sorted((name, home) for name, home in defined.items()
                  if name not in decorated and name not in refs)


def test_the_scan_can_actually_find_an_orphan() -> None:
    """Assert the detector's own property first. If the reference scan were too permissive it would
    report zero orphans on any codebase and every assertion below would pass vacuously — which is
    exactly how the truncation-invariance test in kumo-trading-strategies passed twice with the bug in."""
    names = {n for n, _ in _orphans()}
    assert names, "the scan finds NO orphans at all — it is not discriminating"
    assert "bedtime_trim" in names, (
        "the scan cannot see a known orphan — #425's bedtime_trim is built and deliberately unwired"
    )


def test_no_public_function_is_orphaned_without_a_written_reason() -> None:
    """A NEW orphan is the failure this exists to prevent. Nine cost a week."""
    surprises = {n: str(p) for n, p in _orphans() if n not in ALLOWED}
    assert not surprises, (
        "public function(s) with no caller anywhere in production:\n  "
        + "\n  ".join(f"{n} ({p})" for n, p in sorted(surprises.items()))
        + "\n\nWire it, delete it, or add it to ALLOWED with the reason. Nine of these were found by "
          "hand on 2026-08-23; each had been built, tested and documented with the incident it caught."
    )


def test_the_allowlist_does_not_go_STALE() -> None:
    """The other direction, and the one an allowlist normally gets wrong. An entry that has since been
    wired must be removed, or the list slowly becomes a place names go to be forgotten."""
    live = {n for n, _ in _orphans()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, (
        f"these are no longer orphans and their ALLOWED entries are now false: {stale}"
    )
