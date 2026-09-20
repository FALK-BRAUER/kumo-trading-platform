"""Repair a durable Nautilus cache whose ORDER plane disagrees with the venue (#807).

THE DEFECT. A resting protective stop that reconciliation marked `REJECTED / ORDER_NOT_FOUND_AT_VENUE`
(#791's wave, #354's shape) keeps resting at the venue. When it FIRES, every fill hits
`InvalidStateTrigger: REJECTED -> PARTIALLY_FILLED, did not apply` on the order — and Nautilus
1.229.0 `execution/engine.pyx::_apply_event_to_order` then `return True  # Continue processing`,
so the fill IS applied to the position. The order never records `filled_qty`, so the venue's
cumulative figure is inferred again on the next pass (PATH: 80 + 11 + 4 + 3 + 94, the last one
reusing trade id `5641a8b1` — 192 booked against 112 sold), and every boot replays the poisoned
position list and mints an EXTERNAL counterparty to chase the broker's net. Measured 2026-09-09:
four mirrored shorts, $5,051 of holdings shown that the account did not own.

THE REPAIR, IN ORDER — and the order is the point:
  1. Make the ORDER plane agree with the venue. A corpse (REJECTED in the cache for a reason that
     means "could not ask", present at the venue) is rewritten to what the venue says it is:
     ACCEPTED if it rests, FILLED with the venue's fills if it fired. The cached events BEFORE the
     rejection are kept verbatim; the rejection and everything reconciliation appended after it are
     dropped. Fills are the live ones the cache already holds, taken in time order while they fit
     inside the venue's `filled_qty`, plus ONE synthetic remainder at the venue's average price —
     `reconciliation=True`, the flag Nautilus itself uses for a fill it did not witness.
  2. Re-derive the POSITION plane from the order plane, per instrument, splitting instances where
     the quantity crosses flat exactly as NETTING does. The position list is a projection of fills;
     once the fills are right the projection follows.
  3. Verify against the only hard anchor there is (ADR 0001): per symbol, the open instances must
     net to the venue's position. A symbol that does not reconcile REFUSES the whole plan — nothing
     is written on a guess, and "0 of 0" is not "0 of 5".

WHAT THIS IS NOT. Not a Nautilus fork: it edits the durable cache between two runs of the node,
through the kernel's own serializer (`system/kernel.py:317`, byte-identical round trip measured),
and the node rebuilds its indexes from the objects it loads (`cache.pyx::build_index`). Not a
transfer or a contra-close (#437/#779): those append legs and the corpse fills replay over them
on the next boot — measured 2026-09-08 21:06, when a hand transfer of PATH was undone in one
restart. The planner is PURE: it reads a `CacheImage` and a `VenueTruth` and returns a plan; only
`apply_plan` touches Redis, and only with the engine stopped.
"""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import msgspec
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderRejected
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.model.position import Position
from nautilus_trader.serialization.serializer import MsgSpecSerializer

#: The kernel's cache serializer — `system/kernel.py:317`, `timestamps_as_str=True  # Hardcoded for now`.
#: Anything else produces bytes the node cannot read back; the round trip is pinned in the tests.
SERIALIZER = MsgSpecSerializer(encoding=msgspec.msgpack, timestamps_as_str=True)

#: Rejection reasons that mean "the venue could not be asked", not "the venue said no". Only these
#: make a REJECTED order a candidate: a genuine venue rejection has nothing resting to revive.
CORPSE_REASONS: tuple[str, ...] = (
    "ORDER_NOT_FOUND_AT_VENUE",  # #791 — a failed batch read, read as an empty venue
    "Connection timeout",  # #354's shape on SUBMIT — the request may well have landed
)

#: Venue states under which an order still rests (Alpaca vocabulary).
VENUE_RESTING = frozenset({"new", "accepted", "held", "pending_new", "partially_filled"})
VENUE_FILLED = frozenset({"filled"})
#: Terminal at the venue with nothing resting — but a `filled_qty` > 0 on one of these is still money
#: that moved, and it is revived to CANCELED carrying those fills rather than left as a corpse.
VENUE_DONE = frozenset({"canceled", "expired", "done_for_day", "replaced", "stopped", "suspended"})

#: A synthetic remainder fill's trade id is `uuid5(venue_order_id + "-807")`: `TradeId` caps at 36
#: characters so a readable suffix cannot ride along, but the id is DETERMINISTIC — recompute it and
#: you can tell a fill this tool inferred from one the venue reported (the same reason `capture_kind`
#: exists), and running the plan twice cannot book the remainder twice.
SYNTHETIC_NAMESPACE = uuid.UUID("2b7e0a4e-5f3d-4c7a-9e1b-000000000807")


