"""Adopt standing positions into the claims ledger (#540).

`_claim` fires on ACCEPT, so a lane claims only what it opened after the wiring landed. QC345-003
held five positions on 2026-08-29 and claimed none of them: the book predates the wiring and
nothing back-filled it. An unclaimed holding is invisible to every ceiling that sizes off claims,
it is why DELL reads dual-held with one claimant (#541), and it is the same ledger whose
disagreement with the cache refused the BDX exit (#692).

THREE STATES, NOT TWO. A position is CLAIMED (leave it — a lane that added to a position has an
entry that means something the broker's average does not), ADOPTABLE (the broker supplies an
entry — claim it at that entry), or UNKNOWABLE (no broker entry — name it and leave it). A guessed
entry is worse than a missing claim: it drives the give-back and peak rules, and seeding from
today's price asserts a peak that never happened (kumo-cockpit#197 B1).

NEVER RAISES. It runs before a decision; a bookkeeping failure must not abandon a session (#377).
"""

from __future__ import annotations

import logging

#: Quantities are share counts; this only guards float noise from the min() above.
_EPS = 1e-9

_log = logging.getLogger("kumo.claims_backfill")


class Refused(str):
    """`_record_claim`'s answer when nothing was adopted: falsy, and it SAYS WHY. Two refusals exist
    — the lane holds nothing under the lock, or another writer's row already stands — and one label
    for both was wrong for the second (review, #848). The reason travels with the value so the
    sweep cannot re-derive it wrongly."""
    __slots__ = ()

    def __bool__(self) -> bool:
        return False


async def _record_claim(journal, strategy_id: str, symbol: str, qty, entry_px: float, *,
                        reread) -> int | Refused:
    """Adopt `qty` of `symbol` — DECIDED UNDER THE KEY'S LOCK, not from the sweep's up-front read.

    THE DELETE-RACE (kumo-cockpit#845, codex scope review). The sweep reads "held 6, no claim" and
    writes later; a fill's `sync_claim(..., 0, ...)` landing in between deletes the row and takes the
    lane's cache position to 0. `ON CONFLICT DO NOTHING` cannot help — after the DELETE the row is
    absent, and adoption would re-insert exactly the resurrected row #845 measured. So the write goes
    through `store.write_claim`, which queues it behind that drop in call order, and `build` re-reads
    the lane's quantity AS IT IS THEN: nothing held now → nothing adopted, whatever the sweep saw.

    `reread()` -> `(attributed_now, account_net_now)`, REQUIRED — a default here would be the
    pre-#845 behaviour with no warning (review). The adopted quantity is capped by both, for the same
    reason the sweep caps it.

    Returns the quantity actually adopted, or a falsy `Refused` naming which of the two refusals
    happened: nothing held under the lock, or a row that already existed there (a sync landed
    between the sweep's read and this write — the row is theirs, not an adoption, so it is not
    counted as one).

    Indirection kept so the sweep's DECISIONS can be driven in a test without a live journal.
    """
    from sqlalchemy import text as _text

    from kumo_strategies.runtime.executor.store import canonical_symbol, claim_upsert, write_claim

    adopted: dict = {"qty": 0, "why": None}
    sym = canonical_symbol(symbol)

    async def _build(session):
        existing = (await session.execute(
            _text("SELECT symbol, qty FROM exec_position_state WHERE strategy_id = :sid AND symbol = :sym"),
            {"sid": strategy_id, "sym": sym})).all()
        if existing:
            # A writer got here first; its row stands, nothing adopted — and NOT "held none".
            adopted["why"] = Refused("a row stands under the lock — another writer's, not an adoption")
            return []
        take = float(qty)
        attributed_now, net_now = reread()
        take = min(take, float(attributed_now or 0), float(net_now or 0))
        if take <= _EPS:
            _log.warning("%s: NOT adopting %s — the lane held %g at the sweep's read and holds none "
                         "under the lock (a fill landed in between)", strategy_id, symbol, qty)
            adopted["why"] = Refused(f"held {float(qty):g} at read, none under the lock")
            return []
        adopted["qty"] = int(take)
        return [claim_upsert(strategy_id, sym, int(take), entry_px, only_if_absent=True)]

    await write_claim(journal, strategy_id, sym, build=_build)
    if adopted["qty"]:
        return adopted["qty"]
    assert adopted["why"] is not None, "refused without a reason"     # build ran one of two branches
    return adopted["why"]


