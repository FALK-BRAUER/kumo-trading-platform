"""The one caller: fetch once, gate, then write history (#734, and the fix #738 describes).

WHY THIS IS A MODULE AND NOT TWO CALL SITES. `verify_at_now` and `backfill_sessions` each take
activities and a resolver, and review's finding was that nothing bound the two calls. Verify against a
full fetch, run the backfill against one truncated by a lookback window, and `agrees` is HONESTLY
true while every reconstructed day is wrong — with nothing in either call looking wrong. The
activities half is bound by a fingerprint. The RESOLVER half cannot be: it is a function, and hashing
one checks identity rather than behaviour, which would refuse a legitimate re-wrap and pass a stale
closure — the worst of both.

So the binding here is STRUCTURAL. Each input is built exactly once and the SAME object goes to both.

IT RUNS IN THE ENGINE PROCESS, and that is forced rather than chosen: the gate compares against the
live Nautilus cache, and a separate CLI process does not have one. Every input it needs is already on
this actor — the activity ledger over `_http`, the venue-order join the realized sweep already uses,
the instrument namespace from the cache itself, and the cache for the gate.

THIS REVERSES A LINE I WROTE. `eod_backfill_runner`'s ALLOWED entry said the backfill is "NOT
reachable from the API: writing history is a deliberate one-off, and a backfill that any request
could trigger would be a write path with no operator behind it". The concern is right and the
conclusion was wrong — reachability is not the hazard, an UNATTENDED write path is. This is gated OFF
by default, has no UI affordance, and an operator has to set an environment flag before the endpoint
exists at all. The alternative designs were worse: running it automatically at boot removes the
operator entirely, and a separate CLI process cannot reach the cache the gate depends on, so it would
have to trust a stale verdict.
"""

from __future__ import annotations

from api.eod_backfill import verify_at_now
from api.eod_backfill_runner import BackfillReport, backfill_sessions
from api.realized_broker import FILL_TYPE


def instrument_map(cache):
    """Bare broker symbol -> the engine's own instrument id.

    THE CACHE IS THE AUTHORITY, because it is what `observe_lanes` keys on. Any other source
    guarantees the two sides of the gate disagree about names while agreeing about quantities, which
    is the failure that made the gate structurally unable to pass.

    BUILT FROM `InstrumentId.symbol`, NEVER FROM `str(iid).split(".")[0]`. The live database holds
    both `BRK.B` and `BRKB`, so that parse yields `BRK` for `BRK.B.XNYS` — a different company. The
    parse is what a hurried fix reaches for; Nautilus already exposes the field.

    An unknown symbol resolves to None, and `reconstruct_lanes` then SKIPS and names it. Returning the
    bare symbol instead would write a value into a column every other table fills with the qualified
    form — one that joins to nothing forever while looking perfectly fine in the row.
    """
    by_symbol: dict[str, str] = {}
    for iid in cache.instrument_ids():
        by_symbol[str(getattr(iid, "symbol", "") or "")] = str(iid)
    return lambda symbol: by_symbol.get(str(symbol))


def strategy_map(cache):
    """A broker fill -> the strategy that submitted it, joined on the VENUE order id.

    THE SAME JOIN THE REALIZED SWEEP ALREADY USES, deliberately. Broker FILL activities carry the
    venue order id and no strategy tag; Nautilus's cached orders carry both and survive restarts via
    the AOF. Measured coverage: 130 of 130 fills from 2026-08-17 onward, about 1% before it. A second
    implementation would drift from the number the realized panel reports.

    No join means UNCLAIMED, never a guess — #292 is explicit that money whose opener is unknowable
    must not be credited to whoever closed the position.
    """
    by_venue_order: dict[str, str] = {}
    for order in cache.orders():
        vid = getattr(order, "venue_order_id", None)
        if vid is None:
            continue
        by_venue_order[str(vid)] = str(order.strategy_id)
    return lambda fill: by_venue_order.get(str(fill.get("order_id") or ""))


async def run_backfill(
    actor,
    store,
    *,
    sessions,
    ledger_start: str,
    #: The instant the GATE certifies — today's ET date, because `verify_at_now` compares the
    #: reconstruction against the LIVE cache and the live cache is today's book. REQUIRED, with no
    #: default: it used to derive `sorted(sessions)[-1]`, which truncated the ledger at a PAST date
    #: and compared that to the present book. Any fill since made the gate disagree for a reason
    #: unrelated to the data, and when it passed for a past date it passed by COINCIDENCE — because
    #: nothing had traded since. Every test fixtured `cache == book(last session)`, so the difference
    #: was invisible. The plausible default was the whole defect.
    gate_as_of: str,
    marks_for,
    currency: str,
    snapshot_ts_for,
    known_lanes=(),
) -> BackfillReport:
    """Gate against the live cache, then write the sessions — from ONE fetch and ONE resolver.

    `ledger_start` is the caller's statement of what the fetch COVERS, never inferred from the
    earliest fill: that is a property of the fetch and not of the account, so a truncated ledger would
    report its own truncation point as inception — most confident exactly where it is most wrong.
    """
    # PROVIDER NEUTRALITY, AND THE HALF THAT IS NOT. `list_activities` exists only on the Alpaca
    # client; an IBKR node has no `_http` at all, which is why `_refresh_realized_periods` already
    # degrades with "supplies no activity ledger this engine can read". Reaching through None here
    # would raise AttributeError from a line that reads like an ordinary fetch.
    #
    # WHAT STILL WORKS ON BOTH: the forward CAPTURE reads only `cache.strategy_ids`, `cache.price`
    # and the Position's own fields — Nautilus-native, no venue client — and the READ PATH reads the
    # table. Only this historical reconstruction is Alpaca-only, and on an IBKR instance the
    # consequence is that 1W/1M/3M stay dark until enough closes accumulate. That is a real
    # limitation and it is REFUSED BY NAME rather than discovered as a stack trace.
    http = getattr(actor, "_http", None)
    if http is None or not hasattr(http, "list_activities"):
        raise RuntimeError(
            "this venue supplies no activity ledger this engine can read, so history cannot be "
            "reconstructed here — the forward capture still works and 1W/1M/3M will fill in as "
            "closes accumulate. Reconstruction requires the Alpaca activity ledger."
        )
    everything = await http.list_activities(activity_type=None)
    # ONLY FILLS REACH THE MATCHER. The ledger also carries fees, withholdings and other cash
    # movements; `_match` counts sales against their opening buys, and a PTP withholding is not a
    # sale. The realized sweep needs the full list for cash reconciliation; the BOOK does not.
    fills = [a for a in everything if str(a.get("activity_type") or "") == FILL_TYPE]

    instrument_of = instrument_map(actor.cache)
    strategy_of = strategy_map(actor.cache)

    gate = verify_at_now(
        actor.cache, fills, as_of=gate_as_of,
        strategy_of=strategy_of, instrument_of=instrument_of,
    )
    if not gate.agrees:
        actor.log.error(f"eod backfill NOT RUN — the acceptance gate did not pass: {gate.verdict}")
        return BackfillReport(gate=gate, sessions=())

    report = await backfill_sessions(
        store, fills, sessions,
        gate=gate,
        marks_for=marks_for,
        strategy_of=strategy_of,
        instrument_of=instrument_of,
        currency=currency,
        ledger_start=ledger_start,
        known_lanes=known_lanes,
        snapshot_ts_for=snapshot_ts_for,
    )
    actor.log.info(f"eod backfill: {report.summary}")
    return report
