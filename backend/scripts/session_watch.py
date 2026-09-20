"""Session monitor: one compact line plus anomalies, counted from the BROKER (#390).

WHY THIS IS IN THE REPO. It used to be a shell script in a session scratchpad, which is how it shipped
three defects into the alarm channel in two days without anyone reviewing a diff:

  * #380 — it reported CASH as equity, a 25% phantom drop, on every deploy;
  * it labels pre-market as `SESSION`, so a stale-feed alarm outside regular hours reads as an outage;
  * #390 — it over-counted protection, reporting "10 held 14 stops" when the broker had nine.

A number that is wrong in the alarm channel is worse than no number: it trains the operator to discount
the channel. Monitoring code is production code and gets reviewed and tested like production code.

WHERE THE COUNT COMES FROM, AND WHY IT CHANGED. The old version derived protection from `GET /orders` —
the cockpit's own cache — by filtering working SELL orders whose type contained "STOP". The cache holds
orders the venue does not (the engine holds orders as REJECTED that Alpaca reports OPEN, and Nautilus
refuses REJECTED -> ACCEPTED so they never recover), and it holds an OUO take-profit leg beside each
stop. So the count inflated exactly when cache and broker disagreed — which is precisely when the number
needs to be trustworthy.

Measured on a live instance paper account, 2026-08-21, at the same instant:

    cache-derived  (old):  17 stop orders across 11 symbols
    broker-derived (new):  14 protected cycles across 8 symbols, 2 unprotected, 0 unknown

`broker_protected` on the trade-cycle DTO is the engine's own broker read — the same one the SECURED
badge and the protection reconciler use. Reading it here means the monitor and the cockpit cannot
disagree, which is the property #390 actually asked for.

IT IS ON THE CYCLE, NOT THE POSITION. `GET /positions` returns `PositionDTO`, which carries only
`instrument_id, quantity, side, avg_px_open, realized_pnl, strategy_id` — no protection field at all.
The three-state answer exists only on `GET /trades`. A monitor reading `/positions` cannot see broker
protection no matter how it counts, which is why this reads the trades plane.

THREE-STATE, NOT TWO. `None` means the broker has not been asked. It is reported as UNKNOWN and never
folded into either side: treating "not asked" as "unprotected" cries wolf on every startup before the
first sweep, and treating it as "protected" is the silencing direction. Both have shipped here before.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass, field


from api.ownership import short_violations

DEFAULT_API = "http://localhost:8000"

#: Order states the CACHE considers live. Retained only for the `working` count in the status line —
#: never for protection. See the module docstring for why.
_WORKING = {"ACCEPTED", "PARTIALLY_FILLED", "PENDING_NEW", "SUBMITTED", "TRIGGERED"}


@dataclass
class Protection:
    """Protection coverage, counted by SYMBOL and derived from the broker."""

    protected: set[str] = field(default_factory=set)
    unprotected: set[str] = field(default_factory=set)
    unknown: set[str] = field(default_factory=set)

    @property
    def held(self) -> int:
        """Distinct symbols with a live position — the denominator every other number is against."""
        return len(self.protected | self.unprotected | self.unknown)

    def alerts(self) -> list[str]:
        out: list[str] = []
        if self.unprotected:
            out.append(f"UNPROTECTED {len(self.unprotected)}/{self.held}: {','.join(sorted(self.unprotected))}")
        if self.unknown:
            # Not an outage and not a naked position — the broker has not been asked yet. Saying so is
            # the point: silence here would be indistinguishable from "everything is covered".
            out.append(f"PROTECTION UNKNOWN {len(self.unknown)}: {','.join(sorted(self.unknown))}")
        return out

    def summary(self) -> str:
        s = f"{self.held} held {len(self.protected)} protected"
        if self.unknown:
            s += f" {len(self.unknown)} unknown"
        return s


def protection_from_trades(trades: list[dict]) -> Protection:
    """Coverage per SYMBOL from the trade-cycle plane's `broker_protected`.

    BY SYMBOL, NOT BY CYCLE. One symbol can carry several cycles — on 2026-08-21 the account had 14
    protected cycles across 8 symbols — and a protective stop is placed against the account's net
    position in an instrument, not against a cycle. Counting cycles reports more stops than exist, which
    is the #390 shape arriving by a second route.

    A symbol is UNPROTECTED if any of its cycles says so: a stop covering one cycle's shares does not
    cover another's, and the pessimistic read is the safe one for an alarm. UNKNOWN only when nothing
    positive or negative is known about the symbol at all.
    """
    protected: set[str] = set()
    unprotected: set[str] = set()
    unknown: set[str] = set()

    # NET THE SIGNED QUANTITY PER SYMBOL FIRST (2026-08-21). A cycle's `quantity` is unsigned and the
    # direction lives in `side`, so two opposing cycles on one instrument both look live. On 2026-08-21
    # WHD carried BCTROT-004 LONG 28 and MOMENTUM-002 SHORT 28 — genuinely flat, and the broker held
    # zero — and this counted it as a held, UNPROTECTED symbol for half a session. There is nothing to
    # protect: a protective SELL stop cannot rest under shares that are not there.
    #
    # Netting must not become a silencer, so it is computed per SYMBOL and only removes a symbol whose
    # legs cancel exactly. An unequal pair still leaves exposure and still alarms.
    net: dict[str, float] = {}
    unknown_side: set[str] = set()
    for t in trades:
        try:
            q = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        sym = str(t.get("instrument_id") or "")
        if not sym:
            continue
        # FAIL CLOSED ON AN UNKNOWN SIDE (codex review). A missing or unrecognised `side` used to
        # default to LONG, which is the SILENCING direction: a mislabelled short would cancel a real
        # long and remove the symbol from the alarm entirely. Unknown sides are excluded from netting,
        # so the symbol keeps whatever alarm its cycles earned.
        # ONLY LIVE CYCLES NET (codex review). Netting removes a symbol from the alarm, so a row that
        # is not live must never participate: a nonzero CLOSED or FLAT cycle would cancel a genuine
        # HELD one and delete the symbol. `TradeDTO` says FLAT implies quantity == 0 today, so this is
        # a guard against a future projection change rather than a live defect — but it is on the
        # silencing side, which is the one that must fail closed.
        state = str(t.get("state") or "").upper()
        if state and state != "HELD":
            continue
        side = str(t.get("side") or "").upper()
        if side not in ("LONG", "SHORT"):
            unknown_side.add(sym)
            continue
        net[sym] = net.get(sym, 0.0) + (-q if side == "SHORT" else q)

    for t in trades:
        try:
            qty = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue  # a flat cycle has nothing to protect
        sym = str(t.get("instrument_id") or "")
        if not sym:
            continue
        if sym not in unknown_side and abs(net.get(sym, 0.0)) < 1e-9:
            continue  # the symbol's legs cancel — no net exposure, nothing to protect
        flag = t.get("broker_protected")
        if flag is False:
            unprotected.add(sym)
        elif flag is True:
            protected.add(sym)
        else:
            unknown.add(sym)
    # A symbol that is unprotected anywhere is unprotected, whatever its other cycles say.
    protected -= unprotected
    unknown -= protected | unprotected
    return Protection(protected=protected, unprotected=unprotected, unknown=unknown)


def _normalise(body: object) -> dict:
    """Every non-dict answer becomes an error dict.

    A 200 with the body `null` is NOT an error and is NOT a dict. Observed live on 2026-08-22 at
    17:21 ET: mid-restart, `/account` answered 200 `null` because the engine had no account snapshot
    yet, `json.load` returned None, and `"__err__" in account` raised TypeError. THE ALARM DIED at the
    one moment it was needed — during a deploy, with three of four strategies not running — so no
    protection, short or claims check ran for the whole window.

    A bare list gets the same treatment: `/trades` and `/orders` can answer with one, and it fails
    identically one endpoint over.

    A real dict passes through UNCHANGED and by identity. Wrapping it would make every endpoint look
    unreachable and report a total outage on a healthy stack.
    """
    if isinstance(body, dict):
        return body
    return {"__err__": f"non-object body: {body!r}"}


def _get(api: str, path: str) -> dict:
    try:
        with urllib.request.urlopen(api + path, timeout=6) as f:
            # NOT normalised here. `collect` normalises at the seam, which covers this path AND the
            # injected-`fetch` path; doing it twice makes this one a branch that cannot fail, and a
            # check that cannot fail is indistinguishable from one that does not work.
            return json.load(f)
    except Exception as exc:  # noqa: BLE001 — the monitor must report unreachability, not raise
        return {"__err__": str(exc)}


def collect(api: str = DEFAULT_API, *, fetch=None) -> tuple[str, list[str]]:
    """The status line and its alerts. `fetch` is injectable so the formatting is testable offline."""
    # NORMALISE AT THE SEAM, not only inside `_get`. `fetch` is injectable for testing and every
    # caller of `collect` that supplies one would otherwise bypass the guard entirely — which is
    # exactly the shape where a fix passes its unit test and does nothing in production.
    _raw = fetch or (lambda path: _get(api, path))
    get = lambda path: _normalise(_raw(path))  # noqa: E731
    health = get("/health")
    account = get("/account")
    trades = get("/trades")
    orders = get("/orders")
    alerts: list[str] = []

    if "__err__" in health:
        return "DEGRADED", [f"API UNREACHABLE: {health['__err__']}"]
    # AN ENGINE THAT HAS TOLD US NOTHING IS NOT AN ENGINE REPORTING NOTHING WRONG (#859). Before
    # #859 every list below degraded to [] when no frame had arrived, so this monitor read drift,
    # protection and lanes off an empty payload and printed "ok" — measured three times in 30 s
    # across the 2026-09-11 00:34 recreate. The frame's presence is judged HERE, before any list is
    # consulted, on the consumer's own word (`bridge_ok`), the engine subsystem, and the lanes count
    # — never on `status` alone, which is also "degraded" for benign, live reasons (a refused
    # protection request) that the checks below exist to report.
    engine_sub = next((s for s in health.get("subsystems", []) if s.get("name") == "engine"), None)
    # ABSENT IS NOT NULL (#859, coordinator review of #883). `automated_lanes_running` is always
    # PRESENT on a live api — an int when known, null when the bridge is down. A payload without the
    # key is an older or truncated api, which is ALSO no engine frame — refused the same way, but the
    # refusal says which it was, because they are different facts about the sender.
    lanes_missing = "automated_lanes_running" not in health
    if (health.get("bridge_ok") is False or (engine_sub is not None and engine_sub.get("ok") is not True)
            or health.get("automated_lanes_running") is None):
        # EVERY down subsystem is named here, because the per-subsystem loop below is not reached on
        # this path and "engine down" must not hide "redis timeout" beside it (review of #883).
        down = [f"{s.get('name')} DOWN: {s.get('detail') or s.get('ok')!r}"
                for s in health.get("subsystems", []) if s.get("ok") is not True]
        lanes = "lanes key ABSENT from the payload (older/truncated api)" if lanes_missing else \
                f"automated_lanes_running={health.get('automated_lanes_running')!r}"
        return "DEGRADED", [
            f"ENGINE FRAME UNKNOWN — status={health.get('status')!r} bridge_ok={health.get('bridge_ok')!r} "
            f"{lanes}: the book is unknown, not clean; no drift, protection or lane figure is derived "
            f"from this payload"] + down
    for sub in health.get("subsystems", []):
        if not sub.get("ok"):
            alerts.append(f"subsystem {sub['name']} DOWN: {sub.get('detail')}")
    # PROTECTION DIVERGENCE (#454) — checked BEFORE drift, because it is the more actionable of the
    # two and the one that was invisible. Drift compares positions; this compares protective ORDERS,
    # and a book can reconcile perfectly while every stop protecting it is unknown to the cache. On
    # 2026-08-22 drift was empty and correct while eleven stops sat unseen.
    for d in health.get("protection_divergence") or []:
        coids = ", ".join(d.get("coids") or [])
        alerts.append(
            f"PROTECTION DIVERGENCE {d.get('instrument_id')}: broker holds "
            f"{len(d.get('coids') or [])} resting protective order(s) the engine cannot see "
            f"({coids}) — exits will be rejected on `available: 0` until they agree"
        )
    # LANES REGISTERED BUT NOT RUNNING (#454). Only when both numbers are KNOWN: `None` means the
    # health frame is stale, and "no lanes running" from an engine we cannot see is not a finding.
    reg, run = health.get("automated_lanes_registered"), health.get("automated_lanes_running")
    if reg is not None and run is not None and reg > 0 and run < reg:
        alerts.append(
            f"AUTOMATED LANES NOT RUNNING {run}/{reg}: the engine is up and "
            f"{reg - run} scheduled lane(s) cannot trade (MANUAL-001 is discretionary and not counted)"
        )
    if health.get("reconcile_drift"):
        alerts.append(f"RECONCILE DRIFT: {json.dumps(health['reconcile_drift'])[:300]}")

    # THE TRADES PLANE MUST SAY IT IS OK BEFORE ITS NUMBERS ARE USED (#298). A projection that failed
    # returns an empty list, and an empty list counted as "0 held" reads as a flat book — which is how
    # an empty tile stood in for eight held positions on 2026-08-14.
    status = trades.get("status") if isinstance(trades, dict) else None
    if "__err__" in trades:
        alerts.append(f"trades plane unreachable: {trades['__err__']}")
        prot = Protection()
    elif status not in ("ok", "OK"):
        alerts.append(f"trades plane status={status} err={trades.get('error')} — protection NOT counted")
        prot = Protection()
    else:
        rows = trades.get("trades") or []
        prot = protection_from_trades(rows)
        alerts.extend(prot.alerts())
        # NO STRATEGY MAY HOLD A SHORT (#437 rule 3). Checked HERE, next to protection, because the
        # two are blind in opposite directions and that is the whole point. Protection nets signed
        # quantity per symbol, so the 2026-08-21 WHD book — BCTROT +28 against MOMENTUM -28, broker
        # zero — is correctly silent: there is nothing to protect. It is also the exact signature of
        # one strategy selling another's shares, and it sat there for twelve hours reporting `ok`.
        #
        # A raise from `signed_qty_of` is NOT swallowed. It means the trades plane changed shape, and
        # a shape this cannot read must break the alarm loudly rather than quietly report no shorts.
        for v in short_violations(rows):
            alerts.append(f"SHORT HELD (long-only cockpit): {v}")

    if "__err__" in account:
        alerts.append(f"account: {account['__err__']}")
        return f"eq ? | {prot.summary()}", alerts

    equity = account.get("equity")
    last = account.get("last_equity")
    day = (equity - last) if (equity is not None and last is not None) else None
    working = [o for o in (orders.get("orders") or []) if o.get("status") in _WORKING]
    day_txt = f"{day:+,.2f}" if day is not None else "?"
    return f"eq ${equity:,.2f} day {day_txt} | {prot.summary()} | {len(working)} working", alerts


def main() -> int:
    ap = argparse.ArgumentParser(description="Session monitor (#390)")
    ap.add_argument("--api", default=DEFAULT_API)
    args = ap.parse_args()
    line, alerts = collect(args.api)
    print(line)
    for a in alerts:
        print("  !! " + a)
    if not alerts:
        print("  ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
