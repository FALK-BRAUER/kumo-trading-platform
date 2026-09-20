"""What the composed pool carries that the venue does not list (#723). Decision only — no I/O.

#663 validates at cockpit's `POST /pool/source/{name}`. That endpoint is not where the symbols come
from: the scheduled refresh runs inside the engine (`pgjobs.py:128 -> PgSymbolPool.refresh_source`),
calls the same writer with no HTTP hop, and is therefore unguarded. Measured on an Alpaca paper instance 2026-08-29,
`ledger_book` refreshed at 10:32 — an hour after #663 deployed — still carrying BLLLN, IQVIA, JEPO
and OVVI.

SWEEP THE RESULT, NOT THE WRITERS. Guarding a door only ever covers the doors you know about, and
this problem has already produced three: the engine refresher, cockpit's endpoint, and an operator
PIN (GTLAB, an `exec_pool_override` row from 2026-08-11 that no feed wrote and no source validation
can remove). `PgSymbolPool.effective()` is the composed answer — sources plus pins minus excludes —
so asking it covers all three and whatever the fourth turns out to be.

REPORT, NEVER MUTATE. Writing an exclude would be a trading-data change decided by a REST call that
can fail, and `must_liquidate` turns an exclude into a SALE at the next session. The oracle here is
good enough to name a suspect and not good enough to trade on: #720 measured this catalog reporting
BOX, DBX, GEN and NSIT as listed and tradable while both lanes fail to resolve them, so the two
readings of "what can this account trade" are known to disagree today.

THE COUNT IS THE POINT. Nine permanent entries in the unresolved list are what makes the TENTH
invisible. That cuts both ways: a sweep that re-pages the standing set every 30 seconds is muted
within a day and then the tenth is invisible again. Per-symbol keys, so the standing set pages once
each and a new arrival is still news.
"""

from __future__ import annotations

from api.notify import Alert


def pool_sweep_alerts(verdict, *, total: int) -> dict[str, Alert]:
    """Currently-true unlisted pool symbols, keyed by condition. Empty when there is nothing to say.

    Keyed per SYMBOL for the same reason drift is keyed per symbol: a single `pool_unlisted` key
    would announce the first name and stay silent through every one after it, which is precisely the
    failure this sweep exists to end.

    A verdict that is not "ok" returns nothing. That is the third state — the catalog could not be
    consulted — and it is NOT a clean pool: an IBKR instance has no Alpaca catalog at all, so
    treating "never told us" as "nothing wrong" would make silence here meaningless everywhere. The
    caller says so in the log; what it must not do is alarm on symbols nothing verified.
    """
    if verdict.status != "ok" or not verdict.refused:
        return {}
    out: dict[str, Alert] = {}
    for sym in verdict.refused:
        twin = verdict.suggestions.get(sym)
        # NO SUGGESTION IS NOT "NO NEAR SPELLING". `_likely_twin` names a twin only when the
        # catalog holds EXACTLY ONE candidate, because a wrong suggestion invites an operator to
        # pin a symbol nobody checked. Measured live 2026-08-29: JEPO has four near neighbours
        # (JEPI, JEPQ, CEPO, JPO) and OVVI has two (OVV, WVVI) — both are misreads with an obvious
        # human answer and no safe machine one. Wording this as "no near spelling exists" would
        # state the opposite of the truth for the two symbols it most often prints.
        fix = (f"\n\nThe venue catalog has *{twin}*, one edit away — the likely correction."
               if twin else
               "\n\nNo single correction is safe to name: the catalog holds either no near "
               "spelling or several, so this needs a human verdict.")
        out[f"pool_unlisted:{sym}"] = Alert(
            title=f"Pool carries {sym}, which the venue does not list",
            # The COUNT and the DENOMINATOR both, because "1 unlisted" and "1 of 118" are different
            # facts and only the second says whether the pool is healthy around it.
            body=(f"*{sym}* is in the composed pool and the venue's asset catalog has no such "
                  f"symbol ({len(verdict.refused)} of {total} pool symbols).{fix}\n\n"
                  f"The ranking can select it and the lane will then silently not trade it. "
                  f"Nothing here has been changed — fix it at the source or with an operator "
                  f"exclude."),
        )
    return out