async def _existing_claims(journal, strategy_id: str) -> dict:
    """This lane's current claims, {symbol: qty}. Empty on any failure — see the caller: an
    unreadable ledger means the sweep does nothing rather than re-claiming what may already exist."""
    from sqlalchemy import text

    async with journal.sessionmaker() as s:
        rows = (await s.execute(
            text("SELECT symbol, qty FROM exec_position_state WHERE strategy_id = :sid"),
            {"sid": strategy_id},
        )).all()
    return {str(r[0]): float(r[1]) for r in rows}


async def backfill_claims(broker, journal, strategy_id: str) -> dict:
    """Claim every attributed holding that has no claim row.

    Returns {"status": "ok"|"unreadable", "adopted": n, "capped": [...], "unknowable": [...]}.

    THREE STATES, NOT A COUNT (review, 2026-08-29). The first version returned 0 for "nothing to
    adopt", "everything already claimed" AND "the broker or ledger read FAILED", the caller
    discarded it, and a sweep that had never once worked looked exactly like a healthy one — on the
    ledger every sizing ceiling reads. "0 of 0 is not 0 of 4".
    """
    try:
        held = {str(k): float(v or 0) for k, v in (broker.strategy_positions() or {}).items()}
        entries = {str(k): float(v) for k, v in (broker.position_entries() or {}).items()}
        # CAPPED BY THE NODE'S NET FOR THE SYMBOL — and be precise about what that is (review
        # round 2): `broker.positions()` sums the SAME cache, grouped by instrument instead of by
        # strategy. It is NOT the broker's account read, and calling it one would be prose that
        # reads as a safety property it does not have. What it defends against is real and is the
        # measured shape: per-lane attribution EXCEEDING the netted quantity (CGAU, one lane LONG
        # 168 beside another SHORT 88 over ~80). What it cannot defend against is a cache wrong as
        # a whole versus the broker — both terms move together there, which is why the drift
        # detector (#26) and the split detector (#692) exist beside this.
        # Adopting the attributed number writes a claim bigger than the position, and
        # `own_ceiling` then collapses every OTHER holder's exit ceiling toward zero: the BETA/WHD
        # freeze, minted by this sweep, and unretirable because retire_claims keeps a positive
        # claim on a positively-held symbol. An unreadable read adopts NOTHING.
        #
        # NOT SUBTRACTED HERE: other lanes' existing claims. `_existing_claims` reads only this
        # lane's rows, so a stale foreign claim can still leave sum(claims) > net — a partial
        # freeze this cap does not prevent. Tracked with the ownership work (#437/#692) rather
        # than guessed at from one lane's view.
        account = {str(k): float(v or 0) for k, v in (broker.positions() or {}).items()}
        claimed = await _existing_claims(journal, strategy_id)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not stop a session (#377)
        _log.warning("%s: claims back-fill FAILED (%r) — standing positions stay unclaimed and "
                     "every claim-based ceiling is blind to them", strategy_id, exc)
        return {"status": "unreadable", "adopted": 0, "capped": [], "unknowable": [], "error": repr(exc)}

    adopted = 0
    unknowable: list[str] = []
    capped: list[str] = []
    #: NAMED, NOT SILENT (review, #845): an adoption refused under the lock — the lane held the
    #: symbol at the sweep's read and none by the time the write ran — must not leave the result
    #: byte-identical to "everything already claimed". "0 of 0 is not 0 of 4".
    refused: list[str] = []
    failed: list[str] = []
    for symbol, qty in sorted(held.items()):
        if symbol in claimed:
            # A CLAIM THAT EXISTS IS LEFT ALONE — including one smaller than we hold.
            #
            # The first version of residual C's fix healed any claim below min(attributed, net),
            # and that predicate cannot tell "this sweep capped it" from "this claim is
            # deliberately smaller". Both live on paper right now, and healing the second FREEZES
            # another lane: MOMENTUM-002 claims BDX 45 against 55 attributed, so a heal to 55 puts
            # BDX claims at 65 against a net of 55 and `own_ceiling` gives BCTROT-004 zero — the
            # #692 BDX freeze, minted by the sweep and unretirable, because retire_claims keeps a
            # positive claim on a positively held symbol. A partial exit produces the same shape:
            # an accepted-but-unfilled sell does not move the position, and `run()` re-enters this
            # sweep on resume.
            #
            # So a capped adoption stays capped until something records WHY it was capped. That
            # marker does not exist yet (the claims row is kumo-strategies' schema) and inventing
            # one from an inequality is guessing. Refuse rather than guess.
            continue
        if qty < 0:
            # A SHORT attributed to a long-only lane is not "nothing to do" — it is the #635 mirror
            # shape, and it was silently skipped. Named, never claimed.
            capped.append(f"{symbol}(SHORT {qty:g} — not adopted)")
            continue
        if qty == 0:
            continue
        entry = entries.get(symbol)
        if entry is None:
            unknowable.append(symbol)
            continue
        net = account.get(symbol, 0.0)
        take = min(qty, net)
        if take <= 0:
            # Attributed to us, but the ACCOUNT does not hold it — a phantom leg or a stale
            # attribution. Named below rather than claimed: a claim on shares the account does not
            # have is the freeze this cap exists to prevent.
            capped.append(f"{symbol}(attr {qty:g} vs net {net:g})")
            continue
        if take < qty:
            capped.append(f"{symbol}({qty:g}->{take:g})")
        try:
            got = await _record_claim(
                journal, strategy_id, symbol, int(take), entry,
                # RE-READ UNDER THE LOCK — see `_record_claim`. Both numbers, because both cap.
                reread=lambda sym=symbol: ((broker.strategy_positions() or {}).get(sym, 0),
                                           (broker.positions() or {}).get(sym, 0)))
            if got:
                adopted += 1
            else:
                refused.append(f"{symbol}({got})")
        except Exception as exc:  # noqa: BLE001
            # NAMED IN THE RESULT, not only logged. `store.write_claim` can stall (ClaimWriteStalled,
            # kumo-strategies#124) and a Postgres error can land on any write; either way this
            # position stays UNCLAIMED and invisible to every claim-based ceiling. Swallowed here, a
            # sweep whose every write failed read byte-identical to one with nothing to do
            # (review, #848). The session journals `failed` as a RISK row.
            failed.append(f"{symbol}({type(exc).__name__}: {exc})")
            _log.warning("%s: could not adopt %s (%r)", strategy_id, symbol, exc)
    if unknowable:
        # NAMED, NOT COUNTED, and not claimed at a guessed price: these positions stay outside the
        # ledger and the operator can see which ones.
        _log.warning(
            "%s: %d held position(s) carry no broker entry price and were NOT claimed: %s — "
            "they remain invisible to claim-based ceilings",
            strategy_id, len(unknowable), ", ".join(unknowable))
    if capped:
        # NAMED, because a silently shrunk claim is a number nobody can explain later.
        _log.warning(
            "%s: %d adoption(s) were capped by the ACCOUNT net or refused entirely: %s — the "
            "cache attributes more than the account holds, which is the #635 mirror shape",
            strategy_id, len(capped), ", ".join(capped))
    if adopted:
        _log.warning("%s: adopted %d standing position(s) into the claims ledger (#540)",
                     strategy_id, adopted)
    if failed:
        _log.error("%s: %d adoption(s) FAILED to write and remain UNCLAIMED: %s",
                   strategy_id, len(failed), ", ".join(failed))
    return {"status": "ok", "adopted": adopted, "capped": capped, "unknowable": unknowable,
            "refused": refused, "failed": failed}
