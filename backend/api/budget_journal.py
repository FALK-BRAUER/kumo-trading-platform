"""The budget gate's DURABLE trace: one `exec_action_log` row per decision, allow and refuse alike (#990).

A log line dies with the container (#955, #974). On 2026-09-11 the gate on staging2 had printed
"budget gate INSTALLED" at four boots and allowed every entry at notional zero for two days with no
trace anywhere; refusals were journaled by the LANES' terminal rows, allows by nobody, so "the gate
allowed this" and "the gate never ran" were the same observation on both tenants.

KIND IS `budget`, NOT `risk` — measured, not chosen: `api/session_state.py:72` and
`api/narrative/check.py` (×4) consume every `risk` row regardless of code; one row per submission
under that kind would dominate the narrative (paper: 33–80 order rows a session against 3–12 risk rows,
e70suark's count). `slot_outcome` ignores `budget` as it ignores `risk`.

Written through kumo_strategies' `PgJournal`, the same writer the lanes use, on the api's own
`session_factory` — one table, one shape. `PgJournal.write` swallows every non-integrity failure and
returns None (deliberate upstream: a journal failure must not break the trading path); this module
therefore LOGS when a row could not be written, so a silent journal cannot look like an idle gate.
"""
from __future__ import annotations

KIND = "budget"


async def journal_row(*, kind: str, code: str, strategy_id: str, symbol: str | None, summary: str,
                      detail: dict, session: str, log=None) -> int | None:
    from kumo_strategies.runtime.executor.pgjournal import PgJournal

    from api.db.engine import session_factory

    payload = dict(detail or {})
    payload["code"] = code
    row_id = await PgJournal(session_factory, strategy_id=str(strategy_id)).write(
        kind, summary, session=session, detail=payload, symbol=symbol)
    if row_id is None and log is not None:
        log.error(f"budget journal row NOT written ({code} {strategy_id} {symbol}) — the decision has no durable trace")
    return row_id