def synthetic_trade_id(venue_order_id: str) -> str:
    return str(uuid.uuid5(SYNTHETIC_NAMESPACE, f"{venue_order_id}-807"))

KEY_PREFIX = "trader-PLATFORM-001"


class RepairRefused(Exception):
    """The plan cannot be proven against the venue; nothing may be written."""


def to_dict(raw: bytes) -> dict[str, Any]:
    return msgspec.msgpack.decode(raw)


def to_obj(raw: bytes):
    return SERIALIZER.deserialize(raw)


def dict_to_obj(d: dict[str, Any]):
    """A dict in the cache's own wire shape (string timestamps) → a Nautilus object."""
    return SERIALIZER.deserialize(msgspec.msgpack.encode(d))


def obj_to_bytes(obj) -> bytes:
    return SERIALIZER.serialize(obj)


# --------------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------------


@dataclass
class CacheImage:
    """The slice of the durable cache the repair reads: raw bytes exactly as Redis holds them."""

    orders: dict[str, list[bytes]]
    positions: dict[str, list[bytes]]
    instruments: dict[str, bytes]
    index: dict[str, set[str]] = field(default_factory=dict)
    order_position: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_fixture(cls, path: str | Path) -> "CacheImage":
        d = json.loads(Path(path).read_text())
        b = base64.b64decode
        return cls(
            orders={k: [b(x) for x in v] for k, v in d["orders"].items()},
            positions={k: [b(x) for x in v] for k, v in d["positions"].items()},
            instruments={k: b(v) for k, v in d["instruments"].items()},
            index={k: set(v) for k, v in d["index"].items() if k != "order_position"},
            order_position=dict(d["index"].get("order_position", {})),
        )

    @classmethod
    def from_redis(cls, r, prefix: str = KEY_PREFIX) -> "CacheImage":
        orders = {k.decode().split(":", 2)[2]: r.lrange(k, 0, -1) for k in r.scan_iter(f"{prefix}:orders:*")}
        positions = {k.decode().split(":", 2)[2]: r.lrange(k, 0, -1) for k in r.scan_iter(f"{prefix}:positions:*")}
        instruments = {k.decode().split(":", 2)[2]: r.get(k) for k in r.scan_iter(f"{prefix}:instruments:*")}
        index = {
            name: {m.decode() for m in r.smembers(f"{prefix}:index:{name}")}
            for name in ("positions", "positions_open", "positions_closed", "orders", "orders_open", "orders_closed")
        }
        op = {k.decode(): v.decode() for k, v in r.hgetall(f"{prefix}:index:order_position").items()}
        return cls(orders=orders, positions=positions, instruments=instruments, index=index, order_position=op)


@dataclass(frozen=True)
class VenueOrder:
    client_order_id: str
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    filled_at_ns: int | None
    venue_order_id: str


