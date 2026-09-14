"""Repair the durable Nautilus cache after corpse stops fired (#807) — dry-run by default.

    python -m scripts.repair_cache_807                      # plan only, prints what WOULD change
    python -m scripts.repair_cache_807 --apply --engine-stopped   # write, after a backup

Runs inside the engine image (it needs the installed Nautilus and the Redis network), with the
ENGINE STOPPED: the node holds the cache in memory and would neither see these edits nor survive
them being made underneath it. The API and UI containers may keep running.

    docker compose --env-file .env.paper -f compose.paper.yml stop engine
    docker compose --env-file .env.paper -f compose.paper.yml run --rm --no-deps \
        -v "$PWD/../backend/api/cache_repair.py:/app/api/cache_repair.py:ro" \
        -v "$PWD/../backend/scripts/repair_cache_807.py:/app/scripts/repair_cache_807.py:ro" \
        engine python -m scripts.repair_cache_807 [--apply --engine-stopped]
    docker compose --env-file .env.paper -f compose.paper.yml start engine

What it does, in order (all of it in `api.cache_repair`, all of it tested against the production bytes
this incident left behind): read the cache, ask the venue about every REJECTED order whose reason means
"could not ask", plan the revival + position rewrite, verify per symbol against the venue's positions,
and only then — under `--apply` — DUMP every key it will touch to a backup file and write the plan in one
transaction. A refusal on any symbol writes nothing. A second run finds nothing to do.

Venue truth comes from Alpaca (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` in the environment — the same
pair the engine reads) or from `--venue-json` for a rehearsal against a capture.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import redis

from api import cache_repair as cr


def _alpaca(path: str, attempts: int = 3) -> dict | list:
    """One venue read. A timeout is retried, then raised — the caller decides what "unanswered" means,
    and it must never mean "gone" (#354, #791: that reading is how the corpses were made)."""
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY are not set — refusing to guess the venue's state")
    base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    req = urllib.request.Request(base + path, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"venue unanswered after {attempts} attempts: {path} ({last!r})")


def venue_truth_from_alpaca(image: cr.CacheImage) -> tuple[cr.VenueTruth, dict]:
    """Ask the venue about every cached order that COULD be a corpse. An order the venue does not know
    stays out of `orders` (not "gone" — never heard of), which `find_corpses` treats as untouchable."""
    candidates = []
    for coid, events in image.orders.items():
        dicts = [cr.to_dict(e) for e in events]
        rejected = [d for d in dicts if d["type"] == "OrderRejected"]
        if rejected and any(rejected[-1].get("reason", "").startswith(r) for r in cr.CORPSE_REASONS):
            candidates.append(coid)
    orders: dict[str, dict] = {}
    unanswered: list[str] = []
    for coid in sorted(candidates):
        try:
            orders[coid] = _alpaca(f"/v2/orders:by_client_order_id?client_order_id={coid}")
        except urllib.error.HTTPError as exc:  # 404 = the venue never heard of it: answered, and "no"
            orders[coid] = {"error": f"HTTP {exc.code}"}
        except RuntimeError as exc:  # timed out: NOT answered — a third state, kept apart from both
            unanswered.append(coid)
            print(f"  unanswered: {exc}")
    positions = _alpaca("/v2/positions")
    raw = {
        "orders": orders,
        "positions": {p["symbol"]: p for p in positions},
        "unanswered": unanswered,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    return cr.VenueTruth.from_alpaca(raw["orders"], raw["positions"]), raw


def backup(r: redis.Redis, plan: cr.RepairPlan, path: Path, prefix: str) -> int:
    """DUMP every key the plan touches (restorable with RESTORE), plus the index sets whole."""
    keys = [f"{prefix}:orders:{rv.client_order_id}" for rv in plan.revivals]
    for rw in plan.rewrites:
        if not rw.unchanged:
            keys += [f"{prefix}:positions:{rw.position_id}", f"{prefix}:snapshots:positions:{rw.position_id}"]
    keys += [f"{prefix}:orders:{coid}" for coid in plan.drop_orders]  # #909: the order being DELETED is dumped too
    keys += [f"{prefix}:index:{n}" for n in ("positions", "positions_open", "positions_closed", "orders", "orders_open", "orders_closed", "order_position")]
    out = {}
    for k in keys:
        dumped = r.dump(k)
        out[k] = None if dumped is None else base64.b64encode(dumped).decode()
    path.write_text(json.dumps({"taken_at": dt.datetime.now(dt.UTC).isoformat(), "keys": out}, indent=1))
    return sum(1 for v in out.values() if v is not None)


#: The engine republishes `ui:state:health` every second with its own clock. Younger than this, the
#: node is RUNNING and holds the cache in memory — no flag from the operator overrides that.
ENGINE_HEARTBEAT_MAX_AGE_S = 30.0


def engine_heartbeat_age_s(r: redis.Redis) -> float | None:
    """Seconds since the engine last published health, or None when it never has (fresh Redis)."""
    raw = r.get("ui:state:health")
    if not raw:
        return None
    ts_ns = json.loads(raw).get("ts")
    if not ts_ns:
        return None
    return (time.time_ns() - int(ts_ns)) / 1e9


def clients_still_writing(r: redis.Redis) -> list[str]:
    """Anything on the cache prefix besides us. The engine is the only writer of `trader-*` keys; the
    API bridge only reads `ui:*` streams. Printed, and fatal under --apply unless the operator vouches."""
    return [f"{c.get('addr')} cmd={c.get('cmd')} age={c.get('age')}s name={c.get('name') or '-'}" for c in r.client_list() if c.get("cmd") not in ("client", "client|list")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--redis-host", default=os.environ.get("KUMO_REDIS_HOST", "localhost"))
    ap.add_argument("--redis-port", type=int, default=int(os.environ.get("KUMO_REDIS_PORT", "6379")))
    ap.add_argument("--prefix", default=cr.KEY_PREFIX)
    ap.add_argument("--venue-json", help="use a captured venue answer instead of asking Alpaca")
    ap.add_argument("--apply", action="store_true", help="write the plan (default: dry run)")
    ap.add_argument("--engine-stopped", action="store_true", help="operator vouches the engine container is stopped")
    ap.add_argument("--backup", default=None, help="backup file (default: /var/log/kumo/repair-807-<ts>.json or ./)")
    ap.add_argument("--plan-out", default=None, help="write the plan summary + venue capture here")
    ap.add_argument("--expect-revivals", type=int, default=None, help="refuse unless the plan revives exactly N orders")
    ap.add_argument("--drop-synthetic-leg", metavar="POSITION_ID", default=None,
                    help="#909 mode: rewrite ONE phantom pre-window leg Nautilus minted at boot to its real closed "
                         "history and drop its synthetic order; refuses unless the leg is a single S-/reconciliation "
                         "fill AND cache net minus the leg equals the venue net. Needs --venue-json (IB: a probe capture)")
    args = ap.parse_args(argv)

    r = redis.Redis(host=args.redis_host, port=args.redis_port)
    r.ping()
    image = cr.CacheImage.from_redis(r, args.prefix)
    print(f"cache: {len(image.orders)} orders, {len(image.positions)} positions, {len(image.instruments)} instruments")
    if args.venue_json:
        venue, raw = cr.VenueTruth.from_fixture(args.venue_json), json.loads(Path(args.venue_json).read_text())
    else:
        venue, raw = venue_truth_from_alpaca(image)
    print(f"venue: {len(venue.orders)} orders answered, {len(raw.get('unanswered', []))} unanswered, {len(venue.positions)} positions")

    def _plan(img, ven):
        if args.drop_synthetic_leg:
            return cr.plan_drop_synthetic_leg(img, ven, position_id=args.drop_synthetic_leg)
        return cr.plan_repair(img, ven)

    plan = _plan(image, venue)
    print("\n" + plan.summary() + "\n")
    if args.plan_out:
        Path(args.plan_out).write_text(json.dumps({"summary": plan.summary(), "venue": raw}, indent=1))
    if plan.refusals or plan.unexplained:
        print("REFUSED — nothing will be written. Fix the input, do not force the plan.")
        return 2
    # The expectation is checked BEFORE the no-op exit (codex, post-merge): `--expect-revivals 16`
    # against a plan that revives nothing must refuse, not report success.
    if args.expect_revivals is not None and len(plan.revivals) != args.expect_revivals:
        print(f"refusing: plan revives {len(plan.revivals)} orders, operator expected {args.expect_revivals}")
        return 6
    if plan.is_noop:
        print("nothing to do")
        return 0
    if not args.apply:
        print("dry run — re-run with --apply --engine-stopped to write")
        return 0
    if raw.get("unanswered"):
        print(f"refusing to write while the venue left {len(raw['unanswered'])} candidate(s) unanswered: {raw['unanswered']}")
        return 5

    writers = clients_still_writing(r)
    print("redis clients:", *writers, sep="\n  ")
    if not args.engine_stopped:
        print("refusing to write without --engine-stopped: the node holds this cache in memory")
        return 3
    age = engine_heartbeat_age_s(r)
    if age is not None and age < ENGINE_HEARTBEAT_MAX_AGE_S:
        print(f"refusing: the engine published health {age:.1f}s ago — it is RUNNING, whatever the flag says")
        return 7
    print(f"engine heartbeat age: {'never' if age is None else f'{age:.0f}s'} — stopped")
    log_dir = Path(os.environ.get("KUMO_LOG_DIR", "."))
    backup_path = Path(args.backup) if args.backup else log_dir / f"repair-807-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.json"
    n = backup(r, plan, backup_path, args.prefix)
    print(f"backup: {n} keys dumped to {backup_path}")
    written = cr.apply_plan(plan, r, args.prefix, provenance={"backup": str(backup_path), "venue_captured_at": raw.get("captured_at")})
    print(f"applied: {written} keys rewritten")

    # Verify against a FRESH venue read, not the capture the plan was built from (codex review).
    after_image = cr.CacheImage.from_redis(r, args.prefix)
    venue_after = venue if args.venue_json else venue_truth_from_alpaca(after_image)[0]
    if args.drop_synthetic_leg:
        # the leg is closed now; asking again must REFUSE, and the net must equal the venue
        try:
            cr.plan_drop_synthetic_leg(after_image, venue_after, position_id=args.drop_synthetic_leg)
        except cr.RepairRefused as exc:
            print(f"verified: a second plan refuses ({exc}) — the leg is no longer open")
            return 0
        print("VERIFY FAILED — the leg still plans after apply")
        return 4
    again = cr.plan_repair(after_image, venue_after)
    if not again.is_noop:
        print("VERIFY FAILED — a second plan against a fresh venue read is not a no-op:\n" + again.summary())
        return 4
    print("verified: a second plan, against a fresh venue read, finds nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