@dataclass
class VenueTruth:
    """What the broker says. `positions` is symbol → signed quantity; an absent symbol is flat."""

    orders: dict[str, VenueOrder]
    positions: dict[str, Decimal]

    @classmethod
    def from_alpaca(cls, orders: dict[str, dict], positions: dict[str, dict] | list[dict]) -> "VenueTruth":
        vo: dict[str, VenueOrder] = {}
        for coid, o in orders.items():
            if "status" not in o:  # a lookup error, not an order — keep it OUT so it cannot be read as "gone"
                continue
            vo[coid] = VenueOrder(
                client_order_id=coid,
                status=o["status"],
                filled_qty=Decimal(o.get("filled_qty") or "0"),
                filled_avg_price=Decimal(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
                filled_at_ns=_iso_to_ns(o["filled_at"]) if o.get("filled_at") else None,
                venue_order_id=o["id"],
            )
        rows = positions.values() if isinstance(positions, dict) else positions
        pos = {p["symbol"]: Decimal(p["qty"]) * (1 if p.get("side", "long") == "long" else -1) for p in rows}
        return cls(orders=vo, positions=pos)

    @classmethod
    def from_fixture(cls, path: str | Path) -> "VenueTruth":
        d = json.loads(Path(path).read_text())
        return cls.from_alpaca(d["orders"], d["positions"])


def _iso_to_ns(s: str) -> int:
    import pandas as pd

    return int(pd.Timestamp(s).value)


# --------------------------------------------------------------------------------------------------
# Replay — the installed package is the arbiter of what a list of events MEANS
# --------------------------------------------------------------------------------------------------


def replay_order(events: list[bytes]):
    objs = [to_obj(e) for e in events]
    order = OrderUnpacker.from_init(objs[0])
    for ev in objs[1:]:
        order.apply(ev)
    return order


def replay_position(instrument, fills: list[OrderFilled]) -> list[Position]:
    """Replay fills through `Position`, starting a new instance whenever the previous one closes —
    which is what NETTING does (`engine.pyx::_open_position` reopens on the next fill)."""
    instances: list[Position] = []
    for f in fills:
        if not instances or instances[-1].is_closed:
            instances.append(Position(instrument, f))
        else:
            instances[-1].apply(f)
    return instances


def _ts(e: dict[str, Any]) -> int:
    return int(e["ts_event"])


def _with_position_id(d: dict[str, Any]) -> dict[str, Any]:
    """A fill with no position id books, under NETTING, to `{instrument}-{strategy}` — exactly what
    `engine.pyx::_determine_netting_position_id` would assign. `Position()` refuses a None."""
    if d.get("position_id"):
        return d
    return dict(d, position_id=f"{d['instrument_id']}-{d['strategy_id']}")


# --------------------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------------------


@dataclass
class OrderRevival:
    client_order_id: str
    instrument_id: str
    before: str
    after: str
    events: list[bytes]
    kept_fill_qty: Decimal
    synthetic_fill_qty: Decimal


@dataclass
class PositionRewrite:
    position_id: str
    before: str  # e.g. "SHORT 192 @ 15.7797 (open)"
    after: str
    events: list[bytes]
    is_open: bool
    unchanged: bool


@dataclass
class RepairPlan:
    revivals: list[OrderRevival] = field(default_factory=list)
    rewrites: list[PositionRewrite] = field(default_factory=list)
    #: symbol → (cache net after repair, venue net). Non-empty means REFUSED: nothing may be applied.
    refusals: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    #: Corpses the venue reports terminal in a way that leaves nothing to revive (canceled/expired/rejected).
    left_alone: dict[str, str] = field(default_factory=dict)
    #: position id → why the planner will not decide it. ALSO blocks apply: a state outside the four
    #: rules is not a state to guess at (an open SHORT in a lane whose stop did not fire, for one).
    unexplained: dict[str, str] = field(default_factory=dict)
    instruments_touched: list[str] = field(default_factory=list)
    #: Orders to DELETE outright (#909): the synthetic order Nautilus minted for a phantom pre-window
    #: fill. Nothing at the venue corresponds to it, so there is no "what the venue says it is" to
    #: rewrite it to — it is removed with the leg it created, and only then.
    drop_orders: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.revivals and not self.drop_orders and not any(not rw.unchanged for rw in self.rewrites)

    def summary(self) -> str:
        lines = []
        for rv in self.revivals:
            lines.append(
                f"ORDER    {rv.client_order_id:34} {rv.before:9} -> {rv.after:9} kept fills {rv.kept_fill_qty} "
                f"synthetic {rv.synthetic_fill_qty}"
            )
        for rw in self.rewrites:
            if not rw.unchanged:
                lines.append(f"POSITION {rw.position_id:30} {rw.before} -> {rw.after}")
        for coid in self.drop_orders:
            lines.append(f"DROP     {coid:34} synthetic reconciliation order — nothing at the venue")
        for coid, why in self.left_alone.items():
            lines.append(f"LEFT     {coid:34} {why}")
        for sym, (cache, venue) in self.refusals.items():
            lines.append(f"REFUSED  {sym:34} cache net {cache} != venue {venue}")
        for pid, why in self.unexplained.items():
            lines.append(f"REFUSED  {pid:34} {why}")
        return "\n".join(lines) if lines else "(nothing to do)"


def _describe(pos: Position | None) -> str:
    if pos is None:
        return "none"
    state = "open" if not pos.is_closed else "closed"
    return f"{pos.side.name} {abs(pos.signed_qty):g} @ {pos.avg_px_open:.4f} ({state})"


def find_corpses(image: CacheImage, venue: VenueTruth) -> dict[str, tuple[Any, str]]:
    """REJECTED cached orders whose reason means the venue was not heard, and which the venue knows.

    Returns coid → (replayed order, venue status). An order the venue does not know at all is NOT a
    corpse — it may genuinely never have landed — and is left exactly as it is.
    """
    out = {}
    for coid, events in image.orders.items():
        order = replay_order(events)
        if order.status != OrderStatus.REJECTED:
            continue
        rejected = [to_obj(e) for e in events if to_dict(e)["type"] == "OrderRejected"]
        reason = rejected[-1].reason if rejected else ""
        if not any(reason.startswith(r) for r in CORPSE_REASONS):
            continue
        vo = venue.orders.get(coid)
        if vo is None:
            continue
        out[coid] = (order, vo.status)
    return out


def _live_fills_for(image: CacheImage, coid: str) -> list[dict[str, Any]]:
    """Every OrderFilled the cache holds for this order, in time order, unique by trade id.

    They live in the POSITION lists — the order list never recorded them, that is the defect. A
    trade id seen twice is the inferred duplicate (PATH's 94 under `5641a8b1`); the first wins.
    """
    seen: set[str] = set()
    fills: list[dict[str, Any]] = []
    for events in image.positions.values():
        for raw in events:
            d = to_dict(raw)
            if d.get("type") == "OrderFilled" and d.get("client_order_id") == coid:
                fills.append(_with_position_id(d))
    # Time order; on a tie the LIVE fill (`reconciliation=False`) precedes the inferred one, so when two
    # carry one trade id (PATH's 80 and 94) the venue-witnessed quantity is the one kept.
    fills.sort(key=lambda d: (_ts(d), bool(d.get("reconciliation"))))
    out = []
    for d in fills:
        if d["trade_id"] in seen:
            continue
        seen.add(d["trade_id"])
        out.append(d)
    return out


def _fill_template(image: CacheImage, instrument_id: str, strategy_id: str) -> dict[str, Any] | None:
    """A real fill on the same instrument to clone a synthetic remainder from — every field the
    tool does not explicitly set (currency, commission shape, account) comes from production."""
    best = None
    for events in image.positions.values():
        for raw in events:
            d = to_dict(raw)
            if d.get("type") == "OrderFilled" and d.get("instrument_id") == instrument_id:
                if d.get("strategy_id") == strategy_id:
                    return d
                best = best or d
    return best


def revive_order(image: CacheImage, coid: str, vo: VenueOrder, instrument) -> OrderRevival:
    events = image.orders[coid]
    dicts = [to_dict(e) for e in events]
    cut = next(i for i, d in enumerate(dicts) if d["type"] == "OrderRejected")
    kept = list(events[:cut])
    if not any(d["type"] == "OrderAccepted" for d in dicts[:cut]):
        raise RepairRefused(f"{coid}: no OrderAccepted before the rejection — nothing to revive it to")
    before = replay_order(events).status_string()
    init = dicts[0]
    accepted_vid = next(d for d in dicts[:cut] if d["type"] == "OrderAccepted").get("venue_order_id")
    if accepted_vid != vo.venue_order_id:
        # `Order.apply` itself refuses a fill whose venue id is not the order's (correctness.pyx:305).
        # A venue answer carrying another id is an answer about ANOTHER order — refuse, do not guess.
        raise RepairRefused(f"{coid}: venue order id {vo.venue_order_id} != cached {accepted_vid}")
    kept_qty = Decimal(0)
    synthetic_qty = Decimal(0)
    if vo.filled_qty > 0:
        live = _live_fills_for(image, coid)
        template = live[-1] if live else _fill_template(image, init["instrument_id"], init["strategy_id"])
        if template is None:
            raise RepairRefused(f"{coid}: venue reports fills but the cache holds no fill on {init['instrument_id']} to shape one from")
        for d in live:
            q = Decimal(d["last_qty"])
            if kept_qty + q > vo.filled_qty:
                # An inferred fill that does not fit inside the venue's own total is invention. SKIP it and
                # keep looking: a fresh-id duplicate carries the timestamp of the fill it copied, so it can
                # sort BEFORE real trades that do fit — `break` here threw those away (mutation-measured).
                continue
            kept.append(obj_to_bytes(dict_to_obj(d)))
            kept_qty += q
        remainder = vo.filled_qty - kept_qty
        if remainder > 0:
            if vo.filled_avg_price is None or vo.filled_at_ns is None:
                raise RepairRefused(f"{coid}: venue reports {vo.filled_qty} filled but no price/time to book the remainder")
            synth = dict(template)
            synth.update(
                {
                    "client_order_id": coid,
                    "venue_order_id": vo.venue_order_id,
                    "strategy_id": init["strategy_id"],
                    "instrument_id": init["instrument_id"],
                    "position_id": f"{init['instrument_id']}-{init['strategy_id']}",
                    "order_side": init["order_side"],
                    "order_type": init["order_type"],
                    "trade_id": synthetic_trade_id(vo.venue_order_id),
                    "event_id": str(uuid.uuid4()),
                    "last_qty": str(remainder),
                    "last_px": str(instrument.make_price(vo.filled_avg_price)),
                    "ts_event": str(vo.filled_at_ns),
                    "ts_init": str(vo.filled_at_ns),
                    "reconciliation": True,
                    "liquidity_side": "NO_LIQUIDITY_SIDE",
                }
            )
            kept.append(obj_to_bytes(dict_to_obj(synth)))
            synthetic_qty = remainder
    if vo.status in VENUE_DONE and replay_order(kept).status != OrderStatus.FILLED:
        # The venue closed it after (or without) fills. `OrderCanceled` is what Nautilus itself emits
        # for a resolved-but-gone order (`live/execution_engine.py`, PARTIALLY_FILLED not found at venue).
        accepted = next(d for d in dicts[:cut] if d["type"] == "OrderAccepted")
        ts = str(vo.filled_at_ns or int(accepted["ts_event"]))
        canceled = {
            "type": "OrderCanceled",
            "trader_id": accepted["trader_id"],
            "strategy_id": accepted["strategy_id"],
            "instrument_id": accepted["instrument_id"],
            "client_order_id": coid,
            "venue_order_id": vo.venue_order_id,
            "account_id": accepted.get("account_id"),
            "event_id": str(uuid.uuid4()),
            "ts_event": ts,
            "ts_init": ts,
            "reconciliation": True,
        }
        kept.append(obj_to_bytes(dict_to_obj(canceled)))
    after = replay_order(kept)
    return OrderRevival(
        client_order_id=coid,
        instrument_id=init["instrument_id"],
        before=before,
        after=after.status_string(),
        events=kept,
        kept_fill_qty=kept_qty,
        synthetic_fill_qty=synthetic_qty,
    )


#: What this cockpit itself placed. Everything else in the order plane is reconciliation-minted
#: (bare-uuid client order ids) — Nautilus's own counterparty for a gap it could not explain.
COCKPIT_PREFIXES = ("kumo-", "PROT-", "RPR-", "TR-", "FL-")


def placed_by_cockpit(client_order_id: str) -> bool:
    return client_order_id.startswith(COCKPIT_PREFIXES)


def _lane_fills_after(orders: dict[str, list[bytes]], instrument_id: str, strategy_id: str, after_ns: int) -> list[dict[str, Any]]:
    """The lane's OWN entries and exits after its stop fired: `kumo-*` orders only, this strategy, this
    instrument, in time order. What the lane holds now is exactly what it did after the stop."""
    fills = []
    for coid, events in orders.items():
        if not coid.startswith("kumo-"):
            continue
        for raw in events:
            d = to_dict(raw)
            if (
                d.get("type") == "OrderFilled"
                and d.get("instrument_id") == instrument_id
                and d.get("strategy_id") == strategy_id
                and _ts(d) > after_ns
            ):
                fills.append(_with_position_id(d))
    fills.sort(key=_ts)
    return fills


def _last_fill_ns(events: list[bytes]) -> int:
    return max(_ts(to_dict(e)) for e in events if to_dict(e)["type"] == "OrderFilled")


def plan_positions(
    image: CacheImage,
    revivals: list[OrderRevival],
    venue: VenueTruth,
) -> tuple[list[PositionRewrite], dict[str, tuple[Decimal, Decimal]], dict[str, str]]:
    """The position plane on every instrument where a corpse FIRED, decided by four rules and one check.

    1. The lane whose stop fired holds exactly what its own `kumo-*` fills after that fill say — the
       stop closed the prior holding at the venue, whatever the cache booked.
    2. An open SHORT in any OTHER lane is a booking error on its face — nothing in this cockpit
       shorts (#437) — but not one these rules explain: a mint lands in the lane whose ORDER sold
       (`engine.pyx::_determine_netting_position_id`), which rule 1 already owns. So it REFUSES.
    3. An open position whose current instance carries no cockpit-placed fill was minted by
       reconciliation to offset one of the above. It goes with them.
    4. Everything else on the instrument is left byte-for-byte as it is.
    CHECK: per symbol, the open positions that remain must net to the venue's position — else REFUSE.
    """
    corpse_fired: dict[tuple[str, str], int] = {}
    for rv in revivals:
        if rv.after == "FILLED" or rv.kept_fill_qty + rv.synthetic_fill_qty > 0:
            init = to_dict(image.orders[rv.client_order_id][0])
            key = (rv.instrument_id, init["strategy_id"])
            # Two corpses in one lane: what the lane holds NOW is what it did after the LATER one.
            corpse_fired[key] = max(corpse_fired.get(key, 0), _last_fill_ns(rv.events))
    rewrites: list[PositionRewrite] = []
    refusals: dict[str, tuple[Decimal, Decimal]] = {}
    unexplained: dict[str, str] = {}
    for instrument_id in sorted({i for i, _ in corpse_fired}):
        if instrument_id not in image.instruments:
            raise RepairRefused(f"instrument {instrument_id} is not in the cache — cannot replay its positions")
        instrument = to_obj(image.instruments[instrument_id])
        net = Decimal(0)
        for pid, raw in sorted(image.positions.items()):
            if not pid.startswith(instrument_id + "-"):
                continue
            strategy_id = pid[len(instrument_id) + 1 :]
            instances = replay_position(instrument, [to_obj(e) for e in raw])
            cur = instances[-1] if instances else None
            if cur is None or cur.is_closed:
                continue  # rule 4: closed history is not ours to touch
            before = _describe(cur)
            if (instrument_id, strategy_id) in corpse_fired:  # rule 1
                fills = _lane_fills_after(image.orders, instrument_id, strategy_id, corpse_fired[(instrument_id, strategy_id)])
                new_instances = replay_position(instrument, [dict_to_obj(d) for d in fills])
                new = new_instances[-1] if new_instances else None
                if new is None or new.is_closed:
                    events: list[bytes] = []
                    new = None
                else:
                    events = [obj_to_bytes(dict_to_obj(d)) for d in fills[-len(new.trade_ids) :]]
            elif cur.signed_qty < 0:  # rule 2
                unexplained[pid] = f"open SHORT {abs(cur.signed_qty):g} in a lane whose stop did not fire — not a state these rules explain"
                continue
            elif not any(placed_by_cockpit(to_dict(e)["client_order_id"]) for e in raw[-len(cur.trade_ids) :]):  # rule 3
                events, new = [], None
            else:  # rule 4
                events, new = list(raw), cur
            unchanged = [to_dict(e) for e in events] == [to_dict(e) for e in raw]
            rewrites.append(
                PositionRewrite(
                    position_id=pid,
                    before=before,
                    after=_describe(new),
                    events=events,
                    is_open=new is not None,
                    unchanged=unchanged,
                )
            )
            if new is not None:
                net += Decimal(str(new.signed_qty))
        symbol = instrument_id.split(".")[0]
        venue_net = venue.positions.get(symbol, Decimal(0))
        if net != venue_net:
            refusals[symbol] = (net, venue_net)
    return rewrites, refusals, unexplained


def plan_repair(image: CacheImage, venue: VenueTruth) -> RepairPlan:
    plan = RepairPlan()
    corpses = find_corpses(image, venue)
    for coid, (order, status) in sorted(corpses.items()):
        vo = venue.orders[coid]
        if status not in VENUE_RESTING and status not in VENUE_FILLED and not (status in VENUE_DONE and vo.filled_qty > 0):
            plan.left_alone[coid] = f"venue says {status} with {vo.filled_qty} filled; nothing rests and nothing moved"
            continue
        raw_instrument = image.instruments.get(order.instrument_id.value)
        if raw_instrument is None:
            raise RepairRefused(f"{coid}: instrument {order.instrument_id.value} is not in the cache — cannot price a remainder or replay a position")
        plan.revivals.append(revive_order(image, coid, vo, to_obj(raw_instrument)))
    plan.rewrites, plan.refusals, plan.unexplained = plan_positions(image, plan.revivals, venue)
    plan.instruments_touched = sorted(
        {i for i in image.instruments if any(rw.position_id.startswith(i + "-") for rw in plan.rewrites)}
        | {rv.instrument_id for rv in plan.revivals}
    )
    return plan


# --------------------------------------------------------------------------------------------------
# Apply — the only function that writes, and it writes nothing on a refusal
# --------------------------------------------------------------------------------------------------


def apply_plan(plan: RepairPlan, r, prefix: str = KEY_PREFIX, *, provenance: dict[str, Any] | None = None) -> int:
    """Write the plan to Redis in ONE transaction. Returns the number of keys rewritten.

    Index sets are kept consistent with the objects because the node reads both; `build_index`
    rebuilds the in-memory indexes from the objects, but the Rust loader enumerates by key.
    """
    if plan.refusals or plan.unexplained:
        raise RepairRefused("plan has refusals: " + ", ".join([*plan.refusals, *plan.unexplained]))
    order_position = {k.decode(): v.decode() for k, v in r.hgetall(f"{prefix}:index:order_position").items()}
    pipe = r.pipeline(transaction=True)
    n = 0
    for rv in plan.revivals:
        key = f"{prefix}:orders:{rv.client_order_id}"
        pipe.delete(key)
        pipe.rpush(key, *rv.events)
        is_open = rv.after in ("ACCEPTED", "PARTIALLY_FILLED", "TRIGGERED", "PENDING_UPDATE", "PENDING_CANCEL")
        pipe.sadd(f"{prefix}:index:orders_open" if is_open else f"{prefix}:index:orders_closed", rv.client_order_id)
        pipe.srem(f"{prefix}:index:orders_closed" if is_open else f"{prefix}:index:orders_open", rv.client_order_id)
        for raw in rv.events:
            d = to_dict(raw)
            if d["type"] == "OrderFilled" and d.get("position_id"):
                pipe.hset(f"{prefix}:index:order_position", rv.client_order_id, d["position_id"])
        n += 1
    for rw in plan.rewrites:
        if rw.unchanged:
            continue
        key = f"{prefix}:positions:{rw.position_id}"
        pipe.delete(key)
        if rw.events:
            pipe.rpush(key, *rw.events)
            pipe.sadd(f"{prefix}:index:positions", rw.position_id)
            pipe.sadd(f"{prefix}:index:positions_open" if rw.is_open else f"{prefix}:index:positions_closed", rw.position_id)
            pipe.srem(f"{prefix}:index:positions_closed" if rw.is_open else f"{prefix}:index:positions_open", rw.position_id)
        else:
            for idx in ("positions", "positions_open", "positions_closed"):
                pipe.srem(f"{prefix}:index:{idx}", rw.position_id)
            pipe.delete(f"{prefix}:snapshots:positions:{rw.position_id}")
            for coid, pid in order_position.items():
                if pid == rw.position_id:
                    pipe.hdel(f"{prefix}:index:order_position", coid)
        n += 1
    for coid in plan.drop_orders:
        pipe.delete(f"{prefix}:orders:{coid}")
        for idx in ("orders", "orders_open", "orders_closed"):
            pipe.srem(f"{prefix}:index:{idx}", coid)
        pipe.hdel(f"{prefix}:index:order_position", coid)
        n += 1
    pipe.hset(
        "kumo:repair:807",
        mapping={str(uuid.uuid4()): json.dumps({"summary": plan.summary(), **(provenance or {})})},
    )
    pipe.execute()
    return n


# --------------------------------------------------------------------------------------------------
# #909 — the phantom pre-window leg Nautilus mints at boot
# --------------------------------------------------------------------------------------------------

SYNTHETIC_PREFIX = "S-"


def _open_net(image: CacheImage, instrument_id: str) -> Decimal:
    instrument = to_obj(image.instruments[instrument_id])
    net = Decimal(0)
    for pid in sorted(image.index.get("positions_open", set())):
        if not pid.startswith(instrument_id + "-") or pid not in image.positions:
            continue
        instances = replay_position(instrument, [to_obj(e) for e in image.positions[pid]])
        if instances and not instances[-1].is_closed:
            net += Decimal(str(instances[-1].signed_qty))
    return net


def plan_drop_synthetic_leg(image: CacheImage, venue: VenueTruth, *, position_id: str) -> RepairPlan:
    """Rewrite ONE position whose current instance is a phantom Nautilus minted at boot (#909).

    Nautilus 1.229's partial-window fill adjustment takes the venue's fills in the lookback window and
    the venue's position, and synthesises the pre-window fill that makes them agree — without reading
    the cache's own positions. On ibkr-paper that produced `GLD.ARCX-EXTERNAL` BUY 23 @ 426.23 (a derived
    price: (62×410.00 − 39×400.43)/23), cache net 85 against the venue's 62, and an unprotected 39-share
    position behind a netting check that had given up.

    TWO INDEPENDENT CONDITIONS, BOTH REQUIRED, else the plan refuses:
      1. the CURRENT instance is exactly one fill, `reconciliation=True`, with `S-` venue and trade ids —
         Nautilus's own signature for a fill it invented;
      2. the instrument's open net MINUS that leg equals the venue's net — removing the phantom must land
         on the broker's number, or the phantom is not the (only) problem.
    The leg is rewritten to the REAL fills the cache's orders hold for the same position id (the closed
    history the mint overwrote), so `snapshots:` and realized P&L are untouched; with no real history it
    is deleted. The synthetic order is dropped with it. Everything else is left byte-for-byte.
    """
    plan = RepairPlan()
    if position_id not in image.positions:
        raise RepairRefused(f"{position_id} is not in the cache")
    instrument_id = position_id.split("-", 1)[0]
    if instrument_id not in image.instruments:
        raise RepairRefused(f"instrument {instrument_id} is not in the cache — cannot replay its positions")
    instrument = to_obj(image.instruments[instrument_id])
    raw = image.positions[position_id]
    instances = replay_position(instrument, [to_obj(e) for e in raw])
    cur = instances[-1] if instances else None
    if cur is None or cur.is_closed or position_id not in image.index.get("positions_open", set()):
        raise RepairRefused(f"{position_id} is not open — nothing synthetic to drop")
    current_events = [to_dict(e) for e in raw[-len(cur.trade_ids):]] if cur.trade_ids else []
    if len(current_events) != 1:
        plan.unexplained[position_id] = (
            f"current instance has {len(current_events)} fills, not one — a phantom is a single "
            f"synthesised fill; this is something else")
        return plan
    fill = current_events[0]
    synthetic = (bool(fill.get("reconciliation"))
                 and str(fill.get("venue_order_id", "")).startswith(SYNTHETIC_PREFIX)
                 and str(fill.get("trade_id", "")).startswith(SYNTHETIC_PREFIX))
    if not synthetic:
        plan.unexplained[position_id] = (
            f"the fill is not synthetic (reconciliation={fill.get('reconciliation')}, "
            f"venue_order_id={fill.get('venue_order_id')}) — refusing to drop a fill the venue may have reported")
        return plan
    symbol = instrument_id.split(".")[0]
    venue_net = venue.positions.get(symbol, Decimal(0))
    after_net = _open_net(image, instrument_id) - Decimal(str(cur.signed_qty))
    if after_net != venue_net:
        plan.refusals[symbol] = (after_net, venue_net)
        return plan
    mint_coid = str(fill["client_order_id"])
    # the real history: every OTHER order whose fills booked to this position id, in time order
    real: list[dict[str, Any]] = []
    for coid, pid in image.order_position.items():
        if pid != position_id or coid == mint_coid or coid not in image.orders:
            continue
        events = [to_dict(e) for e in image.orders[coid]]
        fills = [d for d in events if d["type"] == "OrderFilled"]
        # REAL means "the venue reported it", which `reconciliation=True` does not contradict: the
        # first-seen booking of an account position is a reconciliation fill carrying the venue's own
        # order id. What marks an INVENTED order is Nautilus's `S-` synthetic id — on ANY of its events,
        # not only fills, so a second mint that never filled is caught too — and a SECOND one on the
        # same position is not a corpse but a class: refuse and say so (two mints, not one).
        if any(str(d.get("venue_order_id", "")).startswith(SYNTHETIC_PREFIX) for d in events):
            plan.unexplained[position_id] = (
                f"a second synthetic order {coid} also books to this position — two mints, not one "
                f"phantom; this rule closes one leg and refuses to guess at a class")
            return plan
        real.extend(_with_position_id(d) for d in fills)
    real.sort(key=lambda d: (_ts(d), int(d.get("ts_init", 0))))  # ts_init breaks a same-nanosecond tie deterministically
    if real:
        replayed = replay_position(instrument, [dict_to_obj(d) for d in real])
        if not replayed[-1].is_closed:
            plan.unexplained[position_id] = (
                f"the real fills for this position id replay to an OPEN instance "
                f"({_describe(replayed[-1])}) — the phantom sat on top of a live position, not a closed one")
            return plan
        events = [obj_to_bytes(dict_to_obj(d)) for d in real]
        after = f"closed ({len(real)} real fills)"
    else:
        events, after = [], "deleted (no real history)"
    plan.rewrites.append(PositionRewrite(
        position_id=position_id, before=f"synthetic {_describe(cur)}", after=after,
        events=events, is_open=False, unchanged=False))
    if mint_coid in image.orders:
        plan.drop_orders.append(mint_coid)
    plan.instruments_touched.append(instrument_id)
    return plan
